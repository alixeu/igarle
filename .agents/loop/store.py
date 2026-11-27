"""Durable task state, execution receipts, and intended GitHub writes."""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
import fcntl
import json
from pathlib import Path
import sqlite3
import threading
import time
from uuid import uuid4

from settings import json_text, task_spec


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    spec TEXT NOT NULL,
    source TEXT NOT NULL,
    issue INTEGER UNIQUE,
    issue_digest TEXT,
    fingerprint TEXT UNIQUE,
    revision INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    phase TEXT NOT NULL,
    ready_at INTEGER NOT NULL,
    data TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_ready ON tasks(phase, ready_at, created_at);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    revision INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    phase TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    finished_at INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    task_id TEXT REFERENCES tasks(id),
    revision INTEGER,
    run_id TEXT,
    kind TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS task_events ON events(task_id, id);
CREATE TABLE IF NOT EXISTS effects (
    key TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    state TEXT NOT NULL,
    receipt TEXT
);
CREATE TABLE IF NOT EXISTS inbound (
    id INTEGER PRIMARY KEY,
    handled_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS methods (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    adopted INTEGER NOT NULL DEFAULT 0,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

LOCAL_PHASES = ("plan", "implement", "check", "review", "experiment")
TERMINAL_PHASES = ("done", "cancelled")


@dataclass(frozen=True)
class Task:
    id: str
    spec: dict
    source: str
    issue: int | None
    issue_digest: str | None
    fingerprint: str | None
    revision: int
    generation: int
    phase: str
    ready_at: int
    data: dict
    created_at: int

    @classmethod
    def from_row(cls, row):
        return cls(**(dict(row) | {"spec": json.loads(row["spec"]), "data": json.loads(row["data"])}))


class StaleTask(Exception):
    """A newer observation already replaced a task snapshot."""


class Store:
    def __init__(self, directory: Path, clock=time.time):
        self.directory = directory
        self.path = directory / "state.sqlite3"
        self.clock = clock
        self._locks_guard = threading.Lock()
        self._locks = {}
        self._local = threading.local()
        if directory.resolve() != directory or self.path.is_symlink():
            raise ValueError("The runtime path cannot traverse a symlink.")
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        with closing(self.connect()) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError(f"Unsupported Pi Loop state version: {version}.")
            if version == 0 and db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
            ).fetchone():
                raise ValueError("The state database has an unknown schema.")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.execute("PRAGMA user_version=1")

    @contextmanager
    def task_lock(self, task_id):
        if len(task_id) != 32 or any(char not in "0123456789abcdef" for char in task_id):
            raise ValueError("Invalid task identity.")
        with self._locks_guard:
            lock = self._locks.setdefault(task_id, threading.RLock())
        with lock:
            held = getattr(self._local, "held", set())
            if task_id in held:
                yield
                return
            locks = self.directory / "locks"
            locks.mkdir(mode=0o700, exist_ok=True)
            path = locks / task_id
            if locks.resolve() != locks or path.is_symlink():
                raise ValueError("Task locks cannot traverse symlinks.")
            with path.open("a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                self._local.held = held | {task_id}
                try:
                    yield
                finally:
                    self._local.held = held
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def now(self):
        return int(self.clock())

    def connect(self):
        db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def write(self):
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()

    def event(self, kind, data, task=None, run=None, *, db=None):
        if db is None:
            with self.write() as connection:
                return self.event(kind, data, task, run, db=connection)
        return db.execute(
            "INSERT INTO events(task_id,revision,run_id,kind,data,created_at) VALUES(?,?,?,?,?,?)",
            (task.id if task else None, task.revision if task else None,
             run, kind, json_text(data), self.now()),
        ).lastrowid

    def add(self, spec, *, source="local", issue=None, issue_digest=None, fingerprint=None, data=None,
            db=None):
        spec = task_spec(spec)
        if db is None:
            with self.write() as connection:
                return self.add(spec, source=source, issue=issue, issue_digest=issue_digest,
                                fingerprint=fingerprint, data=data, db=connection)
        row = db.execute("SELECT * FROM tasks WHERE issue=? OR fingerprint=?",
                         (issue, fingerprint)).fetchone()
        if row:
            return Task.from_row(row), False
        task = Task(uuid4().hex, spec, source, issue, issue_digest, fingerprint,
                    1, 1, "plan", self.now(), data or {}, self.now())
        db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (task.id, json_text(spec), source, issue, issue_digest, fingerprint,
                    task.revision, task.generation, task.phase, task.ready_at,
                    json_text(task.data), task.created_at))
        self.event("task_created", {"source": source, "issue": issue, "spec": spec},
                   task, db=db)
        return task, True

    def get(self, task_id):
        with closing(self.connect()) as db:
            rows = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchall()
            if not rows and 6 <= len(task_id) < 32 and all(char in "0123456789abcdef" for char in task_id):
                rows = db.execute("SELECT * FROM tasks WHERE id LIKE ? LIMIT 2", (task_id + "%",)).fetchall()
        if len(rows) != 1:
            raise ValueError(f"Unknown or ambiguous task ID: {task_id}")
        return Task.from_row(rows[0])

    def all(self, *, active=False):
        query = "SELECT * FROM tasks"
        if active:
            query += " WHERE phase NOT IN ('done','cancelled')"
        with closing(self.connect()) as db:
            return [Task.from_row(row) for row in db.execute(query + " ORDER BY created_at,rowid")]

    def next(self):
        with closing(self.connect()) as db:
            row = db.execute("""
                SELECT * FROM tasks
                WHERE phase IN ('plan','implement','check','review','experiment') AND ready_at <= ?
                ORDER BY ready_at, created_at, rowid LIMIT 1
                """, (self.now(),)).fetchone()
        return Task.from_row(row) if row else None

    def active_runs(self):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute("""
                SELECT id,task_id,revision,phase,started_at FROM runs
                WHERE status='running' ORDER BY started_at
                """)]

    def start(self, task):
        run = uuid4().hex
        with self.write() as db:
            current = db.execute("SELECT generation FROM tasks WHERE id=?", (task.id,)).fetchone()
            if current["generation"] != task.generation:
                raise StaleTask(task.id)
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,'running',?,NULL)",
                       (run, task.id, task.revision, task.generation, task.phase, self.now()))
            self.event("run_started", {"phase": task.phase, "checkpoint": task.data.get("checkpoint")},
                       task, run, db=db)
        return run

    def transition(self, task, phase, kind, *, data=None, event=None, run=None, after=0,
                   spec=None, issue=None, issue_digest=None, revise=False, method=None, db=None):
        if db is None:
            with self.task_lock(task.id):
                with self.write() as connection:
                    return self.transition(task, phase, kind, data=data, event=event, run=run, after=after,
                                           spec=spec, issue=issue, issue_digest=issue_digest, revise=revise,
                                           method=method, db=connection)
        updated = Task(task.id, spec if spec is not None else task.spec, task.source,
                       issue if issue is not None else task.issue,
                       issue_digest if issue_digest is not None else task.issue_digest,
                       task.fingerprint, task.revision + int(revise), task.generation + 1, phase,
                       self.now() + after, task.data if data is None else data, task.created_at)
        if method is not None:
            state, details = method
            method_id = db.execute(
                "INSERT INTO methods(task_id,revision,status,data) VALUES(?,?,?,?)",
                (task.id, task.revision, state, json_text(details)),
            ).lastrowid
            if state == "validated":
                updated = Task(**(vars(updated) | {
                    "data": updated.data | {"active_method": method_id},
                }))
            event = (event or {}) | {"method_id": method_id, "method_status": state}
        changed = db.execute("""
            UPDATE tasks SET spec=?,issue=coalesce(?,issue),issue_digest=coalesce(?,issue_digest),
                revision=?,generation=?,phase=?,ready_at=?,data=?
            WHERE id=? AND generation=?
            """, (json_text(updated.spec), updated.issue, updated.issue_digest,
                  updated.revision, updated.generation, phase, updated.ready_at,
                  json_text(updated.data), task.id, task.generation))
        if not changed.rowcount:
            raise StaleTask(task.id)
        if run:
            result = db.execute("""
                UPDATE runs SET status='recorded',finished_at=?
                WHERE id=? AND task_id=? AND generation=? AND status='running'
                """, (self.now(), run, task.id, task.generation))
            if not result.rowcount:
                raise StaleTask(task.id)
        self.event(kind, event or {}, updated, run, db=db)
        return Task.from_row(db.execute("SELECT * FROM tasks WHERE id=?", (task.id,)).fetchone())

    def revise(self, task, spec, *, issue_digest=None, db=None):
        if task.phase in TERMINAL_PHASES:
            raise ValueError("Completed tasks require a new Issue.")
        keep = {key: task.data[key] for key in ("published", "pr", "red", "scan_base", "decision", "decisions")
                if key in task.data}
        return self.transition(
            task, "plan", "task_revised", data=keep | {
                "previous_base": task.data.get("base"),
                "previous_checkpoint": task.data.get("checkpoint"),
                "correction": {"previous_revision": task.revision, "previous_spec": task.spec},
            }, event={"spec": task_spec(spec)}, spec=task_spec(spec), issue_digest=issue_digest,
            revise=True, db=db)

    def attach_issue(self, task_id, number, version):
        with self.write() as db:
            attached = db.execute("UPDATE tasks SET issue=?,issue_digest=? WHERE id=? AND issue IS NULL",
                                  (number, version, task_id))
            task = Task.from_row(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
            if task.issue != number:
                raise StaleTask(task_id)
            if attached.rowcount:
                self.event("issue_linked", {"issue": number, "digest": version}, task, db=db)
            return task

    def abandon(self, run):
        with self.write() as db:
            db.execute("UPDATE runs SET status='superseded',finished_at=? WHERE id=? AND status='running'",
                       (self.now(), run))

    def recover(self):
        with self.write() as db:
            for entry in db.execute("SELECT * FROM runs WHERE status='running'").fetchall():
                task_id, run = entry["task_id"], entry["id"]
                task = Task.from_row(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
                self.event("run_interrupted", {"phase": entry["phase"], "revision": entry["revision"]},
                           task, run, db=db)
            db.execute("UPDATE runs SET status='interrupted',finished_at=? WHERE status='running'",
                       (self.now(),))

    def events(self, task_id=None, limit=40, evidence_id=None):
        with closing(self.connect()) as db:
            return [dict(row) | {"data": json.loads(row["data"])} for row in db.execute("""
                SELECT * FROM events WHERE (? IS NULL OR task_id=?) AND (? IS NULL OR id=?)
                ORDER BY id DESC LIMIT ?
                """, (task_id, task_id, evidence_id, evidence_id, limit))]

    def event_index(self, task_id, limit=35):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute("""
                SELECT id,kind,revision FROM events WHERE task_id=? ORDER BY id DESC LIMIT ?
                """, (task_id, limit))]

    def meta(self, key, default=None):
        with closing(self.connect()) as db:
            row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key, value):
        with self.write() as db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (key, json_text(value)))

    def admit_once(self, comment_id, apply):
        with self.write() as db:
            inserted = db.execute("INSERT OR IGNORE INTO inbound VALUES(?,?)", (comment_id, self.now()))
            if inserted.rowcount:
                return apply(db)
        return None

    def effect(self, key):
        with closing(self.connect()) as db:
            row = db.execute("SELECT state,receipt FROM effects WHERE key=?", (key,)).fetchone()
        if row:
            return row["state"], json.loads(row["receipt"]) if row["receipt"] else None
        return None, None

    def record_ci(self, task, receipt, workflow, job, *, rules=None):
        key = f"ci:{task.id}:{task.revision}:{receipt['head_sha']}:{receipt['id']}:{receipt['run_attempt']}"
        with self.write() as db:
            inserted = db.execute("INSERT OR IGNORE INTO metadata VALUES(?,?)",
                                  (key, json_text({"recorded": True})))
            if inserted.rowcount:
                self.event("ci_passed", {"receipt": receipt, "workflow": workflow, "job": job,
                                         "merge_rules": rules},
                           task, db=db)

    def effect_started(self, key, task):
        with self.write() as db:
            db.execute("INSERT OR IGNORE INTO effects VALUES(?,?,'sent',NULL)", (key, task.id))
            self.event("github_intent", {"key": key}, task, db=db)

    def effect_confirmed(self, key, task, receipt):
        with self.write() as db:
            changed = db.execute("""
                INSERT INTO effects VALUES(?,?,'confirmed',?)
                ON CONFLICT(key) DO UPDATE SET state='confirmed',receipt=excluded.receipt
                WHERE effects.state!='confirmed' OR effects.receipt!=excluded.receipt
                """, (key, task.id, json_text(receipt)))
            if changed.rowcount:
                self.event("github_confirmed", {"key": key, "receipt": receipt}, task, db=db)

    def pending_effects(self):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute(
                "SELECT key,task_id,state FROM effects WHERE state='sent' ORDER BY key"
            )]

    def clear_effect(self, key, task):
        with self.write() as db:
            deleted = db.execute("DELETE FROM effects WHERE key=? AND task_id=? AND state='sent'",
                                 (key, task.id))
            if deleted.rowcount:
                self.event("github_intent_cleared", {"key": key}, task, db=db)

    def methods(self, task_id=None):
        with closing(self.connect()) as db:
            return [dict(row) | {"data": json.loads(row["data"])} for row in db.execute("""
                SELECT * FROM methods WHERE (? IS NULL OR task_id=?) ORDER BY id DESC
                """, (task_id, task_id))]

    def adoption(self, method_id, adopt):
        with self.write() as db:
            row = db.execute("SELECT * FROM methods WHERE id=?", (method_id,)).fetchone()
            if row is None or (adopt and row["status"] not in {"validated", "revoked"}):
                raise ValueError("Only a validated method can be adopted.")
            db.execute("UPDATE methods SET adopted=?,status=? WHERE id=?",
                       (int(adopt), "validated" if adopt else "revoked", method_id))
            task = Task.from_row(db.execute("SELECT * FROM tasks WHERE id=?", (row["task_id"],)).fetchone())
            self.event("method_adopted" if adopt else "method_revoked", {"method_id": method_id},
                       task, db=db)
