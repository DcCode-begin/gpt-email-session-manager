#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
exec /bin/bash "$DIR/tools/stop_server_macos.sh"
