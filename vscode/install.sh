#!/bin/bash
# Packages the extension and installs it into VS Code, and records where the server is.
set -e
cd "$(dirname "$0")"
HOME_DIR="${CARNOT_HOME:-$HOME/.carnot}"
mkdir -p "$HOME_DIR"
(cd ../server && pwd) > "$HOME_DIR/server_dir"
CODE="$(command -v code || echo "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code")"
[ -x "$CODE" ] || { echo "VS Code not found"; exit 1; }
npx --yes @vscode/vsce@3 package --no-dependencies --allow-missing-repository --skip-license -o carnot.vsix
"$CODE" ${CARNOT_VSCODE_ARGS:-} --install-extension carnot.vsix --force
echo "installed. In VS Code: Developer: Reload Window"
