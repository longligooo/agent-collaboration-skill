# Tool Adapters

Keep `SKILL.md`, the protocol references, and `scripts/collab.py` identical across tools. Adapt only discovery and wake-up behavior.

## Codex

Install the repository as a skill directory under the Codex skills home, commonly:

```text
~/.codex/skills/agent-collaboration/
```

Invoke `$agent-collaboration` explicitly or allow its description to trigger it. Use a heartbeat or automation only when continuous polling is required. Configure that automation to perform one `inspect` cycle and stop it on terminal status or human request.

## Claude Code

Install or link the repository as a user or project skill, commonly:

```text
~/.claude/skills/agent-collaboration/
.claude/skills/agent-collaboration/
```

Use a file-change hook or a manual invocation to react to `.ai-collab/threads/*/signals/to-<participant>.json` for the participant this tool runs. Ignore state, turns authored by the same participant, and unrelated recipients. Disable temporary hooks after closure.

## Cursor

Keep the canonical skill in the repository or another stable local path. Add a Cursor project rule that instructs Cursor to:

1. read `agent-collaboration/SKILL.md` and the referenced protocol;
2. identify itself with the participant name the human chose, not with the tool name;
3. run one inbox inspection when collaboration is requested;
4. follow immutability and idempotency rules;
5. avoid continuous polling unless separately configured.

Do not duplicate the full protocol in the Cursor rule. Point the rule to the canonical skill so updates remain consistent.

## AGENTS.md-Compatible Tools

Add a short project instruction:

```md
For multi-agent discussion, planning, implementation handoff, review, or verification, read `agent-collaboration/SKILL.md` and follow its `.ai-collab` file protocol. Use a stable lowercase agent ID and never modify another participant's turns.
```

## Other Coding Agents

If the tool has no skill system, provide this repository path in the prompt and ask the agent to read `SKILL.md` before acting. A tool needs only:

- shared filesystem access;
- Markdown and JSON support;
- Python 3.9 or later for the optional CLI;
- a manual, hook-based, or scheduled wake-up mechanism.

## Local Web UI

The web UI is off until a human asks to start the collaboration background or the web UI. Run:

```bash
python scripts/collab.py ui --root .
```

The process binds to `127.0.0.1:23451` on Windows, macOS, and Linux. The command probes that port first. When that process is already up, the command registers the current repository and does not start another one. Do not start `ui --serve` directly in place of this command. The UI reads every registered repository on this machine and can show one participant across projects and threads. The page does not refresh by itself; use the 刷新 button.

## Shared Inbox Query

Cursor, Claude Code, and ChatGPT each wake their own conversation differently. All three call the same query:

```bash
python scripts/collab.py inbox --root /path/to/repo --agent lead --format text
```

| Exit code | Meaning |
| --- | --- |
| 0 | A `NEW`, `TERMINAL`, or `INVALID` signal is waiting for this agent. |
| 1 | Nothing to do. Duplicate and empty inboxes exit here. |
| 2 | The command failed. |

`--format json` is the machine-readable report. `--format prompt` prints the instruction for one processing cycle, and prints nothing when the exit code is 1. Pass `--agent` or set `COLLAB_AGENT`. The id must be the participant id in `thread.json`.

### Cursor

Cursor can keep a background script that resumes a conversation:

```bash
CURSOR_CHAT_ID=<chat-id> sh scripts/adapters/cursor-wake.sh /path/to/repo lead
```

`lead` is the participant name. The script calls `inbox --format prompt` for that name and, only when that exits 0, runs `agent -p --resume "$CURSOR_CHAT_ID" --force --trust` with the printed prompt. Without `CURSOR_CHAT_ID` it starts a new headless run instead of resuming the desktop chat. Stop it with `.ai-collab/STOP`. The prompt tells the coordinator not to edit product code, allows every participant to run existing tests, and tells every participant not to commit.

### Claude Code

Claude Code uses the same pattern:

```bash
CLAUDE_SESSION_ID=<session-id> sh scripts/adapters/claude-wake.sh /path/to/repo frontend
```

`frontend` is the participant name. This runs `claude -p --resume "$CLAUDE_SESSION_ID"` only after `inbox` reports mail for that name. Resume works after the original session has finished, not while it is still running.

### ChatGPT desktop

ChatGPT's own scheduled task is the wake-up. Create the task from `scripts/adapters/chatgpt-scheduled-task.md` and attach the local repository. The task runs `inbox --format text` for the participant's own name. A task without the local project attached cannot see the inbox. The desktop chat window is not what receives the wake-up; the task runs against the attached folder.

## Automatic Wake-Up With `collab.py watch`

