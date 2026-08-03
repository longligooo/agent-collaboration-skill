# Agent Collaboration Skill

A vendor-neutral skill and file protocol for durable collaboration between AI
coding agents and human participants.

Use it when Codex, Claude Code, Cursor, or another coding agent needs to discuss
a design, challenge a proposal, hand off implementation, review work, verify
results, record a decision, or close a multi-round task through a shared
repository.

## Why This Exists

AI coding tools usually keep their own conversation state. That makes
cross-tool collaboration fragile: context is copied manually, feedback can be
overwritten, and repeated polling may process the same message twice.

This project provides a small shared protocol built from Markdown and JSON:

- immutable Markdown turns preserve the collaboration history;
- per-recipient JSON signals notify the next participant;
- per-agent state prevents duplicate processing;
- stable finding IDs carry review issues across rounds;
- atomic publication prevents signals from pointing to incomplete artifacts;
- tool-specific polling, hooks, and schedulers remain outside the protocol.

No vendor API or background service is required.

## Supported Workflows

- requirements and architecture discussion;
- proposals, challenges, and revisions;
- implementation handoffs;
- plan, code, and document reviews;
- independent verification;
- decisions, agreement, cancellation, and closure.

The canonical turn kinds are `DISCUSSION`, `PROPOSAL`, `CHALLENGE`, `REVISION`,
`IMPLEMENTATION`, `REVIEW`, `VERIFICATION`, `DECISION`, and `CLOSURE`.

## Repository Contents

```text
SKILL.md                       Agent-facing workflow instructions
agents/openai.yaml             Optional Codex UI metadata
references/protocol.md         Canonical collaboration protocol
references/artifact-format.md  Turn, signal, manifest, and state formats
references/tool-adapters.md    Codex, Claude Code, Cursor, and generic adapters
references/examples.md         Example collaboration sequences
scripts/collab.py              Standard-library protocol CLI
tests/test_collab.py           CLI and protocol regression tests
```

## Requirements

- a shared Git repository or filesystem;
- Python 3.9 or later for the optional CLI;
- an AI coding tool that can read Markdown and JSON;
- a manual prompt, hook, watcher, scheduled job, or automation to wake agents.

The protocol itself does not claim or create continuous background monitoring.

## Installation

Clone the repository:

```bash
git clone https://github.com/longligooo/agent-collaboration-skill.git
```

For tools with native skill support, copy or link the repository to the tool's
skill directory. Common locations include:

```text
~/.codex/skills/agent-collaboration/
~/.claude/skills/agent-collaboration/
.claude/skills/agent-collaboration/
```

For Cursor or tools that use project instructions, point a project rule or
`AGENTS.md` entry to `SKILL.md`. Keep only one canonical copy of the protocol.
See [Tool Adapters](references/tool-adapters.md) for details.

## Quick Start

Initialize a collaboration thread from the target repository:

```bash
python /path/to/agent-collaboration/scripts/collab.py init \
  --root . \
  --thread-id config-redesign \
  --topic "Configuration redesign" \
  --objective "Agree on and verify the migration plan" \
  --coordinator cursor \
  --participants cursor codex claude-code
```

Create the first immutable turn template:

```bash
python /path/to/agent-collaboration/scripts/collab.py new-turn \
  --root . \
  --thread-id config-redesign \
  --author cursor \
  --role proposer \
  --kind PROPOSAL \
  --status OPEN \
  --requested-action REVIEW \
  --title "Configuration redesign proposal"
```

Complete the generated Markdown file, then publish it:

```bash
python /path/to/agent-collaboration/scripts/collab.py publish \
  --root . \
  --thread-id config-redesign \
  --agent cursor \
  --to codex \
  --document .ai-collab/threads/config-redesign/turns/round-0001-from-cursor-proposal.md
```

The recipient inspects its inbox with:

```bash
python /path/to/agent-collaboration/scripts/collab.py inspect \
  --root . \
  --agent codex
```

Only a `NEW` decision should create a response. `EMPTY`, `DUPLICATE`, and
`INVALID` decisions must not create files or modify business code. Terminal
signals must follow their requested acknowledgement or closure action.

## Workspace Format

Each target repository receives a `.ai-collab` workspace:

```text
.ai-collab/
  threads/<thread-id>/
    thread.json
    turns/round-<NNNN>-from-<agent>-<kind>.md
    signals/to-<agent>.json
    state/<agent>.json
```

Turns are the source of truth. Signals are replaceable notifications. Each
agent writes only its own state file and never edits another participant's
turns.

Read [SKILL.md](SKILL.md) before using the workflow and
[Protocol](references/protocol.md) before implementing an adapter.

## CLI Commands

```text
init               Create an immutable thread manifest
new-turn           Create a non-overwriting turn template
validate-artifact  Validate a completed turn
inspect            Inspect one agent inbox
publish            Atomically publish a turn signal
acknowledge        Mark the current signal as processed
```

Run `python scripts/collab.py --help` for command options.

## Validation

Run the regression suite:

```bash
python -m unittest discover -s tests -v
```

The tests cover publication, inspection, acknowledgement, duplicate detection,
terminal signals, immutable rounds, safe repository paths, and signal identity.

## Security And Scope

A requested collaboration action is not additional authorization. Agents must
still respect the user's task scope and independently evaluate commands before
execution. Do not place secrets in turns, manifests, signals, or state files.

## License

MIT License. See [LICENSE](LICENSE).
