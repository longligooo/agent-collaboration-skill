"""Read-only conversation projections; never infer human provenance or execution."""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict

import collab
import task_discovery
import task_model


def thread_projection(root: Path, thread_id: str, record: Dict[str, Any], *, include_history: bool = False) -> Dict[str, Any]:
    """Add normalized identities/task facts without altering protocol transport."""
    collab.resolve_repo_path(root, f".ai-collab/threads/{thread_id}", label="thread directory")
    bindings = task_discovery.load_identities(root)
    import human_events
    human = human_events.read_human_events(root, thread_id)
    human_actors = {(event["author"], event["identity"]) for event in human["events"]}
    result = dict(record)
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
        try:
            value = datetime.fromisoformat((event.get("created_at") or "").replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            timestamp = value.timestamp()
        except (ValueError, TypeError):
            timestamp = 0
        return timestamp, event.get("round") or 0, event["id"]
    history.sort(key=chronology)
    result["history"] = history
    return result
