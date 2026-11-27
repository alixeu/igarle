"""Regression tests for durable Pi Loop execution and delivery."""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
import os
import subprocess
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import Engine, ReviewTooLarge, operator_reply, repair, review_matches
from github import GitHub, GitHubError, Synchronizer, iso
from loop import change_task
from pi import PiReport
from processes import ProcessError
from settings import Settings, digest, issue_body, issue_spec, runtime_policy, task_spec
from store import StaleTask, Store
from workspace import Workspaces, head_sha


SPEC = {
    "title": "Fix answer",
    "objective": "The production answer is 42.",
    "constraints": "Keep the public function.",
    "acceptance_criteria": "The registered regression test passes.",
}


def configuration():
    return Settings.from_dict({
        "version": 1,
        "github": {"repository": "test/fixture", "branch": "master",
                   "operator_ids": [123], "workflow": ".github/workflows/ci.yml", "required_job": "check"},
        "pi": {"binary": "pi"},
        "run": {"retries": 3},
        "project": {"allowed_paths": ["src/**", "tests/**"], "auto_paths": ["src/**"]},
        "checks": {"test": {"argv": ["python3", "-B", "-m", "unittest", "discover", "-s", "tests", "-v"],
                            "scan_failure": "41 != 42"}},
    })


class FakePi:
    def __init__(self):
        self.calls = []

    def ask(self, phase, tree, token, context, **kwargs):
        self.calls.append((phase, context))
        if phase == "scout":
            value = {"status": "found", **SPEC, "paths": ["src/answer.py"]}
        elif phase == "plan":
            value = {"status": "planned", "summary": "Fix the function and run the test.",
                     "paths": ["src/answer.py"]}
        elif phase == "implement":
            file = tree / "src/answer.py"
            if (context.get("feedback") or {}).get("kind") == "ci":
                file.write_text("def answer():\n    return 42  # CI repair\n", encoding="utf-8")
            else:
                file.write_text("def answer():\n    return 42\n", encoding="utf-8")
            value = {"status": "candidate", "summary": "Updated answer."}
        elif phase == "improve":
            value = {"status": "skip", "summary": "The failed CI output is already precise."}
        elif phase == "review":
            value = {"status": "approve", "summary": "The function and regression agree."}
        else:
            raise AssertionError(phase)
        if callback := kwargs.get("on_event"):
            callback({"type": "message_end", "message": {"role": "assistant", "content": value}})
        return PiReport(value, "fixture/model", "fixture-session", 1)


class TrialPi(FakePi):
    def __init__(self, *, unchanged_trial=False):
        super().__init__()
        self.unchanged_trial = unchanged_trial

    def ask(self, phase, tree, token, context, **kwargs):
        if phase == "improve":
            return PiReport({"status": "proposed", "instruction": "Read the failing assertion first."},
                            "fixture/model", "proposal", 1)
        if phase != "implement":
            return super().ask(phase, tree, token, context, **kwargs)
        trial = bool(context.get("trial_advice"))
        if context.get("experiment"):
            if not (trial and self.unchanged_trial):
                (tree / "src/answer.py").write_text("def answer():\n    return 42\n")
        else:
            (tree / "src/answer.py").write_text("def answer():\n    return 40\n")
        return PiReport({"status": "candidate", "summary": "Updated answer."},
                        "fixture/model", "experiment", 2 if trial else 3)


class PortableWorkspaces(Workspaces):
    def checks(self, tree, selected=None, stop=None):
        if sys.platform == "darwin" and os.environ.get("PI_LOOP_SANDBOXED") != "1":
            return super().checks(tree, selected, stop)
        names = set(selected) if selected else None
        outcomes = {}
        for check in self.settings.checks:
            if names is not None and check.name not in names:
                continue
            argv = [sys.executable if check.argv[0] == "python3" else check.argv[0], *check.argv[1:]]
            result = subprocess.run(argv, cwd=tree, capture_output=True, timeout=20)
            outcomes[check.name] = self.check_result(tree, result)
        return outcomes


