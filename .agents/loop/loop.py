#!/usr/bin/env python3
"""Operator entry point for the Pi development Loop."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import sqlite3
import sys
import threading

from engine import Engine, operator_reply
from github import Synchronizer
from pi import Pi
from processes import ProcessError, redact, run, text
from settings import Settings, digest, issue_spec, task_spec
from store import StaleTask, Store, TERMINAL_PHASES
from workspace import Workspaces, head_sha, locate_root


ENTRY = Path(__file__).resolve()
RUNTIME = "outputs/pi-loop"


def show(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


@contextmanager
def exclusive(directory):
    lock = directory / "runtime.lock"
    if lock.is_symlink():
        raise ValueError("The runtime lock cannot be a symlink.")
    with lock.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("This repository already has an active Loop.") from error
        yield


class Runtime:
    def __init__(self, root):
        self.root = root
        self.settings = Settings.load(root)
        stop = self.stop = threading.Event()
        self.store = Store(root / RUNTIME)
        self.workspaces = Workspaces(root, self.store.directory, self.settings)
        self.pi = Pi(self.settings, self.store.directory)
        self.engine = Engine(self.store, self.settings, self.workspaces, runner=self.pi, stop=stop)
        self.github = Synchronizer(self.store, self.settings, self.workspaces, stop=stop,
                                   config_guard=self.assert_policy)

    def assert_policy(self):
        if Settings.load(self.root).policy != self.settings.policy:
            self.stop.set()
            raise ValueError("Operator configuration changed. Restarting with the new policy.")

    def doctor(self, live=False, github=False):
        if not shutil.which("git") or sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
            raise ValueError("Pi Loop requires Git and the macOS project-check sandbox.")
        base = self.engine.base()
        if base is None:
            raise ValueError("Configure a target branch with a committed baseline.")
        with self.workspaces.at(base) as tree:
            health = self.workspaces.run_check(tree, (sys.executable, "-B", "-c", """
import errno
import os
import pathlib
import socket
import tempfile

assert not os.environ.get("GITHUB_TOKEN") and not os.environ.get("OPENAI_API_KEY")
with tempfile.NamedTemporaryFile() as scratch:
    scratch.write(b"ok")
try:
    pathlib.Path(".pi-loop-write-probe").write_text("deny")
except PermissionError:
    pass
else:
    raise AssertionError("Tracked-checkout writes are allowed")
try:
    pathlib.Path(os.getcwd()).parent.parent.joinpath("state.sqlite3").open("rb").close()
except PermissionError:
    pass
else:
    raise AssertionError("The task database is readable")
try:
    with socket.socket() as connection:
        connection.connect(("127.0.0.1", 9))
except OSError as error:
    assert error.errno in (errno.EPERM, errno.EACCES), error
else:
    raise AssertionError("Network access was allowed")
