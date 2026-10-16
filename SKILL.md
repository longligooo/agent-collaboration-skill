---
name: agent-collaboration
description: Coordinate durable, file-based collaboration among AI coding tools and human participants. Use when Codex, Claude Code, Cursor, or another agent must discuss a design, challenge assumptions, hand off implementation, review code or plans, verify results, record a decision, or close a multi-round task without overwriting another participant's work. Write a human requirement as a spec and tickets before dispatch. Do not implement or open a collaboration thread without a ticket that locks that one acceptance.
---

# Agent Collaboration

Coordinate agents through immutable Markdown turns and small JSON signals stored in the shared repository. Treat repository artifacts and current code as facts; treat chat context and signals only as notifications.

## Load The Protocol

Read [references/protocol.md](references/protocol.md) before creating or processing a collaboration thread. Read [references/tickets.md](references/tickets.md) before writing a spec, writing a ticket, dispatching work, or opening a thread. Read [references/artifact-format.md](references/artifact-format.md) when writing a turn. Read [references/tool-adapters.md](references/tool-adapters.md) only when configuring a coding tool to discover this skill or monitor signals.

When a turn's task comes from a ticket, read that ticket and the `spec.md` it cites before processing the turn. `scripts/collab.py` does not parse tickets.

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
- Participant ids are names the human chooses. Do not treat `cursor`, `claude`, or `gpt` as identities. Record which tool runs a participant only in that participant's wake command.
- A coordinator exists only when the human passes `--coordinator`. That participant must not create, modify, or delete product code, configuration, or test files. They may run the project's existing tests and write their own turns, signals, and state file. With no `--coordinator`, no participant has that restriction.
- Do not run `git commit` or `git push` unless the human explicitly requested that commit in the current instruction. Publishing a turn is not a commit.
- Write the human's requirements as a spec and tickets before dispatch. One requirement round may become several tickets. Each ticket locks exactly one thing that can be accepted on its own.
- Do not start implementation, and do not run `collab.py init`, unless a ticket already covers that work. Put the ticket path in the thread objective.
- Do not parse tickets into signals or the turn state machine. Turns stay immutable. Ticket status may change. Ticket comments are append-only.
- Do not move a resolved ticket back to in progress. A new problem gets a new ticket number.
- A ticket stays blocked until every ticket named in `Blocked by` is `resolved`. `wontfix` does not lift that block.

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

1. Confirm a ticket already covers this objective. If the human just stated a requirement and no ticket covers it, write the spec and the tickets first, then stop. Do not initialize a thread, and do not start implementation, without that ticket. A thread is a new association: do not reuse an older thread just because an older ticket looks related.
2. Choose a stable lowercase `thread_id` and participant IDs. The objective names the ticket path. The default is one in-progress ticket and one thread. Parallel frontend and backend work on the same ticket uses two threads, and `Owner` names both. Pass `--coordinator` only when the human names one.
3. Initialize the workspace:

```bash
python scripts/collab.py init \
  --root . \
  --thread-id config-redesign \
  --topic "Configuration redesign" \
  --objective "Ticket .ai-collab/tickets/config-redesign/issues/01-migration-plan.md: agree on and verify the configuration migration plan" \
  --coordinator lead \
  --participants lead frontend backend
```

Use the project's declared ticket root in that path when it has one. This repository declares `.scratch/<feature-slug>/` in `.docs/agents/issue-tracker.md`. The `.ai-collab/tickets/` path above is only the fallback.

`lead`, `frontend`, and `backend` are ordinary names. Which tool runs each one belongs in the wake binding, for example `lead=cursor` and `frontend=claude-code`.

4. Create the first turn with `new-turn`, fill its evidence and requested outcome, then publish it. Dispatch with `DECISION`. Accept with `REVIEW`. Set the ticket to `resolved` only after that review passes.
5. Keep unrelated objectives in separate threads. One ticket stays one acceptance even when the thread splits for parallel work.

## Process An Incoming Turn

