"""Read-only conversation projections; never infer human provenance or execution."""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict

import collab
import task_discovery
import task_model


def activity_timestamp(value: Any):
    """Parse a valid recorded time; legacy timezone-less dates are UTC.

    Invalid/missing dates sort last, not above real records or at Unix epoch.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def last_activity_at(record: dict, human: list = ()) -> str:
    """Activity is separate from protocol updated_at (terminal duration end).

    Newly captured originals count even when their source timestamp is older.
    This does not change message chronology, status, or protocol round order.
    """
    candidates = [record.get("started_at"), record.get("updated_at")]
    candidates.extend(turn.get("created_at") for turn in record.get("turns", []) if not turn.get("error"))
    for event in human:
        candidates.extend([event.get("created_at"), event.get("recorded_at")])
    valid = [parsed for value in candidates if (parsed := activity_timestamp(value)) is not None]
    return max(valid).isoformat() if valid else ""


def thread_sort_key(record: dict):
    timestamp = activity_timestamp(record.get("last_activity_at"))
    if timestamp is None:
        timestamp = activity_timestamp(last_activity_at(record))
    # Ordinal arithmetic avoids platform-dependent timestamp() range limits.
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    value = (timestamp - epoch).total_seconds() if timestamp is not None else float("-inf")
    return -value, str(record.get("project_id") or ""), str(record.get("thread_id") or "")


def thread_projection(root: Path, thread_id: str, record: Dict[str, Any], *, include_history: bool = False) -> Dict[str, Any]:
    """Add normalized identities/task facts without altering protocol transport."""
    collab.resolve_repo_path(root, f".ai-collab/threads/{thread_id}", label="thread directory")
    bindings = task_discovery.load_identities(root)
    import human_events
    human = human_events.read_human_events(root, thread_id)
    human_actors = {(event["author"], event["identity"]) for event in human["events"]}
    result = dict(record)
    result["last_activity_at"] = last_activity_at(record, human["events"])
    result["participant_details"] = [
        {"agent": agent, "identity": task_discovery.identity_for(agent, bindings),
         "actor_type": "human" if (agent, task_discovery.identity_for(agent, bindings)) in human_actors else "agent",
         "is_coordinator": agent == record.get("coordinator")}
        for agent in record["participants"]
    ]
    for event in human["events"]:
        if not any(detail["agent"] == event["author"] and detail["identity"] == event["identity"]
                   for detail in result["participant_details"]):
            result["participant_details"].append({"agent": event["author"], "identity": event["identity"],
                                                   "actor_type": "human", "is_coordinator": False})
    result["coordinator_identity"] = (task_discovery.identity_for(record["coordinator"], bindings)
                                      if record.get("coordinator") else None)
    result["coding_task"] = task_model.task_view(root, thread_id)
    result["history_errors"] = human["errors"]
    if not include_history:
        return result
    history = []
    for turn in record["turns"]:
        if turn.get("error"):
            continue
        path = collab.resolve_repo_path(root, turn["document"], must_exist=True, label="turn document")
        metadata, body = collab.parse_turn(path)
        author = metadata["author"]
        # Signal files retain only the latest notification. Missing older
        # recipients are unknown, not guessed from the participant roster.
        recipients = sorted({signal["to"] for signal in record["signals"]
                             if signal.get("document") == turn["document"]
                             and signal.get("decision") != "INVALID"})
        history.append({
            **turn, "id": "turn:" + turn["document"], "type": "turn",
            "actor_type": "agent", "author": author,
            "identity": task_discovery.identity_for(author, bindings),
            "is_coordinator": author == record.get("coordinator"), "body": body,
            "recipients": [{"agent": agent, "identity": task_discovery.identity_for(agent, bindings)}
                           for agent in recipients],
            "recipient_source": "current-signal" if recipients else "unknown",
            "provenance": None, "attribution": "Protocol turn; original human source not declared",
        })
    for event in human["events"]:
        history.append({
            "id": "human:" + event["event_id"], "type": "human-input", "actor_type": "human",
            "author": event["author"], "identity": event["identity"],
            "is_coordinator": event["author"] == record.get("coordinator"),
            "role": "human", "kind": "HUMAN_INPUT", "status": None, "requested_action": None,
            "title": "Human input", "body": event["body"], "summary": event["body"][:280],
            "created_at": event["created_at"], "round": None, "document": None,
            "recipients": [{"agent": agent, "identity": event["recipient_identities"][agent]}
                           for agent in event["recipients"]],
            "recipient_source": "explicit-human-input", "provenance": event["provenance"],
            "attribution": event["attribution_label"],
        })
    def chronology(event):
        value = activity_timestamp(event.get("created_at"))
        timestamp = (value - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds() if value is not None else 0
        round_number = event.get("round") if type(event.get("round")) is int else 0
        return value is None, timestamp, round_number, event["id"]
    history.sort(key=chronology)
    result["history"] = history
    return result
