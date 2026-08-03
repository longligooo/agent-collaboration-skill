#!/usr/bin/env python3
"""Deterministic helpers for the agent-collaboration file protocol."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


PROTOCOL_VERSION = 1
WORKSPACE_DIR = ".ai-collab"
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
TURN_PATTERN = re.compile(
    r"^round-(?P<round>[0-9]{4,})-from-(?P<author>[a-z0-9][a-z0-9._-]{0,63})-"
    r"(?P<kind>[a-z]+)\.md$"
)
META_START = "<!-- agent-collaboration"
META_END = "-->"
PLACEHOLDER = "TODO(agent-collaboration):"

KINDS = {
    "DISCUSSION",
    "PROPOSAL",
    "CHALLENGE",
    "REVISION",
    "IMPLEMENTATION",
    "REVIEW",
    "VERIFICATION",
    "DECISION",
    "CLOSURE",
}
ROLES = {
    "initiator",
    "coordinator",
    "proposer",
    "challenger",
    "implementer",
    "reviewer",
    "verifier",
}
STATUSES = {
    "OPEN",
    "NEEDS_INPUT",
    "NEEDS_CHANGES",
    "NO_BLOCKING_FINDINGS",
    "READY",
    "AGREED",
    "EXECUTING",
    "VERIFYING",
    "CLOSED",
    "CANCELLED",
}
TERMINAL_STATUSES = {"AGREED", "CLOSED", "CANCELLED"}
ACTIONS = {
    "DISCUSS",
    "PROPOSE",
    "CHALLENGE",
    "REVISE",
    "IMPLEMENT",
    "REVIEW",
    "VERIFY",
    "DECIDE",
    "ACKNOWLEDGE",
    "CLOSE",
    "NONE",
}


class CollaborationError(ValueError):
    """Raised when a protocol artifact is invalid or unsafe."""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def validate_id(value: str, label: str) -> str:
    if not ID_PATTERN.fullmatch(value):
        raise CollaborationError(
            f"{label} must match {ID_PATTERN.pattern!r}; received {value!r}"
        )
    return value


def validate_enum(value: str, allowed: Iterable[str], label: str) -> str:
    if value not in allowed:
        raise CollaborationError(
            f"{label} must be one of {sorted(allowed)}; received {value!r}"
        )
    return value


def validate_uuid(value: str, label: str = "message_id") -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise CollaborationError(f"{label} must be a UUID; received {value!r}") from exc
    return str(parsed)


def validate_timestamp(value: Any, label: str = "created_at") -> str:
    if not isinstance(value, str):
        raise CollaborationError(f"{label} must be an ISO-8601 string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise CollaborationError(f"{label} must be an ISO-8601 timestamp") from exc
    return value


def repository_root(raw: str) -> Path:
    root = Path(raw).resolve()
    if not root.is_dir():
        raise CollaborationError(f"repository root does not exist: {root}")
    return root


def resolve_repo_path(
    root: Path, raw: str, *, must_exist: bool = False, label: str = "path"
) -> Path:
    candidate = Path(raw)
    if candidate.is_absolute():
        raise CollaborationError(f"{label} must be repository-relative: {raw!r}")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise CollaborationError(f"{label} escapes the repository root: {raw!r}") from exc
    if must_exist and not resolved.exists():
        raise CollaborationError(f"{label} does not exist: {raw!r}")
    return resolved


def relative_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError as exc:
        raise CollaborationError(f"path is outside the repository root: {path}") from exc


def thread_directory(root: Path, thread_id: str) -> Path:
    validate_id(thread_id, "thread_id")
    return root / WORKSPACE_DIR / "threads" / thread_id


def read_json(path: Path, label: str) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CollaborationError(f"{label} does not exist: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CollaborationError(f"cannot read valid JSON from {label}: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CollaborationError(f"{label} must contain a JSON object: {path}")
    return data


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(data, handle, indent=2, ensure_ascii=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def emit(data: Dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=True))


def require_fields(data: Dict[str, Any], fields: Sequence[str], label: str) -> None:
    missing = [field for field in fields if field not in data]
    if missing:
        raise CollaborationError(f"{label} is missing required fields: {missing}")


def load_manifest(root: Path, thread_id: str) -> Dict[str, Any]:
    path = thread_directory(root, thread_id) / "thread.json"
    data = read_json(path, "thread manifest")
    require_fields(
        data,
        (
            "protocol_version",
            "thread_id",
            "topic",
            "objective",
            "coordinator",
            "participants",
            "created_at",
        ),
        "thread manifest",
    )
    if data["protocol_version"] != PROTOCOL_VERSION:
        raise CollaborationError("unsupported thread protocol_version")
    if data["thread_id"] != thread_id:
        raise CollaborationError("thread manifest ID does not match its directory")
    for field in ("topic", "objective"):
        if not isinstance(data[field], str) or not data[field].strip():
            raise CollaborationError(f"thread {field} must be a non-empty string")
    validate_id(data["coordinator"], "coordinator")
    participants = data["participants"]
    if not isinstance(participants, list) or not participants:
        raise CollaborationError("thread participants must be a non-empty list")
    for participant in participants:
        if not isinstance(participant, str):
            raise CollaborationError("thread participant IDs must be strings")
        validate_id(participant, "participant")
    if len(set(participants)) != len(participants):
        raise CollaborationError("thread participants must be unique")
    if data["coordinator"] not in participants:
        raise CollaborationError("thread coordinator must be a participant")
    validate_timestamp(data["created_at"])
    return data


def parse_turn(path: Path) -> Tuple[Dict[str, Any], str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeError) as exc:
        raise CollaborationError(f"cannot read turn: {path}: {exc}") from exc
    if not text.startswith(META_START):
        raise CollaborationError(f"turn must start with {META_START!r}: {path}")
    end = text.find(META_END, len(META_START))
    if end < 0:
        raise CollaborationError(f"turn metadata comment is not closed: {path}")
    payload = text[len(META_START) : end].strip()
    try:
        metadata = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CollaborationError(f"turn metadata is not valid JSON: {path}: {exc}") from exc
    if not isinstance(metadata, dict):
        raise CollaborationError(f"turn metadata must be a JSON object: {path}")
    return metadata, text[end + len(META_END) :]


def expected_turn_filename(metadata: Dict[str, Any]) -> str:
    return (
        f"round-{metadata['round']:04d}-from-{metadata['author']}-"
        f"{metadata['kind'].lower()}.md"
    )


def validate_turn(root: Path, path: Path, *, require_complete: bool) -> Dict[str, Any]:
    metadata, body = parse_turn(path)
    require_fields(
        metadata,
        (
            "protocol_version",
            "thread_id",
            "topic",
            "round",
            "kind",
            "author",
            "role",
            "status",
            "based_on",
            "requested_action",
            "created_at",
        ),
        "turn metadata",
    )
    if metadata["protocol_version"] != PROTOCOL_VERSION:
        raise CollaborationError("unsupported turn protocol_version")
    validate_id(metadata["thread_id"], "thread_id")
    validate_id(metadata["author"], "author")
    validate_enum(metadata["kind"], KINDS, "kind")
    validate_enum(metadata["role"], ROLES, "role")
    validate_enum(metadata["status"], STATUSES, "status")
    validate_enum(metadata["requested_action"], ACTIONS, "requested_action")
    validate_timestamp(metadata["created_at"])
    if not isinstance(metadata["topic"], str) or not metadata["topic"].strip():
        raise CollaborationError("turn topic must be a non-empty string")
    if not isinstance(metadata["round"], int) or metadata["round"] < 1:
        raise CollaborationError("turn round must be a positive integer")
    if not isinstance(metadata["based_on"], list):
        raise CollaborationError("turn based_on must be a list")
    for dependency in metadata["based_on"]:
        if not isinstance(dependency, str):
            raise CollaborationError("turn based_on entries must be strings")
        resolve_repo_path(root, dependency, must_exist=True, label="based_on path")

    manifest = load_manifest(root, metadata["thread_id"])
    if metadata["author"] not in manifest["participants"]:
        raise CollaborationError("turn author is not a thread participant")
    if metadata["topic"] != manifest["topic"]:
        raise CollaborationError("turn topic does not match the thread manifest")
    expected_parent = thread_directory(root, metadata["thread_id"]) / "turns"
    if path.resolve().parent != expected_parent.resolve():
        raise CollaborationError("turn is not stored in its thread turns directory")
    if path.name != expected_turn_filename(metadata):
        raise CollaborationError(
            f"turn filename must be {expected_turn_filename(metadata)!r}"
        )
    if require_complete and PLACEHOLDER in body:
        raise CollaborationError("turn still contains agent-collaboration placeholders")
    return metadata


def validate_signal(
    root: Path, path: Path, expected_agent: Optional[str] = None
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    signal = read_json(path, "signal")
    require_fields(
        signal,
        (
            "protocol_version",
            "message_id",
            "thread_id",
            "round",
            "from",
            "to",
            "document",
            "kind",
            "requested_action",
            "status",
            "created_at",
        ),
        "signal",
    )
    if signal["protocol_version"] != PROTOCOL_VERSION:
        raise CollaborationError("unsupported signal protocol_version")
    validate_uuid(signal["message_id"])
    validate_id(signal["thread_id"], "thread_id")
    validate_id(signal["from"], "from")
    validate_id(signal["to"], "to")
    validate_enum(signal["kind"], KINDS, "kind")
    validate_enum(signal["requested_action"], ACTIONS, "requested_action")
    validate_enum(signal["status"], STATUSES, "status")
    validate_timestamp(signal["created_at"])
    if expected_agent and signal["to"] != expected_agent:
        raise CollaborationError(
            f"signal recipient is {signal['to']!r}, expected {expected_agent!r}"
        )
    if not isinstance(signal["round"], int) or signal["round"] < 1:
        raise CollaborationError("signal round must be a positive integer")
    if not isinstance(signal["document"], str):
        raise CollaborationError("signal document must be a string")

    manifest = load_manifest(root, signal["thread_id"])
    if signal["from"] not in manifest["participants"]:
        raise CollaborationError("signal sender is not a thread participant")
    if signal["to"] not in manifest["participants"]:
        raise CollaborationError("signal recipient is not a thread participant")
    expected_signal_path = (
        thread_directory(root, signal["thread_id"])
        / "signals"
        / f"to-{signal['to']}.json"
    )
    if path.resolve() != expected_signal_path.resolve():
        raise CollaborationError("signal path does not match its thread and recipient")

    document_path = resolve_repo_path(
        root, signal["document"], must_exist=True, label="signal document"
    )
    metadata = validate_turn(root, document_path, require_complete=True)
    comparisons = {
        "thread_id": "thread_id",
        "round": "round",
        "from": "author",
        "kind": "kind",
        "requested_action": "requested_action",
        "status": "status",
    }
    for signal_key, metadata_key in comparisons.items():
        if signal[signal_key] != metadata[metadata_key]:
            raise CollaborationError(
                f"signal {signal_key} does not match turn {metadata_key}"
            )
    return signal, metadata


def read_state(root: Path, thread_id: str, agent: str) -> Dict[str, Any]:
    path = thread_directory(root, thread_id) / "state" / f"{agent}.json"
    if not path.exists():
        return {
            "protocol_version": PROTOCOL_VERSION,
            "agent": agent,
            "processed_message_ids": [],
            "updated_at": now_iso(),
        }
    state = read_json(path, "agent state")
    require_fields(
        state,
        ("protocol_version", "agent", "processed_message_ids", "updated_at"),
        "agent state",
    )
    if state["protocol_version"] != PROTOCOL_VERSION or state["agent"] != agent:
        raise CollaborationError("agent state identity does not match its path")
    if not isinstance(state["processed_message_ids"], list):
        raise CollaborationError("processed_message_ids must be a list")
    for message_id in state["processed_message_ids"]:
        validate_uuid(message_id)
    return state


def mark_processed(root: Path, thread_id: str, agent: str, message_id: str) -> None:
    validate_uuid(message_id)
    state = read_state(root, thread_id, agent)
    processed = state["processed_message_ids"]
    if message_id not in processed:
        processed.append(message_id)
    state["processed_message_ids"] = processed[-1000:]
    state["updated_at"] = now_iso()
    path = thread_directory(root, thread_id) / "state" / f"{agent}.json"
    atomic_write_json(path, state)


def verify_current_incoming(
    root: Path, thread_id: str, agent: str, message_id: str
) -> Dict[str, Any]:
    signal_path = thread_directory(root, thread_id) / "signals" / f"to-{agent}.json"
    signal, _ = validate_signal(root, signal_path, expected_agent=agent)
    if signal["message_id"] != validate_uuid(message_id):
        raise CollaborationError("incoming message ID is not the current signal")
    return signal


def next_round(root: Path, thread_id: str) -> int:
    turns_dir = thread_directory(root, thread_id) / "turns"
    highest = 0
    for path in turns_dir.glob("round-*.md"):
        match = TURN_PATTERN.fullmatch(path.name)
        if match:
            highest = max(highest, int(match.group("round")))
    return highest + 1


def round_exists(root: Path, thread_id: str, round_number: int) -> bool:
    turns_dir = thread_directory(root, thread_id) / "turns"
    for path in turns_dir.glob("round-*.md"):
        match = TURN_PATTERN.fullmatch(path.name)
        if match and int(match.group("round")) == round_number:
            return True
    return False


def cmd_init(args: argparse.Namespace) -> None:
    root = repository_root(args.root)
    thread_id = validate_id(args.thread_id, "thread_id")
    coordinator = validate_id(args.coordinator, "coordinator")
    participants: List[str] = []
    for raw in args.participants:
        participant = validate_id(raw, "participant")
        if participant not in participants:
            participants.append(participant)
    if coordinator not in participants:
        raise CollaborationError("coordinator must be included in participants")
    if not args.topic.strip() or not args.objective.strip():
        raise CollaborationError("topic and objective must be non-empty")
    target = thread_directory(root, thread_id)
    if target.exists():
        raise CollaborationError(f"thread already exists: {thread_id}")
    for name in ("turns", "signals", "state"):
        (target / name).mkdir(parents=True, exist_ok=False)
    manifest = {
        "protocol_version": PROTOCOL_VERSION,
        "thread_id": thread_id,
        "topic": args.topic,
        "objective": args.objective,
        "coordinator": coordinator,
        "participants": participants,
        "created_at": now_iso(),
    }
    atomic_write_json(target / "thread.json", manifest)
    emit({"result": "CREATED", "manifest": relative_path(root, target / "thread.json")})


def cmd_new_turn(args: argparse.Namespace) -> None:
    root = repository_root(args.root)
    manifest = load_manifest(root, args.thread_id)
    author = validate_id(args.author, "author")
    if author not in manifest["participants"]:
        raise CollaborationError("author is not a thread participant")
    validate_enum(args.role, ROLES, "role")
    validate_enum(args.kind, KINDS, "kind")
    validate_enum(args.status, STATUSES, "status")
    validate_enum(args.requested_action, ACTIONS, "requested_action")
    round_number = args.round if args.round is not None else next_round(root, args.thread_id)
    if round_number < 1:
        raise CollaborationError("round must be a positive integer")
    if round_exists(root, args.thread_id, round_number):
        raise CollaborationError(
            f"round {round_number} already exists and is immutable"
        )
    based_on: List[str] = []
    for raw in args.based_on:
        dependency = resolve_repo_path(root, raw, must_exist=True, label="based_on path")
        based_on.append(relative_path(root, dependency))
    metadata = {
        "protocol_version": PROTOCOL_VERSION,
        "thread_id": args.thread_id,
        "topic": manifest["topic"],
        "round": round_number,
        "kind": args.kind,
        "author": author,
        "role": args.role,
        "status": args.status,
        "based_on": based_on,
        "requested_action": args.requested_action,
        "created_at": now_iso(),
    }
    target = thread_directory(root, args.thread_id) / "turns" / expected_turn_filename(metadata)
    body = (
        f"{META_START}\n{json.dumps(metadata, indent=2, ensure_ascii=True)}\n{META_END}\n\n"
        f"# {args.title}\n\n"
        "## Summary\n\n"
        f"{PLACEHOLDER} state the outcome first.\n\n"
        "## Findings Or Decisions\n\n"
        f"{PLACEHOLDER} use stable IDs where follow-up is required.\n\n"
        "## Evidence\n\n"
        f"{PLACEHOLDER} list inspected artifacts, commands, and results.\n\n"
        "## Open Questions\n\n"
        f"{PLACEHOLDER} name owners, or write None.\n\n"
        "## Next Action\n\n"
        f"{PLACEHOLDER} state the recipient action and acceptance condition.\n"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
    except FileExistsError as exc:
        raise CollaborationError(f"turn already exists and is immutable: {target}") from exc
    emit(
        {
            "result": "CREATED",
            "document": relative_path(root, target),
            "round": round_number,
        }
    )


def cmd_validate_artifact(args: argparse.Namespace) -> None:
    root = repository_root(args.root)
    path = resolve_repo_path(root, args.document, must_exist=True, label="document")
    metadata = validate_turn(root, path, require_complete=not args.allow_placeholders)
    emit({"result": "VALID", "document": relative_path(root, path), "metadata": metadata})


def cmd_inspect(args: argparse.Namespace) -> None:
    root = repository_root(args.root)
    agent = validate_id(args.agent, "agent")
    threads_root = root / WORKSPACE_DIR / "threads"
    if args.thread_id:
        thread_ids = [validate_id(args.thread_id, "thread_id")]
    elif threads_root.is_dir():
        thread_ids = sorted(path.name for path in threads_root.iterdir() if path.is_dir())
    else:
        thread_ids = []

    results: List[Dict[str, Any]] = []
    for thread_id in thread_ids:
        try:
            validate_id(thread_id, "thread_id")
            manifest = load_manifest(root, thread_id)
            if agent not in manifest["participants"]:
                continue
            signal_path = thread_directory(root, thread_id) / "signals" / f"to-{agent}.json"
            if not signal_path.exists():
                continue
            signal, _ = validate_signal(root, signal_path, expected_agent=agent)
            state = read_state(root, thread_id, agent)
            if signal["message_id"] in state["processed_message_ids"]:
                decision = "DUPLICATE"
            elif signal["status"] in TERMINAL_STATUSES:
                decision = "TERMINAL"
            else:
                decision = "NEW"
            results.append(
                {
                    "decision": decision,
                    "thread_id": thread_id,
                    "signal": relative_path(root, signal_path),
                    "message": signal,
                }
            )
        except CollaborationError as exc:
            results.append(
                {
                    "decision": "INVALID",
                    "thread_id": thread_id,
                    "error": str(exc),
                }
            )

    decisions = [item["decision"] for item in results]
    if "NEW" in decisions:
        decision = "NEW"
    elif "TERMINAL" in decisions:
        decision = "TERMINAL"
    elif "INVALID" in decisions:
        decision = "INVALID"
    elif "DUPLICATE" in decisions:
        decision = "DUPLICATE"
    else:
        decision = "EMPTY"
    emit({"decision": decision, "agent": agent, "results": results})


def cmd_publish(args: argparse.Namespace) -> None:
    root = repository_root(args.root)
    manifest = load_manifest(root, args.thread_id)
    agent = validate_id(args.agent, "agent")
    recipient = validate_id(args.to, "to")
    if agent == recipient:
        raise CollaborationError("sender and recipient must differ")
    if agent not in manifest["participants"] or recipient not in manifest["participants"]:
        raise CollaborationError("sender and recipient must be thread participants")
    document_path = resolve_repo_path(
        root, args.document, must_exist=True, label="document"
    )
    metadata = validate_turn(root, document_path, require_complete=True)
    if metadata["thread_id"] != args.thread_id or metadata["author"] != agent:
        raise CollaborationError("turn thread or author does not match publish arguments")
    if args.requested_action and metadata["requested_action"] != args.requested_action:
        raise CollaborationError("requested action does not match turn metadata")

    if args.in_message_id:
        verify_current_incoming(root, args.thread_id, agent, args.in_message_id)
    message_id = validate_uuid(args.message_id) if args.message_id else str(uuid.uuid4())
    signal = {
        "protocol_version": PROTOCOL_VERSION,
        "message_id": message_id,
        "thread_id": args.thread_id,
        "round": metadata["round"],
        "from": agent,
        "to": recipient,
        "document": relative_path(root, document_path),
        "kind": metadata["kind"],
        "requested_action": metadata["requested_action"],
        "status": metadata["status"],
        "created_at": now_iso(),
    }
    signal_path = thread_directory(root, args.thread_id) / "signals" / f"to-{recipient}.json"
    atomic_write_json(signal_path, signal)
    if args.in_message_id:
        mark_processed(root, args.thread_id, agent, args.in_message_id)
    emit(
        {
            "result": "PUBLISHED",
            "signal": relative_path(root, signal_path),
            "message_id": message_id,
        }
    )


def cmd_acknowledge(args: argparse.Namespace) -> None:
    root = repository_root(args.root)
    manifest = load_manifest(root, args.thread_id)
    agent = validate_id(args.agent, "agent")
    if agent not in manifest["participants"]:
        raise CollaborationError("agent is not a thread participant")
    verify_current_incoming(root, args.thread_id, agent, args.message_id)
    mark_processed(root, args.thread_id, agent, args.message_id)
    emit(
        {
            "result": "ACKNOWLEDGED",
            "thread_id": args.thread_id,
            "agent": agent,
            "message_id": validate_uuid(args.message_id),
        }
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the agent-collaboration file protocol."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create an immutable thread manifest")
    init_parser.add_argument("--root", default=".")
    init_parser.add_argument("--thread-id", required=True)
    init_parser.add_argument("--topic", required=True)
    init_parser.add_argument("--objective", required=True)
    init_parser.add_argument("--coordinator", required=True)
    init_parser.add_argument("--participants", nargs="+", required=True)
    init_parser.set_defaults(handler=cmd_init)

    turn_parser = subparsers.add_parser("new-turn", help="create a new immutable turn template")
    turn_parser.add_argument("--root", default=".")
    turn_parser.add_argument("--thread-id", required=True)
    turn_parser.add_argument("--author", required=True)
    turn_parser.add_argument("--role", required=True, choices=sorted(ROLES))
    turn_parser.add_argument("--kind", required=True, choices=sorted(KINDS))
    turn_parser.add_argument("--status", required=True, choices=sorted(STATUSES))
    turn_parser.add_argument("--requested-action", required=True, choices=sorted(ACTIONS))
    turn_parser.add_argument("--based-on", action="append", default=[])
    turn_parser.add_argument("--round", type=int)
    turn_parser.add_argument("--title", required=True)
    turn_parser.set_defaults(handler=cmd_new_turn)

    validate_parser = subparsers.add_parser("validate-artifact", help="validate a turn")
    validate_parser.add_argument("--root", default=".")
    validate_parser.add_argument("--document", required=True)
    validate_parser.add_argument("--allow-placeholders", action="store_true")
    validate_parser.set_defaults(handler=cmd_validate_artifact)

    inspect_parser = subparsers.add_parser("inspect", help="inspect one agent inbox")
    inspect_parser.add_argument("--root", default=".")
    inspect_parser.add_argument("--agent", required=True)
    inspect_parser.add_argument("--thread-id")
    inspect_parser.set_defaults(handler=cmd_inspect)

    publish_parser = subparsers.add_parser("publish", help="atomically publish a turn signal")
    publish_parser.add_argument("--root", default=".")
    publish_parser.add_argument("--thread-id", required=True)
    publish_parser.add_argument("--agent", required=True)
    publish_parser.add_argument("--to", required=True)
    publish_parser.add_argument("--document", required=True)
    publish_parser.add_argument("--requested-action", choices=sorted(ACTIONS))
    publish_parser.add_argument("--message-id")
    publish_parser.add_argument("--in-message-id")
    publish_parser.set_defaults(handler=cmd_publish)

    ack_parser = subparsers.add_parser("acknowledge", help="mark the current signal processed")
    ack_parser.add_argument("--root", default=".")
    ack_parser.add_argument("--thread-id", required=True)
    ack_parser.add_argument("--agent", required=True)
    ack_parser.add_argument("--message-id", required=True)
    ack_parser.set_defaults(handler=cmd_acknowledge)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except CollaborationError as exc:
        print(json.dumps({"result": "ERROR", "error": str(exc)}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
