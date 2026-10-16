"""Local read-only web UI for collaboration across projects on this machine."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

import collab

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
    path = root / collab.WORKSPACE_DIR / "wake.json"
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
        "updated_at": "",
        "turns": [],
        "signals": [],
        "error": None,
    }
    try:
        manifest = collab.load_manifest(root, thread_id)
    except collab.CollaborationError as exc:
        record["error"] = str(exc)
        return record
    record["topic"] = manifest["topic"]
    record["objective"] = manifest["objective"]
    record["coordinator"] = collab.designated_coordinator(manifest)
    record["participants"] = list(manifest["participants"])
    turns_dir = collab.thread_directory(root, thread_id) / "turns"
    turns: List[Dict[str, Any]] = []
    if turns_dir.is_dir():
        for path in sorted(turns_dir.glob("round-*.md")):
            try:
                metadata, body = collab.parse_turn(path)
            except collab.CollaborationError as exc:
                turns.append({"document": collab.relative_path(root, path), "error": str(exc)})
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
    return record


def scan_project(entry: Dict[str, Any]) -> Dict[str, Any]:
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
    threads = []
    for thread_id in collab.list_thread_ids(root):
        threads.append(scan_thread(root, thread_id, project))
    threads.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    project["threads"] = threads
    return project


def collaborator_records(projects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    people: Dict[str, Dict[str, Any]] = {}

    def person(agent: str) -> Dict[str, Any]:
        record = people.get(agent)
        if record is None:
            record = {
                "id": agent,
                "pending": 0,
                "turns": 0,
                "last_at": "",
                "projects": [],
                "threads": [],
                "tools": [],
            }
            people[agent] = record
        return record

    for project in projects:
        if not project["available"]:
            continue
        for thread in project["threads"]:
            involved = set(thread["participants"])
            for turn in thread["turns"]:
                if isinstance(turn.get("author"), str):
                    involved.add(turn["author"])
            for signal in thread["signals"]:
                if isinstance(signal.get("from"), str):
                    involved.add(signal["from"])
                if isinstance(signal.get("to"), str):
                    involved.add(signal["to"])
            for agent in sorted(involved):
                record = person(agent)
                if not any(item["id"] == project["id"] for item in record["projects"]):
                    record["projects"].append({"id": project["id"], "name": project["name"], "root": project["root"]})
                tool = project["tools"].get(agent)
                if tool and tool not in record["tools"]:
                    record["tools"].append(tool)
                pending = [
                    signal
                    for signal in thread["signals"]
                    if signal.get("to") == agent and signal.get("decision") in ATTENTION
                ]
                record["pending"] += len(pending)
                authored = [turn for turn in thread["turns"] if turn.get("author") == agent and not turn.get("error")]
                record["turns"] += len(authored)
                for turn in authored:
                    if (turn.get("created_at") or "") > record["last_at"]:
                        record["last_at"] = turn["created_at"]
                record["threads"].append(
                    {
                        "project_id": project["id"],
                        "project_name": project["name"],
                        "thread_id": thread["thread_id"],
                        "topic": thread["topic"],
                        "coordinator": thread["coordinator"],
                        "participants": list(thread["participants"]),
                        "participant": agent in thread["participants"],
                        "pending": len(pending),
                        "status": thread["status"],
                        "updated_at": thread["updated_at"],
                    }
                )
    return sorted(people.values(), key=lambda item: (-item["pending"], item["id"]))


def snapshot(path: Optional[Path] = None) -> Dict[str, Any]:
    registry = load_registry(path or registry_path())
    projects = [scan_project(entry) for entry in registry["projects"]]
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
        path = self.path.split("?", 1)[0]
        if path == "/api/health":
            registry = load_registry(registry_path())
            self.respond(
                200,
                {
                    "ok": True,
                    "port": self.server.server_address[1],
                    "pid": os.getpid(),
                    "projects": [item["root"] for item in registry["projects"]],
                },
            )
            return
        if path == "/api/snapshot":
            payload = snapshot()
            payload["port"] = self.server.server_address[1]
            self.respond(200, payload)
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        self.serve_file(path)

    def do_POST(self) -> None:
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


def make_server(port: int) -> ThreadingHTTPServer:
    if port < 1 or port > 65535:
        raise collab.CollaborationError(f"port must be between 1 and 65535; received {port}")
    try:
        return ThreadingHTTPServer(("127.0.0.1", port), CollaborationHandler)
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
