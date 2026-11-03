"""Local read-only web UI for collaboration across projects on this machine."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen
from urllib.parse import parse_qs

import collab
import ui_read_model
import read_budget
import snapshot_jobs

UI_PORT = 23451
UI_DIR = Path(__file__).resolve().parents[1] / "ui"
SUMMARY_PATTERN = re.compile(r"^## Summary\s*\n+(.+?)(?:\n## |\Z)", re.M | re.S)
TITLE_PATTERN = re.compile(r"^# (.+)$", re.M)
ATTENTION = {"NEW", "TERMINAL", "INVALID"}
REGISTRY_LOCK = threading.Lock()


def ui_home() -> Path:
    override = os.environ.get("COLLAB_UI_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".agent-collaboration"


def registry_path() -> Path:
    return ui_home() / "registry.json"


def project_id(root: str) -> str:
    return hashlib.sha256(root.encode("utf-8")).hexdigest()[:12]


def load_registry(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"projects": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    projects = data.get("projects")
    if not isinstance(projects, list):
        return {"projects": []}
    return {"projects": [item for item in projects if isinstance(item, dict) and isinstance(item.get("root"), str)]}


def save_registry(path: Path, data: Dict[str, Any]) -> None:
    collab.atomic_write_json(path, data)


def turn_title(body: str) -> str:
    match = TITLE_PATTERN.search(body)
    return match.group(1).strip() if match else ""


def turn_summary(body: str) -> str:
    match = SUMMARY_PATTERN.search(body)
    if not match:
        return ""
    text = re.sub(r"\s+", " ", match.group(1)).strip()
    return text[:280]


def tool_bindings(root: Path) -> Dict[str, str]:
    try:
        path = collab.resolve_repo_path(root, ".ai-collab/wake.json", label="wake config")
    except collab.CollaborationError:
        return {}
    if not path.is_file():
        return {}
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    agents = config.get("agents")
    if not isinstance(agents, dict):
        return {}
    bindings: Dict[str, str] = {}
    for name, spec in agents.items():
        if isinstance(name, str) and isinstance(spec, dict) and isinstance(spec.get("tool"), str):
            bindings[name] = spec["tool"]
    return bindings


def scan_thread(root: Path, thread_id: str, project: Dict[str, Any]) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "project_id": project["id"],
        "project_name": project["name"],
        "project_root": project["root"],
        "thread_id": thread_id,
        "topic": thread_id,
        "objective": "",
        "coordinator": None,
        "participants": [],
        "status": "NONE",
        "terminal": False,
        "round": 0,
        "started_at": "",
        "updated_at": "",
        "last_activity_at": "",
        "turns": [],
        "signals": [],
        "error": None,
    }
    try:
        collab.resolve_repo_path(root, f".ai-collab/threads/{thread_id}", label="thread directory")
        manifest = collab.load_manifest(root, thread_id)
    except collab.CollaborationError as exc:
        record["error"] = str(exc)
        return record
    record["topic"] = manifest["topic"]
    record["objective"] = manifest["objective"]
    record["coordinator"] = collab.designated_coordinator(manifest)
    record["participants"] = list(manifest["participants"])
    record["started_at"] = manifest.get("created_at") or ""
    turns_dir = collab.thread_directory(root, thread_id) / "turns"
    turns: List[Dict[str, Any]] = []
    if turns_dir.is_dir():
        for path in sorted(turns_dir.glob("round-*.md")):
            read_budget.check()
            try:
                safe_path = collab.resolve_repo_path(root, collab.relative_path(root, path), must_exist=True, label="turn document")
                metadata, body = collab.parse_turn(safe_path)
            except collab.CollaborationError as exc:
                turns.append({"document": path.relative_to(root).as_posix(), "error": str(exc)})
                continue
            turns.append(
                {
                    "round": metadata.get("round"),
                    "author": metadata.get("author"),
                    "role": metadata.get("role"),
                    "kind": metadata.get("kind"),
                    "status": metadata.get("status"),
                    "requested_action": metadata.get("requested_action"),
                    "created_at": metadata.get("created_at"),
                    "title": turn_title(body),
                    "summary": turn_summary(body),
                    "document": collab.relative_path(root, path),
                }
            )
    turns.sort(key=lambda item: (item.get("round") is None, item.get("round") or 0))
    record["turns"] = turns
    if turns and turns[-1].get("round") is not None:
        latest = turns[-1]
        record["round"] = latest.get("round") or 0
        record["status"] = latest.get("status") or "NONE"
        record["terminal"] = record["status"] in collab.TERMINAL_STATUSES
        record["updated_at"] = latest.get("created_at") or ""
    for participant in record["participants"]:
        inbox = collab.inspect_inbox(root, participant, thread_id)
        for result in inbox["results"]:
            message = result.get("message") or {}
            record["signals"].append(
                {
                    "to": participant,
                    "from": message.get("from"),
                    "decision": result["decision"],
                    "message_id": message.get("message_id"),
                    "requested_action": message.get("requested_action"),
                    "status": message.get("status"),
                    "round": message.get("round"),
                    "document": message.get("document"),
                    "created_at": message.get("created_at"),
                    "error": result.get("error"),
                }
            )
    record["last_activity_at"] = ui_read_model.last_activity_at(record)
    try:
        record = ui_read_model.thread_projection(root, thread_id, record)
    except (collab.CollaborationError, OSError, ValueError, TypeError, KeyError) as exc:
        record["projection_error"] = str(exc)
        record["participant_details"] = []
        record["coding_task"] = None
    return record


def project_shell(entry: Dict[str, Any]) -> Dict[str, Any]:
    root = Path(entry["root"])
    project = {
        "id": project_id(str(root)),
        "name": root.name,
        "root": str(root),
        "registered_at": entry.get("registered_at"),
        "available": root.is_dir(),
        "threads": [],
        "tools": {},
    }
    if not root.is_dir():
        return project
    project["tools"] = tool_bindings(root)
    try:
        import task_discovery
        project["identity_bindings"] = task_discovery.load_identities(root)
    except collab.CollaborationError as exc:
        project["identity_error"] = str(exc)
        project["identity_bindings"] = None
    return project


def scan_project(entry: Dict[str, Any]) -> Dict[str, Any]:
    project = project_shell(entry)
    if not project["available"]:
        return project
    root = Path(project["root"])
    threads = []
    try:
        thread_ids = collab.list_thread_ids(root)
    except collab.CollaborationError as exc:
        project["error"] = str(exc)
        return project
    for thread_id in thread_ids:
        threads.append(scan_thread(root, thread_id, project))
    threads.sort(key=ui_read_model.thread_sort_key)
    project["threads"] = threads
    return project


def collaborator_records(projects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group by stable identity, preserving historical human identity snapshots."""
    people: Dict[str, Dict[str, Any]] = {}
    for project in projects:
        if not project["available"] or project.get("identity_error"):
            continue
        bindings = project.get("identity_bindings") or {}
        for thread in project["threads"]:
            actors = {(detail["agent"], detail["identity"]): detail["actor_type"]
                      for detail in thread.get("participant_details", [])}
            involved = set(thread["participants"])
            involved.update(turn["author"] for turn in thread["turns"] if isinstance(turn.get("author"), str))
            for signal in thread["signals"]:
                involved.update(signal[field] for field in ("from", "to") if isinstance(signal.get(field), str))
            for agent in involved:
                actors.setdefault((agent, bindings.get(agent, agent)), "agent")
            for (agent, identity), actor_type in sorted(actors.items()):
                record = people.setdefault(identity, {"id": identity, "identity": identity,
                    "aliases": [], "actor_type": actor_type, "pending": 0, "turns": 0,
                    "last_at": "", "projects": [], "threads": [], "tools": []})
                alias = {"project_id": project["id"], "agent": agent}
                if alias not in record["aliases"]:
                    record["aliases"].append(alias)
                if actor_type == "human":
                    record["actor_type"] = "human"
                if not any(item["id"] == project["id"] for item in record["projects"]):
                    record["projects"].append({"id": project["id"], "name": project["name"], "root": project["root"]})
                current_actor = identity == bindings.get(agent, agent)
                tool = project["tools"].get(agent) if current_actor else None
                if tool and tool not in record["tools"]:
                    record["tools"].append(tool)
                pending = [signal for signal in thread["signals"] if current_actor
                           and signal.get("to") == agent and signal.get("decision") in ATTENTION]
                record["pending"] += len(pending)
                authored = [turn for turn in thread["turns"] if current_actor
                            and turn.get("author") == agent and not turn.get("error")]
                record["turns"] += len(authored)
                for turn in authored:
                    record["last_at"] = max(record["last_at"], turn.get("created_at") or "")
                existing = next((item for item in record["threads"] if item["project_id"] == project["id"]
                                 and item["thread_id"] == thread["thread_id"]), None)
                if existing:
                    existing["pending"] += len(pending)
                    existing["participant"] = existing["participant"] or (current_actor and agent in thread["participants"])
                else:
                    record["threads"].append({"project_id": project["id"], "project_name": project["name"],
                        "thread_id": thread["thread_id"], "topic": thread["topic"],
                        "coordinator": thread["coordinator"], "coordinator_identity": thread.get("coordinator_identity"),
                        "participants": list(thread["participants"]),
                        "participant": current_actor and agent in thread["participants"],
                        "pending": len(pending), "status": thread["status"],
                        "started_at": thread.get("started_at") or "", "terminal": bool(thread.get("terminal")),
                        "updated_at": thread["updated_at"], "last_activity_at": thread.get("last_activity_at", "")})
    for person in people.values():
        person["threads"].sort(key=ui_read_model.thread_sort_key)
    return sorted(people.values(), key=lambda item: (-item["pending"], item["id"]))