"""), self.stop)
        if health["exit"]:
            raise ValueError("The macOS sandbox probe failed: " + health["output"])
        data = {"repo": str(self.root), "base": base, "sandbox": "passed",
                "pi": self.pi.probe(self.root, live=live)}
        if github:
            binding = self.store.meta("binding")
            if binding:
                self.github.assert_bound(binding)
            else:
                self.github.gh.repository()
            protection = self.github.gh.protection()
            data["github"] = {"binding": binding, "strict_merge_gate": protection,
                              "branch_rule_access": "unavailable" if protection is None else "available"}
        return data

    def sync_once(self):
        try:
            self.assert_policy()
            synced = self.github.sync()
            self.store.set_meta("github_health", {"at": self.store.now(), "result": synced})
            return True
        except (OSError, ValueError, ProcessError) as error:
            message = redact(str(error))[-2000:]
            previous = self.store.meta("github_health", {})
            self.store.set_meta("github_health", {"at": self.store.now(), "error": message})
            if previous.get("error") != message:
                self.store.event("github_sync_unavailable", {"message": message})
            return False

    def sync_forever(self):
        errors = 0
        while not self.stop.is_set():
            try:
                errors = 0 if self.sync_once() else min(errors + 1, 6)
            except (sqlite3.Error, KeyError, TypeError) as error:
                print(f"GitHub synchronization could not finish: {redact(str(error))}", file=sys.stderr, flush=True)
                errors = min(errors + 1, 6)
            self.stop.wait(min(self.settings.poll_interval * 2**errors, 300))

    def run(self, *, once=False, local=False):
        with exclusive(self.store.directory):
            self.store.recover()
            if once:
                if not local:
                    self.sync_once()
                progressed = False if self.stop.is_set() else self.engine.tick()
                if not local and not self.stop.is_set():
                    self.sync_once()
                return {"progressed": progressed, "next_scout": self.store.meta("scout_after")}
            self.pi.version(self.root)
            for handle in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                signal.signal(handle, lambda *_: self.stop.set())
            self.store.event("loop_started", {"pid": os.getpid(), "local": local})
            sync = None
            if not local:
                sync = threading.Thread(target=self.sync_forever, name="github-sync", daemon=True)
                sync.start()
            try:
                while not self.stop.is_set():
                    self.assert_policy()
                    self.store.set_meta("scheduler_health", {"pid": os.getpid(), "at": self.store.now()})
                    progressed = self.engine.tick()
                    self.stop.wait(0.2 if progressed else min(self.settings.poll_interval, 10))
            finally:
                self.stop.set()
                if sync:
                    sync.join(timeout=5)
                self.store.event("loop_stopped", {"pid": os.getpid()})

    def status(self):
        return {"repository": self.settings.repository, "branch": self.settings.branch,
                "policy": self.settings.policy,
                "binding": self.store.meta("binding"), "base": self.engine.base(),
                "scheduler": self.store.meta("scheduler_health"), "github": self.store.meta("github_health"),
                "tasks": [{key: value for key, value in asdict(task).items() if key not in {"spec", "data"}}
                          | {"title": task.spec["title"], "block": task.data.get("block"),
                             "checkpoint": task.data.get("checkpoint"), "pr": task.data.get("pr"),
                             "github_retry": self.store.meta("github_retry:" + task.id)}
                          for task in self.store.all()],
                "active_runs": self.store.active_runs(),
                "pending_writes": self.store.pending_effects()}

    def service(self, action):
        if sys.platform != "darwin":
            raise ValueError("The LaunchAgent is available on macOS.")
        label = "dev.pi-loop." + sha256(os.fsencode(self.root)).hexdigest()[:16]
        target = f"gui/{os.getuid()}/{label}"
        directory = Path.home() / "Library" / "LaunchAgents"
        plist = directory / f"{label}.plist"
        if action == "status":
            result = run(["/bin/launchctl", "print", target], cwd=self.root, timeout=15)
            details = {}
            if result.returncode == 0:
                for line in result.stdout.decode(errors="replace").splitlines():
                    key, separator, value = line.strip().partition(" = ")
                    if separator and key in {"state", "pid", "runs", "last exit code"}:
                        details[key] = value
            return {"label": label, "installed": plist.is_file(),
                    "loaded": result.returncode == 0, "service": details}
        if action == "uninstall":
            result = run(["/bin/launchctl", "bootout", target], cwd=self.root, timeout=20)
            if result.returncode:
                active = run(["/bin/launchctl", "print", target], cwd=self.root, timeout=15)
                if active.returncode == 0:
                    raise ProcessError(text(result.stderr) or "Could not stop the LaunchAgent.")
            plist.unlink(missing_ok=True)
            return {"label": label, "removed": True}

        if not self.store.meta("binding"):
            raise ValueError("Initialize the repository binding before installing the service.")
        health = self.doctor()
        self.github.assert_bound(self.store.meta("binding"))
        logs = self.store.directory
        definition = {
            "Label": label,
            "ProgramArguments": ["/usr/bin/caffeinate", "-s", sys.executable, "-B", str(ENTRY),
                                 "--repo", str(self.root), "run"],
            "WorkingDirectory": str(self.root), "RunAtLoad": True, "KeepAlive": True,
            "ThrottleInterval": 15, "ExitTimeOut": 15, "ProcessType": "Background",
            "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                     "PYTHONDONTWRITEBYTECODE": "1"},
            "StandardOutPath": str(logs / "launchd.log"), "StandardErrorPath": str(logs / "launchd.err"),
        }
        directory.mkdir(exist_ok=True, parents=True)
        run(["/bin/launchctl", "bootout", target], cwd=self.root, timeout=20)
        temporary = directory / f"{label}.{os.getpid()}.tmp"
        try:
            with temporary.open("xb") as stream:
                plistlib.dump(definition, stream)
            os.replace(temporary, plist)
        finally:
            temporary.unlink(missing_ok=True)
        started = run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
                      cwd=self.root, timeout=25)
        if started.returncode:
            raise ProcessError(f"Could not activate {label}: {text(started.stderr)}")
        return {"label": label, "installed": True, "doctor": health}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", type=Path, default=Path.cwd(), help="main Git checkout (default: current directory)")
    s = p.add_subparsers(dest="command", required=True)
    for name in ("init", "status", "sync"):
        s.add_parser(name)
    validate = s.add_parser("doctor")
    validate.add_argument("--live", action="store_true", help="perform a real Pi model call")
    validate.add_argument("--github", action="store_true", help="check the configured GitHub repository and rule")
    execute = s.add_parser("run")
    execute.add_argument("--once", action="store_true")
    execute.add_argument("--local", action="store_true", help="skip GitHub synchronization")
    prepare = s.add_parser("prepare-issue")
    prepare.add_argument("number", type=int)
    history = s.add_parser("history")
    history.add_argument("id", nargs="?")
    history.add_argument("--evidence", type=int)
    history.add_argument("--limit", type=int, default=40)
    task = s.add_parser("task").add_subparsers(dest="action", required=True)
    for name in ("add", "revise", "show", "reply", "retry", "rollback", "clear-intent"):
        action = task.add_parser(name)
        if name != "add":
            action.add_argument("id")
        if name in {"add", "revise"}:
            action.add_argument("--spec", required=True, type=Path)
        if name == "reply":
            action.add_argument("decision")
            action.add_argument("choice")
        if name == "clear-intent":
            action.add_argument("key", help="the pending write shown by status")
    learn = s.add_parser("learn").add_subparsers(dest="action", required=True)
    learn.add_parser("list").add_argument("--task")
    for name in ("adopt", "revoke"):
        learn.add_parser(name).add_argument("method", type=int)
    service = s.add_parser("service")
    service.add_argument("action", choices=("install", "uninstall", "status"))
    return p


def task_command(runtime, args):
    store = runtime.store
    if args.action in {"add", "revise"}:
        spec = task_spec(json.loads(args.spec.read_text(encoding="utf-8")))
    if args.action == "add":
        return asdict(store.add(spec)[0])
    task = store.get(args.id)
    if args.action == "show":
        return asdict(task)
    with store.task_lock(task.id):
        return change_task(runtime, args, store.get(task.id), spec if args.action == "revise" else None)


def change_task(runtime, args, task, spec):
    store = runtime.store
    if task.phase in TERMINAL_PHASES:
        raise ValueError("This task has reached a terminal phase.")
    if args.action == "revise":
        if task.issue:
            raise ValueError("Revise the attached GitHub Issue and authorize the replacement specification.")
        return asdict(store.revise(task, spec))
    if args.action == "reply":
        return asdict(operator_reply(store, task, args.decision, args.choice))
    if args.action == "clear-intent":
        with exclusive(runtime.store.directory):
            store.clear_effect(args.key, task)
        return {"task": task.id, "pending_writes": store.pending_effects()}
    if args.action == "rollback":
        previous = task.data.get("previous_good")
        if not previous or not head_sha(runtime.root, previous):
            raise ValueError("There is no previous verified candidate for this revision.")
        return asdict(store.transition(task, "check", "artifact_rollback", data=task.data | {
            "checkpoint": previous, "candidate": previous, "validation": None, "review": None,
            "active_method": None, "block": None, "action_error": None, "repairs": 0,
        }, event={"previous": previous, "replaced": task.data.get("candidate")}))
    block = task.data.get("block") or {}
    if task.phase != "blocked" or block.get("reason") in {"decision", "issue_revised"}:
        raise ValueError("Retry requires a blocked task without a pending operator decision or Issue revision.")
    with store.write() as db:
        retried = store.transition(task, block["resume"], "operator_retry", db=db, data=task.data | {
            "block": None, "action_error": None, "repairs": 0, "steps": 0,
            "waiting_since": store.now(),
        })
        db.execute("DELETE FROM metadata WHERE key=?", ("github_retry:" + task.id,))
    return asdict(retried)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        runtime = Runtime(locate_root(args.repo.resolve()))
        match args.command:
            case "doctor":
                show(runtime.doctor(args.live, args.github))
            case "init":
                with exclusive(runtime.store.directory):
                    show(runtime.github.bind())
            case "run":
                result = runtime.run(once=args.once, local=args.local)
                if result is not None:
                    show(result)
            case "sync":
                with exclusive(runtime.store.directory):
                    show(runtime.github.sync())
            case "status":
                show(runtime.status())
            case "prepare-issue":
                runtime.github.gh.repository()
                spec = issue_spec(runtime.github.gh.issue(args.number) or {})
                show({"issue": args.number, "spec": spec, "authorization": f"/agent start {digest(spec)}"})
            case "task":
                show(task_command(runtime, args))
            case "history":
                task_id = runtime.store.get(args.id).id if args.id else None
                show(runtime.store.events(task_id, max(1, min(args.limit, 500)), args.evidence))
            case "learn":
                if args.action == "list":
                    task_id = runtime.store.get(args.task).id if args.task else None
                    show(runtime.store.methods(task_id))
                else:
                    runtime.store.adoption(args.method, args.action == "adopt")
                    show({"method": args.method, "action": args.action})
            case "service":
                show(runtime.service(args.action))
        return 0
    except (OSError, ValueError, ProcessError, StaleTask, KeyError, TypeError, sqlite3.Error) as error:
        print(redact(str(error)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
