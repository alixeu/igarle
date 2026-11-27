"""GitHub admission and delivery, driven by persisted state and exact commit evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from urllib.parse import quote, urlencode

from engine import operator_reply, repair, restart_plan, review_matches
from processes import ProcessError, redact, run, text
from settings import SHA, digest, issue_body, issue_spec, json_text
from store import StaleTask, Task, TERMINAL_PHASES
from workspace import is_ancestor, verify_origin


START = re.compile(r"/agent start ([0-9a-f]{64})\Z")
CHOOSE = re.compile(r"CHOOSE ([0-9a-f]{12}) ([A-E])\Z")


class GitHubError(ProcessError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class RemoteConflict(ValueError):
    pass


class GitHub:
    def __init__(self, root, settings):
        self.root = root
        self.settings = settings
        self.repo = f"/repos/{settings.repository}"

    def api(self, path, *, method="GET", query=None, payload=None, pages=False, missing=False):
        route = path + ("?" + urlencode(query) if query else "")
        args = ["gh", "api", "--hostname", "github.com", "--method", method,
                "-H", "X-GitHub-Api-Version: 2022-11-28", route]
        if pages:
            args.extend(("--paginate", "--slurp"))
        if payload is not None:
            args.extend(("--input", "-"))
        result = run(args, cwd=self.root, timeout=40, limit=12_000_000,
                     input_bytes=json_text(payload).encode() if payload is not None else None)
        if result.returncode:
            message = text(result.stderr) or text(result.stdout)
            status = re.search(r"\bHTTP (\d{3})\b", message)
            code = int(status[1]) if status else None
            if missing and code == 404:
                return None
            raise GitHubError(code, f"GitHub {method} {path}: {message[-1400:]}")
        try:
            return json.loads(result.stdout) if result.stdout.strip() else None
        except ValueError as error:
            raise ProcessError(f"GitHub {method} {path} returned invalid JSON.") from error

    def repository(self):
        verify_origin(self.root, self.settings.repository)
        return self.api(self.repo)

    def actor(self):
        return self.api("/user")

    def base_sha(self):
        sha = self.api(self.repo + "/branches/" + quote(self.settings.branch, safe=""))["commit"]["sha"]
        if not SHA.fullmatch(sha):
            raise ValueError("GitHub returned an invalid target commit.")
        return sha

    def protection(self):
        try:
            rule = self.api(self.repo + "/branches/" + quote(self.settings.branch, safe="") + "/protection",
                            missing=True) or {}
        except GitHubError as error:
            if error.status == 403:
                return None
            raise
        statuses = rule.get("required_status_checks") or {}
        contexts = set(statuses.get("contexts", []))
        contexts.update(check.get("context") for check in statuses.get("checks", []))
        return bool(statuses.get("strict") and self.settings.required_job in contexts
                    and (rule.get("enforce_admins") or {}).get("enabled"))

    def merge_rules(self, base):
        """Check the target branch without relying on an administrator bypass."""
        branch_name = quote(self.settings.branch, safe="")
        branch = self.api(self.repo + "/branches/" + branch_name)
        if (branch.get("commit") or {}).get("sha") != base:
            raise ProcessError("The target branch changed while checking its merge rules.")
        protected = branch.get("protected")
        if type(protected) is not bool:
            raise ProcessError("GitHub did not report the target branch protection status.")
        strict = self.protection() if protected else False
        if protected and strict is not True:
            raise ProcessError("Cannot verify an administrator-enforced strict branch protection gate.")
        try:
            pages = self.api(self.repo + "/rules/branches/" + branch_name,
                             query={"per_page": 100}, pages=True)
        except GitHubError as error:
            # GitHub Free private repositories return this specific response when
            # rulesets are unavailable, even to their administrator.
            unavailable = "Upgrade to GitHub Pro or make this repository public to enable this feature."
            if error.status != 403 or unavailable not in str(error) or protected:
                raise
            pages = []
        if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
            raise ProcessError("GitHub returned invalid branch rules.")
        rules = [rule for page in pages for rule in page]
        if rules:
            raise ProcessError("Active rulesets require operator review before automatic merging.")
        return {"base": base, "protected": protected, "strict_gate": strict,
                "active_rulesets": 0}

    def issue(self, number):
        return self.api(self.repo + f"/issues/{number}", missing=True)

    def issues(self, after):
        pages = self.api(self.repo + "/issues", query={"state": "all", "since": after, "per_page": 100}, pages=True)
        return [issue for page in pages for issue in page if not issue.get("pull_request")]

    def comments(self, since):
        pages = self.api(self.repo + "/issues/comments",
                         query={"since": since, "sort": "updated", "direction": "asc", "per_page": 100},
                         pages=True)
        return [comment for page in pages for comment in page]

    def issue_comments(self, number):
        pages = self.api(self.repo + f"/issues/{number}/comments", query={"per_page": 100}, pages=True)
        return [comment for page in pages for comment in page]

    def create_issue(self, title, body):
        return self.api(self.repo + "/issues", method="POST", payload={"title": title, "body": body})

    def comment(self, number, body):
        return self.api(self.repo + f"/issues/{number}/comments", method="POST", payload={"body": body})

    def pull(self, number):
        return self.api(self.repo + f"/pulls/{number}", missing=True)

    def pulls(self, branch):
        owner = self.settings.repository.split("/", 1)[0]
        pages = self.api(self.repo + "/pulls", query={"state": "all", "head": f"{owner}:{branch}",
                                                    "base": self.settings.branch, "per_page": 100}, pages=True)
        return [pr for page in pages for pr in page]

    def create_pull(self, branch, title, body):
        return self.api(self.repo + "/pulls", method="POST", payload={
            "head": branch, "base": self.settings.branch, "title": title, "body": body,
        })

    def ci(self, pull, head, repo_id):
        if self.settings.ci_source == "status":
            pages = self.api(self.repo + f"/commits/{head}/statuses",
                             pages=True, query={"per_page": 100})
            statuses = [item for page in pages for item in page
                        if item.get("context") == self.settings.required_job
                        and (item.get("creator") or {}).get("id") == self.settings.status_actor]
            if not statuses:
                return None
            latest = max(statuses, key=lambda item: (item["created_at"], item["id"]))
            return {
                "id": latest["id"], "head_sha": head, "run_attempt": 1,
                "status": "in_progress" if latest["state"] == "pending" else "completed",
                "conclusion": latest["state"], "description": latest.get("description") or "",
                "source": "status",
            }
        name = quote(self.settings.workflow.rsplit("/", 1)[-1], safe="")
        pages = self.api(self.repo + f"/actions/workflows/{name}/runs", pages=True, query={
            "event": "pull_request", "head_sha": head, "per_page": 100,
        }, missing=True) or []
        runs = [item for page in pages for item in page["workflow_runs"]
                if item.get("head_sha") == head and item.get("event") == "pull_request"
                and item.get("head_branch") == pull["head"]["ref"]
                and (item.get("head_repository") or {}).get("id") == repo_id
                and (not item.get("pull_requests") or any(pr["number"] == pull["number"]
                                                     for pr in item["pull_requests"]))]
        if not runs:
            return None
        latest = max(runs, key=lambda item: (item["run_number"], item["run_attempt"]))
        receipt = {key: latest[key] for key in ("id", "head_sha", "run_attempt", "status", "conclusion")}
        if latest["status"] == "completed" and latest["conclusion"] == "success":
            pages = self.api(self.repo + f"/actions/runs/{latest['id']}/attempts/{latest['run_attempt']}/jobs",
                             pages=True, query={"per_page": 100})
            matches = [job for page in pages for job in page["jobs"]
                       if job["name"] == self.settings.required_job and job.get("status") == "completed"]
            if len(matches) != 1 or matches[0].get("conclusion") != "success":
                receipt["conclusion"] = "required_job_missing_or_failed"
        return receipt

    def failed_logs(self, receipt):
        if receipt.get("source") == "status":
            return receipt.get("description", "")
        args = ["gh", "run", "view", str(receipt["id"]), "--repo", self.settings.repository,
                "--attempt", str(receipt["run_attempt"]), "--log-failed"]
        try:
            result = run(args, cwd=self.root, timeout=40, limit=3_000_000)
            return text(result.stdout if result.returncode == 0 else result.stderr)[-5000:]
        except ProcessError as error:
            return str(error)

    def merge(self, number, head):
        return self.api(self.repo + f"/pulls/{number}/merge", method="PUT",
                        payload={"merge_method": "squash", "sha": head})

    def close(self, number):
        return self.api(self.repo + f"/issues/{number}", method="PATCH", payload={"state": "closed"})


def iso(timestamp):
    return datetime.fromtimestamp(max(0, timestamp), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(stamp):
    return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())


def marker_for(binding, task):
    return f"<!-- pi-loop:{binding['repository_id']}:{task.id} -->"


def short_issue(value):
    return value.replace("@", "＠").replace("<!-- pi-loop:", "〈pi-loop:").replace("\r", "")


class Synchronizer:
    def __init__(self, store, settings, workspaces, *, client=None, stop=None, config_guard=None):
        self.store = store
        self.settings = settings
        self.workspaces = workspaces
        self.gh = client or GitHub(workspaces.root, settings)
        self.stop = stop
        self.config_guard = config_guard

    def bind(self):
        repo = self.gh.repository()
        actor = self.gh.actor()
        if repo["full_name"].lower() != self.settings.repository or not repo.get("permissions", {}).get("push"):
            raise ValueError("The authenticated GitHub account needs write access to the configured repository.")
        operators = self.settings.operators or (actor["id"],)
        existing = self.store.meta("binding")
        if existing and (existing["repository_id"] != repo["id"] or existing["actor"] != actor["id"]
                         or existing["branch"] != self.settings.branch):
            raise ValueError("This state directory is bound to another repository, branch, or delivery identity.")
        base = self.workspaces.fetch_base(self.gh.base_sha())
        binding = {"repository": self.settings.repository, "repository_id": repo["id"],
                   "branch": self.settings.branch, "actor": actor["id"], "operators": list(operators),
                   "created_at": existing["created_at"] if existing else self.store.now()}
        self.store.set_meta("binding", binding)
        self.store.set_meta("base", base)
        return binding

    def assert_bound(self, binding):
        repo = self.gh.repository()
        actor = self.gh.actor()
        if (repo["id"] != binding["repository_id"] or repo["full_name"].lower() != binding["repository"]
                or actor["id"] != binding["actor"] or self.settings.branch != binding["branch"]
                or binding["repository"] != self.settings.repository
                or (self.settings.operators and list(self.settings.operators) != binding["operators"])):
            raise ValueError("GitHub identity, operators, or repository changed. Check the explicit binding.")

    def sync(self):
        binding = self.store.meta("binding")
        if binding is None:
            return {"bound": False}
        self.assert_bound(binding)
        base = self.workspaces.fetch_base(self.gh.base_sha())
        self.store.set_meta("base", base)
        self.import_comments(binding)
        count = 0
        for queued in self.store.all(active=True):
            if self.stop and self.stop.is_set():
                break
            try:
                with self.store.task_lock(queued.id):
                    task = self.store.get(queued.id)
                    block = task.data.get("block") or {}
                    if task.phase == "blocked" and block.get("reason") == "github_action_limit":
                        continue
                    key = "github_retry:" + task.id
                    retry = self.store.meta(key) or {}
                    if (retry.get("revision") == task.revision and retry.get("phase") == task.phase
                            and retry.get("next_at", 0) > self.store.now()):
                        continue
                    try:
                        self.sync_task(task, binding, base)
                    except RemoteConflict as error:
                        self.block(task, "remote_conflict", str(error))
                    except (ProcessError, ValueError) as error:
                        if self.stop and self.stop.is_set():
                            break
                        self.retry_failed(task, str(error))
                    else:
                        if retry:
                            self.store.set_meta(key, None)
                        count += 1
            except StaleTask:
                continue
        return {"bound": True, "base": base, "tasks_checked": count}

    def retry_failed(self, task, message):
        key = "github_retry:" + task.id
        message = redact(message)[-2400:]
        with self.store.write() as db:
            row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            previous = json.loads(row["value"]) if row else None
            same = (previous and previous.get("message") == message
                    and previous.get("phase") == task.phase and previous.get("revision") == task.revision)
            count = previous["count"] + 1 if same else 1
            retry = {"message": message, "count": count, "phase": task.phase,
                     "revision": task.revision,
                     "next_at": self.store.now() + min(self.settings.poll_interval * 2**min(count, 6), 300)}
            db.execute("INSERT INTO metadata(key,value) VALUES(?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json_text(retry)))
            if count > self.settings.retries and task.phase != "blocked":
                self.store.transition(task, "blocked", "github_action_exhausted", db=db,
                                      data=task.data | {"block": {"reason": "github_action_limit",
                                                                  "resume": task.phase, "message": message}},
                                      event=retry)
            else:
                self.store.event("github_action_failed", retry, task, db=db)

    def record_error(self, task, message):
        key = "github_error:" + task.id
        previous = self.store.meta(key)
        if previous != message:
            self.store.set_meta(key, message)
            self.store.event("github_error", {"message": message[-2400:]}, task)

    def import_comments(self, binding):
        until = self.store.now()
        since = self.store.meta("comment_cursor", binding["created_at"])
        comments = self.gh.comments(iso(max(binding["created_at"], since - 180) - 1))
        for comment in sorted(comments, key=lambda item: (item["created_at"], item["id"])):
            if (comment.get("user", {}).get("id") not in binding["operators"]
                    or comment["created_at"] != comment["updated_at"]
                    or not binding["created_at"] <= epoch(comment["created_at"]) <= until):
                continue
            body = (comment.get("body") or "").strip()
            match = START.fullmatch(body) or CHOOSE.fullmatch(body)
            uri = self.gh.repo + "/issues/"
            issue_url = comment.get("issue_url", "")
            prefix = "https://api.github.com" + uri
            if not match or not issue_url.startswith(prefix) or not issue_url[len(prefix):].isdigit():
                continue
            issue = int(issue_url[len(prefix):])
            if not issue:
                continue
            try:
                if body.startswith("/agent start "):
                    current = self.gh.issue(issue)
                    spec = issue_spec(current or {})
                    if digest(spec) != match[1]:
                        continue
                    self.admit_comment(issue, comment["id"],
                                       lambda db: self.authorize(db, issue, spec, match[1]))
                else:
                    self.admit_comment(issue, comment["id"],
                                       lambda db: self.choose(db, issue, *match.groups()))
            except (StaleTask, ValueError):
                continue
        self.store.set_meta("comment_cursor", until)

    def admit_comment(self, issue, comment_id, apply):
        existing = next((task for task in self.store.all() if task.issue == issue), None)
        if existing:
            with self.store.task_lock(existing.id):
                return self.store.admit_once(comment_id, apply)
        return self.store.admit_once(comment_id, apply)

    def authorize(self, db, issue, spec, version):
        row = db.execute("SELECT * FROM tasks WHERE issue=?", (issue,)).fetchone()
        if row:
            task = Task.from_row(row)
            if task.issue_digest != version:
                return self.store.revise(task, spec, issue_digest=version, db=db)
            return task
        task, _ = self.store.add(spec, source="github", issue=issue, issue_digest=version, db=db)
        return task

    def choose(self, db, issue, decision_id, choice):
        row = db.execute("SELECT * FROM tasks WHERE issue=?", (issue,)).fetchone()
        if row:
            operator_reply(self.store, Task.from_row(row), decision_id, choice, db=db)

    def effect(self, key, task, find, send, *, repeat=False):
        if self.stop is not None and self.stop.is_set():
            raise ProcessError("The Loop is stopping.")
        if self.config_guard is not None:
            self.config_guard()
        if self.store.get(task.id).generation != task.generation:
            raise StaleTask(task.id)
        state, receipt = self.store.effect(key)
        if state == "confirmed":
            return receipt
        observed = find()
        if observed is not None:
            self.store.effect_confirmed(key, task, observed)
            return observed
        if state == "sent" and not repeat:
            self.record_error(task, f"Waiting to reconcile uncertain GitHub operation {key}.")
            return None
        self.store.effect_started(key, task)
        try:
            sent = send()
        except GitHubError as error:
            if error.status in {400, 401, 403, 404, 405, 409, 422}:
                self.store.clear_effect(key, task)
            raise
        if sent is not None:
            self.store.effect_confirmed(key, task, sent)
        return sent

    def block(self, task, reason, message):
        if task.phase in TERMINAL_PHASES:
            return
        block = task.data.get("block") or {}
        if task.phase == "blocked" and block.get("reason") == reason:
            return
        self.store.transition(task, "blocked", reason, data=task.data | {
            "block": {"reason": reason, "message": message,
                      "resume": block.get("resume", task.phase)},
        }, event={"message": message})

    def ensure_issue(self, task, binding):
        if task.issue is not None:
            return task
        marker = marker_for(binding, task)
        spec = {key: short_issue(value) for key, value in task.spec.items()}
        def find():
            matching = [issue for issue in self.gh.issues(iso(task.created_at - 180))
                        if marker in (issue.get("body") or "")
                        and (issue.get("user") or {}).get("id") == binding["actor"]]
            if len(matching) > 1:
                raise RemoteConflict("More than one Issue matches this Loop task.")
            return {"number": matching[0]["number"]} if matching else None

        def send():
            return {"number": self.gh.create_issue(spec["title"], issue_body(spec, marker))["number"]}

        received = self.effect(f"{task.id}:issue", task, find, send)
        if received:
            return self.store.attach_issue(task.id, received["number"], digest(spec))
        return task

    def current_issue(self, task):
        issue = self.gh.issue(task.issue)
        if not issue or issue["state"] != "open":
            self.store.transition(task, "cancelled", "issue_closed_externally",
                                  event={"issue": task.issue})
            return False
        try:
            matches = digest(issue_spec(issue)) == task.issue_digest
        except ValueError:
            matches = False
        if not matches:
            self.block(task, "issue_revised", "Issue content changed. Authorize the new specification.")
        return matches

    def decision(self, task, binding):
        decision = (task.data.get("block") or {}).get("decision")
        if not decision or not self.current_issue(task):
            return
        marker = f"<!-- pi-loop:decision:{decision['id']} -->"
        options = "\n".join(f"- {key} — {short_issue(value)}" for key, value in decision["options"].items())
        body = f"{short_issue(decision['question'])}\n\n{options}\n\nReply with CHOOSE {decision['id']} A (or the selected option).\n\n{marker}"

        def find():
            for comment in self.gh.issue_comments(task.issue):
                if ((comment.get("user") or {}).get("id") == binding["actor"]
                        and marker in (comment.get("body") or "")):
                    return {"comment": comment["id"]}
            return None

        self.effect(f"{task.id}:decision:{decision['id']}", task, find,
                    lambda: {"comment": self.gh.comment(task.issue, body)["id"]})

    def checked_pull(self, pull, task, binding, branch):
        if (not pull or pull.get("base", {}).get("ref") != self.settings.branch
                or pull.get("head", {}).get("ref") != branch
                or (pull["head"].get("repo") or {}).get("id") != binding["repository_id"]
                or marker_for(binding, task) not in (pull.get("body") or "")
                or (pull.get("user") or {}).get("id") != binding["actor"]):
            raise RemoteConflict("The PR no longer matches the authorized repository, branch, or task.")
        return pull

    def find_pull(self, task, binding, branch):
        pulls = self.gh.pulls(branch)
        if len(pulls) > 1:
            raise RemoteConflict("More than one PR uses the task branch.")
        if not pulls:
            return None
        pr = self.checked_pull(pulls[0], task, binding, branch)
        return {"number": pr["number"]}

    def publish(self, task, binding, branch):
        head = task.data["candidate"]
        expected = task.data.get("published")
        push_key = f"{task.id}:push:{head}"

        def find_branch():
            observation = self.workspaces.remote_head(branch)
            if observation == head:
                return {"head": head}
            if observation != expected:
                raise RemoteConflict("The remote task branch changed outside Loop.")
            return None

        if find_branch() is None:
            existing, _ = self.store.effect(push_key)
            if existing == "confirmed":
                raise RemoteConflict("A previously published task branch moved or was deleted.")

        def send_branch():
            self.workspaces.push(branch, head, expected)
            return {"head": head} if self.workspaces.remote_head(branch) == head else None

        if not self.effect(push_key, task, find_branch, send_branch, repeat=True):
            return
        body = f"Tracks #{task.issue}\n\n{marker_for(binding, task)}"
        saved = self.effect(f"{task.id}:pull", task,
                            lambda: self.find_pull(task, binding, branch),
                            lambda: {"number": self.gh.create_pull(branch, short_issue(task.spec["title"]), body)["number"]})
        if not saved:
            return
        pull = self.checked_pull(self.gh.pull(saved["number"]), task, binding, branch)
        if pull["state"] != "open":
            raise RemoteConflict("The PR is no longer open.")
        if pull["head"]["sha"] != head:
            if pull["head"]["sha"] == expected:
                raise ProcessError("The PR head has not caught up with the published branch.")
            raise RemoteConflict("The PR head changed outside Loop.")
        self.store.transition(task, "ci", "pull_published", data=task.data | {
            "pr": pull["number"], "published": head,
        }, event={"issue": task.issue, "pr": pull["number"], "head": head})

    def merged(self, task, pull, base):
        head = task.data["candidate"]
        sha = pull.get("merge_commit_sha")
        if pull["head"]["sha"] != head or not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise RemoteConflict("GitHub merged a different candidate or returned an invalid merge commit.")
        if not is_ancestor(self.workspaces.root, sha, base):
            return

        self.store.effect_confirmed(f"{task.id}:merge:{head}", task,
                                    {"number": pull["number"], "merge": sha})

        def find_issue():
            issue = self.gh.issue(task.issue)
            return {"issue": task.issue} if issue and issue["state"] == "closed" else None

        if self.effect(f"{task.id}:close:{sha}", task, find_issue,
                       lambda: {"issue": self.gh.close(task.issue)["number"]}, repeat=True):
            self.store.transition(task, "done", "merged", data=task.data | {"merge_commit": sha},
                                  event={"pr": pull["number"], "issue": task.issue,
                                         "head": head, "merge": sha})

    def assess_ci(self, task, pull, binding, base):
        head = task.data["candidate"]
        if pull["state"] != "open" or pull["head"]["sha"] != head:
            raise RemoteConflict("The checked PR no longer has the published candidate.")
        ci = self.gh.ci(pull, head, binding["repository_id"])
        if not ci or ci["status"] != "completed":
            return
        if ci["head_sha"] != head:
            raise RemoteConflict("The CI result is for a different commit.")
        if ci["conclusion"] != "success":
            failure = {"kind": "ci", "receipt": ci, "output": self.gh.failed_logs(ci)}
            repair(self.store, task, self.settings, failure, kind="ci_failed", allow_trial=True)
            return
        current = self.gh.base_sha()
        if current != base:
            self.store.set_meta("base", self.workspaces.fetch_base(current))
            restart_plan(self.store, task, "Target branch advanced before merge.")
            return
        if not self.current_issue(task):
            return
        fresh = self.checked_pull(self.gh.pull(pull["number"]), task, binding, pull["head"]["ref"])
        if (fresh["head"]["sha"] != head or fresh["state"] != "open"
                or not fresh.get("mergeable") or fresh.get("mergeable_state") != "clean"):
            return
        current_ci = self.gh.ci(fresh, head, binding["repository_id"])
        if (not current_ci or current_ci["head_sha"] != head
                or current_ci["status"] != "completed" or current_ci["conclusion"] != "success"):
            return
        rules = self.gh.merge_rules(base)
        if not self.current_issue(task):
            return
        self.store.record_ci(task, current_ci, self.settings.workflow, self.settings.required_job,
                             rules=rules)

        def find():
            observed = self.gh.pull(pull["number"])
            if observed and observed.get("merged") and observed["head"]["sha"] == head:
                return {"number": observed["number"], "merge": observed.get("merge_commit_sha")}
            return None

        def send():
            outcome = self.gh.merge(pull["number"], head)
            return {"number": pull["number"], "merge": outcome["sha"]} if outcome.get("merged") else None

        if self.effect(f"{task.id}:merge:{head}", task, find, send, repeat=True):
            fresh = self.checked_pull(self.gh.pull(pull["number"]), task, binding, pull["head"]["ref"])
            if fresh.get("merged"):
                updated_base = self.workspaces.fetch_base(self.gh.base_sha())
                self.store.set_meta("base", updated_base)
                self.merged(task, fresh, updated_base)

    def sync_task(self, task, binding, base):
        task = self.ensure_issue(task, binding)
        if not task.issue:
            return
        branch = f"pi-loop/{task.id}"
        pull = None
        if number := task.data.get("pr"):
            pull = self.checked_pull(self.gh.pull(number), task, binding, branch)
            if pull.get("merged"):
                if task.phase != "ci" or not review_matches(task, self.settings.policy):
                    raise RemoteConflict("A PR merged while its verified task revision was no longer current.")
                self.merged(task, pull, base)
                return
        if task.phase == "blocked":
            self.decision(task, binding)
            return
        if not self.current_issue(task):
            return
        if task.phase not in {"publish", "ci"}:
            return
        if task.data.get("base") != base or not review_matches(task, self.settings.policy):
            restart_plan(self.store, task, "GitHub base or local validation policy changed.")
            return
        if task.phase == "publish":
            self.publish(task, binding, branch)
        else:
            self.assess_ci(task, pull, binding, base)
