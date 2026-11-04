#!/bin/bash
# Builds dist/Carnot-Installer.pkg: everything inside, for a Mac with no developer tools.
# Build machine needs: Apple silicon, Xcode command line tools, Node, internet.
# CARNOT_EDITION=cn builds dist/Carnot-Installer-CN.pkg, which fetches Claude Code from mirrors in China.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
VERSION="$(date +%Y.%m.%d.%H%M)"   # always higher than the last, so any installer upgrades
OS_MIN=14.0                # the oldest macOS the Python packages run on
SPEEX=speexdsp-1.2.1
PY_TAG=20261026; PY_VER=3.12.16
PY_NAME="cpython-$PY_VER+$PY_TAG-aarch64-apple-darwin-install_only.tar.gz"
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/$PY_TAG/cpython-$PY_VER%2B$PY_TAG-aarch64-apple-darwin-install_only.tar.gz"
PY_SUMS="https://github.com/astral-sh/python-build-standalone/releases/download/$PY_TAG/SHA256SUMS"
SIGNER="Carnot Local Signing"
WORK="${CARNOT_PKG_WORK:-$(mktemp -d)}"; CACHE="$WORK/cache"; STAGE="$WORK/stage"; SERVER="$STAGE/payload/server"
PKG_NAME=Carnot-Installer.pkg; WELCOME=welcome.txt
[ "$CARNOT_EDITION" != cn ] || { PKG_NAME=Carnot-Installer-CN.pkg; WELCOME=welcome-cn.txt; }
step() { printf '\n==> %s\n' "$1"; }
[ "$(uname -m)" = arm64 ] || { echo "build on Apple silicon"; exit 1; }
rm -rf "$STAGE"; mkdir -p "$CACHE" "$SERVER" "$STAGE/root/Applications" "$ROOT/dist"