AI coding tools cannot be pushed awake while idle, but most of them ship a non-interactive mode that runs one prompt and exits. `collab.py watch` uses that mode: it scans every configured agent's inbox and, when a `NEW` or `TERMINAL` signal is waiting, runs that agent's command once with a prompt that names the thread, message, and this skill. The next agent's turn then produces the next signal, so rounds continue without a human forwarding messages.

Generate `.ai-collab/wake.json` and adjust it to the tools and permissions you trust:

```bash
python scripts/collab.py init-wake --root . \
  --bind lead=cursor --bind frontend=claude-code --bind backend=codex
```

```json
{
  "timeout": 1800,
  "agents": {
    "lead": {"tool": "cursor", "command": ["agent", "-p", "--force", "{prompt}"]},
    "frontend": {"tool": "claude-code", "command": ["claude", "-p", "{prompt}", "--permission-mode", "acceptEdits", "--allowedTools", "Bash,Read,Edit,Write,Glob,Grep"]},
    "backend": {"tool": "codex", "command": ["codex", "exec", "--sandbox", "workspace-write", "--ask-for-approval", "never", "{prompt}"]}
  }
}
```

The `tool` field is the only place a product name is required. `lead` can be the coordinator: the wake prompt then forbids product-code edits. Every wake prompt forbids `git commit` and `git push` unless the human explicitly requested that commit.

| Tool | Non-interactive command |
| --- | --- |
| Claude Code | `claude -p "<prompt>"` |
| Codex | `codex exec "<prompt>"` |
| Cursor | `agent -p --force "<prompt>"` (Cursor CLI; older installs name the binary `cursor-agent`) |
| Gemini CLI | `gemini -p "<prompt>"` |
| Aider | `aider --message "<prompt>"` |

Configuration rules:

- Each command is an argument list run without a shell, from the repository root.
- Placeholders: `{prompt}`, `{agent}`, `{thread_id}`, `{message_id}`, `{document}`, `{requested_action}`, `{decision}`, `{root}`, `{skill}`, `{script}`, `{python}`. Unknown placeholders stay literal.
- A top-level `prompt` overrides the built-in prompt; `timeout` sets seconds per run and can be overridden per agent.
- Per-agent `env` adds environment variables. The watcher also sets `COLLAB_WATCHER=1`, `COLLAB_AGENT`, `COLLAB_THREAD_ID`, `COLLAB_MESSAGE_ID`, `COLLAB_ROOT`, and `COLLAB_SCRIPT`.
- `notify_only` participants, typically humans, are notified once per message and are not expected to process it automatically.
- Non-interactive runs cannot answer permission prompts. Grant only the tools the collaboration needs, and keep destructive permissions disabled.

Run the watcher:

```bash
python scripts/collab.py watch --root . --thread-id config-redesign
```

Without `--thread-id` it serves every thread until stopped. Each agent run writes its output to `.ai-collab/logs/<thread-id>/`. The watcher prints one JSON event per line (`WAKE`, `EXITED`, `PROCESSED`, `NO_PROGRESS`, `STUCK`, `ROUND_LIMIT`, `DONE`, and so on).

Safety limits:

| Option | Default | Effect |
| --- | --- | --- |
| `--max-attempts` | 2 | Wakes per message before it is reported as stuck. |
| `--max-rounds` | 20 | Turns per thread before new work is refused. |
| `--max-wakes` | 100 | Agent runs per watcher process. |
| `--timeout` | 1800 | Seconds per agent run. |
| `--idle-timeout` | none | Exit after this many seconds without progress. |

With `--thread-id`, exit code `0` means the thread finished or was stopped, and `3` means it stalled or hit a limit. Create `.ai-collab/STOP` or `.ai-collab/threads/<thread-id>/STOP` to halt automation. A lock file, `.ai-collab/watch.lock`, prevents two watchers from serving the same repository.

### Agents On Different Machines

Run one watcher per machine, restricted to the agents installed there, and share the thread through Git:

```bash
python scripts/collab.py watch --root . --thread-id config-redesign --agents backend --git-sync
```

`--agents` filters by participant name. Only a human may pass `--git-sync`. It commits and pushes `.ai-collab/threads/` before each scan, pulls with rebase, and pushes again after every processed signal. It does not commit product code. A participant must not enable it, and must not run `git commit` or `git push` unless the human explicitly requested that commit.

## Wake-Up Boundaries

The protocol does not create background execution by itself. Configure wake-up separately:

- `collab.py watch` for unattended multi-round collaboration;
- manual prompt for occasional collaboration;
- tool hooks for event-driven reactions inside an open session;
- scheduled `collab.py watch --once` for tools without a long-running watcher;
- CI job for repository-hosted verification.

Every wake-up must run one bounded scan cycle. Stop or remove temporary polling, hooks, and watchers when the thread is terminal or a human requests a stop.
