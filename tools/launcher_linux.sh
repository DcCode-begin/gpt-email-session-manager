#!/bin/bash
set -u

APP_TITLE="Email Manager"
URL="http://127.0.0.1:8765/"
STATUS_URL="${URL}api/status"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_DIR="$APP_ROOT"
DATA_DIR="$APP_DIR/data"
VENV_DIR="$APP_DIR/.venv"
LOG_DIR="$DATA_DIR/logs"
PID_FILE="$DATA_DIR/server.pid"
VENV_PYTHON="$VENV_DIR/bin/python"

message() {
  printf '[%s] %s\n' "$APP_TITLE" "$1"
}

test_server() {
  curl -fsS --max-time 2 "$STATUS_URL" >/dev/null 2>&1
}

open_url() {
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 || true
  elif command -v sensible-browser >/dev/null 2>&1; then
    sensible-browser "$URL" >/dev/null 2>&1 || true
  else
    message "Open this URL in your browser: $URL"
  fi
}

find_python() {
  local candidate
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      candidate="$(command -v "$candidate")"
      if "$candidate" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
      then
        printf '%s\n' "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

ensure_venv() {
  local base_python="$1"
  if [ ! -x "$VENV_PYTHON" ]; then
    message "First run: creating local Python runtime..."
    if ! "$base_python" -m venv "$VENV_DIR" >>"$LOG_DIR/install.log" 2>&1; then
      message "Failed to create Python runtime. Install python3-venv and Python 3.10+. Log: $LOG_DIR/install.log"
      exit 1
    fi
  fi

  if ! "$VENV_PYTHON" -m pip --version >/dev/null 2>&1; then
    "$VENV_PYTHON" -m ensurepip --upgrade >>"$LOG_DIR/install.log" 2>&1 || true
  fi
}

test_playwright() {
  "$VENV_PYTHON" -c "import playwright" >/dev/null 2>&1
}

test_local_browser() {
  if [ -n "${EMAIL_MANAGER_BROWSER:-}" ] && [ -x "$EMAIL_MANAGER_BROWSER" ]; then
    return 0
  fi
  command -v google-chrome >/dev/null 2>&1 && return 0
  command -v microsoft-edge >/dev/null 2>&1 && return 0
  command -v chromium >/dev/null 2>&1 && return 0
  command -v chromium-browser >/dev/null 2>&1 && return 0
  return 1
}

ensure_dependencies() {
  if ! test_playwright; then
    message "First run: installing dependencies..."
    if ! "$VENV_PYTHON" -m pip install --retries 2 --timeout 15 -r "$APP_DIR/requirements.txt" >>"$LOG_DIR/install.log" 2>&1; then
      message "Failed to install Python dependencies. Log: $LOG_DIR/install.log"
      exit 1
    fi
  fi

  if ! test_local_browser; then
    message "Chrome/Edge/Chromium was not found. Installing fallback Chromium..."
    "$VENV_PYTHON" -m playwright install chromium >>"$LOG_DIR/install.log" 2>&1 || true
  fi
}

start_server() {
  export EMAIL_MANAGER_ROOT="$APP_DIR"
  export EMAIL_MANAGER_DATA_DIR="$DATA_DIR"

  cd "$APP_DIR" || exit 1
  printf 'Starting at %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" >"$LOG_DIR/server.out.log"
  printf 'Starting at %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" >"$LOG_DIR/server.err.log"
  nohup "$VENV_PYTHON" "$APP_DIR/app.py" >>"$LOG_DIR/server.out.log" 2>>"$LOG_DIR/server.err.log" &
  echo $! >"$PID_FILE"
}

mkdir -p "$DATA_DIR" "$LOG_DIR"

if [ ! -f "$APP_DIR/app.py" ]; then
  message "app.py was not found. Keep this script in the project root."
  exit 1
fi

if test_server; then
  open_url
  exit 0
fi

PYTHON="$(find_python || true)"
if [ -z "$PYTHON" ]; then
  message "Python 3.10+ was not found. Install Python 3.10+ and try again."
  exit 1
fi

ensure_venv "$PYTHON"
ensure_dependencies
start_server

for _ in $(seq 1 30); do
  sleep 1
  if test_server; then
    message "Started: $URL"
    open_url
    exit 0
  fi
done

message "Startup timed out. Log: $LOG_DIR/server.err.log"
exit 1
