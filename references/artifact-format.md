# Artifact And Signal Format

## Turn Filename

Use:

```text
.ai-collab/threads/<thread-id>/turns/round-<NNNN>-from-<agent>-<kind>.md
```

Use lowercase kind text in filenames and uppercase kind values in metadata.

## Turn Metadata

Start every turn with a structured JSON block inside an HTML comment:

```md
<!-- agent-collaboration
{
  "protocol_version": 1,
  "thread_id": "config-redesign",
  "topic": "Configuration redesign",
  "round": 2,
  "kind": "CHALLENGE",
  "author": "codex",
  "role": "challenger",
  "status": "NEEDS_CHANGES",
  "based_on": [
    ".ai-collab/threads/config-redesign/turns/round-0001-from-cursor-proposal.md"
  ],
  "requested_action": "REVISION",
  "created_at": "2026-08-03T14:30:00+08:00"
}
-->
```

Required fields:

- `protocol_version`
- `thread_id`
- `topic`
- `round`
- `kind`
- `author`
- `role`
- `status`
- `based_on`
- `requested_action`
- `created_at`

Use repository-relative paths in `based_on`. Include both collaboration turns and important source artifacts when useful.

## Turn Body

Use only the sections needed for the turn, but keep conclusions and evidence explicit:

```md
# Configuration redesign challenge

## Summary

State the outcome first.

## Findings Or Decisions

Use stable IDs for tracked findings or decisions.

## Evidence

List inspected files, commands, test results, or observed behavior.

## Open Questions

Name the owner of each unresolved question.

## Next Action

State what the recipient should do and the acceptance condition.
```

## Signal

Use a fixed signal file for each recipient:

```json
{
  "protocol_version": 1,
  "message_id": "550e8400-e29b-41d4-a716-446655440000",
  "thread_id": "config-redesign",
  "round": 2,
  "from": "codex",
  "to": "cursor",
  "document": ".ai-collab/threads/config-redesign/turns/round-0002-from-codex-challenge.md",
  "kind": "CHALLENGE",
  "requested_action": "REVISION",
  "status": "NEEDS_CHANGES",
  "created_at": "2026-08-03T14:35:00+08:00"
}
```

Signals are notifications, not the source of truth. The referenced turn wins if descriptive prose differs, but structural mismatches make the signal invalid and must be corrected by its sender.

## Thread Manifest

`thread.json` contains:

```json
{
  "protocol_version": 1,
  "thread_id": "config-redesign",
  "topic": "Configuration redesign",
  "objective": "Agree on and verify the configuration migration plan",
  "coordinator": "cursor",
  "participants": ["cursor", "codex", "claude-code"],
  "created_at": "2026-08-03T14:00:00+08:00"
}
```

Do not mutate the manifest to express progress. Publish turns and signals instead.

## Agent State

Each agent owns `state/<agent>.json`:

```json
{
  "protocol_version": 1,
  "agent": "codex",
  "processed_message_ids": [
    "550e8400-e29b-41d4-a716-446655440000"
  ],
  "updated_at": "2026-08-03T14:40:00+08:00"
}
```

State is an idempotency aid, not collaboration evidence. Keep at most the most recent 1000 message IDs unless project policy requires more.
