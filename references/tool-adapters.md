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

## Wake-Up Boundaries

The protocol does not create background execution by itself. Configure wake-up separately:

- manual prompt for occasional collaboration;
- file watcher for event-driven local tools;
- scheduled scan for tools without watchers;
- CI job for repository-hosted verification.

Every wake-up must run one bounded scan cycle. Stop or remove temporary polling, hooks, and watchers when the thread is terminal or a human requests a stop.
