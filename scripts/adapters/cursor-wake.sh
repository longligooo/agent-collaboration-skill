#!/bin/sh
# Poll one participant's inbox and wake it with the Cursor CLI.
# The participant name is theirs, not the tool name. Set CURSOR_CHAT_ID to resume that chat.
# Usage: sh cursor-wake.sh <repo> <participant>
set -eu

INTERVAL="${COLLAB_INTERVAL:-30}"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
ROOT=$(CDPATH= cd -- "${1:-.}" && pwd)
AGENT=${2:-${COLLAB_AGENT:-}}
if [ -z "$AGENT" ]; then
  printf '%s\n' "usage: cursor-wake.sh <repo> <participant>" >&2
  exit 2
fi

run_once() {
  if [ -f "$ROOT/.ai-collab/STOP" ]; then
    printf '%s\n' "stopped"
    exit 0
  fi
  python3 "$SCRIPT_DIR/wake_adapter.py" --tool cursor --root "$ROOT" --agent "$AGENT" || true
}

if [ "${COLLAB_ONCE:-}" = "1" ]; then
  run_once
  exit 0
fi

while :; do
  run_once
  sleep "$INTERVAL"
done
