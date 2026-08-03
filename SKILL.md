---
name: agent-collaboration
description: Coordinate durable, file-based collaboration among AI coding tools and human participants. Use when Codex, Claude Code, Cursor, or another agent must discuss a design, challenge assumptions, hand off implementation, review code or plans, verify results, record a decision, or close a multi-round task without overwriting another participant's work.
---

# Agent Collaboration

Coordinate agents through immutable Markdown turns and small JSON signals stored in the shared repository. Treat repository artifacts and current code as facts; treat chat context and signals only as notifications.

## Load The Protocol

Read [references/protocol.md](references/protocol.md) before creating or processing a collaboration thread. Read [references/artifact-format.md](references/artifact-format.md) when writing a turn. Read [references/tool-adapters.md](references/tool-adapters.md) only when configuring a coding tool to discover this skill or monitor signals.

Use `python scripts/collab.py --help` for deterministic workspace operations. The script uses only the Python standard library.

## Preserve These Invariants

- Never edit or delete another participant's turn.
- Create a new turn for every response; never reuse a round filename.
- Write the complete turn before publishing its signal.
- Re-read the referenced plans, code, tests, or documents before making factual claims.
- Validate recipient, thread, message ID, document path, and requested action before acting.
- Keep business-code changes separate from collaboration metadata. Modify business code only when the requested action and user authorization permit it.
- Keep each agent's processed-message state separate. Do not rely on conversation memory for idempotency.
- Stop on duplicate, invalid, mismatched, or stale signals without writing files.
- Let a human instruction override the protocol and record that override in the next turn when collaboration continues.

## Choose The Collaboration Action

Use these turn kinds:

- `DISCUSSION`: explore requirements, constraints, or alternatives.
- `PROPOSAL`: present a concrete plan or design.
- `CHALLENGE`: test assumptions, risks, and tradeoffs.
- `REVISION`: respond to feedback with a changed proposal.
- `IMPLEMENTATION`: report implementation work and evidence.
- `REVIEW`: assess a plan, implementation, test, or document.
- `VERIFICATION`: independently verify claims or acceptance criteria.
- `DECISION`: record an agreed choice and its consequences.
- `CLOSURE`: close or cancel the thread and define retained artifacts.

Use roles independently of tools: `initiator`, `coordinator`, `proposer`, `challenger`, `implementer`, `reviewer`, or `verifier`. An agent may change roles between turns.

## Start A Thread

1. Choose a stable lowercase `thread_id`, participant IDs, a coordinator, and a bounded objective.
2. Initialize the workspace:

```bash
python scripts/collab.py init \
  --root . \
  --thread-id config-redesign \
  --topic "Configuration redesign" \
  --objective "Agree on and verify the configuration migration plan" \
  --coordinator cursor \
  --participants cursor codex claude-code
```

3. Create the first turn with `new-turn`, fill its evidence and requested outcome, then publish it.
4. Keep unrelated objectives in separate threads.

## Process An Incoming Turn

1. Inspect the inbox:

```bash
python scripts/collab.py inspect --root . --agent codex
```

2. Act only on a `NEW` decision. For `TERMINAL`, follow the terminal action and acknowledge it. For `EMPTY`, `DUPLICATE`, or `INVALID`, do not create a response.
3. Read the referenced turn and every relevant source artifact. Continue earlier finding IDs until each is explicitly closed, deferred with owner and reason, or still open.
4. Perform the requested action at the evidence level appropriate to the risk. Separate observations, inferences, and unverified claims.
5. Create a new turn. Use `based_on` to identify the exact inputs and include commands, results, diffs, or file references that support the conclusion.
6. Publish only after the turn is complete. Pass the incoming message ID so publication also records it as processed:

```bash
python scripts/collab.py publish \
  --root . \
  --thread-id config-redesign \
  --agent codex \
  --to cursor \
  --document .ai-collab/threads/config-redesign/turns/round-0002-from-codex-challenge.md \
  --requested-action REVISION \
  --in-message-id 550e8400-e29b-41d4-a716-446655440000
```

## Write A Turn

Generate a non-overwriting template:

```bash
python scripts/collab.py new-turn \
  --root . \
  --thread-id config-redesign \
  --author codex \
  --role challenger \
  --kind CHALLENGE \
  --status NEEDS_CHANGES \
  --requested-action REVISION \
  --based-on .ai-collab/threads/config-redesign/turns/round-0001-from-cursor-proposal.md \
  --title "Configuration redesign challenge"
```

Fill the generated sections before publishing. Use stable finding IDs such as `P1-1` across rounds. Do not mark a finding closed from prose alone when code, tests, or runtime state can verify it.

## Reach A Decision Or Close

- Use `AGREED` only when the current scope and evidence support agreement.
- Use `NO_BLOCKING_FINDINGS` when work may proceed but non-blocking items remain.
- Use `CLOSED` only after required acknowledgements, retention decisions, and owned cleanup are complete.
- Preserve final `DECISION` or `CLOSURE` turns when an audit trail is required.
- Remove only temporary artifacts, signals, watchers, hooks, or automations owned by the acting participant.
- Acknowledge a terminal signal without publishing another turn when no response is requested:

```bash
python scripts/collab.py acknowledge \
  --root . \
  --thread-id config-redesign \
  --agent codex \
  --message-id 550e8400-e29b-41d4-a716-446655440000
```

## Adapt To The Tool

Keep the protocol identical across tools. Change only discovery and wake-up behavior:

- Use native skills when the tool supports `SKILL.md`.
- Use project rules or `AGENTS.md` to point other tools to this file.
- Use hooks, file watchers, scheduled jobs, or manual prompts to invoke one scan cycle.
- Keep polling configuration outside this skill and disable it when the thread becomes terminal.

Do not claim continuous monitoring unless a tool-specific watcher or scheduler is actually active.
