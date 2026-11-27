"""Local task progression. Remote availability does not gate model work."""

from __future__ import annotations

from contextlib import suppress
import re
from uuid import uuid4

from pi import Pi
from processes import Interrupted, ProcessError, redact
from settings import digest, literal, task_spec
from store import LOCAL_PHASES, StaleTask
from workspace import git


def review_matches(task, policy):
    head = task.data.get("candidate")
    validation = task.data.get("validation") or {}
    review = task.data.get("review") or {}
    return (head and validation.get("head") == review.get("head") == head
            and validation.get("policy") == review.get("policy") == policy
            and validation.get("revision") == review.get("revision") == task.revision
            and review.get("verdict") == "approve"
            and bool(validation.get("checks"))
            and all(result["exit"] == 0 for result in validation["checks"].values()))


def restart_plan(store, task, reason, *, run=None):
    retain = {key: task.data[key] for key in ("published", "pr", "red", "scan_base",
                                               "decision", "decisions")
              if key in task.data}
    return store.transition(task, "plan", "baseline_invalidated", run=run,
                            data=retain | {"previous_base": task.data.get("base"),
                                           "previous_checkpoint": task.data.get("checkpoint")},
                            event={"reason": reason})


def repair(store, task, settings, failure, *, kind, run=None, allow_trial=False, event=None):
    data = task.data
    signature = {"kind": failure.get("kind"), "check": failure.get("check")}
    count = data.get("repairs", 0) + 1 if data.get("repair_key") == signature else 1
    head = data.get("candidate")
    rolled_back = data.get("active_method")
    data = data | {"last_failure": failure, "repairs": count, "repair_key": signature,
                   "active_method": None,
                   "validation": None, "review": None}
    details = {"failure": failure, "head": head, "repair": count,
               "method_rolled_back": rolled_back} | (event or {})
    if count > settings.retries:
        return store.transition(task, "blocked", kind, run=run, event=details,
                                data=data | {"block": {"reason": "repair_limit", "resume": "implement"},
                                             "checkpoint": data.get("known_good") or data.get("base"),
                                             "candidate": data.get("known_good")})
    phase = ("experiment" if allow_trial and failure.get("kind") == "check"
             and not data.get("trial_used") else "implement")
    return store.transition(task, phase, kind, run=run, event=details, data=data)


