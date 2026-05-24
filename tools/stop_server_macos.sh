#!/bin/bash
set -u

APP_TITLE="Email Manager"
APP_NAME="EmailManager"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -f "$APP_ROOT/Resources/app/app.py" ]; then
  DATA_DIR="$HOME/Library/Application Support/$APP_NAME/data"
else
  DATA_DIR="$APP_ROOT/data"
fi

PID_FILE="$DATA_DIR/server.pid"
PORT="8765"

escape_applescript() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

dialog() {
  local msg
  msg="$(escape_applescript "$1")"
  /usr/bin/osascript -e "display dialog \"$msg\" buttons {\"OK\"} default button \"OK\" with title \"$APP_TITLE\"" >/dev/null 2>&1 || true
}

if [ -f "$PID_FILE" ]; then
  PID="$(head -n 1 "$PID_FILE")"
  if [ -n "$PID" ] && kill "$PID" >/dev/null 2>&1; then
    rm -f "$PID_FILE"
    dialog "Email Manager server stopped."
    exit 0
  fi
  rm -f "$PID_FILE"
fi

if command -v lsof >/dev/null 2>&1; then
  PIDS="$(lsof -ti tcp:$PORT -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "$PIDS" ]; then
    echo "$PIDS" | while IFS= read -r PID; do
      [ -n "$PID" ] && kill "$PID" >/dev/null 2>&1 || true
    done
    dialog "Email Manager server stopped."
    exit 0
  fi
fi

dialog "No running Email Manager server was found."
