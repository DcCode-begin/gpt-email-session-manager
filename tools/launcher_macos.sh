#!/bin/bash
set -u

APP_TITLE="Email Manager"
APP_NAME="EmailManager"
URL="http://127.0.0.1:8765/"
STATUS_URL="${URL}api/status"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -f "$APP_ROOT/Resources/app/app.py" ]; then
  APP_DIR="$APP_ROOT/Resources/app"
  DATA_DIR="$HOME/Library/Application Support/$APP_NAME/data"
  VENV_DIR="$DATA_DIR/.venv"
elif [ -f "$APP_ROOT/app.py" ]; then
  APP_DIR="$APP_ROOT"
  DATA_DIR="$APP_DIR/data"
  VENV_DIR="$APP_DIR/.venv"
else
  APP_DIR="$APP_ROOT"
  DATA_DIR="$APP_DIR/data"
  VENV_DIR="$APP_DIR/.venv"
fi

LOG_DIR="$DATA_DIR/logs"
PID_FILE="$DATA_DIR/server.pid"
VENV_PYTHON="$VENV_DIR/bin/python"

escape_applescript() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

dialog() {
  local msg
  msg="$(escape_applescript "$1")"
  /usr/bin/osascript -e "display dialog \"$msg\" buttons {\"OK\"} default button \"OK\" with title \"$APP_TITLE\"" >/dev/null 2>&1 || true
}

notify() {
  local msg
  msg="$(escape_applescript "$1")"
  /usr/bin/osascript -e "display notification \"$msg\" with title \"$APP_TITLE\"" >/dev/null 2>&1 || true
}

test_server() {
  /usr/bin/curl -fsS --max-time 2 "$STATUS_URL" >/dev/null 2>&1
}

find_python() {
  local candidate
  for candidate in python3 python /usr/bin/python3; do
    if command -v "$candidate" >/dev/null 2>&1 || [ -x "$candidate" ]; then
      candidate="$(command -v "$candidate" 2>/dev/null || printf '%s' "$candidate")"
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
    notify "First run: creating the local runtime. Please wait."
    if ! "$base_python" -m venv "$VENV_DIR" >>"$LOG_DIR/install.log" 2>&1; then
      dialog "Failed to create the Python runtime. Install Python 3.10+. Log: $LOG_DIR/install.log"
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
  [ -d "/Applications/Google Chrome.app" ] && return 0
  [ -d "/Applications/Microsoft Edge.app" ] && return 0
  [ -d "/Applications/Chromium.app" ] && return 0
  return 1
}

ensure_dependencies() {
  if ! test_playwright; then
    notify "First run: installing dependencies. Please wait."
    if ! "$VENV_PYTHON" -m pip install --retries 2 --timeout 15 -r "$APP_DIR/requirements.txt" >>"$LOG_DIR/install.log" 2>&1; then
      dialog "Failed to install Python dependencies. Log: $LOG_DIR/install.log"
      exit 1
    fi
  fi

  if ! test_local_browser; then
    notify "Chrome/Edge was not found. Installing fallback Chromium."
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
  dialog "app.py was not found. Keep the launcher in the project root and keep the tools directory intact."
  exit 1
fi

if test_server; then
  /usr/bin/open "$URL"
  exit 0
fi

PYTHON="$(find_python || true)"
if [ -z "$PYTHON" ]; then
  dialog "Python 3.10+ was not found. Install Python 3, then open Email Manager again."
  /usr/bin/open "https://www.python.org/downloads/macos/" >/dev/null 2>&1 || true
  exit 1
fi

ensure_venv "$PYTHON"
ensure_dependencies
start_server

for _ in $(seq 1 30); do
  sleep 1
  if test_server; then
    /usr/bin/open "$URL"
    exit 0
  fi
done

dialog "Startup timed out. Log: $LOG_DIR/server.err.log"
exit 1