step "Service code"
cp server/*.py server/persona.md server/portrait.png server/config.toml server/requirements.txt "$SERVER/"
cp server/config.toml "$SERVER/config.default.toml"
cp -R server/knowledge server/test_clips "$SERVER/"

if [ "$CARNOT_EDITION" = cn ]; then
  step "Claude Code mirrors"
  /usr/bin/python3 packaging/pin_mirror.py "$SERVER/mirror.json"
fi

step "Python $PY_VER, relocatable"
[ -e "$CACHE/$PY_NAME" ] || curl -fsSL -o "$CACHE/$PY_NAME" "$PY_URL"
WANT="$(curl -fsSL "$PY_SUMS" | awk -v n="$PY_NAME" '$2 == n {print $1}')"
GOT="$(shasum -a 256 "$CACHE/$PY_NAME" | cut -d' ' -f1)"
[ -n "$WANT" ] && [ "$WANT" = "$GOT" ] || { echo "Python download checksum mismatch"; exit 1; }
tar xzf "$CACHE/$PY_NAME" -C "$STAGE"; mv "$STAGE/python" "$SERVER/.venv"
env -u PIP_USER "$SERVER/.venv/bin/python3" -m pip install -q --only-binary :all: --no-warn-script-location -r server/requirements.txt
ln -sf python3.12 "$SERVER/.venv/bin/python"
find "$SERVER/.venv" \( -name '__pycache__' -o -name 'test' -o -name 'tests' -o -name 'idle_test' -o -name 'tkinter' -o -name 'ensurepip' \) -type d -prune -exec rm -rf {} +
rm -rf "$SERVER/.venv/include" "$SERVER/.venv/share"

step "Echo canceller, built for macOS $OS_MIN"
[ -e "$CACHE/$SPEEX.tar.gz" ] || curl -fsSL -o "$CACHE/$SPEEX.tar.gz" "https://downloads.xiph.org/releases/speex/$SPEEX.tar.gz"
rm -rf "$WORK/speex"; mkdir "$WORK/speex"; tar xzf "$CACHE/$SPEEX.tar.gz" -C "$WORK/speex"
( cd "$WORK/speex/$SPEEX" && ./configure --quiet --prefix="$WORK/speex/out" --disable-static CFLAGS="-O2 -mmacosx-version-min=$OS_MIN" LDFLAGS="-mmacosx-version-min=$OS_MIN" >/dev/null && make -s -j8 install >/dev/null 2>&1 )
mkdir -p "$SERVER/lib"; cp "$WORK/speex/out/lib/libspeexdsp.1.dylib" "$SERVER/lib/libspeexdsp.dylib"
install_name_tool -id "@rpath/libspeexdsp.dylib" "$SERVER/lib/libspeexdsp.dylib"
codesign --force -s - "$SERVER/lib/libspeexdsp.dylib"

step "System audio helper"
mkdir -p "$SERVER/syscap"; cp server/syscap/main.swift "$SERVER/syscap/"
swiftc -O -target "arm64-apple-macos$OS_MIN" -o "$SERVER/syscap/syscap" server/syscap/main.swift

step "Video frame helper"
mkdir -p "$SERVER/videoframes"; cp server/videoframes/main.swift "$SERVER/videoframes/"
swiftc -O -target "arm64-apple-macos$OS_MIN" -o "$SERVER/videoframes/videoframes" server/videoframes/main.swift

step "Speech models"
M="$ROOT/server/models"; [ -e "$M/silero_vad.onnx" ] || M="${CARNOT_MODELS:-$M}"
[ -e "$M/silero_vad.onnx" ] || { server/download_models.sh >/dev/null; M="$ROOT/server/models"; }
KWS=sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20; ASR=sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17
mkdir -p "$SERVER/models/$KWS" "$SERVER/models/$ASR"
for f in tokens.txt en.phone encoder-epoch-13-avg-2-chunk-16-left-64.onnx decoder-epoch-13-avg-2-chunk-16-left-64.onnx joiner-epoch-13-avg-2-chunk-16-left-64.onnx; do cp "$M/$KWS/$f" "$SERVER/models/$KWS/"; done
for f in tokens.txt model.int8.onnx; do cp "$M/$ASR/$f" "$SERVER/models/$ASR/"; done
cp "$M/silero_vad.onnx" "$SERVER/models/"

step "Carnot.app"
APP="$STAGE/root/Applications/Carnot.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
swiftc -O -target "arm64-apple-macos$OS_MIN" -o "$APP/Contents/MacOS/Carnot" app/main.swift
cp app/Info.plist "$APP/Contents/Info.plist"
plutil -replace CarnotServerDir -string '~/Library/Application Support/Carnot/server' "$APP/Contents/Info.plist"
plutil -replace LSMinimumSystemVersion -string "$OS_MIN" "$APP/Contents/Info.plist"
plutil -replace CFBundleVersion -string "$VERSION" "$APP/Contents/Info.plist"
plutil -replace CFBundleShortVersionString -string "$VERSION" "$APP/Contents/Info.plist"
cp -R app/index.html app/settings.html app/art app/icon.icns "$APP/Contents/Resources/"
security find-certificate -c "$SIGNER" >/dev/null 2>&1 || { echo "run app/build.sh once first, it creates the signing certificate"; exit 1; }
codesign --force --deep -s "$SIGNER" "$APP"

step "VS Code extension"
( cd vscode && npx --yes @vscode/vsce@3 package --no-dependencies --allow-missing-repository --skip-license -o "$SERVER/carnot.vsix" >/dev/null 2>&1 )

step "Package"
mkdir -p "$STAGE/root/Library/Application Support/Carnot"; mv "$STAGE/payload" "$STAGE/root/Library/Application Support/Carnot/payload"
mkdir -p "$STAGE/scripts" "$STAGE/res"; cp packaging/preinstall packaging/postinstall "$STAGE/scripts/"; cp "packaging/$WELCOME" "$STAGE/res/welcome.txt"
pkgbuild --analyze --root "$STAGE/root" "$STAGE/comp.plist" >/dev/null
# always the app in /Applications, replaced whatever version or identifier it has now
plutil -replace 0.BundleIsRelocatable -bool NO "$STAGE/comp.plist"
plutil -replace 0.BundleIsVersionChecked -bool NO "$STAGE/comp.plist"
plutil -replace 0.BundleHasStrictIdentifier -bool NO "$STAGE/comp.plist"
plutil -replace 0.BundleOverwriteAction -string upgrade "$STAGE/comp.plist"
pkgbuild --quiet --root "$STAGE/root" --component-plist "$STAGE/comp.plist" --scripts "$STAGE/scripts" --identifier com.carnot.assistant.pkg --version "$VERSION" --install-location / "$STAGE/carnot-component.pkg"
sed "s/__VERSION__/$VERSION/" packaging/Distribution.xml > "$STAGE/Distribution.xml"
productbuild --quiet --distribution "$STAGE/Distribution.xml" --package-path "$STAGE" --resources "$STAGE/res" "$ROOT/dist/$PKG_NAME"
ls -lh "$ROOT/dist/$PKG_NAME"
echo "work folder: $WORK"
