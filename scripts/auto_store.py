"""Local-only transactional automatic-task state and durable dispatch queue.

The store is an internal trusted service API, not a provider tool. Registry lookup,
process ownership proofs, provider capability checks and actual thread publication
belong to the runtime/API. No method runs a process or writes protocol artifacts.
Expired leases become UNKNOWN: they are never automatically redispatched.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from auto_process import is_unsafe_link
from auto_model import AutoError, TERMINAL, binding, canonical, digest, fail, identifier, plan as validate_plan, recipients as route_recipients, text


class AutoStore:
    def __init__(self, root, project_id, *, clock=time.time):
        self.root = Path(root).absolute()
        if self.root.is_symlink() or not self.root.is_dir() or self.root.resolve() != self.root:
            fail("unsafe_path", "project root must be a canonical directory")
        self.project_id = identifier(project_id, "project_id")
        self.clock = clock
        self.directory = self.root / ".ai-collab" / "local" / "automatic"
        cursor = self.root
        for part in (".ai-collab", "local", "automatic"):
            cursor = cursor / part
            if is_unsafe_link(cursor):
                fail("unsafe_path", "local state path cannot contain symlinks")
            cursor.mkdir(mode=0o700, exist_ok=True)
            if not cursor.is_dir():
                fail("unsafe_path", "local state parent must be a directory")
        # Privacy must exist before even creating the database: projects are not
        # required to ignore .ai-collab globally and the runtime may never start.
        self._ensure_private_ignore()
        self.path = self.directory / "state.sqlite3"
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if is_unsafe_link(self.path) or not self.path.is_file() or self.path.stat().st_nlink != 1:
                fail("unsafe_path", "unsafe state database")
            if os.name != "nt" and stat.S_IMODE(self.path.stat().st_mode) & 0o077:
                fail("unsafe_path", "state database permissions must be private")
        else:
            os.close(fd)
        with self._connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    task_id TEXT NOT NULL REFERENCES tasks(id), participant_id TEXT NOT NULL,
                    kind TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(state, seq);
                CREATE TABLE IF NOT EXISTS messages (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    task_id TEXT NOT NULL REFERENCES tasks(id), data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS delivery (
                    message_id TEXT NOT NULL REFERENCES messages(id), recipient TEXT NOT NULL,
                    state TEXT NOT NULL, job_id TEXT, data TEXT NOT NULL,
                    PRIMARY KEY(message_id, recipient));
                CREATE TABLE IF NOT EXISTS commands (
                    scope TEXT NOT NULL, key TEXT NOT NULL, digest TEXT NOT NULL,
                    result TEXT NOT NULL, PRIMARY KEY(scope,key));
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    kind TEXT NOT NULL, at REAL NOT NULL, data TEXT NOT NULL);
            ''')
            db.execute("BEGIN IMMEDIATE")
            for key, value in (("schema", "1"), ("project_id", self.project_id), ("root", str(self.root))):
                existing = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
                if existing and existing[0] != value:
                    fail("store_mismatch", "database identity/schema mismatch")
                db.execute("INSERT OR IGNORE INTO meta VALUES (?,?)", (key, value))
            db.commit()

    def _ensure_private_ignore(self):
        path = self.directory / ".gitignore"
        content = b"*\n"
        temporary = self.directory / (".ignore-" + uuid.uuid4().hex + ".tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # Publish complete bytes without replacing an existing policy.
                os.link(temporary, path)
            except FileExistsError:
                pass
            if is_unsafe_link(path) or not path.is_file() or path.read_bytes() != content:
                fail("unsafe_path", "automatic local state requires an unchanged private ignore policy")
            if os.name != "nt":
                fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def _connection(self):
        # Recheck the local path for each connection, including after initialization.
        if self.root.is_symlink() or self.root.resolve() != self.root or not self.root.is_dir():
            fail("unsafe_path", "project root changed")
        for path in (self.root / ".ai-collab", self.root / ".ai-collab/local", self.directory, self.path):
            if is_unsafe_link(path):
                fail("unsafe_path", "local database path changed to symlink")
        ignore = self.directory / ".gitignore"
        if is_unsafe_link(ignore) or not ignore.is_file() or ignore.read_bytes() != b"*\n":
            fail("unsafe_path", "automatic local state privacy policy changed")
        if not self.path.is_file() or self.path.stat().st_nlink != 1 or (os.name != "nt" and stat.S_IMODE(self.path.stat().st_mode) & 0o077):
            fail("unsafe_path", "database file identity or permissions changed")
        for suffix in ("-journal", "-wal", "-shm"):
            if is_unsafe_link(Path(str(self.path) + suffix)):
                fail("unsafe_path", "unsafe SQLite sidecar")
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    def _require_key(self, key):
        text(key, "idempotency key", 200)

    def _write(self, scope, key, payload, action):
        if key is not None:
            text(key, "idempotency key", 200)
        fingerprint = digest(payload)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if key is not None:
                    prior = db.execute("SELECT digest,result FROM commands WHERE scope=? AND key=?", (scope, key)).fetchone()
                    if prior:
                        if prior["digest"] != fingerprint:
                            fail("idempotency_conflict", "key already used with different request")
                        return json.loads(prior["result"])
                result = action(db)
                if key is not None:
                    db.execute("INSERT INTO commands VALUES (?,?,?,?)", (scope, key, fingerprint, canonical(result)))
                db.commit()
                return result
            except BaseException:
                db.rollback()
                raise

    def _task(self, db, task_id):
        identifier(task_id, "task_id")
        row = db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            fail("not_found", "task not found")
        return json.loads(row["data"])

    def _job(self, db, job_id):
        identifier(job_id, "job_id")
        row = db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            fail("not_found", "job not found")
        return json.loads(row["data"])

    def _expected(self, task, revision):
        if type(revision) is not int or revision != task["revision"]:
            fail("revision_conflict", "task revision changed")

    def _save_task(self, db, task):
        task["revision"] += 1
        task["updated_at"] = self.clock()
        db.execute("UPDATE tasks SET data=? WHERE id=?", (canonical(task), task["id"]))
        return task

    def _save_job(self, db, job):
        db.execute("UPDATE jobs SET state=?, data=? WHERE id=?", (job["state"], canonical(job), job["id"]))

    def _event(self, db, task_id, kind, data=None):
        db.execute("INSERT INTO events(task_id,kind,at,data) VALUES (?,?,?,?)", (task_id, kind, self.clock(), canonical(data or {})))

    def _active(self, db, task_id):
        return db.execute("SELECT 1 FROM jobs WHERE task_id=? AND state IN ('claimed','unknown') LIMIT 1", (task_id,)).fetchone() is not None

    def _binding(self, task, participant):
        return next((p for p in task["participants"] if p["participant_id"] == participant), None)

    def _insert_job(self, db, task, participant, kind, payload):
        selected = self._binding(task, participant)
        if not selected:
            fail("invalid_recipient", "job recipient not in approved roster")
        if kind not in {"planning", "worker", "message"}:
            fail("invalid", "unsupported job kind")
        if kind == "planning":
            if participant != task["coordinator"]["participant_id"] or task["state"] != "planning":
                fail("approval_required", "only coordinator may plan")
        elif kind == "worker":
            if task["approved_plan_revision"] != task["plan_revision"] or not task["approved_plan_revision"] or not task["thread_id"]:
                fail("approval_required", "worker requires exact approved plan and finalized thread")
            planned_id = payload.get("planned_task_id") if isinstance(payload, dict) else None
            planned = next((p for p in task["plan"]["tasks"] if p["id"] == planned_id), None)
            if not planned or planned["participant_id"] != participant:
                fail("invalid_job", "worker must match an approved planned task and recipient")
        else:
            if participant != task["coordinator"]["participant_id"] and (not task["approved_plan_revision"] or not task["thread_id"]):
                fail("approval_required", "message worker requires finalized approved roster")
            if not isinstance(payload, dict) or set(payload) - {"event_id", "reason"}:
                fail("invalid_job", "invalid message job payload")
            if payload.get("reason") == "execution_summary" and "event_id" not in payload:
                if participant != task["coordinator"]["participant_id"] or not task["thread_id"]:
                    fail("invalid_job", "summary requires finalized coordinator")
            elif "event_id" in payload and "reason" not in payload:
                delivery = db.execute("SELECT d.state FROM delivery d JOIN messages m ON m.id=d.message_id WHERE m.task_id=? AND d.message_id=? AND d.recipient=?", (task["id"], payload["event_id"], participant)).fetchone()
                if not delivery or delivery[0] != "queued":
                    fail("invalid_job", "message job requires queued recipient delivery")
            else:
                fail("invalid_job", "message job needs an event or summary reason")
        if task["state"] in TERMINAL | {"cancelling"}:
            fail("invalid_state", "terminal task cannot queue jobs")
        job = {"id": "job-" + uuid.uuid4().hex, "task_id": task["id"], "participant_id": participant,
               "kind": kind, "state": "queued", "payload": payload, "binding": selected.copy(),
               "binding_revision": task["binding_revision"], "plan_revision": task["plan_revision"],
               "attempt": 0, "owner": None, "token": None, "lease_until": None,
               "created_at": self.clock(), "result": None, "process_stopped": True}
        db.execute("INSERT INTO jobs(id,task_id,participant_id,kind,state,data) VALUES (?,?,?,?,?,?)",
                   (job["id"], task["id"], participant, kind, job["state"], canonical(job)))
        return job

    def create_task(self, title, instruction, coordinator, *, idempotency_key, guard=None):
        self._require_key(idempotency_key)
        title = text(title, "title", 300)
        instruction = text(instruction, "instruction")
        coordinator = binding(coordinator)
        if coordinator["participant_id"] == "all":
            fail("invalid", "all is reserved")
        payload = {"title": title, "instruction": instruction, "coordinator": coordinator}
        def action(db):
            now = self.clock()
            task = {"id": "auto-" + uuid.uuid4().hex, "project_id": self.project_id, "title": title,
                    "instruction": instruction, "coordinator": coordinator, "participants": [coordinator],
                    "revision": 1, "state": "planning", "plan_revision": 0, "plan_hash": None,
                    "approved_plan_revision": None, "approved_plan_hash": None, "plan": None,
                    "thread_id": None, "binding_revision": 1, "queued_provider_changes": [],
                    "acceptance": {"status": "unverified"}, "blocked_reason": None,
                    "created_at": now, "updated_at": now}
            if guard is not None:
                guard(task)
            db.execute("INSERT INTO tasks VALUES (?,?)", (task["id"], canonical(task)))
            self._insert_job(db, task, coordinator["participant_id"], "planning", {"instruction": instruction})
            self._event(db, task["id"], "created")
            return task
        return self._write("create", idempotency_key, payload, action)

    def get_task(self, task_id):
        with self._connection() as db:
            return self._task(db, task_id)

    def list_tasks(self, *, after=None, limit=100):
        if type(limit) is not int or not 1 <= limit <= 200:
            fail("invalid", "invalid page limit")
        with self._connection() as db:
            rows = db.execute("SELECT data FROM tasks WHERE id>? ORDER BY id LIMIT ?", (after or "", limit)).fetchall()
            return [json.loads(row[0]) for row in rows]

    def get_job(self, job_id):
        with self._connection() as db:
            return self._job(db, job_id)

    def list_jobs(self, task_id=None, *, states=None, limit=200, descending=False):
        if type(limit) is not int or not 1 <= limit <= 1000:
            fail("invalid", "invalid page limit")
        with self._connection() as db:
            clauses, parameters = [], []
            if task_id is not None:
                identifier(task_id, "task_id")
                clauses.append("task_id=?"); parameters.append(task_id)
            if states is not None:
                if not isinstance(states, (list, tuple, set)) or not states or any(s not in {"queued", "claimed", "unknown", "succeeded", "failed", "cancelled"} for s in states):
                    fail("invalid", "invalid job states")
                clauses.append("state IN (" + ",".join("?" for _ in states) + ")")
                parameters.extend(states)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            if type(descending) is not bool:
                fail("invalid", "invalid ordering")
            rows = db.execute("SELECT data FROM jobs" + where + (" ORDER BY seq DESC LIMIT ?" if descending else " ORDER BY seq LIMIT ?"), parameters + [limit]).fetchall()
            return [json.loads(row[0]) for row in rows]

    def job_headers(self, task_id, *, limit=200):
        """Bounded summary without materializing private provider-output blobs."""
        if type(limit) is not int or not 1 <= limit <= 200:
            fail("invalid", "invalid page limit")
        with self._connection() as db:
            self._task(db, task_id)
            rows = db.execute("SELECT id,participant_id,kind,state,seq FROM jobs WHERE task_id=? ORDER BY seq DESC LIMIT ?", (task_id,limit)).fetchall()
            count = db.execute("SELECT COUNT(*) FROM jobs WHERE task_id=?", (task_id,)).fetchone()[0]
            return {"jobs": [dict(row) for row in reversed(rows)], "truncated": count > len(rows)}

    def submit_plan(self, task_id, plan, *, expected_revision):
        def action(db):
            task = self._task(db, task_id)
            self._expected(task, expected_revision)
            if task["thread_id"] or task["state"] not in {"planning", "awaiting_approval"}:
                fail("invalid_state", "cannot replace finalized or inactive plan")
            value = validate_plan(plan, task["coordinator"])
            task.update(plan=value, plan_revision=task["plan_revision"] + 1, plan_hash=digest(value),
                        approved_plan_revision=None, approved_plan_hash=None, state="awaiting_approval", blocked_reason=None)
            self._event(db, task_id, "plan_proposed", {"plan_revision": task["plan_revision"], "plan_hash": task["plan_hash"]})
            return self._save_task(db, task)
        return self._write("submit_plan:" + task_id, None, plan, action)

    def edit_plan(self, task_id, plan, *, expected_revision, plan_revision, plan_hash, idempotency_key, guard=None):
        """Human edit of exactly the displayed draft; creates a new approval target."""
        self._require_key(idempotency_key)
        def action(db):
            task = self._task(db, task_id)
            if guard is not None:
                guard(task)
            self._expected(task, expected_revision)
            if task["thread_id"] or task["state"] != "awaiting_approval" or type(plan_revision) is not int or plan_revision != task["plan_revision"] or plan_hash != task["plan_hash"]:
                fail("approval_conflict", "draft edit does not match current pending plan")
            value = validate_plan(plan, task["coordinator"])
            task.update(plan=value, plan_revision=task["plan_revision"] + 1, plan_hash=digest(value),
                        approved_plan_revision=None, approved_plan_hash=None, blocked_reason=None)
            self._event(db, task_id, "plan_edited", {"plan_revision": task["plan_revision"], "plan_hash": task["plan_hash"]})
            return self._save_task(db, task)
        return self._write("edit_plan:" + task_id, idempotency_key, [plan, expected_revision, plan_revision, plan_hash], action)

    def approve(self, task_id, *, expected_revision, plan_revision, plan_hash, idempotency_key, guard=None):
        self._require_key(idempotency_key)
        def action(db):
            task = self._task(db, task_id)
            self._expected(task, expected_revision)
            if task["state"] != "awaiting_approval" or not task["plan"] or type(plan_revision) is not int or plan_revision != task["plan_revision"] or plan_hash != task["plan_hash"]:
                fail("approval_conflict", "approval does not match current pending plan")
            if guard is not None:
                guard(task)
            task.update(approved_plan_revision=plan_revision, approved_plan_hash=plan_hash,
                        participants=task["plan"]["participants"], state="ready")
            self._event(db, task_id, "approved", {"plan_revision": plan_revision, "plan_hash": plan_hash})
            return self._save_task(db, task)
        return self._write("approve:" + task_id, idempotency_key, [expected_revision, plan_revision, plan_hash], action)

    def bind_thread(self, task_id, thread_id, *, expected_revision):
        identifier(thread_id, "thread_id")
        def action(db):
            task = self._task(db, task_id)
            self._expected(task, expected_revision)
            if not task["approved_plan_revision"] or task["approved_plan_revision"] != task["plan_revision"] or task["state"] != "ready":
                fail("approval_required", "thread requires approved ready plan")
            if task["thread_id"] and task["thread_id"] != thread_id:
                fail("immutable_thread", "cannot rebind finalized thread")
            task["thread_id"] = thread_id
            self._event(db, task_id, "thread_bound", {"thread_id": thread_id})
            return self._save_task(db, task)
        return self._write("bind_thread:" + task_id, None, [thread_id, expected_revision], action)

    def revise(self, task_id, instruction, *, expected_revision, idempotency_key, guard=None):
        self._require_key(idempotency_key)
        text(instruction, "instruction")
        def action(db):
            task = self._task(db, task_id)
            if guard is not None:
                guard(task)
            self._expected(task, expected_revision)
            if task["thread_id"] or task["state"] in TERMINAL | {"cancelling"} or self._active(db, task_id):
                fail("invalid_state", "finalized or active plan cannot be revised")
            for row in db.execute("SELECT data FROM jobs WHERE task_id=? AND state='queued'", (task_id,)):
                job = json.loads(row[0]); job["state"] = "cancelled"; self._save_job(db, job)
            task.update(instruction=instruction, state="planning", approved_plan_revision=None,
                        approved_plan_hash=None, participants=[task["coordinator"]], blocked_reason=None)
            self._insert_job(db, task, task["coordinator"]["participant_id"], "planning", {"instruction": instruction})
            self._event(db, task_id, "revision_requested")
            return self._save_task(db, task)
        return self._write("revise:" + task_id, idempotency_key, [instruction, expected_revision], action)

    def enqueue(self, task_id, participant_id, kind, payload, *, idempotency_key):
        self._require_key(idempotency_key)
        canonical(payload)
        def action(db):
            task = self._task(db, task_id)
            job = self._insert_job(db, task, participant_id, kind, payload)
            self._event(db, task_id, "job_queued", {"job_id": job["id"], "participant_id": participant_id})
            return job
        return self._write("enqueue:" + task_id, idempotency_key, [participant_id, kind, payload], action)

    def claim(self, owner, *, lease_seconds=60):
        text(owner, "owner", 200)
        if type(lease_seconds) not in (int, float) or not 1 <= lease_seconds <= 3600:
            fail("invalid", "invalid lease")
        def action(db):
            # One active/unknown job per checkout, not only per task. Runtime also
            # needs an OS process lock to exclude legacy watcher/external writers.
            if db.execute("SELECT 1 FROM jobs WHERE state IN ('claimed','unknown') LIMIT 1").fetchone():
                return None
            for row in db.execute("SELECT data FROM jobs WHERE state='queued' ORDER BY seq"):
                job = json.loads(row[0]); task = self._task(db, job["task_id"])
                if job["kind"] == "planning":
                    allowed = task["state"] == "planning"
                elif job["kind"] == "message":
                    allowed = task["state"] in {"planning", "awaiting_approval", "ready", "running"} or (task["state"] == "blocked" and task["blocked_reason"] == "awaiting_verification")
                else:
                    allowed = task["state"] in {"ready", "running"}
                if not allowed:
                    continue
                if job["kind"] == "worker" and (job["plan_revision"] != task["approved_plan_revision"] or not task["thread_id"]):
                    continue
                if job["kind"] == "worker":
                    planned = next(p for p in task["plan"]["tasks"] if p["id"] == job["payload"]["planned_task_id"])
                    all_jobs = [json.loads(r[0]) for r in db.execute("SELECT data FROM jobs WHERE task_id=? AND kind='worker'", (task["id"],))]
                    dependencies_ready = True
                    for dep in planned["depends_on"]:
                        matching = [j for j in all_jobs if j["payload"]["planned_task_id"] == dep]
                        if not matching or any(j["state"] != "succeeded" for j in matching):
                            dependencies_ready = False
                    if not dependencies_ready:
                        continue
                if job["kind"] == "message" and "event_id" in job["payload"]:
                    delivery = db.execute("SELECT state FROM delivery WHERE message_id=? AND recipient=?", (job["payload"]["event_id"], job["participant_id"])).fetchone()
                    if not delivery or delivery[0] != "queued":
                        job["state"] = "cancelled"
                        self._save_job(db, job)
                        continue
                selected = self._binding(task, job["participant_id"])
                for field in ("pid", "process_token", "finished_at", "recovery_evidence"):
                    job.pop(field, None)
                job.update(state="claimed", owner=owner, token=uuid.uuid4().hex, attempt=job["attempt"] + 1,
                           lease_until=self.clock() + lease_seconds, process_stopped=False,
                           binding=selected.copy(), binding_revision=task["binding_revision"], claimed_at=self.clock(), task_state_at_claim=task["state"])
                self._save_job(db, job)
                if job["kind"] == "worker":
                    task["state"] = "running"; self._save_task(db, task)
                # Reserve queued messages for this exact attempt. They remain queued
                # until runtime confirms provider input transfer.
                db.execute("UPDATE delivery SET job_id=? WHERE recipient=? AND state='queued' AND job_id IS NULL AND message_id IN (SELECT id FROM messages WHERE task_id=?)", (job["id"], job["participant_id"], task["id"]))
                self._event(db, task["id"], "job_claimed", {"job_id": job["id"], "attempt": job["attempt"]})
                return job
            return None
        return self._write("claim", None, [owner, lease_seconds], action)

    def _fenced(self, db, job_id, token):
        job = self._job(db, job_id)
        if not isinstance(token, str) or job["state"] != "claimed" or job["token"] != token or job["lease_until"] <= self.clock():
            fail("stale_claim", "job claim is no longer current")
        return job

    def heartbeat(self, job_id, token, *, lease_seconds=60):
        if type(lease_seconds) not in (int, float) or not 1 <= lease_seconds <= 3600:
            fail("invalid", "invalid lease")
        def action(db):
            job = self._fenced(db, job_id, token)
            job["lease_until"] = self.clock() + lease_seconds
            self._save_job(db, job)
            return job
        return self._write("heartbeat", None, [job_id, token, lease_seconds], action)

    def attach_process(self, job_id, token, pid, process_token):
        if type(pid) is not int or pid <= 0:
            fail("invalid", "invalid process id")
        text(process_token, "process identity token", 256)
        def action(db):
            job = self._fenced(db, job_id, token)
            if job.get("pid") is not None and (job["pid"] != pid or job["process_token"] != process_token):
                fail("process_conflict", "claim already attached to a different process")
            job.update(pid=pid, process_token=process_token)
            self._save_job(db, job)
            return job
        return self._write("attach_process", None, [job_id, token, pid, process_token], action)

    def recover_owner(self, owner=None):
        """Quarantine claimed jobs, including the spawn-before-pid persistence gap."""
        def action(db):
            recovered = []
            for row in db.execute("SELECT data FROM jobs WHERE state='claimed'"):
                job = json.loads(row[0])
                if owner is not None and job["owner"] != owner:
                    continue
                job["state"] = "unknown"
                self._save_job(db, job)
                task = self._task(db, job["task_id"])
                if task["state"] not in {"cancelling", "pausing"}:
                    task["state"] = "blocked"
                task["blocked_reason"] = "process_unknown"
                self._save_task(db, task)
                self._fail_deliveries(db, job["id"], "delivery_unknown")
                self._event(db, task["id"], "owner_recovered", {"job_id": job["id"]})
                recovered.append(job)
            return recovered
        return self._write("recover_owner", None, owner, action)

    def _fail_deliveries(self, db, job_id, reason):
        for row in db.execute("SELECT message_id,recipient,data FROM delivery WHERE job_id=? AND state='queued'", (job_id,)):
            data = json.loads(row["data"]); data.update(error_code=reason)
            db.execute("UPDATE delivery SET state='failed',data=? WHERE message_id=? AND recipient=?", (canonical(data), row["message_id"], row["recipient"]))

    def finish(self, job_id, token, outcome, result=None):
        if outcome not in {"succeeded", "failed", "unknown", "cancelled"}:
            fail("invalid", "invalid job outcome")
        canonical(result)
        def action(db):
            job = self._fenced(db, job_id, token)
            job.update(state=outcome, result=result, finished_at=self.clock(), process_stopped=outcome != "unknown")
            self._save_job(db, job)
            self._fail_deliveries(db, job_id, "delivery_unknown" if outcome == "unknown" else "not_delivered")
            task = self._task(db, job["task_id"])
            if outcome == "unknown":
                if task["state"] not in {"cancelling", "pausing"}:
                    task["state"] = "blocked"
                task["blocked_reason"] = "process_unknown"
            elif task["state"] == "cancelling":
                task["state"] = "cancelled"
            elif task["state"] == "pausing":
                task["state"] = "paused"
            elif outcome in {"failed", "cancelled"}:
                task.update(state="blocked", blocked_reason="job_" + outcome)
            elif task["state"] == "running":
                # Exit success is not acceptance or task completion.
                task["state"] = "ready"
            self._apply_changes(db, task)
            self._save_task(db, task)
            self._event(db, task["id"], "job_finished", {"job_id": job_id, "outcome": outcome})
            return job
        return self._write("finish", None, [job_id, token, outcome, result], action)

    def expire_claims(self):
        def action(db):
            expired = []
            for row in db.execute("SELECT data FROM jobs WHERE state='claimed'"):
                job = json.loads(row[0])
                if job["lease_until"] > self.clock():
                    continue
                job["state"] = "unknown"; self._save_job(db, job)
                task = self._task(db, job["task_id"])
                if task["state"] not in {"cancelling", "pausing"}:
                    task["state"] = "blocked"
                task["blocked_reason"] = "process_unknown"; self._save_task(db, task)
                self._fail_deliveries(db, job["id"], "delivery_unknown")
                self._event(db, task["id"], "lease_expired", {"job_id": job["id"]})
                expired.append(job)
            return expired
        return self._write("expire", None, {}, action)

    def resolve_unknown(self, job_id, *, process_stopped, evidence):
        # Internal runtime call only. A browser assertion is NOT process proof.
        if process_stopped is not True:
            fail("process_unknown", "explicit process stop proof required")
        text(evidence, "process stop evidence", 8192)
        def action(db):
            job = self._job(db, job_id)
            if job["state"] != "unknown":
                fail("invalid_state", "only unknown job needs recovery proof")
            job.update(state="failed", process_stopped=True, recovery_evidence=evidence)
            self._save_job(db, job)
            task = self._task(db, job["task_id"])
            if task["state"] == "cancelling":
                task["state"] = "cancelled"
            elif task["state"] == "pausing":
                task["state"] = "paused"
            else:
                task["state"] = "blocked"
            task["blocked_reason"] = "retry_required"; self._save_task(db, task)
            self._event(db, task["id"], "process_stop_verified", {"job_id": job_id})
            return job
        return self._write("resolve_unknown", None, [job_id, evidence], action)

    def retry(self, task_id, job_id, *, expected_revision, idempotency_key):
        self._require_key(idempotency_key)
        def action(db):
            task = self._task(db, task_id); self._expected(task, expected_revision)
            job = self._job(db, job_id)
            if job["task_id"] != task_id or job["state"] != "failed" or not job["process_stopped"] or self._active(db, task_id) or task["state"] in TERMINAL | {"cancelling"}:
                fail("unsafe_retry", "job cannot safely be retried")
            if job["kind"] == "planning" and task["thread_id"]:
                fail("unsafe_retry", "cannot retry planning after thread finalization")
            job.update(state="queued", owner=None, token=None, lease_until=None, result=None)
            self._save_job(db, job)
            task.update(state="planning" if job["kind"] == "planning" else (job.get("task_state_at_claim", "ready") if job["kind"] == "message" else "ready"), blocked_reason=None)
            self._save_task(db, task)
            self._event(db, task_id, "retry_requested", {"job_id": job_id})
            return {"task": task, "job": job}
        return self._write("retry:" + task_id, idempotency_key, [job_id, expected_revision], action)

    def control(self, task_id, action, *, expected_revision, idempotency_key):
        self._require_key(idempotency_key)
        if action not in {"pause", "resume", "cancel"}:
            fail("invalid", "invalid control action")
        def change(db):
            task = self._task(db, task_id); self._expected(task, expected_revision)
            if task["state"] in TERMINAL:
                fail("invalid_state", "task is terminal")
            active = self._active(db, task_id)
            if action == "pause":
                if task["state"] not in {"planning", "awaiting_approval", "ready", "running"}:
                    fail("invalid_state", "task cannot pause")
                task["resume_state"] = "ready" if task["state"] == "running" else task["state"]
                task["state"] = "pausing" if active else "paused"
            elif action == "resume":
                if task["state"] != "paused" or active:
                    fail("invalid_state", "task is not safely paused")
                task["state"] = task.pop("resume_state", "ready")
            else:
                task["state"] = "cancelling" if active else "cancelled"
                for row in db.execute("SELECT data FROM jobs WHERE task_id=? AND state='queued'", (task_id,)):
                    job = json.loads(row[0]); job["state"] = "cancelled"; self._save_job(db, job)
                for row in db.execute("SELECT id FROM messages WHERE task_id=?", (task_id,)):
                    db.execute("UPDATE delivery SET state='failed',data=? WHERE message_id=? AND state='queued'", (canonical({"error_code": "task_cancelled"}), row[0]))
            self._event(db, task_id, action + "_requested")
            return self._save_task(db, task)
        return self._write("control:" + task_id, idempotency_key, [action, expected_revision], change)

    def mark_blocked(self, task_id, reason):
        identifier(reason, "blocked reason")
        def action(db):
            task = self._task(db, task_id)
            if task["state"] in TERMINAL | {"cancelling", "pausing"} or self._active(db, task_id):
                fail("invalid_state", "cannot block active/terminal task")
            task.update(state="blocked", blocked_reason=reason)
            self._event(db, task_id, "blocked", {"reason": reason})
            return self._save_task(db, task)
        return self._write("blocked", None, [task_id, reason], action)

    def mark_execution_finished(self, task_id):
        def action(db):
            task = self._task(db, task_id)
            if not task["thread_id"] or task["state"] not in {"ready", "running"} or self._active(db, task_id):
                fail("invalid_state", "execution is not safely finished")
            jobs = [json.loads(r[0]) for r in db.execute("SELECT data FROM jobs WHERE task_id=? AND kind='worker'", (task_id,))]
            for planned in task["plan"]["tasks"]:
                matching = [j for j in jobs if j["payload"]["planned_task_id"] == planned["id"]]
                if not matching or any(j["state"] != "succeeded" for j in matching):
                    fail("invalid_state", "planned execution remains unfinished")
            task.update(state="blocked", blocked_reason="awaiting_verification")
            self._event(db, task_id, "execution_finished")
            return self._save_task(db, task)
        return self._write("execution_finished", None, task_id, action)

    def _apply_changes(self, db, task):
        if self._active(db, task["id"]):
            return
        changes = task["queued_provider_changes"]
        for change in changes:
            selected = self._binding(task, change["participant_id"])
            selected.update(provider_id=change["provider_id"], model_id=change.get("model_id"))
            if selected["participant_id"] == task["coordinator"]["participant_id"]:
                task["coordinator"] = selected.copy()
            task["binding_revision"] += 1
            self._event(db, task["id"], "provider_changed", {"participant_id": selected["participant_id"], "binding_revision": task["binding_revision"]})
        task["queued_provider_changes"] = []

    def request_provider_change(self, task_id, participant_id, provider_id, model_id=None, *, expected_revision, idempotency_key, guard=None):
        self._require_key(idempotency_key)
        proposed = binding({"participant_id": participant_id, "provider_id": provider_id, "model_id": model_id})
        def action(db):
            task = self._task(db, task_id)
            if guard is not None:
                guard(task)
            self._expected(task, expected_revision)
            if not task["approved_plan_revision"] or task["state"] in TERMINAL | {"cancelling"} or not self._binding(task, participant_id):
                fail("invalid_state", "provider change requires approved active participant")
            task["queued_provider_changes"] = [p for p in task["queued_provider_changes"] if p["participant_id"] != participant_id] + [proposed]
            active = self._active(db, task_id)
            self._apply_changes(db, task)
            self._save_task(db, task)
            self._event(db, task_id, "provider_change_requested", {"participant_id": participant_id})
            return {"task": task, "change": {"state": "queued" if active else "applied", **proposed}}
        return self._write("provider:" + task_id, idempotency_key, [proposed, expected_revision], action)

    def send_message(self, task_id, body, recipients=None, *, idempotency_key):
        self._require_key(idempotency_key)
        text(body, "message body")
        def action(db):
            task = self._task(db, task_id)
            if task["state"] in TERMINAL | {"cancelling"}:
                fail("invalid_state", "terminal task cannot receive messages")
            names = [p["participant_id"] for p in task["participants"]]
            routed = route_recipients(body, names, task["coordinator"]["participant_id"], recipients)
            event = {"event_id": "human-" + uuid.uuid4().hex, "task_id": task_id, "body": body,
                     "recipients": routed, "actor_type": "human", "provenance": {"kind": "manual", "original_text": True}, "created_at": self.clock()}
            db.execute("INSERT INTO messages(id,task_id,data) VALUES (?,?,?)", (event["event_id"], task_id, canonical(event)))
            for recipient in routed:
                db.execute("INSERT INTO delivery VALUES (?,?,'queued',NULL,?)", (event["event_id"], recipient, canonical({"attempt": 0})))
            self._event(db, task_id, "human_message_queued", {"event_id": event["event_id"], "recipients": routed})
            return self._message(db, event["event_id"])
        return self._write("message:" + task_id, idempotency_key, [body, recipients], action)

    def _message(self, db, event_id):
        row = db.execute("SELECT data FROM messages WHERE id=?", (event_id,)).fetchone()
        event = json.loads(row[0])
        event["delivery"] = [{**json.loads(r["data"]), "recipient": r["recipient"], "state": r["state"], "job_id": r["job_id"]}
                             for r in db.execute("SELECT * FROM delivery WHERE message_id=? ORDER BY recipient", (event_id,))]
        return event

    def messages(self, task_id, *, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
            fail("invalid", "invalid pagination")
        with self._connection() as db:
            self._task(db, task_id)
            rows = db.execute("SELECT seq,id FROM messages WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?", (task_id, after, limit)).fetchall()
            return {"events": [self._message(db, r["id"]) for r in rows], "next_cursor": rows[-1]["seq"] if rows else after}

    def job_messages(self, job_id, token):
        with self._connection() as db:
            self._fenced(db, job_id, token)
            rows = db.execute("SELECT message_id FROM delivery WHERE job_id=? AND state='queued' ORDER BY rowid", (job_id,)).fetchall()
            return [self._message(db, r[0]) for r in rows]

    def mark_delivered(self, job_id, token, event_ids):
        if not isinstance(event_ids, list) or any(not isinstance(e, str) for e in event_ids) or len(set(event_ids)) != len(event_ids):
            fail("invalid", "invalid event_ids")
        def action(db):
            job = self._fenced(db, job_id, token)
            for event_id in event_ids:
                row = db.execute("SELECT state FROM delivery WHERE message_id=? AND recipient=? AND job_id=?", (event_id, job["participant_id"], job_id)).fetchone()
                if not row or row[0] not in {"queued", "delivered"}:
                    fail("delivery_conflict", "message is not assigned to this attempt")
                db.execute("UPDATE delivery SET state='delivered',data=? WHERE message_id=? AND recipient=?", (canonical({"attempt": job["attempt"], "delivered_at": self.clock()}), event_id, job["participant_id"]))
            return {"event_ids": event_ids, "job_id": job_id}
        return self._write("delivered", None, [job_id, token, event_ids], action)

    def events(self, task_id, *, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
            fail("invalid", "invalid pagination")
        with self._connection() as db:
            self._task(db, task_id)
            rows = db.execute("SELECT * FROM events WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?", (task_id, after, limit)).fetchall()
            return {"events": [dict(r, data=json.loads(r["data"])) for r in rows], "next_cursor": rows[-1]["seq"] if rows else after}
