#!/bin/sh
# Poll this agent's inbox and wake the Claude Code conversation when mail is waiting.
# The query is always `collab.py inbox`. Set CLAUDE_SESSION_ID to resume that session.
# Usage, from the target repository: sh /path/to/scripts/adapters/claude-wake.sh
set -eu

AGENT=claude-code
INTERVAL="${COLLAB_INTERVAL:-30}"
SESSION_ID="${CLAUDE_SESSION_ID:-}"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
ROOT=$(CDPATH= cd -- "${1:-.}" && pwd)

wake() {
  prompt=$1
  if [ -n "$SESSION_ID" ]; then
    claude -p "$prompt" --resume "$SESSION_ID" --permission-mode acceptEdits --allowedTools "Bash,Read,Edit,Write,Glob,Grep" || true
  else
    claude -p "$prompt" --permission-mode acceptEdits --allowedTools "Bash,Read,Edit,Write,Glob,Grep" || true
  fi
}

run_once() {
  if [ -f "$ROOT/.ai-collab/STOP" ]; then
    printf '%s\n' "stopped"
    exit 0
  fi
  if prompt=$(python3 "$SCRIPT_DIR/collab.py" inbox --root "$ROOT" --agent "$AGENT" --format prompt); then
    wake "$prompt"
  fi
}

if [ "${COLLAB_ONCE:-}" = "1" ]; then
  run_once
  exit 0
fi

while :; do
  run_once
  sleep "$INTERVAL"
done