When the task comes from a ticket, read that ticket and the `spec.md` it cites before step 1. The spec is the `spec.md` in the same feature directory. Treat spec decision numbers, ticket-local decision numbers, and `What to build` as the requirement. Finding IDs stay on turns. If the requested action implements work and no ticket covers it, do not implement.

1. Inspect the inbox:

```bash
python scripts/collab.py inspect --root . --agent frontend
```

2. Act only on a `NEW` decision. For `TERMINAL`, follow the terminal action and acknowledge it. For `EMPTY`, `DUPLICATE`, or `INVALID`, do not create a response. If the manifest's `coordinator` is your id, do the coordination in a turn and do not edit product code. If `coordinator` is null, that restriction applies to nobody. Running the project's tests is allowed for acceptance.
3. Read the referenced turn and every relevant source artifact. Continue earlier finding IDs until each is explicitly closed, deferred with owner and reason, or still open.
4. Perform the requested action at the evidence level appropriate to the risk. Separate observations, inferences, and unverified claims.
5. Create a new turn. Use `based_on` to identify the exact inputs and include commands, results, diffs, or file references that support the conclusion.
6. Publish only after the turn is complete. Pass the incoming message ID so publication also records it as processed:

```bash
python scripts/collab.py publish \
  --root . \
  --thread-id config-redesign \
  --agent frontend \
  --to lead \
  --document .ai-collab/threads/config-redesign/turns/round-0002-from-frontend-challenge.md \
  --requested-action REVISE \
  --in-message-id 550e8400-e29b-41d4-a716-446655440000
```

## Write A Turn

Generate a non-overwriting template:

```bash
python scripts/collab.py new-turn \
  --root . \
  --thread-id config-redesign \
  --author frontend \
  --role challenger \
  --kind CHALLENGE \
  --status NEEDS_CHANGES \
  --requested-action REVISE \
  --based-on .ai-collab/threads/config-redesign/turns/round-0001-from-lead-proposal.md \
  --title "Configuration redesign challenge"
```

Fill the generated sections before publishing. Use stable finding IDs such as `P1-1` across rounds. Do not mark a finding closed from prose alone when code, tests, or runtime state can verify it.

## Tickets

Human requirements become a spec and tickets before anyone is dispatched. Read [references/tickets.md](references/tickets.md) for the file layout and the exact header.

1. Find the ticket root. Use the directory the project's agent docs already declare, including that declaration's layout. In this repository the declaration is `.docs/agents/issue-tracker.md` and the root is `.scratch/<feature-slug>/`. If nothing is declared, do not treat a directory named `issues` as the tracker. Write `.ai-collab/tickets/<feature-slug>/` next to `threads/`, never inside a thread directory.
2. One feature is one directory. `spec.md` is the spec. Tickets are `issues/<NN>-<slug>.md`, numbered from `01`, two digits, English kebab-case. The number in the filename matches the number in the title.
3. Split one requirement round into as many tickets as it needs. Each ticket locks one independently acceptable outcome.
4. Do not implement, and do not open a collaboration thread, until that ticket exists.
5. Open a thread only as a new association. Do not infer one from an older ticket. Put the ticket path in the objective. One ticket may name two threads in `Owner` when frontend and backend proceed in parallel.
6. Dispatch with a `DECISION` turn. Accept with a `REVIEW` turn. Set the ticket `Status` to `resolved` only after that review passes.
7. Append comments. Do not edit old comments, replace `What to build`, or reopen a resolved ticket.

`collab.py` does not read these files.

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
  --agent frontend \
  --message-id 550e8400-e29b-41d4-a716-446655440000
