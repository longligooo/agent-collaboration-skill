#!/bin/sh
# Poll this agent's inbox and wake the Cursor conversation when mail is waiting.
# The query is always `collab.py inbox`. Set CURSOR_CHAT_ID to resume that chat.
# Usage, from the target repository: sh /path/to/scripts/adapters/cursor-wake.sh
set -eu

AGENT=cursor
INTERVAL="${COLLAB_INTERVAL:-30}"
CHAT_ID="${CURSOR_CHAT_ID:-}"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
ROOT=$(CDPATH= cd -- "${1:-.}" && pwd)

wake() {
  prompt=$1
  if [ -n "$CHAT_ID" ]; then
    agent -p --resume "$CHAT_ID" --force --trust "$prompt" || true
  else
    agent -p --force --trust "$prompt" || true
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
