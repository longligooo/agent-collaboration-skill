"""Opt-in single-machine automatic collaboration supervisor.

No discovery/model process is started by importing this module. The caller owns
registered-project resolution and explicit start authorization. Provider input and
results remain local; shared protocol artifacts contain structural references only.
"""
from __future__ import annotations

import concurrent.futures
import copy
import hashlib
import json
import os
import queue
import stat
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import collab
import auto_process
import human_events
import runtime_activity
import task_model
import task_discovery
from auto_model import AutoError, canonical, fail, plan as validate_plan
from auto_providers import ProviderError, ProviderRegistry, parse_result

_MESSAGE_PAGE = 100
_MESSAGE_BATCHES = 20


class _PipePump:
    """Bounded pipe threads work on Windows anonymous pipes as well as POSIX."""
    def __init__(self, process, limit):
        self.process = process
        self.events = queue.Queue(64)
        self.writes = queue.Queue(64)
        self.stop = threading.Event()
        self.exceeded = threading.Event()
        self.count = 0
        self.lock = threading.Lock()
        self.threads = []
        for stream, label in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            thread = threading.Thread(target=self._read, args=(stream, label), daemon=True)
            self.threads.append(thread)
        self.limit = limit
        self.threads.append(threading.Thread(target=self._write, daemon=True))
        for thread in self.threads:
            thread.start()

    def _emit(self, value):
        while not self.stop.is_set():
            try:
                self.events.put(value, timeout=.05)
                return
            except queue.Full:
                pass

    def _read(self, stream, label):
        try:
            while not self.stop.is_set():
                data = os.read(stream.fileno(), 65536)
                if not data:
                    break
                with self.lock:
                    self.count += len(data)
                    if self.count > self.limit:
                        self.exceeded.set()
                        break
                # stderr is bounded but never retained or exposed.
                if label == "stdout":
                    self._emit((label, data))
        except (OSError, ValueError):
            pass
        finally:
            self._emit((label, None))

    def _write(self):
        try:
            while not self.stop.is_set():
                try:
                    data, marker, close = self.writes.get(timeout=.05)
                except queue.Empty:
                    continue
                view = memoryview(data)
                while view and not self.stop.is_set():
                    size = os.write(self.process.stdin.fileno(), view[:65536])
                    if size <= 0:
                        raise BrokenPipeError()
                    view = view[size:]
                if self.stop.is_set():
                    return
                self.process.stdin.flush()
                self._emit(("sent", marker))
                if close:
                    self.process.stdin.close()
                    return
        except (OSError, ValueError):
            self._emit(("write_failed", None))

    def send(self, data, marker=None, close=False):
        try:
            self.writes.put_nowait((data, marker, close))
        except queue.Full:
            raise ProviderError("protocol_backpressure") from None

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=1)


def _safe(root, relative):
    root = Path(root)
    if auto_process.is_unsafe_link(root) or root.resolve() != root or not root.is_dir():
        fail("unsafe_path", "registered project root changed")
    path = collab.resolve_repo_path(root, relative, label="automatic runtime path")
    cursor = root
    for part in Path(relative).parts:
        cursor /= part
        if auto_process.is_unsafe_link(cursor):
            fail("unsafe_path", "automatic runtime paths cannot contain symlinks")
    return path


def _publish_once(root, relative, content):
    path = _safe(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = content.encode("utf-8")
    if path.exists():
        if not path.is_file() or path.read_bytes() != encoded:
            fail("artifact_conflict", "immutable automatic artifact differs")
        return
    temp = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded); stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            if path.read_bytes() != encoded:
                fail("artifact_conflict", "concurrent immutable artifact differs")
        # Windows cannot open a directory with CRT os.open. File content is
        # flushed before atomic no-clobber publication; no stronger power-loss
        # directory durability is claimed on Windows.
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


def _artifact_snapshots(workspace, artifacts):
    """Only explicitly approved shared text artifacts cross recipient boundaries."""
    result, total = [], 0
    for relative in artifacts[:16]:
        item = {"path": relative, "status": "unavailable"}
        result.append(item)
        try:
            parts = Path(relative).parts
            if not parts or any(part.startswith('.') for part in parts):
                continue
            path = _safe(Path(workspace["path"]), relative)
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    continue
                data = stream.read(16385)
            if len(data) > 16384 or total + len(data) > 65536 or b"\0" in data:
                item["status"] = "not_text_or_limit"
                continue
            item.update(status="shared", text=data.decode("utf-8"))
            total += len(data)
        except (AutoError, collab.CollaborationError, OSError, UnicodeError, ValueError):
            pass
    return result


