# Agent Collaboration Skill

[English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md) | [Français](README.fr.md) | [한국어](README.ko.md) | [日本語](README.ja.md)

**Cross-tool, multi-agent collaboration for Codex, Claude Code, Cursor, and other AI coding agents.**

A vendor-neutral skill and file protocol for durable collaboration between AI
coding agents and human participants, using shared Markdown and JSON files
instead of a vendor-specific service. Includes a Python CLI, an optional
automatic wake-up runner, and a local web UI.

Use it when Codex, Claude Code, Cursor, or another coding agent needs to discuss
a design, challenge a proposal, hand off implementation, review work, verify
results, record a decision, or close a multi-round task through a shared
repository.

[Installation](#installation) · [Quick start](#quick-start) · [Automatic collaboration](#automatic-multi-round-collaboration) · [Examples](references/examples.md)

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

Typical use cases:

| Use case | Example |
| --- | --- |
| Architecture discussion | Have one agent propose a design and another challenge its assumptions before deciding. |
| Implementation handoff and code review | Hand off an implementation, track review findings across revisions, then verify the fixes. |
| Independent verification | Ask another agent to reproduce test results or check a claim against repository evidence. |

## Tickets

Write a human requirement as a spec and tickets before dispatch. One round may
become several tickets, and each ticket locks one thing that can be accepted on
its own. Do not start implementation, and do not open a collaboration thread,
without a ticket for that work. Turns stay immutable; ticket status may change.
`collab.py` does not parse tickets. See [Tickets](references/tickets.md). This
repository stores them under `.scratch/<feature-slug>/`, as declared in
[`.docs/agents/issue-tracker.md`](.docs/agents/issue-tracker.md).

## Repository Contents

```text
SKILL.md                       Agent-facing workflow instructions
agents/openai.yaml             Optional Codex UI metadata
references/protocol.md         Canonical collaboration protocol
references/artifact-format.md  Turn, signal, manifest, and state formats
references/tool-adapters.md    Codex, Claude Code, Cursor, and generic adapters
references/examples.md         Example collaboration sequences
references/tickets.md          Spec and ticket format; required before dispatch
.docs/agents/issue-tracker.md  This repository's ticket root
scripts/collab.py              Standard-library protocol CLI
scripts/ui_server.py           Local web UI server
scripts/adapters/             Cursor, Claude Code, and ChatGPT callers of `inbox`
ui/                            Dark local web UI
tests/test_collab.py           CLI and protocol regression tests
tests/test_watch.py            Automatic wake-up (watch) tests
tests/test_inbox.py            Shared inbox query and per-tool wake scripts
tests/test_ui.py               Local web UI registry, views, and single-process startup
```

## Requirements

- a shared Git repository or filesystem;
- Python 3.9 or later for the optional CLI;
- an AI coding tool that can read Markdown and JSON;
- a way to wake agents: the bundled `collab.py watch` command with each tool's
  non-interactive CLI, or a manual prompt, hook, or scheduled job.

The protocol itself does not create background monitoring; `collab.py watch`
is an optional runner that does.

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

Write the spec and the ticket first. Then initialize a collaboration thread from the target repository. The objective names that ticket:

`/path/to/agent-collaboration` below means the location where you installed this
skill. Run these commands from the repository you want the agents to work on.

```bash
python /path/to/agent-collaboration/scripts/collab.py init \
  --root . \
  --thread-id config-redesign \
  --topic "Configuration redesign" \
  --objective "Ticket .ai-collab/tickets/config-redesign/issues/01-migration-plan.md: agree on and verify the migration plan" \
  --coordinator lead \
  --participants lead frontend backend
```

`--topic` is the readable title, summarized in the primary language of the conversation. A Chinese conversation uses a Chinese topic, for example `--topic "配置改造"`, while `--thread-id` stays `config-redesign`. The sample above is English because that conversation is English.

A new collaborator gets one opening prompt that tells them to use this skill on that thread. The human pastes it into their chat, unless the coordinator is a subagent of the current session: then the agent that started the thread delivers it. A collaborator who already uses the skill gets no opening prompt. The human only tells them a new thread was assigned.

Use the project's declared ticket root in that path when it has one.

Create the first immutable turn template:

```bash
python /path/to/agent-collaboration/scripts/collab.py new-turn \
  --root . \
  --thread-id config-redesign \
  --author lead \
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
  --agent lead \
  --to frontend \
  --document .ai-collab/threads/config-redesign/turns/round-0001-from-lead-proposal.md
```

The recipient inspects its inbox with:

```bash
python /path/to/agent-collaboration/scripts/collab.py inspect \
  --root . \
  --agent frontend
```

Only a `NEW` decision should create a response. `EMPTY`, `DUPLICATE`, and
`INVALID` decisions must not create files or modify business code. Terminal
signals must follow their requested acknowledgement or closure action.

## Automatic Multi-Round Collaboration

Without automation, someone must tell each agent "a new message is waiting"
after every turn. The `watch` command does that instead: it scans each agent's
inbox and runs the recipient's non-interactive CLI (`claude -p`, `codex exec`,
`agent -p`, `gemini -p`, ...) for one processing cycle whenever a signal
is waiting. Each reply creates the next signal, so the thread proceeds round by
round until an agent publishes `AGREED`, `CLOSED`, or `CANCELLED`.

```bash
# once per repository: choose the wake command for each participant
python /path/to/agent-collaboration/scripts/collab.py init-wake \
  --root . --bind lead=cursor --bind frontend=claude-code --bind backend=codex

# open the thread and publish the first turn (manually or by asking any agent),
# then let the watcher drive the remaining rounds
python /path/to/agent-collaboration/scripts/collab.py watch \
  --root . --thread-id config-redesign
```

The watcher stops when the thread is finished, when a `STOP` file appears,
or when a round, wake, or retry limit is reached. Agent output is written to
`.ai-collab/logs/`. Participants on different machines can share a thread
through Git with `--agents backend --git-sync`. `--git-sync` publishes `.ai-collab/threads/` only, and only a human may turn it on. Participants do not commit product code unless the human explicitly asks. Review the generated
`.ai-collab/wake.json` permissions before running unattended. See
[Tool Adapters](references/tool-adapters.md#automatic-wake-up-with-collabpy-watch)
for the configuration format and safety limits.

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
inbox              Exit 0 when this agent has mail, otherwise exit 1
publish            Atomically publish a turn signal
acknowledge        Mark the current signal as processed
init-wake          Write a wake config template for the watcher
watch              Wake agents automatically whenever a signal is waiting
ui                 Start the local web UI, or only register this repository
```

## Local Web UI

The web UI stays off until someone runs:

```bash
python scripts/collab.py ui --root .
```

It binds to `127.0.0.1:23451` on Windows, macOS, and Linux. The command probes that port first. A second run registers the current repository with the UI that is already running. Open `http://127.0.0.1:23451/` to see every registered project, its threads, and each participant across projects. The page does not refresh by itself; use 刷新. The first version is dark. Start it only when a human asks for the collaboration background or the web UI.

Run `python scripts/collab.py --help` for command options.

## Validation

Run the regression suite:

```bash
python -m unittest discover -s tests -v
```

The tests cover publication, inspection, acknowledgement, duplicate detection,
terminal signals, immutable rounds, safe repository paths, signal identity,
and unattended multi-round runs of the watcher, including its limits and Git sync.

## Security And Scope

A requested collaboration action is not additional authorization. Agents must
still respect the user's task scope and independently evaluate commands before
execution. Do not place secrets in turns, manifests, signals, or state files.

## License

MIT License. See [LICENSE](LICENSE).
