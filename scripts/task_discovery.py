"""Stable collaborator identities and read-only, cross-project task discovery.

This module does not wake agents or process their notifications. In particular,
listing a task never acknowledges a signal or authorizes command execution.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import collab

CATEGORIES = ("actionable", "waiting-peer", "waiting-human")


def _identity_path(root: Path) -> Path:
    return collab.resolve_repo_path(root, ".ai-collab/identities.json", label="identity registry")


def load_identities(root: Path) -> Dict[str, str]:
    """Missing mappings retain the protocol's existing stable participant names."""
    path = _identity_path(root)
    if not path.exists():
        return {}
    data = collab.read_json(path, "identity registry")
    if data.get("schema_version") != 1 or not isinstance(data.get("participants"), dict):
        raise collab.CollaborationError("identity registry must have schema_version 1 and participants object")
    bindings = data["participants"]
    for agent, identity in bindings.items():
        if not isinstance(agent, str) or not isinstance(identity, str):
            raise collab.CollaborationError("identity bindings must map participant strings to identity strings")
        collab.validate_id(agent, "participant")
        collab.validate_id(identity, "identity")
    return dict(bindings)


def identity_for(agent: str, bindings: Dict[str, str]) -> str:
    return bindings.get(agent, agent)


@contextmanager
def _identity_lock(root: Path):
    path = collab.resolve_repo_path(root, ".ai-collab/identities.lock", label="identity lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        # Use an OS lock rather than a process-id file: a process crash releases it.
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def bind_identity(root: Path, agent: str, identity: str, replace: bool = False) -> Dict[str, Any]:
    """Explicitly map a participant; never infer identity from a tool/session ID."""
    root = collab.repository_root(str(root))
    collab.validate_id(agent, "agent")
    collab.validate_id(identity, "identity")
    with _identity_lock(root):
        bindings = load_identities(root)
        old = bindings.get(agent)
        if old == identity:
            return {"result": "UNCHANGED", "agent": agent, "identity": identity}
        if old is not None and not replace:
            raise collab.CollaborationError("participant already has a different stable identity; use --replace explicitly")
        bindings[agent] = identity
        target = _identity_path(root)
        collab.atomic_write_json(target, {"schema_version": 1, "participants": bindings})
        if os.name != "nt":
            directory = os.open(str(target.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    return {"result": "BOUND", "agent": agent, "identity": identity}


def _classification(view: Optional[Dict[str, Any]], pending: list, aliases: list,
                    identity: str, bindings: Dict[str, str], status: Optional[str]) -> str:
    if any(item["decision"] == "INVALID" for item in pending):
        return "waiting-human"
    if any((item.get("message") or {}).get("status") == "NEEDS_INPUT" for item in pending):
        return "waiting-human"
    if pending:
        return "actionable"
    if view:
        checkpoint = view.get("checkpoint") or {}
        waiting = checkpoint.get("waiting_on") or {}
        if waiting.get("kind") == "human":
            return "waiting-human"
        if waiting.get("kind") == "peer":
            waiting_identity = waiting.get("identity")
            if waiting_identity in aliases or (waiting_identity and identity_for(waiting_identity, bindings) == identity):
                return "actionable"
            return "waiting-peer"
        owner = checkpoint.get("owner") or view.get("owner")
        if owner in aliases or (owner and identity_for(owner, bindings) == identity):
            return "actionable"
        return "waiting-peer"
    return "waiting-human" if status == "NEEDS_INPUT" else "waiting-peer"


def query_tasks(roots: Iterable[Path], identity: str, include_terminal: bool = False,
                category: Optional[str] = None) -> Dict[str, Any]:
    """Return only the queried identity's tasks, preserving per-project failures.

    `roots` is an explicit caller-supplied allowlist. The HTTP adapter supplies
    registered projects only. This is a recovery query, not an inbox replacement.
    """
    collab.validate_id(identity, "identity")
    if category is not None:
        collab.validate_enum(category, CATEGORIES, "category")
    import task_model

    items = []
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
            bindings = load_identities(root)
            thread_ids = collab.list_thread_ids(root)
        except (collab.CollaborationError, OSError) as exc:
            errors.append({"project_root": str(root), "error": str(exc)})
            continue
        for thread_id in thread_ids:
            try:
                collab.validate_id(thread_id, "thread_id")
                collab.resolve_repo_path(root, f".ai-collab/threads/{thread_id}", label="thread directory")
                manifest = collab.load_manifest(root, thread_id)
                aliases = [agent for agent in manifest["participants"] if identity_for(agent, bindings) == identity]
                if not aliases:
                    continue
                pending = []
                for agent in aliases:
                    inbox = collab.inspect_inbox(root, agent, thread_id)
                    pending.extend({**item, "agent": agent} for item in inbox["results"]
                                   if item["decision"] in collab.ATTENTION_DECISIONS)
                view = task_model.task_view(root, thread_id)
                status = collab.latest_turn_status(root, thread_id)
                if view:
                    phase = view["phase"]
                    terminal = phase == "cancelled" or (
                        phase == "accepted" and view.get("acceptance", {}).get("ready", False)
                    )
                else:
                    phase = None
                    terminal = status in collab.TERMINAL_STATUSES
                # Terminal work with an unprocessed transport acknowledgement is
                # still resumable; listing it must not silently consume that ACK.
                if terminal and not pending and not include_terminal:
                    continue
                classification = _classification(view, pending, aliases, identity, bindings, status)
                if category is not None and classification != category:
                    continue
                checkpoint = (view or {}).get("checkpoint") or {}
                items.append({
                    "project_id": hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12],
                    "project_root": str(root), "project_name": root.name,
                    "thread_id": thread_id, "topic": manifest["topic"],
                    "objective": manifest["objective"], "identity": identity,
                    "agents": aliases, "profile": "coding" if view else "legacy",
                    "classification": classification, "terminal": terminal,
                    "status": status or "NONE", "phase": phase,
                    "owner": (view or {}).get("owner"),
                    "next_action": checkpoint.get("next_action"),
                    "pending": pending, "coding_task": view,
                    "updated_at": (view or {}).get("updated_at") or manifest["created_at"],
                })
            except (collab.CollaborationError, OSError, ValueError, TypeError, KeyError) as exc:
                errors.append({"project_root": str(root), "thread_id": thread_id, "error": str(exc)})
    rank = {name: index for index, name in enumerate(CATEGORIES)}
    items.sort(key=lambda item: (rank[item["classification"]], item["project_root"], item["thread_id"]))
    return {"schema_version": 1, "identity": identity, "items": items, "errors": errors,
            "count": len(items), "generated_at": collab.now_iso()}


def registered_roots() -> list:
    # Lazy import avoids a cycle: ui_server also uses this query for its read API.
    import ui_server
    return [Path(item["root"]) for item in ui_server.load_registry(ui_server.registry_path())["projects"]]


def cmd_bind_identity(args: argparse.Namespace) -> None:
    collab.emit(bind_identity(Path(args.root), args.agent, args.identity, args.replace))


def cmd_my_tasks(args: argparse.Namespace) -> None:
    identity = args.identity or os.environ.get("COLLAB_IDENTITY") or os.environ.get("COLLAB_AGENT")
    if not identity:
        raise collab.CollaborationError("pass --identity or set COLLAB_IDENTITY/COLLAB_AGENT")
    roots = [Path(root) for root in args.root or []]
    if args.all_projects:
        roots.extend(registered_roots())
    if not roots and not args.all_projects:
        roots = [Path(".")]
    collab.emit(query_tasks(roots, identity, args.include_terminal, args.category))


def add_parsers(subparsers: Any) -> None:
    bind = subparsers.add_parser("identity-bind", help="explicitly bind a participant to a stable collaborator identity")
    bind.add_argument("--root", default=".")
    bind.add_argument("--agent", required=True)
    bind.add_argument("--identity", required=True)
    bind.add_argument("--replace", action="store_true", help="explicitly replace an existing identity binding")
    bind.set_defaults(handler=cmd_bind_identity)
    tasks = subparsers.add_parser("my-tasks", help="read unfinished/resumable work without processing signals")
    tasks.add_argument("--root", action="append", help="project root; repeat to query several projects")
    tasks.add_argument("--all-projects", action="store_true", help="include every project in the local UI registry")
    tasks.add_argument("--identity", help="stable identity; defaults to COLLAB_IDENTITY or COLLAB_AGENT")
    tasks.add_argument("--include-terminal", action="store_true")
    tasks.add_argument("--category", choices=CATEGORIES)
    tasks.set_defaults(handler=cmd_my_tasks)
