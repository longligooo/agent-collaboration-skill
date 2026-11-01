#!/usr/bin/env python3
"""One inbox cycle for the bundled Claude/Cursor wake scripts.

The shell scripts retain their existing polling and opt-in launch behavior. This
helper takes no arbitrary command, captures no chat history or output, and only
observes the same vendor invocation those scripts already made.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

import collab
import runtime_activity


def run_once(root: Path, agent: str, tool: str) -> int:
    root = collab.repository_root(str(root))
    agent = collab.resolve_agent_argument(agent)
    if collab.stop_requested(root):
        print("stopped")
        return 0
    report = collab.inbox_report(root, agent)
    if not report["needs_attention"]:
        return 0
    prompt = collab.attention_prompt(root, report)
    item = report["items"][0]
    if tool == "claude":
        command = ["claude", "-p", prompt]
        session = os.environ.get("CLAUDE_SESSION_ID")
        if session:
            command.extend(["--resume", session])
        command.extend(["--permission-mode", "acceptEdits", "--allowedTools", "Bash,Read,Edit,Write,Glob,Grep"])
    elif tool == "cursor":
        command = ["agent", "-p"]
        session = os.environ.get("CURSOR_CHAT_ID")
        if session:
            command.extend(["--resume", session])
        command.extend(["--force", "--trust", prompt])
    else:
        raise collab.CollaborationError("unsupported bundled wake tool")
    # INVALID inbox notifications intentionally have no verified message UUID.
    # Record that absence, rather than inventing a message or guessing task state.
    result = runtime_activity.run_observed(
        command, root=root, thread_id=item["thread_id"], agent=agent,
        message_id=item.get("message_id"), log=None, cwd=Path.cwd(),
        env=dict(os.environ), stdin=None, stdout=None, stderr=None, timeout=None,
        observer="wake-adapter",
    )
    return result.returncode


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool", choices=("claude", "cursor"), required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--agent", required=True)
    args = parser.parse_args(argv)
    try:
        return run_once(Path(args.root), args.agent, args.tool)
    except (collab.CollaborationError, OSError) as exc:
        # Never copy argv, session IDs, environment or source text into errors.
        print(f"wake adapter failed ({type(exc).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
