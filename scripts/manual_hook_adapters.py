"""Normalize documented hook payloads, never inspect transcripts or infer humans.

Only allowlisted metadata is returned. Prompt, tool arguments/output, environment,
transcript paths and account details are deliberately not retained.
"""
from __future__ import annotations

PROFILES = {"claude": "claude-hooks-v1", "codex": "codex-hooks-v1", "cursor": "cursor-hooks-v1"}
EVENTS = {
    "claude": {"UserPromptSubmit": "prompt", "PreToolUse": "activity", "PostToolUse": "activity",
               "Stop": "stop", "StopFailure": "error", "SessionEnd": "session-end"},
    "codex": {"UserPromptSubmit": "prompt", "PreToolUse": "activity", "PostToolUse": "activity",
              "Stop": "stop", "Interrupt": "interrupt", "SessionEnd": "session-end"},
    "cursor": {"beforeSubmitPrompt": "prompt", "preToolUse": "activity", "postToolUse": "activity",
               "postToolUseFailure": "activity", "stop": "stop", "sessionEnd": "session-end"},
}


def string(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError(f"invalid {label}")
    return value


def normalize_hook(vendor: str, payload: dict) -> dict:
    if vendor not in EVENTS or not isinstance(payload, dict):
        raise ValueError("unsupported vendor or hook payload")
    event = payload.get("hook_event_name")
    if not isinstance(event, str) or event not in EVENTS[vendor]:
        raise ValueError("unsupported hook event")
    kind = EVENTS[vendor][event]
    if vendor == "cursor":
        session = string(payload.get("conversation_id"), "conversation_id")
        generation = string(payload.get("generation_id"), "generation_id") if event != "sessionEnd" else None
        roots = payload.get("workspace_roots")
        if not isinstance(roots, list) or not roots:
            raise ValueError("workspace_roots required")
        roots = [string(item, "workspace root") for item in roots]
        version = string(payload.get("cursor_version"), "cursor_version")
        if event == "stop":
            status = payload.get("status")
            if status not in ("completed", "aborted", "error"):
                raise ValueError("unsupported Cursor stop status")
            kind = {"completed": "stop", "aborted": "interrupt", "error": "error"}[status]
    else:
        session = string(payload.get("session_id"), "session_id")
        roots = [string(payload.get("cwd"), "cwd")]
        generation = None
        if vendor == "codex" and event != "SessionEnd":
            generation = string(payload.get("turn_id"), "turn_id")
        # These payloads do not document a reliable installed CLI version field.
        version = None
    if kind == "prompt":
        string(payload.get("prompt"), "prompt")
    return {"vendor": vendor, "event": event, "kind": kind, "session_id": session,
            "turn_id": generation, "roots": roots, "reported_version": version,
            "evidence_kind": "hook-event-observed", "human_original": False}
