#!/bin/bash
# One-step install on a Mac: dependencies, voice service, Carnot.app, VS Code extension, shared
# memory. Safe to run again; finished steps are skipped. Nothing is started at the end.
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
step() { printf '\n==> %s\n' "$1"; }
die() { printf '\n%s\n' "$1"; exit 1; }

[ "$(uname)" = Darwin ] || die "Carnot runs on macOS only."
[ "$(sw_vers -productVersion | cut -d. -f1)" -ge 13 ] || die "macOS 13 or newer is needed."

step "Xcode command line tools"
xcode-select -p >/dev/null 2>&1 || { xcode-select --install; die "Finish that installer, then run ./install.sh again."; }

step "Homebrew packages"
if ! command -v brew >/dev/null; then
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && eval "$("$b" shellenv)"; done
fi
command -v brew >/dev/null || die "Homebrew is needed. Install it from https://brew.sh, then run ./install.sh again."
[ -e "$(brew --prefix)/lib/libspeexdsp.dylib" ] || brew install speexdsp
command -v node >/dev/null || brew install node
command -v ffmpeg >/dev/null || echo "(optional) brew install ffmpeg, for the camera commands"

step "Python 3.11+ and packages"
PY=
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then PY="$(command -v "$c")"; break; fi
done
[ -n "$PY" ] || { brew install python@3.12; PY="$(brew --prefix)/bin/python3.12"; }
echo "using $PY"
cd "$ROOT/server"
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

step "Speech models (about 1 GB, first time only)"
./download_models.sh

step "Claude Code"
if ! command -v claude >/dev/null; then
  npm install -g @anthropic-ai/claude-code
  echo "Installed. Sign in once with: claude"
else
  claude --version
fi

step "Shared memory and skills"
.venv/bin/python carnot_agent.py seed
.venv/bin/python -c "import shared; shared.ensure_persona(); print(len(shared.list_skills()), 'skills shared; memory in', shared.MEMORY_DIR)"

step "Self-test on a recorded clip (no microphone, nothing sent to any AI)"
TEST="$(mktemp -d)/config.toml"
sed 's/^enabled = true/enabled = false/' config.toml > "$TEST"
OUT="$(.venv/bin/python voice_server.py --wav test_clips/command.wav --dry-run --config "$TEST" 2>&1 || true)"
echo "$OUT" | grep -E "\[wake\]|\[asr\]" || true
echo "$OUT" | grep -q "\[wake\]" && echo "self-test passed" || echo "WARNING: the wake word wasn't detected in the self-test clip"

step "Carnot.app"
"$ROOT/app/build.sh"

step "VS Code extension"
if command -v code >/dev/null || [ -x "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code" ]; then
  "$ROOT/vscode/install.sh"
else
  (cd "$ROOT/server" && mkdir -p "${CARNOT_HOME:-$HOME/.carnot}" && pwd > "${CARNOT_HOME:-$HOME/.carnot}/server_dir")
  echo "VS Code not found, skipped. Install it, then run: $ROOT/vscode/install.sh"
fi

cat <<MSG

Done.
1. Open Carnot from Applications. Allow the microphone and Screen Recording, then quit and reopen it.
2. In VS Code run "Developer: Reload Window". Carnot appears in the activity bar.
3. To use other AIs: the button on the left of the input box opens the connection settings.
Settings are in $ROOT/server/config.toml
MSG