def thread_detail(project_identifier: str, thread_id: str, path: Optional[Path] = None) -> Dict[str, Any]:
    collab.validate_id(thread_id, "thread_id")
    registry = load_registry(path or registry_path())
    matches = [entry for entry in registry["projects"] if project_id(str(Path(entry["root"]))) == project_identifier]
    if len(matches) != 1:
        raise collab.CollaborationError("unknown or ambiguous registered project")
    root = collab.repository_root(matches[0]["root"])
    project = {"id": project_identifier, "name": root.name, "root": str(root)}
    record = scan_thread(root, thread_id, project)
    if record.get("error") or record.get("projection_error"):
        raise collab.CollaborationError(record.get("error") or record["projection_error"])
    import ui_read_model
    return {"project": project, "thread": ui_read_model.thread_projection(root, thread_id, record, include_history=True)}


def snapshot(path: Optional[Path] = None) -> Dict[str, Any]:
    registry = load_registry(path or registry_path())
    return snapshot_from_projects([scan_project(entry) for entry in registry["projects"]])


def snapshot_from_projects(projects: List[Dict[str, Any]]) -> Dict[str, Any]:
    projects.sort(key=lambda item: item["name"])
    threads = [thread for project in projects for thread in project["threads"]]
    people = collaborator_records(projects)
    attention = []
    recent = []
    for project in projects:
        for thread in project["threads"]:
            for signal in thread["signals"]:
                if signal.get("decision") in ATTENTION:
                    attention.append(
                        {
                            "project_id": project["id"],
                            "project_name": project["name"],
                            "thread_id": thread["thread_id"],
                            "topic": thread["topic"],
                            **signal,
                        }
                    )
            for turn in thread["turns"]:
                if turn.get("error"):
                    continue
                recent.append(
                    {
                        "project_id": project["id"],
                        "project_name": project["name"],
                        "thread_id": thread["thread_id"],
                        "topic": thread["topic"],
                        **turn,
                    }
                )
    attention.sort(key=lambda item: item.get("created_at") or "")
    recent.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return {
        "generated_at": collab.now_iso(),
        "projects": projects,
        "collaborators": people,
        "overview": {
            "projects": len(projects),
            "threads": len(threads),
            "collaborators": len(people),
            "pending": len(attention),
            "attention": attention,
            "recent": recent[:12],
        },
    }


