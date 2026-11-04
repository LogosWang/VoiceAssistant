#!/usr/bin/env python3
"""Writes mirror.json for the China edition: the Claude Code stable build, pinned to the hash published
on registry.npmjs.org, and the mirrors that hold that same build."""
import json
import subprocess
import sys

OFFICIAL = "https://registry.npmjs.org"
MIRRORS = ["https://registry.npmmirror.com", "https://repo.huaweicloud.com/repository/npm"]
NATIVE = "@anthropic-ai/claude-code-win32-x64" if sys.platform == "win32" else "@anthropic-ai/claude-code-darwin-arm64"
CURL = "curl.exe" if sys.platform == "win32" else "/usr/bin/curl"


def fetch(url):
    r = subprocess.run([CURL, "-fsSL", "--max-time", "60", url], capture_output=True, encoding="utf-8", errors="replace")
    return json.loads(r.stdout) if r.returncode == 0 else None


official = fetch(f"{OFFICIAL}/{NATIVE}/stable") or sys.exit("can't read the stable version from npmjs")
version, integrity = official["version"], official["dist"]["integrity"]
holding = []
for registry in MIRRORS:
    d = fetch(f"{registry}/{NATIVE}/{version}")
    if d and d["dist"].get("integrity") == integrity:
        holding.append(registry)
    else:
        print(f"skipping {registry}: doesn't hold {version} with the official hash", file=sys.stderr)
if MIRRORS[0] not in holding:
    sys.exit(f"{MIRRORS[0]} doesn't have Claude Code {version} with the official hash yet")
with open(sys.argv[1], "w") as f:
    json.dump({"registries": holding, "claude_code": {"version": version, "integrity": integrity}}, f, indent=2)
print(f"Claude Code {version}, mirrors: {', '.join(holding)}")
