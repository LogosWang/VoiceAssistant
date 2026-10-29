#!/bin/bash
# Starts Carnot on this Mac's microphone. Extra args go to voice_server.py (--dry-run, --save-dir DIR).
set -e
cd "$(dirname "$0")/app"
[ -x .venv/bin/python ] && [ -x syscap/syscap ] || { echo "not installed yet, run ../install.sh"; exit 1; }
pkill -f "$(pwd)/voice_server.py" 2>/dev/null && sleep 1 || true   # one instance only
exec .venv/bin/python "$(pwd)/voice_server.py" --mic "$@"