class Engine:
    def __init__(self, store, settings, workspaces, *, runner=None, stop=None):
        self.store = store
        self.settings = settings
        self.workspaces = workspaces
        self.pi = runner or Pi(settings, store.directory)
        self.stop = stop

    def base(self):
        return self.workspaces.local_base(self.store.meta("base"))

    def tick(self, *, scout=True):
        task = self.store.next()
        if task:
            self.process(task)
            return True
        if not scout or not self.settings.auto_paths or not any(c.scan_failure for c in self.settings.checks):
            return False
        now = self.store.now()
        if self.store.meta("scout_after", 0) > now:
            return False
        if any(task.source == "scout" and task.phase in (*LOCAL_PHASES, "publish", "ci")
               for task in self.store.all(active=True)):
            return False
        self.store.set_meta("scout_after", now + self.settings.scan_interval)
        try:
            return self.scan()
        except Interrupted:
            return False
        except (ProcessError, OSError, ValueError) as error:
            self.store.event("scout_error", {"message": redact(str(error))[-2000:]})
            return False

    def process(self, task):
        try:
            run = self.store.start(task)
        except StaleTask:
            return
        try:
            if task.phase != "plan" and task.data.get("policy") != self.settings.policy:
                restart_plan(self.store, task, "Operator configuration changed.", run=run)
                return
            handler = {
                "plan": self.plan, "implement": self.implement, "check": self.check,
                "review": self.review, "experiment": self.experiment,
            }[task.phase]
            handler(task, run)
        except (StaleTask, Interrupted):
            self.store.abandon(run)
        except (ProcessError, OSError, ValueError) as error:
            with suppress(StaleTask):
                self.failed_action(task, run, error)
            self.store.abandon(run)

    def failed_action(self, task, run, error):
        failure = {"phase": task.phase, "message": redact(str(error))[-2000:]}
        prior = task.data.get("action_error") or {}
        count = prior.get("count", 0) + 1 if prior.get("phase") == task.phase else 1
        data = task.data | {"action_error": failure | {"count": count}}
        if count > self.settings.retries:
            self.store.transition(task, "blocked", "action_exhausted", data=data | {
                "block": {"reason": "action_limit", "resume": task.phase},
            }, event=failure, run=run)
        else:
            self.store.transition(task, task.phase, "action_failed", data=data,
                                  event=failure, run=run, after=min(15 * 2**(count - 1), 300))

    def advance(self, task, phase, kind, run, event=None, **data):
        return self.store.transition(task, phase, kind, data=task.data | data | {"action_error": None},
                                     event=event, run=run)

    def ask(self, phase, task, tree, token, *, extra=None, editable=(), model=None):
        history = self.store.event_index(task.id) if task else []
        context = {
            "task_id": task.id, "revision": task.revision, "spec": task.spec,
            "phase": task.phase, "base": task.data.get("base"),
            "checkpoint": task.data.get("checkpoint"), "plan": task.data.get("plan"),
            "feedback": task.data.get("last_failure"), "decision": task.data.get("decision"),
            "decisions": task.data.get("decisions", [])[-8:],
            "methods": self.guidance(task), "evidence_index": history,
        } if task else {}
        if extra:
            context.update(extra)
        return self.pi.ask(phase, tree, token, context, editable=editable,
                           stop=self.stop, model=model,
                           on_event=lambda item: self.store.event("pi_trace", item, task, token),
                           evidence_db=self.store.path if task else None,
                           task_id=task.id if task else None)

    def guidance(self, task):
        active = task.data.get("active_method")
        guidance = []
        for item in self.store.methods():
            method = item["data"]
            if item["status"] != "validated" or method.get("policy") != self.settings.policy:
                continue
            if item["id"] == active or (
                item["adopted"] and method.get("check") and task.data.get("red", {}).get("check") == method["check"]
            ):
                guidance.append({"id": item["id"], "advice": method["instruction"]})
            if len(guidance) == 3:
                break
        return guidance

    @staticmethod
    def receipt(report):
        return {"model": report.model, "session": report.session, "tool_calls": report.tools}

    def human_block(self, task, run, report, *, checkpoint=None):
        reason = literal(report.result.get("summary"), "blocked reason", 2500)
        raw = report.result.get("options")
        if (not isinstance(raw, dict) or not 2 <= len(raw) <= 5
                or any(not re.fullmatch(r"[A-E]", option) for option in raw)):
            raise ValueError("A decision needs two to five labeled options A–E.")
        options = {key: literal(value, f"option {key}", 600) for key, value in raw.items()}
        decision = {"id": uuid4().hex[:12], "question": reason, "options": options,
                    "revision": task.revision}
        return self.advance(task, "blocked", "decision_requested", run,
                            event=decision | self.receipt(report),
                            checkpoint=checkpoint or task.data.get("checkpoint"),
                            block={"reason": "decision", "resume": task.phase, "decision": decision})

    def plan(self, task, run):
        base = self.base()
        if not base:
            raise ProcessError("A committed target branch is required to plan a task.")
        if task.data.get("red") and task.data.get("scan_base") != base:
            check = task.data["red"]["check"]
            registered = next((item for item in self.settings.checks if item.name == check and item.scan_failure), None)
            if registered is None:
                return self.advance(task, "blocked", "scan_scope_removed", run,
                                    block={"reason": "scan_scope_removed", "resume": "plan"})
            red = self.confirm_red(base, registered)
            if red is None:
                if task.data.get("pr"):
                    return self.advance(task, "blocked", "baseline_already_fixed", run,
                                        block={"reason": "baseline_already_fixed", "resume": "plan"})
                return self.advance(task, "cancelled", "baseline_already_fixed", run,
                                    event={"base": base, "check": check})
        else:
            red = task.data.get("red")
        with self.workspaces.at(base) as tree:
            report = self.ask("plan", task, tree, run, extra={
                "base": base, "rollback_source": task.data.get("previous_checkpoint"),
                "correction": task.data.get("correction"), "red": red,
                "check_names": [check.name for check in self.settings.checks],
            })
        if report.result["status"] == "blocked":
            return self.human_block(task, run, report)
        if report.result["status"] != "planned":
            raise ValueError("Pi did not submit a plan.")
        paths = self.settings.paths(report.result.get("paths"), autonomous=task.source == "scout")
        summary = literal(report.result.get("summary"), "plan summary", 3200)
        checkpoint = base
        previous = task.data.get("previous_base")
        unfinished = task.data.get("previous_checkpoint")
        if previous and unfinished and previous != unfinished:
            try:
                checkpoint = self.workspaces.transplant(previous, unfinished, base, paths, run) or base
            except ValueError:
                pass
        correction = task.data.get("correction")
        data = {"base": base, "checkpoint": checkpoint, "allowed": paths, "plan": summary,
                "policy": self.settings.policy, "steps": 0, "repairs": 0,
                "candidate": None, "validation": None, "review": None, "block": None,
                "trial_used": False, "active_method": None, "known_good": None, "previous_good": None}
        if red is not None:
            data.update(red=red, scan_base=base)
        if correction:
            data["last_failure"] = {"kind": "correction", "previous_spec": correction["previous_spec"]}
        step = "experiment" if correction else "implement"
        self.advance(task, step, "plan_frozen", run, event={
            "base": base, "checkpoint": checkpoint, "files": paths, "plan": summary, **self.receipt(report),
        }, **data)

    def make_candidate(self, task, run, *, method=None, experiment=False, model=None):
        parent = task.data["checkpoint"]
        with self.workspaces.at(parent) as tree:
            report = self.ask("implement", task, tree, run, editable=task.data["allowed"], model=model,
                              extra={"trial_advice": method, "experiment": experiment})
            status = report.result["status"]
            if status not in {"candidate", "progress", "blocked"}:
                raise ValueError("Unexpected implementation response.")
            changed = self.workspaces.checkpoint(tree, parent, task.data["allowed"], run)
        return report, changed, changed or parent

    def implement(self, task, run):
        steps = task.data.get("steps", 0)
        if steps >= self.settings.max_steps:
            return self.advance(task, "blocked", "step_limit", run,
                                block={"reason": "step_limit", "resume": "implement"})
        report, changed, checkpoint = self.make_candidate(task, run)
        status = report.result["status"]
        if status == "blocked":
            return self.human_block(task, run, report, checkpoint=checkpoint)
        summary = literal(report.result.get("summary"), "implementation summary", 3200)
        if status == "progress" and not changed and summary == task.data.get("progress"):
            raise ValueError("Pi repeated its progress report without new changes.")
        phase = "check" if status == "candidate" else "implement"
        if phase == "check":
            self.workspaces.verify_diff(task.data["base"], checkpoint, task.data["allowed"])
        self.advance(task, phase, "checkpoint" if phase == "implement" else "candidate", run,
                     event={"summary": summary, "checkpoint": checkpoint, **self.receipt(report)},
                     checkpoint=checkpoint, progress=summary, steps=steps + 1,
                     candidate=checkpoint if phase == "check" else None,
                     validation=None, review=None)

    def validate(self, task, head):
        self.workspaces.verify_diff(task.data["base"], head, task.data["allowed"])
        with self.workspaces.at(head) as tree:
            checks = self.workspaces.checks(tree, stop=self.stop)
        return {"head": head, "base": task.data["base"], "policy": self.settings.policy,
                "revision": task.revision, "checks": checks}

    def check(self, task, run):
        validation = self.validate(task, task.data["candidate"])
        for check, result in validation["checks"].items():
            if result["exit"]:
                failure = {"kind": "check", "check": check, "output": result["output"]}
                return repair(self.store, task, self.settings, failure, kind="check_failed",
                              run=run, allow_trial=True, event={"validation": validation})
        return self.advance(task, "review", "checks_passed", run,
                            event={"validation": validation}, validation=validation)

    def review_candidate(self, task, run, validation):
        head, base = validation["head"], validation["base"]
        with self.workspaces.at(head) as tree:
            diff = git(tree, "diff", "--no-ext-diff", "--stat", base, head, "--").stdout.decode()
            change = git(tree, "diff", "--no-ext-diff", "--no-textconv", "--unified=3",
                         base, head, "--", limit=4_000_000).stdout.decode(errors="replace")
            report = self.ask("review", task, tree, run, extra={
                "candidate": head, "base": base, "diff_stat": diff[-3000:],
                "diff_excerpt": redact(change[:23000]),
                "plan_paths": task.data["allowed"],
                "checks": {key: value | {"output": value["output"][-1000:]}
                           for key, value in validation["checks"].items()},
            })
        status = report.result["status"]
        if status not in {"approve", "reject"}:
            raise ValueError("Pi did not return an independent review verdict.")
        return {"head": head, "base": base, "policy": self.settings.policy,
                "revision": task.revision, "verdict": status,
                "summary": literal(report.result.get("summary"), "review summary", 3200),
                **self.receipt(report)}

    def publishable(self, task, run, validation, review, *, method=None, event=None):
        head = validation["head"]
        previous = task.data.get("known_good")
        return self.store.transition(task, "publish", "candidate_approved", run=run, method=method,
                                     event={"validation": validation, "review": review,
                                            "replaced": previous} | (event or {}),
                                     data=task.data | {
                                         "checkpoint": head, "candidate": head, "validation": validation,
                                         "review": review, "known_good": head, "action_error": None,
                                         "trial_used": task.phase == "experiment" or task.data.get("trial_used"),
                                         "previous_good": previous if previous != head else task.data.get("previous_good"),
                                     })

    def review(self, task, run):
        validation = task.data["validation"]
        if (validation["head"] != task.data["candidate"] or
                validation["revision"] != task.revision or validation["policy"] != self.settings.policy):
            raise ValueError("Review requires current check receipts.")
        review = self.review_candidate(task, run, validation)
        if review["verdict"] == "reject":
            return repair(self.store, task, self.settings, {"kind": "review", "summary": review["summary"]},
                          kind="review_rejected", run=run, allow_trial=True, event={"review": review})
        return self.publishable(task, run, validation, review)

    def experiment_arm(self, task, run, *, method=None, model=None):
        report, changed, head = self.make_candidate(task, run, method=method, experiment=True, model=model)
        fixing_check = (task.data.get("last_failure") or {}).get("kind") == "check"
        if (report.result["status"] != "candidate" or head == task.data["base"]
                or (fixing_check and not changed)):
            return {"run": self.receipt(report), "validation": None, "pass": False}
        validation = self.validate(task, head)
        passed = all(item["exit"] == 0 for item in validation["checks"].values())
        return {"run": self.receipt(report), "validation": validation, "pass": passed}

    def experiment(self, task, run):
        parent = task.data["checkpoint"]
        if task.data.get("trial_used"):
            return self.advance(task, "implement", "method_trial_already_used", run)
        with self.workspaces.at(parent) as tree:
            idea = self.ask("improve", task, tree, run)
        if idea.result["status"] == "skip":
            return self.advance(task, "implement", "method_trial_skipped", run,
                                event={"summary": literal(idea.result.get("summary"), "skip reason", 1200)},
                                trial_used=True)
        if idea.result["status"] != "proposed":
            raise ValueError("Pi did not submit a method proposal.")
        method = literal(idea.result.get("instruction"), "method instruction", 1600)
        baseline = self.experiment_arm(task, run + "-baseline")
        trial = self.experiment_arm(task, run + "-trial", method=method,
                                   model=baseline["run"]["model"])
        details = {"instruction": method, "check": (task.data.get("last_failure") or {}).get("check"),
                   "policy": self.settings.policy, "start": parent, "budget_seconds": self.settings.timeout,
                   "baseline": baseline, "trial": trial}
        better = (baseline["validation"] is not None and trial["pass"]
                  and baseline["run"]["model"] == trial["run"]["model"]
                  and (not baseline["pass"] or trial["run"]["tool_calls"] < baseline["run"]["tool_calls"]))
        if better:
            verdict = self.review_candidate(task, run + "-trial-review", trial["validation"])
            details["independent_review"] = verdict
            if verdict["verdict"] == "approve":
                return self.publishable(task, run, trial["validation"], verdict,
                                        method=("validated", details))
        if baseline["pass"]:
            return self.store.transition(task, "review", "method_trial_rejected", run=run,
                                         method=("rejected", details),
                                         data=task.data | {"trial_used": True, "action_error": None,
                                                           "checkpoint": baseline["validation"]["head"],
                                                           "candidate": baseline["validation"]["head"],
                                                           "validation": baseline["validation"]})
        return self.store.transition(task, "implement", "method_trial_rejected", run=run,
                                     method=("rejected", details),
                                     data=task.data | {"trial_used": True, "action_error": None})

    def confirm_red(self, base, check):
        results = []
        for _ in range(2):
            with self.workspaces.at(base) as tree:
                result = self.workspaces.checks(tree, [check.name], stop=self.stop)[check.name]
            if not 0 < result["exit"] < 126 or check.scan_failure not in result["output"]:
                return None
            results.append(result)
        if results[0]["exit"] != results[1]["exit"]:
            return None
        return {"check": check.name, "marker": check.scan_failure, "base": base, "runs": results}

    def scan(self):
        base = self.base()
        if not base:
            return False
        for check in self.settings.checks:
            if not check.scan_failure:
                continue
            fingerprint = digest([base, check.name, check.scan_failure])
            if any(task.fingerprint == fingerprint for task in self.store.all()):
                continue
            red = self.confirm_red(base, check)
            if red is None:
                continue
            with self.workspaces.at(base) as tree:
                report = self.ask("scout", None, tree, uuid4().hex, extra={
                    "base": base, "failed_check": red, "write_scope": self.settings.auto_paths,
                })
            if report.result["status"] == "skip":
                self.store.event("scout_skipped", {"check": check.name, "base": base,
                                                  "summary": literal(report.result.get("summary"), "skip reason", 1200)})
                continue
            if report.result["status"] != "found":
                raise ValueError("Pi did not submit a discovery result.")
            paths = self.settings.paths(report.result.get("paths"), autonomous=True)
            spec = task_spec(report.result)
            task, made = self.store.add(spec, source="scout", fingerprint=fingerprint,
                                       data={"red": red, "scan_base": base, "scout_paths": paths})
            if made:
                self.store.event("reproduced_defect", {"red": red, "paths": paths, **self.receipt(report)}, task)
                return True
        return False


def operator_reply(store, task, decision_id, choice, *, db=None):
    block = task.data.get("block") or {}
    decision = block.get("decision") or {}
    if (task.phase != "blocked" or decision.get("id") != decision_id
            or decision.get("revision") != task.revision or choice not in decision.get("options", {})):
        raise ValueError("The decision or choice does not match the current task revision.")
    selected = {"id": decision_id, "choice": choice, "text": decision["options"][choice],
                "revision": task.revision}
    return store.transition(task, block["resume"], "decision_answered", data=task.data | {
        "block": None, "decision": selected, "action_error": None,
        "decisions": [*task.data.get("decisions", []), selected],
    }, event=selected, db=db)
