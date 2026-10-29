#!/bin/bash
# One-time setup on a new Mac: system deps, Python venv, models (~1 GB), system-audio helper.
# Safe to re-run; finished steps are skipped.
set -e
cd "$(dirname "$0")/app"
say_step() { printf '\n==> %s\n' "$1"; }

[ "$(uname)" = Darwin ] || { echo "macOS only"; exit 1; }
major=$(sw_vers -productVersion | cut -d. -f1)
[ "$major" -ge 13 ] || { echo "macOS 13 or newer needed (system audio capture)"; exit 1; }

say_step "Xcode command line tools (swiftc)"
xcode-select -p >/dev/null 2>&1 || { xcode-select --install; echo "finish that installer, then run this again"; exit 1; }

say_step "Homebrew packages"
command -v brew >/dev/null || { echo 'Homebrew missing. Install it from https://brew.sh, then run this again'; exit 1; }
[ -e "$(brew --prefix)/lib/libspeexdsp.dylib" ] || brew install speexdsp
command -v ffmpeg >/dev/null || echo "(optional) brew install ffmpeg  -- only for the camera commands"

say_step "Python 3.11+"
PY=
for c in python3.13 python3.12 python3.11 python3; do
  if command -v $c >/dev/null && $c -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then PY=$(command -v $c); break; fi
done
[ -n "$PY" ] || { brew install python@3.12; PY="$(brew --prefix)/bin/python3.12"; }
echo "using $PY"
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

say_step "Models (wake word, VAD, ASR)"
./download_models.sh

say_step "System audio helper (echo cancellation)"
[ -x syscap/syscap ] || swiftc -O -o syscap/syscap syscap/main.swift
echo "built syscap/syscap"

say_step "Claude Code"
if command -v claude >/dev/null; then claude --version; else
  echo "claude CLI not found. Install Claude Code (https://claude.com/claude-code) and log in, then run: claude"; fi

say_step "Self-test (recorded clip, nothing sent to Claude)"
.venv/bin/python voice_server.py --wav test_clips/command.wav --dry-run 2>&1 | grep -E "ready|\[wake\]|\[asr\]|\[aec\]|dry-run" || true

cat <<MSG

Done. Start it with:
  "$(cd .. && pwd)/run.sh"
The first start asks for Microphone and Screen Recording (屏幕与系统录音) access for your terminal app.
Allow both, then quit and reopen the terminal. Settings: app/config.toml
MSG
