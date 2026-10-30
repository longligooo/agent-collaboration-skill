"""Short-lived observations of processes started by the existing watcher.

This is telemetry, not a task phase or an execution queue. There is deliberately
no CLI, HTTP mutation, command discovery, or background runner in this module.
"""
from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import collab
import task_discovery

SCHEMA_VERSION = 1
TERMINAL_STATUSES = {"completed", "failed", "timed-out", "cancelled"}
HEARTBEAT_INTERVAL = 1.0
HEARTBEAT_TTL = 5.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise collab.CollaborationError("runtime timestamp must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise collab.CollaborationError("invalid runtime timestamp") from exc
    if parsed.tzinfo is None:
        raise collab.CollaborationError("runtime timestamps require a timezone")
    return parsed.astimezone(timezone.utc)


def machine_token() -> str:
    """Hash local-machine/boot identity; do not expose hostnames or machine IDs."""
    parts = [platform.system(), platform.node()]
    # uuid.getnode() may fall back to a different random multicast address in
    # every process. Never let that fallback masquerade as machine identity.
    node = uuid.getnode()
    if not (node & (1 << 40)):
        parts.append(str(node))
    if platform.system() == "Linux":
        for name in ("/etc/machine-id", "/proc/sys/kernel/random/boot_id"):
            try:
                parts.append(Path(name).read_text(encoding="ascii").strip())
            except (OSError, UnicodeError):
                pass
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _linux_process(pid: int) -> Optional[tuple]:
    if platform.system() != "Linux":
        return None
    try:
        # comm may contain spaces and parentheses. The final ')' precedes state.
        value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = value[value.rfind(")") + 2:].split()
        return fields[0], fields[19]
    except (OSError, UnicodeError, IndexError):
        return None


def process_token(pid: int) -> Optional[str]:
    details = _linux_process(pid)
    return "linux-start:" + details[1] if details else None


def process_alive(pid: Any) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    if os.name == "nt":
        # os.kill on Windows is not a portable non-mutating liveness check.
        try:
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.GetExitCodeProcess.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                exit_code = wintypes.DWORD()
                return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259
            finally:
                kernel.CloseHandle(handle)
        except (OSError, AttributeError):
            return False
    details = _linux_process(pid)
    if details and details[0] in {"Z", "X"}:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        # No permission / no observable liveness is unknown, never active.
        return False


def _runs_directory(root: Path) -> Path:
    return collab.resolve_repo_path(root, ".ai-collab/runtime/runs", label="runtime directory")


def _write(root: Path, record: Dict[str, Any]) -> None:
    directory = _runs_directory(root)
    path = collab.resolve_repo_path(root, ".ai-collab/runtime/runs/" + record["run_id"] + ".json", label="runtime record")
    if path.parent != directory:
        raise collab.CollaborationError("runtime record is outside its directory")
    collab.atomic_write_json(path, record)


def _observe_write(root: Path, record: Dict[str, Any]) -> bool:
    """Telemetry is best-effort; storage failure must not cancel authorized work.

    A previous lease will expire naturally, and liveness checks stop displaying
    it as soon as the process exits. No replacement observation means inactive.
    """
    try:
        _write(root, record)
        return True
    except (OSError, collab.CollaborationError):
        return False


def _heartbeat(record: Dict[str, Any], ttl: float) -> None:
    now = datetime.now(timezone.utc)
    record["heartbeat_at"] = now.isoformat(timespec="milliseconds")
    record["expires_at"] = (now + timedelta(seconds=ttl)).isoformat(timespec="milliseconds")


def _finish(root: Path, record: Dict[str, Any], status: str, exit_code: Optional[int], error: Optional[str] = None) -> None:
    now = utc_now()
    record.update(status=status, exit_code=exit_code, error=error, finished_at=now, heartbeat_at=now, expires_at=now)
    _observe_write(root, record)


def _kill_wait(process: subprocess.Popen) -> None:
    try:
        if process.poll() is None:
            process.kill()
    except ProcessLookupError:
        pass  # Child exited between poll and kill; still reap it below.
    process.wait()


def run_observed(command: list, *, root: Path, thread_id: str, agent: str,
                 message_id: str, log: str, cwd: Path, env: dict,
                 stdin: Any, stdout: Any, stderr: Any, timeout: Optional[float],
                 heartbeat_interval: float = HEARTBEAT_INTERVAL,
                 ttl: float = HEARTBEAT_TTL) -> subprocess.CompletedProcess:
    """Observe the watcher's already-authorized argv invocation, preserving exit semantics.

    No running record exists until Popen successfully starts a real process. A
    crash leaves only an expiring record; readers also require both PIDs alive.
    """
    root = collab.repository_root(str(root))
    collab.validate_id(thread_id, "thread_id")
    collab.validate_id(agent, "agent")
    collab.validate_uuid(message_id)
    collab.resolve_repo_path(root, log, label="runtime log")
    _runs_directory(root)  # Reject escaped symlinks before starting anything.
    if heartbeat_interval <= 0 or ttl <= heartbeat_interval:
        raise collab.CollaborationError("runtime ttl must exceed a positive heartbeat interval")
    bindings = task_discovery.load_identities(root)
    record = {"schema_version": 1, "run_id": str(uuid.uuid4()), "thread_id": thread_id,
              "agent": agent, "identity": task_discovery.identity_for(agent, bindings),
              "message_id": message_id, "observer": "watcher", "machine_token": machine_token(),
              "runner_pid": os.getpid(), "runner_start_token": process_token(os.getpid()),
              "process_pid": None, "process_start_token": None,
              "started_at": None, "heartbeat_at": None, "expires_at": None,
              "finished_at": None, "status": "failed", "exit_code": None, "error": None, "log": log}
    process = None
    started = time.monotonic()
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=stdin, stdout=stdout, stderr=stderr)
        record.update(process_pid=process.pid, process_start_token=process_token(process.pid), started_at=utc_now())
        # Fast commands may exit before the first observation. Do not label those
        # running just because Popen was created successfully.
        while process.poll() is None:
            if timeout is not None and time.monotonic() - started >= timeout:
                raise subprocess.TimeoutExpired(command, timeout)
            record["status"] = "running"
            _heartbeat(record, ttl)
            _observe_write(root, record)
            remaining = None if timeout is None else max(0.0, timeout - (time.monotonic() - started))
            delay = heartbeat_interval if remaining is None else min(heartbeat_interval, remaining)
            try:
                process.wait(timeout=delay)
            except subprocess.TimeoutExpired:
                pass
        code = process.wait()
        _finish(root, record, "completed" if code == 0 else "failed", code)
        return subprocess.CompletedProcess(command, code)
    except subprocess.TimeoutExpired as exc:
        if process is not None:
            _kill_wait(process)
        try:
            _finish(root, record, "timed-out", process.returncode if process else None, f"timed out after {timeout}s")
        except (OSError, collab.CollaborationError):
            pass  # Preserve TimeoutExpired; dead child + expiry remain fail-closed.
        raise exc
    except BaseException as exc:
        if process is not None:
            _kill_wait(process)
        status = "cancelled" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed"
        try:
            _finish(root, record, status, process.returncode if process else None, str(exc) or type(exc).__name__)
        except (OSError, collab.CollaborationError):
            # Preserve the original failure; any previously written heartbeat
            # expires, and the child has already been killed/reaped.
            pass
        raise