class FakeGitHub:
    def __init__(self, base, remote):
        self.repo = "/repos/test/fixture"
        self.base = base
        self.remote = remote
        self.issues_by_id = {}
        self.pulls_by_id = {}
        self.outcomes = {}
        self.creations = {"issues": 0, "pulls": 0, "merges": 0}
        self.fail_issue_after_write = False

    def issue(self, number):
        return self.issues_by_id.get(number)

    def issues(self, after):
        return list(self.issues_by_id.values())

    def create_issue(self, title, body):
        self.creations["issues"] += 1
        issue = {"number": self.creations["issues"], "title": title, "body": body,
                 "state": "open", "user": {"id": 123}}
        self.issues_by_id[issue["number"]] = issue
        if self.fail_issue_after_write:
            self.fail_issue_after_write = False
            raise ProcessError("response lost")
        return issue

    def pulls(self, branch):
        return [pull for pull in self.pulls_by_id.values() if pull["head"]["ref"] == branch]

    def pull(self, number):
        pull = self.pulls_by_id.get(number)
        if pull and not pull.get("merged"):
            pull["head"]["sha"] = self.remote[pull["head"]["ref"]]
        return pull

    def create_pull(self, branch, title, body):
        self.creations["pulls"] += 1
        pull = {"number": self.creations["pulls"] + 100, "title": title, "body": body,
                "state": "open", "user": {"id": 123}, "base": {"ref": "master"},
                "head": {"ref": branch, "sha": self.remote[branch], "repo": {"id": 1}},
                "mergeable": True, "mergeable_state": "clean", "merged": False}
        self.pulls_by_id[pull["number"]] = pull
        return pull

    def ci(self, pull, head, repo_id):
        outcome = self.outcomes.get(head)
        if not outcome:
            return None
        return {"id": 111, "head_sha": head, "run_attempt": 1, "status": "completed", "conclusion": outcome}

    def failed_logs(self, receipt):
        return "synthetic CI failure"

    def base_sha(self):
        return self.base

    def merge_rules(self, base):
        if base != self.base:
            raise ProcessError("Target branch moved.")
        if getattr(self, "unsafe_rules", False):
            raise ProcessError("Branch rules could not be verified.")
        return {"base": base, "protected": False, "strict_gate": False, "active_rulesets": 0}

    def merge(self, number, head):
        pull = self.pulls_by_id[number]
        assert pull["head"]["sha"] == head
        self.creations["merges"] += 1
        pull.update(merged=True, state="closed", merge_commit_sha=head)
        self.base = head
        return {"sha": head, "merged": True}

    def close(self, number):
        self.issues_by_id[number]["state"] = "closed"
        return self.issues_by_id[number]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pi-loop-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "repo"
        self.root.mkdir()
        subprocess.run(["git", "init", "-qb", "master", "--template="], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Loop Fixture"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "loop@example.invalid"], cwd=self.root, check=True)
        (self.root / "src").mkdir()
        (self.root / "src/answer.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
        (self.root / "tests").mkdir()
        (self.root / "tests/test_answer.py").write_text(
            "import unittest\nfrom src.answer import answer\n"
            "class Answer(unittest.TestCase):\n"
            "    def test_answer(self):\n        self.assertEqual(answer(), 42)\n", encoding="utf-8")
        subprocess.run(["git", "add", "--all"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture baseline"], cwd=self.root, check=True)
        self.base = head_sha(self.root)
        self.settings = configuration()
        self.store = Store(self.root / "outputs/pi-loop")
        self.workspaces = PortableWorkspaces(self.root, self.store.directory, self.settings)
        self.pi = FakePi()
        self.engine = Engine(self.store, self.settings, self.workspaces, runner=self.pi)

    def steps(self, task_id, target, max_steps=12):
        for _ in range(max_steps):
            task = self.store.get(task_id)
            if task.phase == target:
                return task
            self.engine.process(task)
        self.fail(f"Task never reached {target}: {asdict(self.store.get(task_id))}")


class DurableTaskTests(Fixture):
    def test_progress_survives_restart_and_stale_result_is_rejected(self):
        task, _ = self.store.add(SPEC)
        self.engine.process(task)
        planned = self.store.get(task.id)
        self.assertEqual(planned.phase, "implement")
        run_id = self.store.start(planned)
        reopened = Store(self.store.directory)
        reopened.recover()
        self.assertEqual(reopened.get(task.id).data["base"], self.base)
        self.assertIn("run_interrupted", [row["kind"] for row in reopened.events(task.id)])
        revised = reopened.revise(reopened.get(task.id), SPEC | {"objective": "A revised goal."})
        with self.assertRaises(StaleTask):
            reopened.transition(planned, "check", "stale", run=run_id)
        self.assertEqual(revised.revision, 2)
        self.assertEqual(reopened.get(task.id).spec["objective"], "A revised goal.")

    def test_manual_decision_is_bound_to_revision_and_retained(self):
        task, _ = self.store.add(SPEC)
        decision = {"id": "abcdef123456", "revision": 1, "question": "Choose", "options": {"A": "yes", "B": "no"}}
        task = self.store.transition(task, "blocked", "decision_requested", data={
            "block": {"reason": "decision", "resume": "plan", "decision": decision},
        })
        with self.assertRaises(ValueError):
            operator_reply(self.store, task, decision["id"], "C")
        task = operator_reply(self.store, task, decision["id"], "A")
        self.assertEqual(task.phase, "plan")
        self.assertEqual(task.data["decisions"][0]["choice"], "A")
        self.engine.process(task)
        self.assertIn("decision_answered", [event["kind"] for event in self.store.events(task.id)])

    def test_task_lock_serializes_external_effect_and_local_revision(self):
        task, _ = self.store.add(SPEC)
        entered = threading.Event()
        finished = threading.Event()
        def revise():
            entered.set()
            self.store.revise(self.store.get(task.id), SPEC | {"objective": "new"})
            finished.set()
        with self.store.task_lock(task.id):
            worker = threading.Thread(target=revise)
            worker.start()
            self.assertTrue(entered.wait(1))
            self.assertFalse(finished.wait(.1))
        worker.join(3)
        self.assertTrue(finished.is_set())
        self.assertEqual(self.store.get(task.id).revision, 2)

    def test_project_scope_blocks_paths_and_invalid_specs(self):
        with self.assertRaises(ValueError):
            self.settings.paths([".github/workflows/ci.yml"])
        with self.assertRaises(ValueError):
            self.settings.paths(["src/../../.ssh/id_rsa"])
        with self.assertRaises(ValueError):
            self.settings.paths(["tests/test_answer.py"], autonomous=True)
        with self.assertRaises(ValueError):
            task_spec(SPEC | {"title": "first\nsecond"})

    def test_github_outage_leaves_local_queue_available(self):
        from loop import Runtime
        from types import SimpleNamespace

        def fail():
            raise ProcessError("simulated GitHub outage")

        runtime = Runtime.__new__(Runtime)
        runtime.store = self.store
        runtime.github = SimpleNamespace(sync=fail)
        runtime.assert_policy = lambda: None
        task, _ = self.store.add(SPEC)
        self.assertFalse(runtime.sync_once())
        self.assertTrue(self.engine.tick(scout=False))
        self.assertEqual(self.store.get(task.id).phase, "implement")

    def test_task_github_failure_backs_off_and_does_not_block_other_tasks(self):
        from loop import change_task
        from types import SimpleNamespace

        now = [10_000]
        self.store.clock = lambda: now[0]
        failed, _ = self.store.add(SPEC)
        healthy, _ = self.store.add(SPEC | {"title": "Another task"})
        self.store.set_meta("binding", {"repository_id": 1, "actor": 123})
        sync = Synchronizer(self.store, self.settings, self.workspaces,
                            client=FakeGitHub(self.base, {}))
        calls = []

        def process(task, binding, base):
            calls.append(task.id)
            if task.id == failed.id:
                raise ProcessError("task-specific remote failure")

        with patch.object(sync, "assert_bound"), patch.object(sync, "import_comments"), \
             patch.object(self.workspaces, "fetch_base", return_value=self.base), \
             patch.object(sync, "sync_task", side_effect=process):
            sync.sync()
            self.assertEqual(self.store.get(failed.id).phase, "plan")
            self.assertIsNotNone(self.store.next())
            retry = self.store.meta("github_retry:" + failed.id)
            self.assertEqual(retry["count"], 1)
            sync.sync()
            self.assertEqual(calls.count(failed.id), 1)
            for count in range(2, 5):
                now[0] = retry["next_at"]
                sync.sync()
                retry = self.store.meta("github_retry:" + failed.id)
                self.assertEqual(retry["count"], count)
            blocked = self.store.get(failed.id)
            self.assertEqual(blocked.phase, "blocked")
            self.assertEqual(blocked.data["block"]["reason"], "github_action_limit")
            self.assertGreaterEqual(calls.count(healthy.id), 5)
            change_task(SimpleNamespace(store=self.store), SimpleNamespace(action="retry"), blocked, None)
            self.assertEqual(self.store.get(failed.id).phase, "plan")
            self.assertIsNone(self.store.meta("github_retry:" + failed.id))

    def test_failure_budget_is_per_kind_and_allows_three_retries(self):
        task, _ = self.store.add(SPEC, data={"base": self.base})
        for _ in range(2):
            task = repair(self.store, task, self.settings, {"kind": "review"}, kind="review_rejected")
        task = repair(self.store, task, self.settings, {"kind": "ci"}, kind="ci_failed")
        self.assertEqual(task.phase, "implement")
        self.assertEqual(task.data["repairs"], 1)
        for _ in range(2):
            task = repair(self.store, task, self.settings, {"kind": "ci"}, kind="ci_failed")
            self.assertEqual(task.phase, "implement")
        task = repair(self.store, task, self.settings, {"kind": "ci"}, kind="ci_failed")
        self.assertEqual(task.phase, "blocked")

    def test_unknown_database_version_is_left_untouched(self):
        directory = self.root / "unknown-state"
        directory.mkdir()
        with closing(sqlite3.connect(directory / "state.sqlite3")) as db:
            db.execute("PRAGMA user_version=77")
            db.commit()
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            Store(directory)
        with closing(sqlite3.connect(directory / "state.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 77)

    def test_runtime_changes_invalidate_the_frozen_policy(self):
        bundle = self.root / "policy"
        bundle.mkdir()
        source = bundle / "adapter.py"
        source.write_text("version = 1\n")
        before = runtime_policy({"version": 1}, bundle)
        source.write_text("version = 2\n")
        self.assertNotEqual(before, runtime_policy({"version": 1}, bundle))

    def test_scan_schedule_remains_stable_across_a_simulated_day(self):
        now = [10_000]
        self.store.clock = lambda: now[0]
        with patch.object(self.engine, "scan", return_value=False) as scan:
            for hour in range(25):
                now[0] = 10_000 + hour * 3600
                self.assertFalse(self.engine.tick())
                self.assertFalse(self.engine.tick())
            self.assertEqual(scan.call_count, 25)

    @unittest.skipUnless(sys.platform == "darwin" and os.environ.get("PI_LOOP_SANDBOXED") != "1",
                         "requires an outer macOS process sandbox")
    def test_check_sandbox_allows_temporary_git_and_denies_workspace_writes(self):
        script = """
import pathlib, subprocess, tempfile
with tempfile.TemporaryDirectory() as temp:
    subprocess.run(["git", "init", "-q", "--template="], cwd=temp, check=True)
try:
    pathlib.Path("denied.txt").write_text("denied")
except PermissionError:
    pass
else:
    raise AssertionError("checkout was writable")
"""
        with self.workspaces.at(self.base) as tree:
            result = self.workspaces.run_check(tree, (sys.executable, "-B", "-c", script), None)
        self.assertEqual(result["exit"], 0, result["output"])

    def test_revision_preserves_the_previous_work_for_replanning(self):
        task, _ = self.store.add(SPEC)
        self.engine.process(task)
        self.engine.process(self.store.get(task.id))
        task = self.store.get(task.id)
        revised = self.store.revise(task, SPEC | {"constraints": "Preserve the function and its signature."})
        self.assertEqual(revised.data["previous_base"], task.data["base"])
        self.assertEqual(revised.data["previous_checkpoint"], task.data["checkpoint"])
        self.engine.process(revised)
        replanned = self.store.get(task.id)
        self.assertNotEqual(replanned.data["checkpoint"], replanned.data["base"])
        self.assertEqual(replanned.phase, "experiment")


class EndToEndTests(Fixture):
    def delivery(self):
        remote = {}
        fake = FakeGitHub(self.base, remote)
        self.workspaces.remote_head = remote.get
        self.workspaces.push = lambda branch, head, expected: remote.update({branch: head})
        return fake, Synchronizer(self.store, self.settings, self.workspaces, client=fake)

    def test_scout_red_resume_repair_ci_and_merge(self):
        self.assertTrue(self.engine.tick())
        task = self.store.all()[0]
        self.assertEqual(task.source, "scout")
        self.assertEqual(task.data["red"]["check"], "test")
        task = self.steps(task.id, "publish")
        self.assertTrue(review_matches(task, self.settings.policy))
        self.assertEqual(task.data["validation"]["checks"]["test"]["exit"], 0)
        self.assertIn("pi_trace", [event["kind"] for event in self.store.events(task.id)])

        remote = {}
        fake = FakeGitHub(self.base, remote)
        self.workspaces.remote_head = remote.get
        self.workspaces.push = lambda branch, head, expected: remote.update({branch: head})
        sync = Synchronizer(self.store, self.settings, self.workspaces, client=fake)
        binding = {"repository_id": 1, "actor": 123, "operators": [123]}
        sync.sync_task(task, binding, self.base)
        task = self.store.get(task.id)
        self.assertEqual(task.phase, "ci")
        first = task.data["candidate"]
        fake.outcomes[first] = "failure"
        sync.sync_task(task, binding, self.base)
        self.assertEqual(self.store.get(task.id).phase, "implement")

        task = self.steps(task.id, "publish")
        self.assertNotEqual(task.data["candidate"], first)
        sync.sync_task(task, binding, self.base)
        task = self.store.get(task.id)
        self.assertEqual(task.phase, "ci")
        fake.outcomes[task.data["candidate"]] = "success"
        fake.unsafe_rules = True
        with self.assertRaises(ProcessError):
            sync.sync_task(task, binding, self.base)
        self.assertEqual(fake.creations["merges"], 0)
        fake.unsafe_rules = False
        sync.sync_task(task, binding, self.base)
        result = self.store.get(task.id)
        self.assertEqual(result.phase, "done")
        self.assertEqual(fake.creations, {"issues": 1, "pulls": 1, "merges": 1})
        self.assertEqual(fake.issue(result.issue)["state"], "closed")
        self.assertEqual(result.data["merge_commit"], result.data["candidate"])
        receipts = [event for event in self.store.events(task.id) if event["kind"] == "ci_passed"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["data"]["receipt"]["head_sha"], result.data["candidate"])
        self.assertEqual(receipts[0]["data"]["merge_rules"]["base"], self.base)

    def test_uncertain_issue_creation_is_reconciled_without_duplication(self):
        task, _ = self.store.add(SPEC)
        remote = {}
        fake = FakeGitHub(self.base, remote)
        fake.fail_issue_after_write = True
        sync = Synchronizer(self.store, self.settings, self.workspaces, client=fake)
        binding = {"repository_id": 1, "actor": 123}
        with self.assertRaises(ProcessError):
            sync.ensure_issue(task, binding)
        self.assertEqual(len(self.store.pending_effects()), 1)
        task = sync.ensure_issue(self.store.get(task.id), binding)
        self.assertEqual(task.issue, 1)
        self.assertEqual(fake.creations["issues"], 1)
        self.assertEqual(self.store.pending_effects(), [])

    def test_ci_or_review_for_old_head_cannot_authorize_new_candidate(self):
        self.engine.tick()
        task = self.steps(self.store.all()[0].id, "publish")
        self.assertTrue(review_matches(task, self.settings.policy))
        self.assertFalse(review_matches(task.__class__(**(asdict(task) | {
            "data": task.data | {"candidate": "0" * 40}
        })), self.settings.policy))
        self.assertFalse(review_matches(task.__class__(**(asdict(task) | {
            "revision": task.revision + 1
        })), self.settings.policy))

    def test_review_candidate_refuses_a_diff_above_the_limit(self):
        (self.root / "src/large.py").write_text("#" * 30_000 + "\n", encoding="utf-8")
        subprocess.run(["git", "add", "--all"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "large candidate"], cwd=self.root, check=True)
        large = head_sha(self.root)
        task, _ = self.store.add(SPEC)
        with self.assertRaisesRegex(ReviewTooLarge, "review limit"):
            self.engine.review_candidate(task, "run", {"head": large, "base": self.base})
        self.assertEqual(self.pi.calls, [])

    def test_oversized_review_blocks_the_task(self):
        task, _ = self.store.add(SPEC)
        task = self.steps(task.id, "review")
        with patch.object(self.engine, "review_candidate",
                          side_effect=ReviewTooLarge("The candidate diff is too large.")):
            self.engine.process(self.store.get(task.id))
        blocked = self.store.get(task.id)
        self.assertEqual(blocked.phase, "blocked")
        self.assertEqual(blocked.data["block"]["reason"], "review_too_large")
        self.assertEqual(blocked.data["block"]["resume"], "review")

    def test_publish_stall_blocks_a_task_without_progress(self):
        task, _ = self.store.add(SPEC)
        task = self.steps(task.id, "publish")
        fake, sync = self.delivery()
        self.workspaces.push = lambda branch, head, expected: None
        binding = {"repository_id": 1, "actor": 123, "operators": [123]}
        now = self.store.now()
        task = self.store.transition(task, "publish", "fixture",
                                     data=task.data | {"waiting_since": now - 4000})
        sync.sync_task(task, binding, self.base)
        blocked = self.store.get(task.id)
        self.assertEqual(blocked.phase, "blocked")
        self.assertEqual(blocked.data["block"]["reason"], "publish_stalled")
        self.assertEqual(blocked.data["block"]["resume"], "publish")
        self.assertEqual(fake.creations["pulls"], 0)

    def test_ci_stall_blocks_a_task_without_progress(self):
        task, _ = self.store.add(SPEC)
        task = self.steps(task.id, "publish")
        fake, sync = self.delivery()
        binding = {"repository_id": 1, "actor": 123, "operators": [123]}
        sync.sync_task(task, binding, self.base)
        task = self.store.get(task.id)
        self.assertEqual(task.phase, "ci")
        now = self.store.now()
        task = self.store.transition(task, "ci", "fixture",
                                     data=task.data | {"waiting_since": now - 4000})
        sync.sync_task(task, binding, self.base)
        blocked = self.store.get(task.id)
        self.assertEqual(blocked.phase, "blocked")
        self.assertEqual(blocked.data["block"]["reason"], "ci_stalled")
        self.assertEqual(fake.creations["merges"], 0)

    def test_retry_restarts_the_stall_clock(self):
        task, _ = self.store.add(SPEC)
        task = self.steps(task.id, "publish")
        fake, sync = self.delivery()
        self.workspaces.push = lambda branch, head, expected: None
        binding = {"repository_id": 1, "actor": 123, "operators": [123]}
        now = self.store.now()
        task = self.store.transition(task, "publish", "fixture",
                                     data=task.data | {"waiting_since": now - 4000})
        sync.sync_task(task, binding, self.base)
        blocked = self.store.get(task.id)
        self.assertEqual(blocked.phase, "blocked")
        self.store.clock = lambda: 20_000
        change_task(SimpleNamespace(store=self.store), SimpleNamespace(action="retry"), blocked, None)
        retried = self.store.get(task.id)
        self.assertEqual(retried.phase, "publish")
        self.assertEqual(retried.data["waiting_since"], 20_000)

    def test_harness_trial_requires_a_better_verified_candidate(self):
        self.engine.pi = TrialPi()
        task, _ = self.store.add(SPEC)
        task = self.steps(task.id, "experiment")
        self.engine.process(task)
        result = self.store.get(task.id)
        self.assertEqual(result.phase, "publish")
        self.assertTrue(review_matches(result, self.settings.policy))
        method = self.store.methods(task.id)[0]
        self.assertEqual(method["status"], "validated")
        self.assertFalse(method["adopted"])
        self.assertEqual(result.data["active_method"], method["id"])
        self.store.adoption(method["id"], True)
        self.store.adoption(method["id"], False)
        self.store.adoption(method["id"], True)
        self.assertTrue(self.store.methods(task.id)[0]["adopted"])

    def test_unchanged_trial_is_not_promoted_to_fix_a_check_failure(self):
        self.engine.pi = TrialPi(unchanged_trial=True)
        task, _ = self.store.add(SPEC)
        task = self.steps(task.id, "experiment")
        broken = task.data["checkpoint"]
        self.engine.process(task)
        result = self.store.get(task.id)
        self.assertEqual(result.phase, "review")
        self.assertNotEqual(result.data["candidate"], broken)
        self.assertEqual(self.store.methods(task.id)[0]["status"], "rejected")

    def test_repeated_scout_scan_deduplicates_the_same_failure(self):
        self.assertTrue(self.engine.scan())
        self.assertFalse(self.engine.scan())
        self.assertEqual(len(self.store.all()), 1)

    def test_issue_snapshot_roundtrips_and_rejects_duplicates(self):
        issue = {"number": 1, "state": "open", "title": SPEC["title"],
                 "body": issue_body(SPEC, "<!-- pi-loop:fixture -->")}
        self.assertEqual(issue_spec(issue), SPEC)
        issue["body"] += "\n\n## Objective\n\nAn extra objective"
        with self.assertRaises(ValueError):
            issue_spec(issue)

    def test_issue_authorization_is_idempotent_and_versions_corrections(self):
        fake = FakeGitHub(self.base, {})
        sync = Synchronizer(self.store, self.settings, self.workspaces, client=fake)
        binding = {"created_at": self.store.now() - 30, "operators": [123]}
        fake.issues_by_id[1] = {"number": 1, "state": "open", "title": SPEC["title"],
                                "body": issue_body(SPEC, "<!-- pi-loop:fixture -->")}
        stamp = iso(self.store.now())
        comment = {"id": 501, "created_at": stamp, "updated_at": stamp, "user": {"id": 123},
                   "issue_url": "https://api.github.com/repos/test/fixture/issues/1",
                   "body": "/agent start " + digest(SPEC)}
        fake.comments = lambda since: [comment, comment]
        sync.import_comments(binding)
        sync.import_comments(binding)
        task = self.store.all()[0]
        self.assertEqual((len(self.store.all()), task.revision), (1, 1))

        changed = SPEC | {"objective": "The corrected objective."}
        fake.issues_by_id[1]["body"] = issue_body(changed, "<!-- pi-loop:fixture -->")
        fake.comments = lambda since: [comment | {"id": 502, "body": "/agent start " + digest(changed)}]
        sync.import_comments(binding)
        revised = self.store.get(task.id)
        self.assertEqual((revised.revision, revised.spec["objective"]), (2, changed["objective"]))

    def test_commit_status_requires_configured_context_and_publisher(self):
        settings = replace(self.settings, ci_source="status", status_actor=123)
        client = GitHub(self.root, settings)
        statuses = [[
            {"context": "check", "creator": {"id": 123}, "id": 1,
             "created_at": "2026-01-01T00:00:00Z", "state": "failure",
             "description": "fix the fixture"},
            {"context": "check", "creator": {"id": 123}, "id": 2,
             "created_at": "2026-01-01T00:00:01Z", "state": "success"},
            {"context": "check", "creator": {"id": 999}, "id": 3,
             "created_at": "2026-01-01T00:00:02Z", "state": "failure"},
        ]]
        with patch.object(client, "api", return_value=statuses):
            result = client.ci({}, self.base, 1)
        self.assertEqual((result["id"], result["conclusion"], result["head_sha"]),
                         (2, "success", self.base))

    def test_branch_rule_inspection_can_be_unavailable_to_a_limited_token(self):
        client = GitHub(self.root, self.settings)
        with patch.object(client, "api", side_effect=GitHubError(403, "forbidden")):
            self.assertIsNone(client.protection())
        with patch.object(client, "api", side_effect=GitHubError(500, "server error")):
            with self.assertRaises(GitHubError):
                client.protection()

    def test_merge_checks_branch_rules_and_never_assumes_permission_denial_is_safe(self):
        client = GitHub(self.root, self.settings)
        branch = {"commit": {"sha": self.base}, "protected": False}
        with patch.object(client, "api", side_effect=[branch, [[]]]):
            self.assertEqual(client.merge_rules(self.base)["active_rulesets"], 0)
        with patch.object(client, "api", side_effect=[branch, [[{"type": "pull_request"}]]]):
            with self.assertRaisesRegex(ProcessError, "Active rulesets"):
                client.merge_rules(self.base)
        with patch.object(client, "api", side_effect=[branch, GitHubError(403, "forbidden")]):
            with self.assertRaises(GitHubError):
                client.merge_rules(self.base)
        feature = "Upgrade to GitHub Pro or make this repository public to enable this feature."
        with patch.object(client, "api", side_effect=[branch, GitHubError(403, feature)]):
            self.assertFalse(client.merge_rules(self.base)["protected"])
        protected = branch | {"protected": True}
        with patch.object(client, "api", side_effect=[protected, {}]):
            with self.assertRaisesRegex(ProcessError, "administrator-enforced"):
                client.merge_rules(self.base)
        rule = {"required_status_checks": {"strict": True, "contexts": ["check"]},
                "enforce_admins": {"enabled": True}}
        with patch.object(client, "api", side_effect=[protected, rule, [[]]]):
            self.assertTrue(client.merge_rules(self.base)["strict_gate"])
        with patch.object(client, "api", return_value={"commit": {"sha": "0" * 40}, "protected": False}):
            with self.assertRaisesRegex(ProcessError, "changed"):
                client.merge_rules(self.base)

    def test_actions_receipt_requires_a_successful_named_job_for_the_candidate(self):
        client = GitHub(self.root, self.settings)
        run_record = {"id": 22, "head_sha": self.base, "head_branch": "pi-loop/task",
                      "head_repository": {"id": 1}, "event": "pull_request",
                      "pull_requests": [{"number": 4}], "run_number": 7, "run_attempt": 1,
                      "status": "completed", "conclusion": "success"}
        pull = {"number": 4, "head": {"ref": "pi-loop/task"}}
        pages = [[{"workflow_runs": [run_record]}],
                 [{"jobs": [{"name": "check", "status": "completed", "conclusion": "success"}]}]]
        with patch.object(client, "api", side_effect=pages):
            receipt = client.ci(pull, self.base, 1)
        self.assertEqual((receipt["id"], receipt["head_sha"], receipt["conclusion"]),
                         (22, self.base, "success"))
        pages[1] = [{"jobs": [{"name": "another-job", "status": "completed", "conclusion": "success"}]}]
        with patch.object(client, "api", side_effect=pages):
            receipt = client.ci(pull, self.base, 1)
        self.assertEqual(receipt["conclusion"], "required_job_missing_or_failed")


if __name__ == "__main__":
    unittest.main()
