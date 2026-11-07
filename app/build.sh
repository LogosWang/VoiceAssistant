#!/bin/bash
# Builds Carnot.app into /Applications and turns on launch at login. Doesn't start it.
set -e
cd "$(dirname "$0")"
SERVER="$(cd ../server && pwd)"
APP="${CARNOT_APP_DIR:-/Applications}/Carnot.app"
SUPPORT="${CARNOT_SUPPORT_DIR:-$HOME/Library/Application Support/Carnot}"
BUILD="$(mktemp -d)/Carnot.app"
mkdir -p "$BUILD/Contents/MacOS" "$BUILD/Contents/Resources"
swiftc -O -o "$BUILD/Contents/MacOS/Carnot" main.swift computer.swift
cp Info.plist "$BUILD/Contents/Info.plist"
plutil -replace CarnotServerDir -string "$SERVER" "$BUILD/Contents/Info.plist"
cp -R index.html settings.html art "$BUILD/Contents/Resources/"
[ ! -e icon.icns ] || cp icon.icns "$BUILD/Contents/Resources/"
[ -x "$SERVER/syscap/syscap" ] || swiftc -O -o "$SERVER/syscap/syscap" "$SERVER/syscap/main.swift"
# A fixed identity: macOS ties permissions to the signature, and an ad-hoc one changes with each
# build, which leaves the System Settings switches on but no longer matching this app.
SIGNER="Carnot Local Signing"
if ! security find-certificate -c "$SIGNER" >/dev/null 2>&1; then
  TMP="$(mktemp -d)"
  /usr/bin/openssl req -x509 -newkey rsa:2048 -nodes -days 7300 -subj "/CN=$SIGNER" \
    -keyout "$TMP/k.pem" -out "$TMP/c.pem" -addext "extendedKeyUsage=codeSigning" \
    -addext "keyUsage=digitalSignature" -addext "basicConstraints=critical,CA:false" 2>/dev/null
  PW="$(/usr/bin/openssl rand -hex 16)"
  /usr/bin/openssl pkcs12 -export -inkey "$TMP/k.pem" -in "$TMP/c.pem" -out "$TMP/i.p12" -passout "pass:$PW" 2>/dev/null
  security import "$TMP/i.p12" -k "$HOME/Library/Keychains/login.keychain-db" -P "$PW" -T /usr/bin/codesign >/dev/null
  rm -rf "$TMP"
fi
codesign --force --deep -s "$SIGNER" "$BUILD"
mkdir -p "$SUPPORT"
cp -R index.html settings.html art "$SUPPORT/"
[ -n "$CARNOT_APP_DIR" ] || { pkill -x Carnot 2>/dev/null && sleep 1 || true; }
rm -rf "$APP"
mv "$BUILD" "$APP"
[ -n "$CARNOT_NO_LOGIN_ITEM" ] || "$APP/Contents/MacOS/Carnot" --enable-login
echo "built $APP"
