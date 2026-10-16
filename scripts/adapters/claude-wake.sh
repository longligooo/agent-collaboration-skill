#!/bin/sh
# Poll one participant's inbox and wake it with the Claude Code CLI.
# The participant name is theirs, not the tool name. Set CLAUDE_SESSION_ID to resume that session.
# Usage: sh claude-wake.sh <repo> <participant>
set -eu

INTERVAL="${COLLAB_INTERVAL:-30}"
SESSION_ID="${CLAUDE_SESSION_ID:-}"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
ROOT=$(CDPATH= cd -- "${1:-.}" && pwd)
AGENT=${2:-${COLLAB_AGENT:-}}
if [ -z "$AGENT" ]; then
  printf '%s\n' "usage: claude-wake.sh <repo> <participant>" >&2
  exit 2
fi

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