# Active sessions retain one delta replay and a copy-on-write record index.
# Completed versions reuse unchanged projections after metadata-only checks.
# No scan/version lock covers activity or detail requests.
SNAPSHOT_BATCH_SIZE = 8
SNAPSHOT_BATCH_SECONDS = 0.1
SNAPSHOT_SESSION_TTL = 120
SNAPSHOT_MAX_SESSIONS = 4
SNAPSHOT_WORKERS = threading.BoundedSemaphore(2)
SNAPSHOT_SESSIONS_LOCK = threading.Lock()
SNAPSHOT_SESSIONS: Dict[str, Dict[str, Any]] = {}
SNAPSHOT_VERSIONS: Dict[str, Dict[str, Any]] = {}
SNAPSHOT_VERSION_TTL = 900
SNAPSHOT_MAX_VERSIONS = 4
SNAPSHOT_MAX_VERSION_BYTES = 32 * 1024 * 1024
SNAPSHOT_MAX_CACHE_BYTES = 64 * 1024 * 1024


class SnapshotRequestError(ValueError):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def metadata_fingerprint(root: Path, relative: str, *, exclude=()) -> str:
    """Hash metadata only, including in-repository symlink targets.

    Resolve links component by component: reject an external target before any
    target access, bound link chains/cycles, and never read source/private bodies.
    ctime/inode detect restored-mtime rewrites and atomic replacements.
    """
    read_budget.check()
    root = root.resolve()
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise collab.CollaborationError("invalid fingerprint path")
    entries, visited = [], set()

    def local_target(path: Path):
        parts = path.relative_to(root).parts
        links = set()
        while True:
            cursor = root
            restarted = False
            for index, component in enumerate(parts):
                read_budget.check()
                cursor = cursor / component
                try:
                    info = cursor.lstat()
                except FileNotFoundError:
                    return root.joinpath(*parts)
                if not stat.S_ISLNK(info.st_mode):
                    continue
                name = cursor.relative_to(root).as_posix()
                entries.append((name, "link", info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino))
                if name in links or len(links) >= 40:
                    entries.append((name, "cycle"))
                    return None
                links.add(name)
                target = Path(os.path.normpath(str(cursor.parent / os.readlink(cursor))))
                try:
                    target_parts = target.relative_to(root).parts
                except ValueError:
                    entries.append((name, "external"))
                    return None
                parts = target_parts + parts[index + 1:]
                restarted = True
                break
            if not restarted:
                return cursor

    def visit(path: Path):
        read_budget.check()
        target = local_target(path)
        if target is None or target in visited:
            return
        visited.add(target)
        try:
            info = target.lstat()
        except FileNotFoundError:
            entries.append((target.relative_to(root).as_posix(), "missing"))
            return
        entries.append((target.relative_to(root).as_posix(), info.st_mode, info.st_size,
                        info.st_mtime_ns, info.st_ctime_ns, info.st_ino))
        if stat.S_ISDIR(info.st_mode):
            for child in sorted(target.iterdir()):
                if child.name not in exclude:
                    visit(child)

    visit(root / relative)
    return hashlib.sha256(repr(entries).encode()).hexdigest()


def project_config_fingerprint(root: Path) -> str:
    return hashlib.sha256(repr((root.is_dir(), metadata_fingerprint(root, ".ai-collab/identities.json"),
                               metadata_fingerprint(root, ".ai-collab/wake.json"))).encode()).hexdigest()