def _validate(record: dict, filename: str) -> None:
    if type(record.get("schema_version")) is not int or record["schema_version"] != 1 or record.get("observer") != "watcher":
        raise collab.CollaborationError("unsupported runtime observation")
    identifier = collab.validate_uuid(record.get("run_id"), "run_id")
    if filename != identifier + ".json":
        raise collab.CollaborationError("runtime run_id does not match filename")
    for field in ("thread_id", "agent", "identity"):
        value = record.get(field)
        if not isinstance(value, str):
            raise collab.CollaborationError(f"runtime {field} must be a string")
        collab.validate_id(value, field)
    collab.validate_uuid(record.get("message_id"))
    if not isinstance(record.get("status"), str) or record["status"] not in TERMINAL_STATUSES | {"running"}:
        raise collab.CollaborationError("unsupported runtime status")
    if not isinstance(record.get("machine_token"), str):
        raise collab.CollaborationError("runtime machine_token is required")
    if type(record.get("runner_pid")) is not int or record["runner_pid"] <= 0:
        raise collab.CollaborationError("runtime runner_pid must be positive")
    if record.get("process_pid") is not None and (type(record["process_pid"]) is not int or record["process_pid"] <= 0):
        raise collab.CollaborationError("runtime process_pid must be positive or null")
    if record.get("status") == "running":
        if record.get("process_pid") is None:
            raise collab.CollaborationError("running observation requires process_pid")
        if record.get("started_at") is None:
            raise collab.CollaborationError("running observation requires started_at")
    for field in ("started_at", "finished_at"):
        if record.get(field) is not None:
            record[field] = _timestamp(record[field]).isoformat(timespec="milliseconds")
    for field in ("heartbeat_at", "expires_at"):
        record[field] = _timestamp(record.get(field)).isoformat(timespec="milliseconds")
    for field in ("runner_start_token", "process_start_token", "error"):
        if record.get(field) is not None and not isinstance(record[field], str):
            raise collab.CollaborationError(f"runtime {field} must be a string or null")
    if record.get("exit_code") is not None and type(record["exit_code"]) is not int:
        raise collab.CollaborationError("runtime exit_code must be an integer or null")
    if not isinstance(record.get("log"), str) or not record["log"]:
        raise collab.CollaborationError("runtime log must be a non-empty path string")


