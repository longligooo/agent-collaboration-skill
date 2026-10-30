"""Explicit immutable human-input records; never infer people or fetch sources.

This records caller-supplied provenance, not independently authenticated evidence.
Manual entry is labelled as such and never presented as an external capture.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import collab
import task_discovery

SCHEMA_VERSION = 1
INPUT_FIELDS = {"schema_version", "event_id", "thread_id", "actor_type", "author", "body",
                "created_at", "recipients", "provenance"}
SOURCE_FIELDS = {"source_conversation_id", "source_message_id", "source_author"}
PROVENANCE_FIELDS = SOURCE_FIELDS | {"kind", "supplied_authorized", "original_text"}
GENERATED_FIELDS = {"identity", "recipient_identities", "recorded_at", "attribution_label"}


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise collab.CollaborationError(f"{label} must be a non-empty string")
    return value


def _timestamp(value: Any, label: str) -> str:
    collab.validate_timestamp(value, label)
    parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise collab.CollaborationError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _safe_path(root: Path, relative: str) -> Path:
    """Disallow symlink aliases as well as escapes for immutable event storage."""
    result = collab.resolve_repo_path(root, relative, label="human event path")
    cursor = root
    for component in Path(relative).parts:
        cursor = cursor / component
        if cursor.is_symlink():
            raise collab.CollaborationError(f"human event path must not contain symlinks: {relative}")
    return result


def _directory(root: Path, thread_id: str) -> Path:
    collab.validate_id(thread_id, "thread_id")
    return _safe_path(root, f".ai-collab/threads/{thread_id}/human-events")


def _label(provenance: dict) -> str:
    if provenance["kind"] == "manual":
        return "Manual human-input entry — not a verified external capture"
    return "User-authorized supplied original record — provenance supplied, not independently verified"


def _input(root: Path, thread_id: str, payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise collab.CollaborationError("human event must be an object")
    unknown = set(payload) - INPUT_FIELDS
    if unknown:
        raise collab.CollaborationError(f"unsupported human event fields (paraphrases cannot be human originals): {sorted(unknown)}")
    if type(payload.get("schema_version", 1)) is not int or payload.get("schema_version", 1) != 1:
        raise collab.CollaborationError("unsupported human event schema_version")
    if payload.get("thread_id", thread_id) != thread_id:
        raise collab.CollaborationError("human event thread_id does not match destination")
    if payload.get("actor_type") != "human":
        raise collab.CollaborationError("human event requires explicit actor_type human")
    identifier = collab.validate_id(_text(payload.get("event_id"), "event_id"), "event_id")
    author = collab.validate_id(_text(payload.get("author"), "author"), "author")
    _directory(root, thread_id)
    manifest = collab.load_manifest(root, thread_id)
    # Historical manifests often name only agents. An explicit human record may
    # name an external human slug, but this grants no transport/task authority.
    recipients = payload.get("recipients")
    if not isinstance(recipients, list) or not recipients:
        raise collab.CollaborationError("recipients must be a non-empty participant list")
    for recipient in recipients:
        collab.validate_id(_text(recipient, "recipient"), "recipient")
        if recipient not in manifest["participants"]:
            raise collab.CollaborationError("human event recipient must be a thread participant")
    if len(set(recipients)) != len(recipients):
        raise collab.CollaborationError("human event recipients must be unique")
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict) or set(provenance) - PROVENANCE_FIELDS:
        raise collab.CollaborationError("invalid or unknown human provenance fields")
    kind = provenance.get("kind")
    if not isinstance(kind, str) or kind not in {"manual", "authorized-import"}:
        raise collab.CollaborationError("human provenance kind must be manual or authorized-import")
    if provenance.get("supplied_authorized") is not True or provenance.get("original_text") is not True:
        raise collab.CollaborationError("human provenance requires explicit authorized supply and original text, not agent paraphrase")
    normalized = {"kind": kind, "supplied_authorized": True, "original_text": True}
    for field in sorted(SOURCE_FIELDS):
        value = provenance.get(field)
        if kind == "authorized-import" or value is not None:
            _text(value, field)
        normalized[field] = value
    return {"schema_version": 1, "event_id": identifier, "thread_id": thread_id,
            "actor_type": "human", "author": author,
            "body": _text(payload.get("body"), "body"),
            "created_at": _timestamp(payload.get("created_at"), "created_at"),
            "recipients": sorted(recipients), "provenance": normalized}


def _normalized(event: dict) -> dict:
    return {**copy.deepcopy(event), "kind": "HUMAN_INPUT", "role": "human",
            "author_id": event["identity"]}


def _read_file(root: Path, thread_id: str, path: Path) -> dict:
    _safe_path(root, path.relative_to(root).as_posix())
    event = collab.read_json(path, "human event")
    if set(event) != INPUT_FIELDS | GENERATED_FIELDS:
        raise collab.CollaborationError("persisted human event fields are missing or unsupported")
    validated = _input(root, thread_id, {key: event[key] for key in INPUT_FIELDS})
    if event["event_id"] + ".json" != path.name or any(event[key] != value for key, value in validated.items()):
        raise collab.CollaborationError("human event identity or normalized fields do not match stored record")
    collab.validate_id(_text(event["identity"], "identity"), "identity")
    recipient_identities = event["recipient_identities"]
    if not isinstance(recipient_identities, dict) or set(recipient_identities) != set(event["recipients"]):
        raise collab.CollaborationError("recipient_identities must map exactly the recorded recipients")
    for identity in recipient_identities.values():
        collab.validate_id(_text(identity, "recipient identity"), "recipient identity")
    _timestamp(event["recorded_at"], "recorded_at")
    if event["attribution_label"] != _label(event["provenance"]):
        raise collab.CollaborationError("human event attribution does not match explicit provenance")
    return event


def _source_key(event: dict):
    provenance = event["provenance"]
    if provenance["kind"] == "authorized-import":
        return (provenance["source_conversation_id"], provenance["source_message_id"])
    return None


def _comparable(event: dict, *, include_id: bool = True) -> dict:
    fields = INPUT_FIELDS if include_id else INPUT_FIELDS - {"event_id"}
    return {key: event[key] for key in fields}


def read_human_events(root: Path, thread_id: str) -> Dict[str, Any]:
    """Read explicit events only; unknown/corrupt records become errors, not humans."""
    events, errors = [], []
    root = Path(root).resolve()
    try:
        directory = _directory(root, thread_id)
        collab.load_manifest(root, thread_id)
        if not directory.exists():
            return {"events": [], "errors": []}
        if not directory.is_dir():
            raise collab.CollaborationError("human-events must be a directory")
        sources = set()
        for path in sorted(directory.glob("*.json")):
            try:
                event = _read_file(root, thread_id, path)
                key = _source_key(event)
                if key and key in sources:
                    raise collab.CollaborationError("duplicate source message in persisted human events")
                if key:
                    sources.add(key)
                events.append(_normalized(event))
            except (collab.CollaborationError, OSError, TypeError, ValueError) as exc:
                errors.append({"document": path.relative_to(root).as_posix(), "error": str(exc)})
    except (collab.CollaborationError, OSError) as exc:
        errors.append({"thread_id": thread_id, "error": str(exc)})
    events.sort(key=lambda item: (datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")), item["event_id"]))
    return {"events": events, "errors": errors}


@contextmanager
def _locked(root: Path, thread_id: str):
    path = _safe_path(root, f".ai-collab/threads/{thread_id}/.human-events.lock")
    descriptor = os.open(str(path), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
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


def _publish(path: Path, event: dict) -> None:
    """Publish fully fsynced bytes without ever replacing an existing event."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         prefix=".human-event-", suffix=".tmp",
                                         dir=str(path.parent), delete=False) as stream:
            temporary = stream.name
            json.dump(event, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic no-clobber publication, even if another writer ignores our lock.
        os.link(temporary, str(path))
        if os.name != "nt":
            descriptor = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def record_human_event(root: Path, thread_id: str, payload: dict) -> Dict[str, Any]:
    root = collab.repository_root(str(root))
    candidate = _input(root, thread_id, payload)
    try:
        with _locked(root, thread_id):
            current = read_human_events(root, thread_id)
            if current["errors"]:
                raise collab.CollaborationError("existing human events require inspection: " + "; ".join(item["error"] for item in current["errors"]))
            for event in current["events"]:
                if event["event_id"] == candidate["event_id"]:
                    if _comparable(event) != candidate:
                        raise collab.CollaborationError("human event_id already exists with different content")
                    return {"result": "DUPLICATE", "event": event}
                if _source_key(candidate) and _source_key(event) == _source_key(candidate):
                    if _comparable(event, include_id=False) != _comparable(candidate, include_id=False):
                        raise collab.CollaborationError("source conversation/message already recorded with conflicting content or recipients")
                    return {"result": "DUPLICATE", "event": event}
            bindings = task_discovery.load_identities(root)
            identity = task_discovery.identity_for(candidate["author"], bindings)
            recipient_identities = {recipient: task_discovery.identity_for(recipient, bindings)
                                    for recipient in candidate["recipients"]}
            event = {**candidate, "identity": identity, "recipient_identities": recipient_identities,
                     "recorded_at": collab.now_iso(),
                     "attribution_label": _label(candidate["provenance"])}
            directory = _directory(root, thread_id)
            directory.mkdir(exist_ok=True)
            path = _safe_path(root, f".ai-collab/threads/{thread_id}/human-events/{candidate['event_id']}.json")
            _publish(path, event)
            return {"result": "RECORDED", "event": _normalized(event)}
    except OSError as exc:
        raise collab.CollaborationError(f"cannot persist human event: {exc}") from exc


def cmd_human_record(args: argparse.Namespace) -> None:
    root = collab.repository_root(args.root)
    path = collab.resolve_repo_path(root, args.event_file, must_exist=True, label="event file")
    collab.emit(record_human_event(root, args.thread_id, collab.read_json(path, "human event input")))


def add_parsers(subparsers) -> None:
    parser = subparsers.add_parser("human-record", help="record explicitly supplied human input; never fetch external conversations")
    parser.add_argument("--root", default=".")
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--event-file", required=True)
    parser.set_defaults(handler=cmd_human_record)