def project_code_fingerprint(root: Path) -> str:
    """Metadata equivalent of task_model.code_version's selected source set.

    Git membership matters even without content changes (e.g. force-adding an
    ignored file). Read-only ls-files/rev-parse also handle linked worktrees.
    """
    listing = read_budget.run(["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if listing.returncode:
        if (root / ".git").exists():
            raise collab.CollaborationError("Git could not enumerate source metadata")
        return metadata_fingerprint(root, ".", exclude={".git", ".ai-collab", "__pycache__"})
    head = read_budget.run(["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    values = [head.stdout.decode("ascii", errors="replace").strip()]
    for name in sorted({os.fsdecode(value) for value in listing.stdout.split(b"\0") if value}):
        read_budget.check()
        if name.split("/", 1)[0] in {".git", ".ai-collab"}:
            continue
        target, linked = root, False
        for component in Path(name).parts:
            if component in {"..", "/"}:
                raise collab.CollaborationError("invalid source metadata path")
            target = target / component
            if target.is_symlink():
                linked = True
                break
        if not linked and target.is_dir() and (target / ".git").exists():
            value = project_code_fingerprint(target)
        else:
            value = metadata_fingerprint(root, name)
        values.append((name, value))
    return hashlib.sha256(repr(values).encode()).hexdigest()


def thread_fingerprint(root: Path, thread_id: str, project: dict, code: Optional[str], previous: Optional[dict] = None) -> str:
    collab.validate_id(thread_id, "thread_id")
    values = [metadata_fingerprint(root, f".ai-collab/threads/{thread_id}"),
              metadata_fingerprint(root, f".ai-collab/local/human-events/{thread_id}"),
              metadata_fingerprint(root, ".ai-collab/identities.json"),
              metadata_fingerprint(root, ".ai-collab/wake.json"), code,
              {key: value for key, value in project.items() if key != "threads"}]
    # Evidence may live outside the thread, including ignored .ai-collab paths.
    for evidence in (previous or {}).get("coding_task", {}).get("evidence", []) if (previous or {}).get("coding_task") else []:
        if isinstance(evidence.get("log"), str):
            checked = collab.resolve_repo_path(root, evidence["log"], label="evidence fingerprint")
            values.append(metadata_fingerprint(root, checked.relative_to(root).as_posix()))
    return hashlib.sha256(repr(values).encode()).hexdigest()


def _version_summary(session: dict) -> dict:
    projects = [{**project, "threads": []} for project in session["projects"]]
    by_id = {project["id"]: project for project in projects}
    for (project_id_value, _), record in session["records"].items():
        by_id[project_id_value]["threads"].append(record)
    for project in projects:
        project["threads"].sort(key=ui_read_model.thread_sort_key)
    result = snapshot_from_projects(projects)
    return {"collaborators": result["collaborators"], "overview": result["overview"]}


def progressive_snapshot(cursor: Optional[str] = None, path: Optional[Path] = None, since: Optional[str] = None) -> Dict[str, Any]:
    if cursor is not None and not re.fullmatch(r"[0-9a-f]{32}:[0-9]{1,12}", cursor):
        raise SnapshotRequestError("invalid snapshot cursor", 400)
    if since is not None and (cursor is not None or not re.fullmatch(r"[0-9a-f]{32}", since)):
        raise SnapshotRequestError("invalid snapshot version", 400)
    if not SNAPSHOT_WORKERS.acquire(blocking=False):
        raise SnapshotRequestError("snapshot readers busy; retry shortly", 503)
    session = None
    locked = False
    try:
        now = time.monotonic()
        batch_projects = {}
        if cursor is None:
            registry_file = path or registry_path()
            origin = str(registry_file.resolve())
            with SNAPSHOT_SESSIONS_LOCK:
                for key in list(SNAPSHOT_VERSIONS):
                    if now - SNAPSHOT_VERSIONS[key]["touched"] > SNAPSHOT_VERSION_TTL:
                        del SNAPSHOT_VERSIONS[key]
                baseline = SNAPSHOT_VERSIONS.get(since)
                if baseline and baseline["origin"] != origin:
                    baseline = None
                if baseline:
                    baseline["touched"] = now
            registry = load_registry(registry_file)
            projects, configs = [], {}
            for entry in registry["projects"]:
                read_budget.check()
                root = Path(entry["root"])
                before_config = project_config_fingerprint(root)
                project = project_shell(entry)
                if project_config_fingerprint(root) != before_config:
                    raise SnapshotRequestError("project configuration changed while reading; refresh again", 409)
                projects.append(project)
                configs[project["id"]] = before_config
            queues, fingerprints, records, codes = [], {}, {}, {}
            old_records = baseline["records"] if baseline else {}
            for project in projects:
                root = Path(project["root"])
                if project.get("error") or project.get("identity_error"):
                    raise SnapshotRequestError("project metadata could not be read; refresh again", 409)
                if not project["available"] and any(key[0] == project["id"] for key in old_records):
                    raise SnapshotRequestError("registered project is unavailable; retaining previous version", 409)
                ids = collab.list_thread_ids(root) if project["available"] else []
                has_tasks = any((root / f".ai-collab/threads/{tid}/task.json").exists() for tid in ids)
                code = project_code_fingerprint(root) if has_tasks else None
                codes[project["id"]] = code
                work = []
                for thread_id in ids:
                    key = (project["id"], thread_id)
                    fingerprint = thread_fingerprint(root, thread_id, project, code, old_records.get(key))
                    fingerprints[key] = fingerprint
                    if baseline and baseline["fingerprints"].get(key) == fingerprint:
                        records[key] = old_records[key]
                    else:
                        work.append((project, thread_id))
                queues.append(work)
            work = [queue[index] for index in range(max(map(len, queues), default=0))
                    for queue in queues if index < len(queue)]
            token = secrets.token_hex(16)
            session = {"projects": projects, "work": work, "offset": 0,
                       "touched": now, "lock": threading.Lock(), "last_cursor": None,
                       "last_payload": None, "token": token, "origin": origin,
                       "records": records, "fingerprints": fingerprints, "codes": codes, "configs": configs,
                       "record_bytes": {key: baseline["record_bytes"][key] for key in records} if baseline else {},
                       "base_version": since if baseline else None, "reset": baseline is None,
                       "removed": [{"project_id": key[0], "thread_id": key[1]} for key in old_records if key not in fingerprints]}
            session["lock"].acquire()
            locked = True
            with SNAPSHOT_SESSIONS_LOCK:
                candidates = sorted(SNAPSHOT_SESSIONS, key=lambda key: (
                    SNAPSHOT_SESSIONS[key]["offset"] < len(SNAPSHOT_SESSIONS[key]["work"]),
                    SNAPSHOT_SESSIONS[key]["touched"]))
                for key in candidates:
                    old = SNAPSHOT_SESSIONS[key]
                    if not old["lock"].locked() and (now - old["touched"] > SNAPSHOT_SESSION_TTL or len(SNAPSHOT_SESSIONS) >= SNAPSHOT_MAX_SESSIONS):
                        del SNAPSHOT_SESSIONS[key]
                if len(SNAPSHOT_SESSIONS) >= SNAPSHOT_MAX_SESSIONS:
                    raise SnapshotRequestError("snapshot sessions busy; retry shortly", 503)
                SNAPSHOT_SESSIONS[token] = session
        else:
            token, _ = cursor.split(":")
            with SNAPSHOT_SESSIONS_LOCK:
                session = SNAPSHOT_SESSIONS.get(token)
                if session is None or now - session["touched"] > SNAPSHOT_SESSION_TTL:
                    if session is not None and not session["lock"].locked():
                        del SNAPSHOT_SESSIONS[token]
                    raise SnapshotRequestError("snapshot cursor expired; start a new refresh", 410)
                locked = session["lock"].acquire(blocking=False)
            if not locked:
                raise SnapshotRequestError("snapshot page already being read; retry shortly", 503)
            if session.get("failed"):
                raise SnapshotRequestError("snapshot changed while reading; start a new refresh", 409)
            session["touched"] = now
            if cursor == session["last_cursor"]:
                return copy.deepcopy(session["last_payload"])
            if cursor != f'{token}:{session["offset"]}' or session["offset"] >= len(session["work"]):
                raise SnapshotRequestError("snapshot cursor is stale; start a new refresh", 410)
            start = time.monotonic()
            stop = min(len(session["work"]), session["offset"] + SNAPSHOT_BATCH_SIZE)
            while session["offset"] < stop:
                read_budget.check()
                project, thread_id = session["work"][session["offset"]]
                root, key = Path(project["root"]), (project["id"], thread_id)
                if project_config_fingerprint(root) != session["configs"][project["id"]]:
                    session["failed"] = True
                    raise SnapshotRequestError("project configuration changed while reading; refresh again", 409)
                task_path = collab.resolve_repo_path(root, f".ai-collab/threads/{thread_id}/task.json", label="task fingerprint")
                task_dependencies = {"coding_task": collab.read_json(task_path, "task fingerprint")} if task_path.is_file() else None
                before = thread_fingerprint(root, thread_id, project, session["codes"][project["id"]], task_dependencies)
                record = scan_thread(root, thread_id, project)
                read_budget.check()
                after = thread_fingerprint(root, thread_id, project, session["codes"][project["id"]], task_dependencies)
                if before != after or record.get("error") or record.get("projection_error") or record.get("history_errors") or any(turn.get("error") for turn in record.get("turns", [])):
                    session["failed"] = True
                    raise SnapshotRequestError("thread changed or failed while reading; refresh again", 409)
                # Include newly discovered evidence dependencies only after a stable
                # scan; initial metadata still forces a future scan if it changed.
                session["fingerprints"][key] = thread_fingerprint(root, thread_id, project, session["codes"][project["id"]], record)
                record_size = len(json.dumps(record, ensure_ascii=False).encode())
                if sum(session["record_bytes"].values()) - session["record_bytes"].get(key, 0) + record_size > SNAPSHOT_MAX_VERSION_BYTES:
                    session["failed"] = True
                    raise SnapshotRequestError("snapshot exceeds the incremental cache limit", 413)
                session["record_bytes"][key] = record_size
                session["records"][key] = record
                batch = batch_projects.setdefault(project["id"], {**project, "threads": []})
                batch["threads"].append(record)
                session["offset"] += 1
                if time.monotonic() - start >= SNAPSHOT_BATCH_SECONDS:
                    break
        projects = session["projects"] if cursor is None else list(batch_projects.values())
        for project in projects:
            project["threads"].sort(key=ui_read_model.thread_sort_key)
        payload = snapshot_from_projects(projects)
        complete = session["offset"] == len(session["work"])
        payload["progress"] = {"cursor": None if complete else f'{session["token"]}:{session["offset"]}',
                               "complete": complete, "loaded": session["offset"], "total": len(session["work"]), "delta": True}
        sync = {"reset": session["reset"], "base_version": session["base_version"], "version": None}
        if cursor is None:
            sync.update({"changed": [{"project_id": project["id"], "thread_id": tid} for project, tid in session["work"]],
                         "removed": session["removed"], "project_ids": [project["id"] for project in session["projects"]]})
        if complete:
            # A code change during the scan must not bless stale task acceptance.
            for project in session["projects"]:
                if project_config_fingerprint(Path(project["root"])) != session["configs"][project["id"]]:
                    session["failed"] = True
                    raise SnapshotRequestError("project configuration changed while reading; refresh again", 409)
                code = session["codes"][project["id"]]
                if code is not None and project_code_fingerprint(Path(project["root"])) != code:
                    session["failed"] = True
                    raise SnapshotRequestError("project code changed while reading; refresh again", 409)
            project_index = {project["id"]: project for project in session["projects"]}
            for key, record in session["records"].items():
                project = project_index[key[0]]
                if thread_fingerprint(Path(project["root"]), key[1], project, session["codes"][key[0]], record) != session["fingerprints"][key]:
                    session["failed"] = True
                    raise SnapshotRequestError("records changed during refresh; start a new refresh", 409)
            sync["summary"] = _version_summary(session)
            size = sum(session["record_bytes"].values())
            if size > SNAPSHOT_MAX_VERSION_BYTES:
                raise SnapshotRequestError("snapshot exceeds the incremental cache limit", 413)
            if size <= SNAPSHOT_MAX_VERSION_BYTES:
                with SNAPSHOT_SESSIONS_LOCK:
                    while SNAPSHOT_VERSIONS and (len(SNAPSHOT_VERSIONS) >= SNAPSHOT_MAX_VERSIONS or sum(v["bytes"] for v in SNAPSHOT_VERSIONS.values()) + size > SNAPSHOT_MAX_CACHE_BYTES):
                        del SNAPSHOT_VERSIONS[min(SNAPSHOT_VERSIONS, key=lambda key: SNAPSHOT_VERSIONS[key]["touched"])]
                    version = secrets.token_hex(16)
                    SNAPSHOT_VERSIONS[version] = {"records": session["records"], "fingerprints": session["fingerprints"],
                                                  "origin": session["origin"], "touched": time.monotonic(), "bytes": size, "record_bytes": session["record_bytes"]}
                    sync["version"] = version
            sync["cacheable"] = sync["version"] is not None
        payload["sync"] = sync
        session["last_cursor"] = cursor
        session["last_payload"] = copy.deepcopy(payload)
        session["touched"] = time.monotonic()
        return copy.deepcopy(payload)
    finally:
        if locked:
            session["lock"].release()
        SNAPSHOT_WORKERS.release()


def register_project(root: Path, path: Optional[Path] = None) -> Dict[str, Any]:
    resolved = root.expanduser().resolve()
    if not resolved.is_dir():
        raise collab.CollaborationError(f"repository root does not exist: {resolved}")
    target = path or registry_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with REGISTRY_LOCK:
        data = load_registry(target)
        created = not any(item["root"] == str(resolved) for item in data["projects"])
        if created:
            data["projects"].append({"root": str(resolved), "registered_at": collab.now_iso()})
            save_registry(target, data)
    return {"result": "REGISTERED", "created": created, "root": str(resolved), "project_id": project_id(str(resolved))}


def lock_file(handle: Any) -> None:
    """Exclusive lock so two ui commands cannot both spawn a server."""
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def unlock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def background_popen_kwargs() -> Dict[str, Any]:
    """Detach the UI process. Windows has no start_new_session and hides these flags off-Windows."""
    if os.name == "nt":
        # CREATE_NEW_PROCESS_GROUP and CREATE_NO_WINDOW. Numeric values stay valid
        # where the subprocess module does not define the names.
        create_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        return {"creationflags": create_group | no_window}
    return {"start_new_session": True}


def ui_build_info() -> Dict[str, Any]:
    """Fingerprint executable UI sources, including dirty/uncommitted installs."""
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    files = sorted((root / "scripts").rglob("*.py")) + sorted((root / "ui").glob("*"))
    for path in files:
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    try:
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=2, check=False)
        revision = result.stdout.decode("ascii", errors="replace").strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        revision = None
    return {"source_root": str(root), "source_fingerprint": digest.hexdigest(), "commit": revision}


def verify_running_ui(existing: Dict[str, Any]) -> None:
    running = existing.get("build") or {}
    expected = ui_build_info()
    if running.get("source_fingerprint") != expected["source_fingerprint"]:
        raise collab.CollaborationError(
            f"UI process {existing.get('pid', 'unknown')} uses older or different code "
            f"(source: {running.get('source_root', 'unknown')}). Stop that UI process and rerun this command; "
            "updating Git or reopening the browser does not restart the server.")


def health_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/api/health"


def read_health(port: int) -> Optional[Dict[str, Any]]:
    try:
        with urlopen(health_url(port), timeout=0.4) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError, TimeoutError):
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        return None
    return data


def post_json(port: int, route: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    request = Request(
        f"http://127.0.0.1:{port}{route}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=2) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError, TimeoutError) as exc:
        raise collab.CollaborationError(f"web UI request failed: {exc}") from exc
    if not isinstance(data, dict):
        raise collab.CollaborationError("web UI returned a non-object response")
    return data


class CollaborationHandler(BaseHTTPRequestHandler):
    server_version = "agent-collaboration-ui"

    def log_message(self, format: str, *args: Any) -> None:
        if os.environ.get("COLLAB_UI_DEBUG"):
            super().log_message(format, *args)

    def do_GET(self) -> None:
        gateway = getattr(self.server, "auto_gateway", None)
        if gateway is not None:
            if gateway.handle(self):
                return
            # Guard legacy local projections too when the write-capable service
            # is enabled: otherwise DNS rebinding could read private thread data.
            try:
                gateway.check_origin(self)
            except ValueError:
                self.respond(403, {"result": "ERROR", "error": "local origin required"})
                return
        path = self.path.split("?", 1)[0]
        if path == "/api/health":
            registry = load_registry(registry_path())
            self.respond(
                200,
                {
                    "ok": True,
                    "port": self.server.server_address[1],
                    "pid": os.getpid(),
                    "build": getattr(self.server, "build_info", None),
                    "projects": [item["root"] for item in registry["projects"]],
                },
            )
            return
        if path == "/api/snapshot":
            try:
                query = parse_qs(self.path.partition("?")[2], keep_blank_values=True)
                asynchronous = query.pop("async", None)
                if asynchronous is not None and asynchronous != ["1"]:
                    raise SnapshotRequestError("invalid async mode", 400)
                jobs = getattr(self.server, "snapshot_jobs", None)
                if set(query) == {"job"} and len(query["job"]) == 1:
                    token = query["job"][0]
                    if not re.fullmatch(r"[0-9a-f]{32}", token) or jobs is None:
                        raise SnapshotRequestError("invalid snapshot job", 400)
                    status, payload = jobs.poll(token)
                    self.respond(status, {**payload, "port": self.server.server_address[1]})
                    return
                if asynchronous:
                    if query.get("progressive") == ["1"] and set(query) <= {"progressive", "since"} and ("since" not in query or len(query["since"]) == 1):
                        since = query.get("since", [None])[0]
                        if since is not None and not re.fullmatch(r"[0-9a-f]{32}", since):
                            raise SnapshotRequestError("invalid snapshot version", 400)
                        origin = registry_path()
                        key = (str(origin.resolve()), "initial", since)
                        operation = lambda: progressive_snapshot(path=origin, since=since)
                    elif set(query) == {"cursor"} and len(query["cursor"]) == 1 and re.fullmatch(r"[0-9a-f]{32}:[0-9]{1,12}", query["cursor"][0]):
                        cursor = query["cursor"][0]
                        key = ("cursor", cursor)
                        operation = lambda: progressive_snapshot(cursor)
                    else:
                        raise SnapshotRequestError("invalid async snapshot query", 400)
                    if jobs is None:
                        raise SnapshotRequestError("async snapshot service unavailable", 503)
                    status, payload = jobs.fast_reply(*jobs.submit(key, operation))
                    self.respond(status, {**payload, "port": self.server.server_address[1]})
                    return
                if not query:
                    payload = snapshot()
                elif query.get("progressive") == ["1"] and set(query) <= {"progressive", "since"} and ("since" not in query or len(query["since"]) == 1):
                    payload = progressive_snapshot(since=query.get("since", [None])[0])
                elif set(query) == {"cursor"} and len(query["cursor"]) == 1:
                    payload = progressive_snapshot(query["cursor"][0])
                else:
                    raise SnapshotRequestError("snapshot requires progressive=1 or one cursor", 400)
                payload["port"] = self.server.server_address[1]
            except (SnapshotRequestError, collab.CollaborationError, OSError, ValueError, TypeError, KeyError) as exc:
                self.respond(getattr(exc, "status", 400), {"result": "ERROR", "error": str(exc)})
                return
            self.respond(200, payload)
            return
        if path in {"/api/thread", "/api/activity"}:
            try:
                query = parse_qs(self.path.partition("?")[2], keep_blank_values=True)
                if any(len(values) != 1 for values in query.values()):
                    raise collab.CollaborationError("query parameters must occur once")
                if path == "/api/activity":
                    if query:
                        raise collab.CollaborationError("activity does not accept query parameters")
                    import runtime_activity
                    import task_discovery
                    roots = task_discovery.registered_roots()
                    payload = runtime_activity.activity_snapshot(roots)
                    import manual_ingress
                    payload["manual_observations"] = manual_ingress.snapshot(roots)
                else:
                    if set(query) != {"project_id", "thread_id"}:
                        raise collab.CollaborationError("thread requires project_id and thread_id only")
                    payload = thread_detail(query["project_id"][0], query["thread_id"][0])
            except (collab.CollaborationError, OSError, ValueError, TypeError, KeyError) as exc:
                self.respond(400, {"result": "ERROR", "error": str(exc)})
                return
            self.respond(200, payload)
            return
        if path == "/api/tasks":
            # A read-only projection over configured projects. A URL cannot add
            # arbitrary filesystem roots or mutate identities/checkpoints.
            import task_discovery

            try:
                query = parse_qs(self.path.partition("?")[2], keep_blank_values=True)
                if set(query) - {"identity", "category", "include_terminal"}:
                    raise collab.CollaborationError("unsupported tasks query parameter")
                if any(len(values) != 1 for values in query.values()):
                    raise collab.CollaborationError("tasks query parameters must occur once")
                identity = query.get("identity", [""])[0]
                if not identity:
                    raise collab.CollaborationError("identity is required")
                terminal = query.get("include_terminal", ["false"])[0]
                if terminal not in {"true", "false", "1", "0"}:
                    raise collab.CollaborationError("include_terminal must be true or false")
                payload = task_discovery.query_tasks(
                    task_discovery.registered_roots(), identity,
                    include_terminal=terminal in {"true", "1"},
                    category=query.get("category", [None])[0],
                )
            except (collab.CollaborationError, OSError, ValueError, TypeError) as exc:
                self.respond(400, {"result": "ERROR", "error": str(exc)})
                return
            self.respond(200, payload)
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        self.serve_file(path)

    def do_POST(self) -> None:
        gateway = getattr(self.server, "auto_gateway", None)
        if gateway is not None:
            if gateway.handle(self):
                return
            # An auto-enabled server never accepts arbitrary HTTP root registration.
            # Projects must already be registered through the existing local CLI.
            self.respond(404, {"result": "ERROR", "error": "not found"})
            return
        path = self.path.split("?", 1)[0]
        if path != "/api/register":
            self.respond(404, {"result": "ERROR", "error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > 1_000_000:
            self.respond(400, {"result": "ERROR", "error": "body is too large"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            root = payload["root"]
            if not isinstance(root, str) or not root.strip():
                raise collab.CollaborationError("root must be a non-empty string")
            result = register_project(Path(root))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            self.respond(400, {"result": "ERROR", "error": f"invalid register request: {exc}"})
            return
        except collab.CollaborationError as exc:
            self.respond(400, {"result": "ERROR", "error": str(exc)})
            return
        self.respond(200, result)

    def respond(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def serve_file(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        candidate = (UI_DIR / relative).resolve()
        if UI_DIR.resolve() not in candidate.parents and candidate != UI_DIR.resolve():
            self.respond(404, {"result": "ERROR", "error": "not found"})
            return
        if not candidate.is_file():
            self.respond(404, {"result": "ERROR", "error": "not found"})
            return
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
        }.get(candidate.suffix, "application/octet-stream")
        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def make_server(port: int, *, auto_service=None, auto_credentials=None) -> ThreadingHTTPServer:
    if port < 1 or port > 65535:
        raise collab.CollaborationError(f"port must be between 1 and 65535; received {port}")
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), CollaborationHandler)
        server.snapshot_jobs = snapshot_jobs.SnapshotJobs()
        server.build_info = ui_build_info()
        if auto_service is not None:
            import auto_api
            if auto_credentials is None:
                server.server_close()
                raise collab.CollaborationError("auto service requires explicit local credentials")
            server.auto_gateway = auto_api.Gateway(auto_credentials, lambda: load_registry(registry_path()), auto_service.dispatch)
        return server
    except OSError as exc:
        raise collab.CollaborationError(f"cannot listen on 127.0.0.1:{port}: {exc}") from exc


def serve(port: int) -> None:
    httpd = make_server(port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def ensure_ui(root: Path, port: int = UI_PORT) -> Dict[str, Any]:
    """Start the UI when needed. An existing UI only receives this project registration."""
    home = ui_home()
    home.mkdir(parents=True, exist_ok=True)
    existing = read_health(port)
    if existing:
        verify_running_ui(existing)
        registered = post_json(port, "/api/register", {"root": str(root)})
        return {**registered, "started": False, "url": f"http://127.0.0.1:{port}/", "pid": existing.get("pid")}

    lock_path = home / "ui.lock"
    lock_path.touch(exist_ok=True)
    with lock_path.open("r+b") as lock:
        try:
            lock_file(lock)
        except OSError as exc:
            raise collab.CollaborationError(f"could not lock {lock_path}: {exc}") from exc
        try:
            existing = read_health(port)
            if existing:
                verify_running_ui(existing)
                registered = post_json(port, "/api/register", {"root": str(root)})
                return {
                    **registered,
                    "started": False,
                    "url": f"http://127.0.0.1:{port}/",
                    "pid": existing.get("pid"),
                }
            log_handle = (home / "ui.log").open("a", encoding="utf-8")
            process = subprocess.Popen(
                [sys.executable, str(collab.SCRIPT_PATH), "ui", "--serve", "--port", str(port)],
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env={**os.environ, "COLLAB_UI_HOME": str(home)},
                **background_popen_kwargs(),
            )
            log_handle.close()
            deadline = time.time() + 8
            while time.time() < deadline:
                if process.poll() is not None:
                    raise collab.CollaborationError(
                        f"web UI exited during startup; see {home / 'ui.log'}"
                    )
                existing = read_health(port)
                if existing:
                    registered = post_json(port, "/api/register", {"root": str(root)})
                    return {
                        **registered,
                        "started": True,
                        "url": f"http://127.0.0.1:{port}/",
                        "pid": existing.get("pid"),
                    }
                time.sleep(0.1)
            process.terminate()
            raise collab.CollaborationError(f"web UI did not become ready; see {home / 'ui.log'}")
        finally:
            unlock_file(lock)