def _inactive_reason(record: dict, host: str, now: datetime) -> Optional[str]:
    if record["status"] != "running":
        return record["status"]
    if record["machine_token"] != host:
        return "foreign-host"
    if _timestamp(record["expires_at"]) <= now:
        return "expired"
    heartbeat = _timestamp(record["heartbeat_at"])
    if heartbeat > now + timedelta(seconds=1) or _timestamp(record["started_at"]) > heartbeat:
        return "invalid-clock"
    # Even hand-edited or old-schema leases cannot claim activity indefinitely.
    if now - heartbeat > timedelta(seconds=HEARTBEAT_TTL):
        return "expired"
    if record.get("finished_at"):
        return "finished"
    for role in ("runner", "process"):
        pid = record[role + "_pid"]
        if not process_alive(pid):
            return role + "-exited"
        expected = record.get(role + "_start_token")
        actual = process_token(pid)
        if actual is not None and expected is None:
            return role + "-unverified"
        if expected is not None and actual != expected:
            return role + "-identity-changed"
    return None


def activity_snapshot(roots: Iterable[Path]) -> Dict[str, Any]:
    """Read explicit project roots only. Missing, stale or unverifiable is inactive."""
    now = datetime.now(timezone.utc)
    host = machine_token()
    runs = []
    errors = []
    seen = set()
    for raw in roots:
        root = Path(raw).expanduser().resolve()
        if str(root) in seen:
            continue
        seen.add(str(root))
        if not root.is_dir():
            errors.append({"project_root": str(root), "error": "project is unavailable"})
            continue
        try:
            directory = _runs_directory(root)
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.json")):
                try:
                    resolved = collab.resolve_repo_path(root, str(path.relative_to(root)), must_exist=True, label="runtime record")
                    if resolved.parent != directory:
                        raise collab.CollaborationError("runtime record escapes its directory")
                    record = collab.read_json(resolved, "runtime record")
                    _validate(record, path.name)
                    collab.resolve_repo_path(root, record.get("log", ""), label="runtime log")
                    reason = _inactive_reason(record, host, now)
                    runs.append({**record, "project_root": str(root), "project_name": root.name,
                                 "project_id": hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12],
                                 "active": reason is None, "inactive_reason": reason})
                except (collab.CollaborationError, OSError, ValueError, TypeError, KeyError) as exc:
                    errors.append({"project_root": str(root), "record": path.name, "error": str(exc)})
        except (collab.CollaborationError, OSError) as exc:
            errors.append({"project_root": str(root), "error": str(exc)})
    runs.sort(key=lambda item: (item.get("started_at") or item["heartbeat_at"], item["run_id"]), reverse=True)
    identities = {}
    for run in runs:
        item = identities.setdefault(run["identity"], {"identity": run["identity"], "active": False, "runs": [], "expires_at": None})
        item["runs"].append(run["run_id"])
        if run["active"]:
            item["active"] = True
            if item["expires_at"] is None or run["expires_at"] > item["expires_at"]:
                item["expires_at"] = run["expires_at"]
    return {"schema_version": 1, "generated_at": now.isoformat(timespec="milliseconds"),
            "runs": runs, "identities": sorted(identities.values(), key=lambda item: item["identity"]), "errors": errors}
