#!/bin/bash
set -u

APP_TITLE="Email Manager"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="$APP_ROOT/data"
PID_FILE="$DATA_DIR/server.pid"
PORT="8765"

message() {
  printf '[%s] %s\n' "$APP_TITLE" "$1"
}

if [ -f "$PID_FILE" ]; then
  PID="$(head -n 1 "$PID_FILE")"
  if [ -n "$PID" ] && kill "$PID" >/dev/null 2>&1; then
    rm -f "$PID_FILE"
    message "Server stopped."
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
    message "Server stopped."
    exit 0
  fi
fi

if command -v fuser >/dev/null 2>&1; then
  if fuser -k "${PORT}/tcp" >/dev/null 2>&1; then
    message "Server stopped."
    exit 0
  fi
fi

message "No running server was found."