def _clip_text(value, budget):
    def size(text):
        return len(json.dumps(text, ensure_ascii=False).encode("utf-8"))
    if size(value) <= budget:
        return value, False
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if size(value[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    return value[:low], True


def _compact_result(result, retained):
    value = copy.deepcopy(result)
    value["retained_attempt"] = retained
    if isinstance(value.get("output"), str):
        value["output"], truncated = _clip_text(value["output"], 256 * 1024)
        value["output_truncated"] = bool(value.get("output_truncated")) or truncated
    application = value.get("application")
    if isinstance(application, dict):
        if isinstance(application.get("tracked_patch"), str):
            application["tracked_patch"], truncated = _clip_text(application["tracked_patch"], 128 * 1024)
            application["patch_truncated"] = bool(application.get("patch_truncated")) or truncated
        if isinstance(application.get("untracked_files"), list):
            original = application["untracked_files"]
            selected = []
            for name in original[:256]:
                candidate = selected + [name]
                if len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) > 64 * 1024:
                    break
                selected = candidate
            application["untracked_files"] = selected
            application["untracked_files_truncated"] = bool(application.get("untracked_files_truncated")) or len(selected) != len(original)
        if isinstance(application.get("changes"), list):
            original, selected, size = application["changes"], [], 2
            for change in original[:512]:
                count = len(json.dumps(change, ensure_ascii=False).encode("utf-8")) + 1
                if size + count > 128 * 1024:
                    break
                selected.append(change); size += count
            application["changes"] = selected
            application["changes_truncated"] = bool(application.get("changes_truncated")) or len(selected) != len(original)
    canonical(value)  # Whole record, not just each field, must fit durable store.
    return value


def plan_paths(task_id):
    base = f".scratch/automatic-{task_id}"
    return base + "/spec.md", base + "/issues/01-approved-plan.md"


def _validated_plan(task, value):
    value = validate_plan(value, task["coordinator"])
    spec, ticket = plan_paths(task["id"])
    if value["spec"] != spec or value["ticket"] != ticket:
        fail("invalid_plan", "plan must reference the server-assigned spec/ticket paths")
    lead = task["coordinator"]["participant_id"]
    if any(t["participant_id"] == lead for t in value["tasks"]):
        fail("invalid_plan", "coordinator cannot implement product tasks")
    return value


class _RootLease:
    """Separate flock plus legacy-compatible marker; never force or steal."""
    def __init__(self, store, owner):
        self.store, self.owner = store, owner
        _publish_once(store.root, ".ai-collab/local/automatic/.gitignore", "*\n")
        self.handle = None
        self.watch = _safe(store.root, ".ai-collab/watch.lock")
        lock = _safe(store.root, ".ai-collab/local/automatic/service.lock")
        try:
            self.handle = auto_process.AdvisoryLock(lock).acquire()
        except auto_process.ProcessError as exc:
            fail("runtime_busy" if exc.code == "lock_busy" else exc.code, "cannot own checkout lock")
        # With the exclusive automatic-service lock held, old claims cannot be
        # owned by another cooperating supervisor. Quarantine even the spawn gap.
        store.recover_owner()
        try:
            fd = os.open(self.watch, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            self.close(remove=False)
            fail("checkout_busy", "watch lock exists; explicit operator inspection required")
        self.marker = canonical({"pid": os.getpid(), "owner": owner, "observer": "automatic-runtime",
                                 "started_at": collab.now_iso()})
        with os.fdopen(fd, "w") as stream:
            stream.write(self.marker); stream.flush(); os.fsync(stream.fileno())

    def owned(self):
        try:
            return not self.watch.is_symlink() and self.watch.read_text() == self.marker
        except OSError:
            return False

    def close(self, remove=True):
        if self.handle is None:
            return
        try:
            if remove and self.owned():
                # Do not let a legacy watcher overlap a process whose death is unknown.
                unknown = self.store.list_jobs(states=["claimed", "unknown"], limit=1)
                if not unknown:
                    self.watch.unlink(missing_ok=True)
        except (AutoError, OSError):
            pass  # Preserve the marker; path/proof uncertainty is not clearance.
        finally:
            self.handle.close(); self.handle = None


class Supervisor:
    def __init__(self, stores, providers, max_workers=2, *, job_timeout=300.0,
                 max_output=1024 * 1024, poll_interval=.1, workspace_manager=None):
        if type(max_workers) is not int or not 1 <= max_workers <= 8:
            raise ValueError("max_workers must be 1..8")
        if not 0 < job_timeout <= 3600 or not 1024 <= max_output <= 4 * 1024 * 1024 or not .01 <= poll_interval <= 5:
            raise ValueError("invalid runtime bounds")
        if isinstance(providers, ProviderRegistry) and workspace_manager is None:
            raise ProviderError("managed_workspace_required")
        self.workspace_manager = workspace_manager
        self.stores, self.providers = stores, providers
        self.max_workers, self.job_timeout = max_workers, job_timeout
        self.max_output, self.poll_interval = max_output, poll_interval
        self.owner = "runtime-" + uuid.uuid4().hex
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="auto-cycle")
        self._lock = threading.RLock()
        self._tick_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._leases, self._futures, self._active, self._errors, self._cursors = {}, {}, {}, {}, {}
        self._message_cursors = {}
        self._registered = set()
        self._closed = False

    def start(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("supervisor is closed")
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._loop, name="automatic-supervisor", daemon=True)
            self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                # No raw exception output: argv, model text and paths may be private.
                with self._lock:
                    self._errors["service"] = "scheduler_error"
            self._stop.wait(self.poll_interval)

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)
        self._executor.shutdown(wait=True, cancel_futures=False)
        with self._lock:
            for lease in self._leases.values():
                lease.close()
            self._leases.clear()
            self._closed = True

    def tick(self):
        """One bounded scheduling pass; jobs execute asynchronously, never on HTTP."""
        if self._stop.is_set() or not self._tick_lock.acquire(blocking=False):
            return
        try:
            stores = list(self.stores())
            with self._lock:
                self._registered = {str(s.root) for s in stores}
                for root, future in list(self._futures.items()):
                    if future.done():
                        try:
                            future.result()
                        except Exception:
                            self._errors[root] = "cycle_error"
                        del self._futures[root]
                for root in set(self._leases) - self._registered:
                    if root not in self._futures:
                        self._leases.pop(root).close()
                capacity = self.max_workers - len(self._futures)
            for store in stores:
                root = str(store.root)
                with self._lock:
                    if root in self._futures:
                        continue
                if root not in self._leases:
                    try:
                        self._leases[root] = _RootLease(store, self.owner)
                        self._errors.pop(root, None)
                    except (AutoError, OSError) as exc:
                        self._errors[root] = getattr(exc, "code", "checkout_lock_error")
                        continue
                if not self._leases[root].owned():
                    self._errors[root] = "checkout_lock_lost"
                    continue
                if capacity <= 0:
                    continue
                store.expire_claims()
                self._prepare(store)
                if store.list_jobs(states=["unknown"], limit=1):
                    self._errors[root] = "process_unknown"
                    continue
                job = store.claim(self.owner, lease_seconds=30)
                if job is not None:
                    with self._lock:
                        self._futures[root] = self._executor.submit(self._cycle, store, job)
                    capacity -= 1
        finally:
            self._tick_lock.release()

    def activity(self, root, task_id):
        key = str(Path(root).resolve())
        with self._lock:
            record = dict(self._active.get(key, {}))
            error = self._errors.get(key)
        fresh = bool(record and record.get("task_id") == task_id and
                     time.monotonic() - record.get("observed_at", 0) < 5 and
                     runtime_activity.process_alive(record.get("pid")))
        if fresh and runtime_activity.process_token(record["pid"]) != record.get("process_token"):
            fresh = False
        identity = None
        if fresh:
            try:
                identity = task_discovery.identity_for(record["participant_id"], task_discovery.load_identities(Path(root)))
            except (OSError, collab.CollaborationError):
                pass
        return {"identity": identity, "active": fresh, "status": "running" if fresh else "unknown" if error else "idle",
                "observer": "automatic-runtime", "job_id": record.get("job_id") if fresh else None,
                "participant_id": record.get("participant_id") if fresh else None,
                "process_pid": record.get("pid") if fresh else None,
                "heartbeat_at": record.get("heartbeat_at") if fresh else None,
                "expires_at": record.get("expires_at") if fresh else None, "reason": error}

    def _prepare(self, store):
        root = str(store.root)
        tasks = store.list_tasks(after=self._cursors.get(root), limit=100)
        self._cursors[root] = tasks[-1]["id"] if len(tasks) == 100 else None
        for task in tasks:
            try:
                if task["state"] == "planning":
                    # Pause can occur after the planner produced output. Keep it
                    # local and install only after an explicit resume.
                    with store._connection() as db:
                        row = db.execute("SELECT data FROM jobs WHERE task_id=? AND kind='planning' ORDER BY seq DESC LIMIT 1", (task["id"],)).fetchone()
                    candidate = json.loads(row[0]) if row else None
                    if candidate and candidate["state"] == "succeeded" and (candidate.get("result") or {}).get("deferred_plan"):
                        if candidate["payload"].get("instruction") == task["instruction"]:
                            value = _validated_plan(task, json.loads(self._retained_output(store, candidate)))
                            task = store.submit_plan(task["id"], value, expected_revision=task["revision"])
                if task["state"] == "ready" and not task["thread_id"]:
                    task = self._finalize(store, task)
                if task["thread_id"]:
                    self._bridge_humans(store, task)
                if task["state"] in {"ready", "running"} and task["thread_id"]:
                    for planned in task["plan"]["tasks"]:
                        store.enqueue(task["id"], planned["participant_id"], "worker",
                                      {"planned_task_id": planned["id"]},
                                      idempotency_key="worker:" + str(task["plan_revision"]) + ":" + planned["id"])
                    workers = store.list_jobs(task["id"], states=["queued", "claimed", "unknown", "failed", "succeeded"], limit=1000)
                    executions = [j for j in workers if j["kind"] == "worker"]
                    if executions and all(j["state"] == "succeeded" for j in executions):
                        summary = store.enqueue(task["id"], task["coordinator"]["participant_id"], "message",
                                                {"reason": "execution_summary"}, idempotency_key="execution-summary")
                        if store.get_job(summary["id"])["state"] == "succeeded":
                            store.mark_execution_finished(task["id"])
                # Message jobs are real next turns; they never inject into a live CLI.
                if task["state"] not in {"cancelled", "cancelling", "completed"}:
                    cursor_key = (root, task["id"], "queue")
                    after = self._message_cursors.get(cursor_key, 0)
                    for _ in range(_MESSAGE_BATCHES):
                        page = store.messages(task["id"], after=after, limit=_MESSAGE_PAGE)
                        for event in page["events"]:
                            for delivery in event["delivery"]:
                                if delivery["state"] == "queued" and delivery["job_id"] is None:
                                    store.enqueue(task["id"], delivery["recipient"], "message", {"event_id": event["event_id"]},
                                                  idempotency_key="message:" + event["event_id"] + ":" + delivery["recipient"])
                        after = page["next_cursor"]
                        if len(page["events"]) < _MESSAGE_PAGE:
                            after = 0
                            break
                    self._message_cursors[cursor_key] = after
            except (AutoError, collab.CollaborationError, OSError, ValueError) as exc:
                reason = getattr(exc, "code", "artifact_error")
                self._errors[root] = reason
                try:
                    store.mark_blocked(task["id"], reason)
                except AutoError:
                    pass

    def _finalize(self, store, task):
        # Hold the same mutation transaction as approval/revision through the
        # filesystem commit. A human edit cannot invalidate the accepted snapshot
        # between reading it and freezing its immutable manifest.
        def action(db):
            current = store._task(db, task["id"])
            store._expected(current, task["revision"])
            if current["state"] != "ready" or current["thread_id"]:
                fail("approval_required", "only approved unbound ready task can finalize")
            return self._finalize_locked(store, current, db)
        return store._write("runtime_finalize", None, [task["id"], task["revision"]], action)

    def _finalize_locked(self, store, task, db):
        plan = _validated_plan(task, task["plan"])
        if task["approved_plan_revision"] != task["plan_revision"] or task["approved_plan_hash"] != task["plan_hash"]:
            fail("approval_required", "plan approval changed")
        spec, ticket = plan_paths(task["id"])
        structural = f"Automatic task `{task['id']}`; approved plan revision {task['plan_revision']}, hash `{task['plan_hash']}`."
        _publish_once(store.root, spec, "# 自动协作规格\nStatus: ready-for-agent\n\n## 目标\n" + structural +
                      "\n\n## 已拍定的决策（人类确认，协作方不要重新论证）\n### D1 已批准的本地计划\n" +
                      "Detailed scope and acceptance remain in the authorized local approved plan. Do not export originals.\n\n## 范围之外\nNo commit, push, deployment or implicit acceptance.\n")
        _publish_once(store.root, ticket, "# 01 · 已批准的自动协作计划\n\nType: task\n\nStatus: ready-for-agent\n\nOwner: " +
                      ", ".join(p["participant_id"] for p in plan["participants"]) + "\n\nBlocked by: 无\n\n**What to build:** " + structural +
                      " Follow local approved plan, spec.md D1.\n\n## Acceptance\nIndependent code-bound evidence and spec/quality reviews required; CLI exit is not acceptance.\n")
        thread = task["id"]
        target = _safe(store.root, f".ai-collab/threads/{thread}")
        for name in ("turns", "signals", "state"):
            _safe(store.root, f".ai-collab/threads/{thread}/{name}").mkdir(parents=True, exist_ok=True)
        # Deterministic timestamp makes interrupted finalization safely repeatable.
        manifest = {"protocol_version": 1, "thread_id": thread, "topic": "自动协作任务",
                    "objective": "Ticket " + ticket + ": " + structural,
                    "participants": [p["participant_id"] for p in plan["participants"]],
                    "coordinator": task["coordinator"]["participant_id"],
                    "created_at": datetime.fromtimestamp(task["created_at"], timezone.utc).isoformat(),
                    "automatic_task_id": task["id"], "approved_plan_hash": task["plan_hash"]}
        _publish_once(store.root, f".ai-collab/threads/{thread}/thread.json", json.dumps(manifest, indent=2) + "\n")
        criteria = [{"id": p["id"], "text": "Verify approved local plan task " + p["id"] + "; plan hash " + task["plan_hash"]} for p in plan["tasks"]]
        existing = task_model.load_task(store.root, thread)
        if existing is None:
            task_model.initialize_task(store.root, thread, plan["tasks"][0]["participant_id"], criteria)
        elif existing["criteria"] != criteria:
            fail("artifact_conflict", "coding acceptance criteria differ")
        self._turn(store, task, "approved", task["coordinator"]["participant_id"], "DECISION", "READY")
        task["thread_id"] = thread
        store._event(db, task["id"], "thread_bound", {"thread_id": thread})
        return store._save_task(db, task)

    def _turn(self, store, task, event_id, participant, kind, status):
        thread = task["thread_id"] or task["id"]
        marker = hashlib.sha256(event_id.encode()).hexdigest()
        directory = _safe(store.root, f".ai-collab/threads/{thread}/turns")
        for path in directory.glob("round-*.md"):
            metadata, _ = collab.parse_turn(path)
            if metadata.get("automatic_event") == marker:
                return
        manifest = collab.load_manifest(store.root, thread)
        number = collab.next_round(store.root, thread)
        metadata = {"protocol_version": 1, "thread_id": thread, "topic": manifest["topic"], "round": number,
                    "kind": kind, "author": participant, "role": "coordinator" if participant == manifest["coordinator"] else "implementer",
                    "status": status, "based_on": [], "requested_action": "NONE", "created_at": collab.now_iso(),
                    "automatic_event": marker}
        body = f"{collab.META_START}\n{json.dumps(metadata, indent=2)}\n{collab.META_END}\n\n# 自动运行器事件\n\n## Summary\n" + \
               "Service-observed lifecycle event; not a model quotation or acceptance. Details remain in authorized local task records.\n"
        _publish_once(store.root, f".ai-collab/threads/{thread}/turns/" + collab.expected_turn_filename(metadata), body)

    def _bridge_humans(self, store, task):
        def record(event_id, body, recipients, created_at):
            human_events.record_human_event(store.root, task["thread_id"], {
                "schema_version": 1, "event_id": event_id, "thread_id": task["thread_id"], "actor_type": "human",
                "author": "human", "body": body, "created_at": datetime.fromtimestamp(created_at, timezone.utc).isoformat(),
                "recipients": recipients, "provenance": {"kind": "manual", "original_text": True, "supplied_authorized": True}})
        record("input-" + task["id"], task["instruction"], [task["coordinator"]["participant_id"]], task["created_at"])
        cursor_key = (str(store.root), task["id"], "bridge")
        after = self._message_cursors.get(cursor_key, 0)
        for _ in range(_MESSAGE_BATCHES):
            page = store.messages(task["id"], after=after, limit=_MESSAGE_PAGE)
            for event in page["events"]:
                record(event["event_id"], event["body"], event["recipients"], event["created_at"])
            after = page["next_cursor"]
            if len(page["events"]) < _MESSAGE_PAGE:
                after = 0
                break
        self._message_cursors[cursor_key] = after

    def _prompt(self, store, task, job, messages, workspace=None):
        spec, ticket = plan_paths(task["id"])
        lead = task["coordinator"]["participant_id"]
        if job["kind"] == "planning":
            value = {"action": "propose_plan", "instruction": task["instruction"], "coordinator": task["coordinator"],
                "required_spec": spec, "required_ticket": ticket,
                "response_schema": {"goal": "text", "spec": spec, "ticket": ticket,
                    "participants": [task["coordinator"], {"participant_id": "worker", "provider_id": "claude", "role": "implementer"}],
                    "tasks": [{"id": "work", "participant_id": "worker", "instruction": "scope", "acceptance": ["criterion"], "depends_on": []}],
                    "artifacts": [], "limitations": []},
                "provider_capabilities": self.providers.discover(),
                "rules": "Return ONLY JSON matching response_schema. Coordinator cannot implement or launch collaborators. Human approval is required before any worker."}
        else:
            selected = next((p for p in (task.get("plan") or {}).get("tasks", []) if p["id"] == job["payload"].get("planned_task_id")), None)
            history = store.list_jobs(task["id"], states=["succeeded"], limit=256)
            # Bounded, local handoff, no session resume assumptions across providers.
            results = [{"participant_id": j["participant_id"], "kind": j["kind"], "state": j["state"],
                        "result": (j.get("result") or {}).get("output", "")[:16384]
                                  if j["participant_id"] == job["participant_id"] else None} for j in history[-16:]]
            value = {"action": "implement" if job["kind"] == "worker" else "respond_to_human" if "event_id" in job["payload"] else "summarize_execution",
                "participant_id": job["participant_id"], "coordinator": lead, "approved_task": selected,
                "plan_revision": task["approved_plan_revision"], "shared_artifacts": (task.get("plan") or {}).get("artifacts", []),
                "prior_results": results,
                "rules": "Only approved task scope. No commit/push/deploy. Do not modify .ai-collab or server-owned automatic tickets. Coordinator has no product-write tools. Report limits; never equate exit success with acceptance."}
        if workspace is not None:
            if job["participant_id"] == task["coordinator"]["participant_id"]:
                value["shared_artifact_snapshots"] = _artifact_snapshots(workspace, task["plan"]["artifacts"])
            value["execution_workspace"] = {"kind": workspace["kind"], "application": "pending",
                "snapshot_id": workspace.get("snapshot_id"), "diff_base": workspace.get("diff_base"),
                "rules": "Current cwd is the isolated task checkout captured after approval. Relative artifacts refer to this checkout. Diff is against captured content, not original HEAD; base_commit is informational. Never apply changes to the original project or claim original-checkout acceptance."}
        value["human_messages"] = [{"event_id": m["event_id"], "body": m["body"], "recipients": m["recipients"]} for m in messages]
        prompt = canonical(value)
        if len(prompt.encode()) > 900000:
            fail("input_limit", "local handoff exceeds limit")
        return prompt

    def _cycle(self, store, job):
        root = str(store.root)
        workspace = None
        try:
            task = store.get_task(job["task_id"])
            messages = store.job_messages(job["id"], job["token"])
            phase = "planning" if job["kind"] == "planning" else "coordinator" if job["participant_id"] == task["coordinator"]["participant_id"] else "worker"
            if phase == "worker" and self.workspace_manager is not None:
                if not task["thread_id"] or not task["approved_plan_revision"]:
                    fail("approval_required", "worker checkout requires finalized approval")
                paths, after = [], None
                while True:
                    tasks = store.list_tasks(after=after, limit=200)
                    for known in tasks:
                        if known["thread_id"] and known["approved_plan_revision"]:
                            paths.extend(plan_paths(known["id"]))
                    if len(tasks) < 200:
                        break
                    after = tasks[-1]["id"]
                workspace = self.workspace_manager.ensure(store.root, store.project_id, task["id"], metadata_paths=paths)
            if phase == "coordinator" and self.workspace_manager is not None and task["thread_id"]:
                # Never create a checkout merely for bootstrap/coordinator input.
                existing = self.workspace_manager.describe(store.root, store.project_id, task["id"])
                if existing is not None:
                    workspace = self.workspace_manager.ensure(store.root, store.project_id, task["id"])
            launch_options = {"protected_roots": (store.root, self.workspace_manager.home)} if workspace and isinstance(self.providers, ProviderRegistry) else {}
            spec = self.providers.build_launch(job["binding"]["provider_id"], job["binding"].get("model_id"),
                        phase=phase, prompt=self._prompt(store, task, job, messages, workspace),
                        cwd=Path(workspace["path"]) if workspace else store.root, **launch_options)
            outcome, output, reason = self._run(store, task, job, spec, messages)
            result = {"output": "", "error_code": reason}
            if outcome == "succeeded":
                decoder = getattr(self.providers, "parse_result", parse_result)
                result["output"] = output.decode("utf-8") if spec.output_format == "codex-app-server" else decoder(job["binding"]["provider_id"], output)
                if job["kind"] == "planning":
                    plan = _validated_plan(task, json.loads(result["output"]))
                    current = store.get_task(task["id"])
                    if current["state"] == "planning":
                        store.submit_plan(task["id"], plan, expected_revision=current["revision"])
                    elif current["state"] == "pausing":
                        result["deferred_plan"] = True
                    else:
                        result["error_code"] = "planning_interrupted"
                        outcome = "cancelled" if current["state"] == "cancelling" else "failed"
            if workspace is not None:
                result["execution_workspace"] = workspace
                try:
                    result["application"] = self.workspace_manager.diff(workspace)
                except (AutoError, OSError) as exc:
                    result["application"] = {"status": "unavailable", "automatic": False,
                                             "error_code": getattr(exc, "code", "diff_unavailable")}
            retained = self._attempt(store, job, outcome, result)
            store.finish(job["id"], job["token"], outcome, _compact_result(result, retained))
            if task["thread_id"]:
                self._turn(store, task, job["id"] + ":" + str(job["attempt"]), job["participant_id"],
                           "IMPLEMENTATION", "READY" if outcome == "succeeded" else "NEEDS_INPUT")
            self._errors.pop(root, None)
        except (ProviderError, AutoError, collab.CollaborationError, OSError, ValueError, TypeError) as exc:
            reason = getattr(exc, "code", "invalid_provider_result")
            self._errors[root] = reason
            current = store.get_job(job["id"])
            if current["state"] == "claimed":
                try:
                    result = {"error_code": reason}
                    state = "unknown" if reason == "process_unknown" else "failed"
                    self._attempt(store, job, state, result)
                    store.finish(job["id"], job["token"], state, result)
                except AutoError:
                    store.expire_claims()
        finally:
            with self._lock:
                self._active.pop(root, None)

    def _attempt(self, store, job, outcome, result):
        # Retain prior attempts before retry clears mutable job result/process data.
        current = store.get_job(job["id"])
        retained = {"job_id": job["id"], "task_id": job["task_id"], "attempt": job["attempt"],
                    "participant_id": job["participant_id"], "binding": job["binding"],
                    "outcome": outcome, "result": result,
                    "owner": current.get("owner"), "claim_token": current.get("token"),
                    "binding_revision": current.get("binding_revision"),
                    "plan_revision": current.get("plan_revision"),
                    "pid": current.get("pid"), "process_token": current.get("process_token"),
                    "created_at": current.get("created_at"), "claimed_at": current.get("claimed_at"),
                    "lease_until": current.get("lease_until"), "observed_finished_at": time.time()}
        relative = f".ai-collab/local/automatic/attempts/{job['id']}-{job['attempt']}.json"
        # Provider/manager streams are independently bounded. Full local evidence
        # is not subject to the SQLite row's smaller canonical() one-MiB cap.
        _publish_once(store.root, relative, json.dumps(retained, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        return relative

    @staticmethod
    def _retained_output(store, job):
        if not (job.get("result") or {}).get("output_truncated"):
            return job["result"]["output"]
        relative = f".ai-collab/local/automatic/attempts/{job['id']}-{job['attempt']}.json"
        path = _safe(store.root, relative)
        with path.open("rb") as stream:
            raw = stream.read(32 * 1024 * 1024 + 1)
        if len(raw) > 32 * 1024 * 1024:
            fail("retained_output_limit", "local retained planning evidence exceeds limit")
        value = json.loads(raw)
        if value.get("job_id") != job["id"] or value.get("attempt") != job["attempt"]:
            fail("artifact_conflict", "retained planning evidence differs")
        return value["result"]["output"]

    def _run(self, store, task, job, spec, messages):
        root = str(store.root)
        process = None
        pump = None
        output, sent = bytearray(), False
        broker = None
        pending = 0
        eof = set()
        line_buffer = bytearray()
        outcome, reason = "failed", "process_failed"
        observed = None
        start = time.monotonic()
        last_heartbeat = start
        try:
            if self._stop.is_set() or not self._leases[root].owned():
                return "cancelled", b"", "runtime_stopped"
            process = auto_process.spawn(spec.argv, cwd=spec.cwd, env=spec.env, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            token = runtime_activity.process_token(process.pid)
            store.attach_process(job["id"], job["token"], process.pid, token or "unobservable")
            observed = runtime_activity.observe_process(store.root, task["thread_id"] or task["id"], job["participant_id"], process,
                                                         observer="automatic-runtime")
            pump = _PipePump(process, self.max_output)
            if spec.output_format == "codex-app-server":
                from auto_providers.codex import CodexSession
                metadata = spec.transport or {}
                broker = CodexSession(metadata["phase"], spec.stdin.decode("utf-8"),
                                      spec.cwd, model_id=metadata.get("model_id"))
                for value in broker.start():
                    pump.send((canonical(value) + "\n").encode())
                    pending += 1
            else:
                pump.send(spec.stdin, "prompt", close=True)
            while True:
                now = time.monotonic()
                with self._lock:
                    self._active[root] = {"task_id": task["id"], "job_id": job["id"], "participant_id": job["participant_id"],
                                          "pid": process.pid, "process_token": token, "observed_at": now,
                                          "heartbeat_at": runtime_activity.utc_now(),
                                          "expires_at": datetime.fromtimestamp(time.time()+5, timezone.utc).isoformat()}
                current = store.get_task(task["id"])
                if self._stop.is_set() or root not in self._registered or current["state"] == "cancelling" or not self._leases[root].owned():
                    outcome, reason = "cancelled", "cancel_requested"
                    break
                if now-start > min(self.job_timeout, spec.timeout):
                    outcome, reason = "failed", "process_timeout"
                    break
                if now-last_heartbeat >= 1:
                    store.heartbeat(job["id"], job["token"], lease_seconds=30)
                    if observed:
                        runtime_activity.observe_process_heartbeat(store.root, observed, process)
                    last_heartbeat = now
                try:
                    label, data = pump.events.get(timeout=.05)
                except queue.Empty:
                    label, data = None, None
                if label == "sent":
                    if broker:
                        pending -= 1
                    elif data == "prompt":
                        sent = True
                        store.mark_delivered(job["id"], job["token"], [m["event_id"] for m in messages])
                elif label == "write_failed":
                    outcome, reason = "failed", "input_failed"
                    break
                elif label in {"stdout", "stderr"}:
                    if data is None:
                        eof.add(label)
                        if broker and label == "stdout" and not broker.done:
                            broker.eof()
                    elif broker:
                        line_buffer.extend(data)
                        while b"\n" in line_buffer and not broker.done:
                            line, _, rest = line_buffer.partition(b"\n")
                            line_buffer = bytearray(rest)
                            try:
                                value = json.loads(line)
                            except (ValueError, UnicodeError):
                                raise ProviderError("invalid_protocol_json") from None
                            for response in broker.receive(value):
                                pump.send((canonical(response) + "\n").encode())
                                pending += 1
                            if broker.delivered and not sent:
                                store.mark_delivered(job["id"], job["token"], [m["event_id"] for m in messages])
                                sent = True
                    else:
                        output.extend(data)
                if pump.exceeded.is_set():
                    outcome, reason = "failed", "output_limit"
                    break
                if broker and broker.done and pending == 0:
                    # A denial must be physically flushed before terminating the server.
                    outcome, reason = ("failed", broker.error_code) if broker.error_code else ("succeeded", None)
                    output = bytearray(broker.output.encode("utf-8"))
                    break
                if process.poll() is not None and eof == {"stdout", "stderr"} and (broker or sent):
                    if broker:
                        outcome, reason = "failed", broker.error_code or "incomplete_protocol"
                    else:
                        outcome, reason = ("succeeded", None) if process.returncode == 0 and sent else ("failed", "process_failed")
                    break
            self._terminate_group(process)
            if observed:
                runtime_activity.observe_process_finish(store.root, observed, process,
                    "completed" if outcome == "succeeded" else "cancelled" if outcome == "cancelled" else "failed")
            return outcome, bytes(output), reason
        except BaseException:
            if process is not None:
                self._terminate_group(process)
                if observed:
                    runtime_activity.observe_process_finish(store.root, observed, process, "failed")
            raise
        finally:
            if pump is not None:
                pump.close()
            if process is not None:
                process.close()

    @staticmethod
    def _terminate_group(process):
        # Only this supervisor's owned handle, never a stored PID.
        try:
            process.terminate_tree(timeout=5)
        except auto_process.ProcessError:
            raise ProviderError("process_unknown") from None

    @staticmethod
    def _group_alive(group):
        return auto_process._posix_group_alive(group)
