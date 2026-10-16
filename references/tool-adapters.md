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

Use a file-change hook or a manual invocation to react to `.ai-collab/threads/*/signals/to-claude-code.json`. Ignore state, turns authored by the same agent, and unrelated recipients. Disable temporary hooks after closure.

## Cursor

Keep the canonical skill in the repository or another stable local path. Add a Cursor project rule that instructs Cursor to:

1. read `agent-collaboration/SKILL.md` and the referenced protocol;
2. identify itself with a stable agent ID such as `cursor`;
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

## Automatic Wake-Up With `collab.py watch`

AI coding tools cannot be pushed awake while idle, but most of them ship a non-interactive mode that runs one prompt and exits. `collab.py watch` uses that mode: it scans every configured agent's inbox and, when a `NEW` or `TERMINAL` signal is waiting, runs that agent's command once with a prompt that names the thread, message, and this skill. The next agent's turn then produces the next signal, so rounds continue without a human forwarding messages.

Generate `.ai-collab/wake.json` and adjust it to the tools and permissions you trust:

```bash
python scripts/collab.py init-wake --root . --agents cursor codex claude-code
```

```json
{
  "timeout": 1800,
  "agents": {
    "claude-code": {"command": ["claude", "-p", "{prompt}", "--permission-mode", "acceptEdits", "--allowedTools", "Bash,Read,Edit,Write,Glob,Grep"]},
    "codex": {"command": ["codex", "exec", "--full-auto", "{prompt}"]},
    "cursor": {"command": ["cursor-agent", "-p", "--force", "{prompt}"]},
    "human": {"notify_only": true, "command": ["notify-send", "Collaboration", "{thread_id} needs you: {document}"]}
  }
}
```

| Tool | Non-interactive command |
| --- | --- |
| Claude Code | `claude -p "<prompt>"` |
| Codex | `codex exec "<prompt>"` |
| Cursor | `cursor-agent -p "<prompt>"` (Cursor CLI) |
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
python scripts/collab.py watch --root . --thread-id config-redesign --agents codex --git-sync
```

`--git-sync` commits and pushes local changes under `.ai-collab/threads/` before each scan, pulls with rebase, and pushes again after every processed signal. Business-code changes made by agents are not committed by the watcher.

## Wake-Up Boundaries

The protocol does not create background execution by itself. Configure wake-up separately:

- `collab.py watch` for unattended multi-round collaboration;
- manual prompt for occasional collaboration;
- tool hooks for event-driven reactions inside an open session;
- scheduled `collab.py watch --once` for tools without a long-running watcher;
- CI job for repository-hosted verification.

Every wake-up must run one bounded scan cycle. Stop or remove temporary polling, hooks, and watchers when the thread is terminal or a human requests a stop.