```

## Check Whether You Have Mail

Every scheduler, background script, and scheduled task decides whether to wake you by running this command and no other query:

```bash
python scripts/collab.py inbox --root . --agent frontend --format text
```

Exit `0` means a `NEW`, `TERMINAL`, or `INVALID` signal is waiting. Exit `1` means there is nothing to do; do not write files. Exit `2` means the command itself failed. `--format json` prints the same report for a program, including `is_coordinator`, `may_edit_code`, and `may_commit`. `--format prompt` prints the one-cycle instruction a wake script passes into the conversation. `--agent` is the participant's own name and defaults to `COLLAB_AGENT`.

The ready-made callers take that name as an argument. The script selects the tool; the argument selects the person:

- Cursor tool: `sh scripts/adapters/cursor-wake.sh /path/to/repo lead`. Set `CURSOR_CHAT_ID` to resume that chat.
- Claude Code tool: `sh scripts/adapters/claude-wake.sh /path/to/repo frontend`. Set `CLAUDE_SESSION_ID` to resume that session.
- ChatGPT scheduled task: create the task from `scripts/adapters/chatgpt-scheduled-task.md`, with this repository attached as a local project. Put the participant's name in the task, not the product name.

Ask the user to leave the computer awake. A background script stops when `.ai-collab/STOP` exists. Do not create a second scheduler for an agent that already has one. When one of these callers starts you, handle the single listed message and exit.

## Local Web UI

Do not start the web UI during an ordinary turn. Start it only when the human explicitly asks to start the collaboration background or the web UI. Matching requests include "启动协作后台", "启动 web ui", and "打开协作界面".

```bash
python scripts/collab.py ui --root .
```

This listens on `127.0.0.1:23451`. The same command is used on Windows, macOS, and Linux. It probes that port first. If a UI is already running, the command only registers the current repository and does not start a second process. Do not skip the probe and start `ui --serve` by hand. Tell the human to open `http://127.0.0.1:23451/`. The page shows every repository registered on this machine, by project, by thread, and by participant, including a participant who works in more than one project. It does not refresh by itself. Use the 刷新 button.

When the human asks to start the collaboration background, run this command and then start the wake script or `watch` they asked for.

## Run Rounds Automatically

`scripts/collab.py watch` removes manual forwarding. It scans inboxes and, whenever a `NEW` or `TERMINAL` signal is waiting, starts the recipient's non-interactive CLI (`claude -p`, `codex exec`, `agent -p`, and so on) for one processing cycle. Commands live in `.ai-collab/wake.json`; `init-wake` writes a template.

When a user asks for automatic collaboration:

1. Confirm the ticket exists, then create the thread and publish the first turn as usual.
2. Ensure `.ai-collab/wake.json` lists every automated participant:

```bash
python scripts/collab.py init-wake --root . \
  --bind lead=cursor --bind frontend=claude-code --bind backend=codex
```

3. Start the watcher in a terminal that outlives your session, for example `tmux` or `nohup`, or ask the user to run it:

```bash
python scripts/collab.py watch --root . --thread-id config-redesign
```

The watcher exits when the thread reaches a terminal status and every automated participant has processed its signal. It stops early on a `.ai-collab/STOP` or `.ai-collab/threads/<thread-id>/STOP` file, on the round or wake limits, or when a participant fails to process its signal after the allowed attempts.

When the watcher starts you (`COLLAB_WATCHER=1` is set):

- Run exactly one processing cycle for the named thread and message, then exit.
- Do not wait for or ask the user anything; nobody is reading the session.
- End every `NEW` cycle by publishing a turn with `--in-message-id`, and every `TERMINAL` cycle by its requested action or `acknowledge`. A cycle that publishes nothing is retried and then reported as stuck.
- Address each turn to the participant who must act next. Publish `AGREED`, `CLOSED`, or `CANCELLED` when the objective is met or cannot be met, so the thread terminates.
- When a human decision is required, publish `NEEDS_INPUT` to the coordinator or a human participant instead of guessing.
- Never start another watcher, loop, or scheduler.

## Adapt To The Tool

Keep the protocol identical across tools. Change only discovery and wake-up behavior:

- Use native skills when the tool supports `SKILL.md`.
- Use project rules or `AGENTS.md` to point other tools to this file.
- Prefer `collab.py watch` for unattended multi-round work; use hooks, scheduled jobs, or manual prompts only when a tool has no non-interactive CLI.
- Keep polling configuration outside this skill and disable it when the thread becomes terminal.

Do not claim continuous monitoring unless a tool-specific watcher or scheduler is actually active.
