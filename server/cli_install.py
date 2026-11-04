"""Installs the CLI an AI service runs through.

The China edition of the installer ships mirror.json, naming registries that are reachable there and
the exact Claude Code build to take from them. That build is a native program published as an npm
package, so it needs neither Node nor anything from outside those mirrors. Its hash was compared with
registry.npmjs.org when the installer was built, so a mirror can't swap in something else.
"""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile

import providers

NATIVE = "@anthropic-ai/claude-code-darwin-arm64"
CURL_LIMITS = ["--connect-timeout", "20", "--speed-time", "60", "--speed-limit", "10240", "--retry", "2"]


def mirror():
    try:
        with open(providers.MIRROR_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _native(m):
    """The pinned build, from the first mirror that delivers it intact. Returns (ok, detail)."""
    pin = m["claude_code"]
    algo, want = pin["integrity"].split("-", 1)
    name = NATIVE.split("/")[1]
    bin_dir = os.path.expanduser("~/.local/bin")
    os.makedirs(bin_dir, exist_ok=True)
    failed = []
    with tempfile.TemporaryDirectory(dir=bin_dir, prefix=".claude-install-") as work:
        archive = os.path.join(work, "claude.tgz")
        for registry in m["registries"]:
            url = f"{registry}/{NATIVE}/-/{name}-{pin['version']}.tgz"
            r = subprocess.run(["/usr/bin/curl", "-fsSL", *CURL_LIMITS, "-o", archive, url],
                               capture_output=True, text=True)
            if r.returncode != 0:
                failed.append(f"{registry}: curl {r.returncode} {r.stderr.strip()[-200:]}")
                continue
            digest = hashlib.new(algo)
            with open(archive, "rb") as f:
                while chunk := f.read(1 << 20):
                    digest.update(chunk)
            if base64.b64encode(digest.digest()).decode() != want:
                failed.append(f"{registry}: hash mismatch")
                continue
            target = os.path.join(work, "claude")
            with tarfile.open(archive) as tar, open(target, "wb") as out:
                shutil.copyfileobj(tar.extractfile("package/claude"), out)
            os.chmod(target, 0o755)
            r = subprocess.run([target, "--version"], capture_output=True, text=True, timeout=120)
            if r.returncode != 0 or pin["version"] not in r.stdout:
                return False, f"won't run: exit {r.returncode} {r.stdout.strip()} {r.stderr.strip()[-200:]}"
            os.replace(target, os.path.join(bin_dir, "claude"))
            return True, f"{pin['version']} from {registry}"
    return False, "; ".join(failed)


def needs_node(pid):
    m = mirror()
    return not (m and providers.PROVIDERS[pid]["package"] == providers.CLAUDE_CODE)


def node_url():
    return "npmmirror.com/mirrors/node" if mirror() else "nodejs.org"


def install(pid):
    """Returns (ok, detail)."""
    providers.fix_path()
    m = mirror()
    package = providers.PROVIDERS[pid]["package"]
    try:
        if not needs_node(pid):
            return _native(m)
        if not shutil.which("npm"):
            return False, "npm not found"
        registry = [f"--registry={m['registries'][0]}"] if m else []
        r = subprocess.run(["npm", "install", "-g", package, *registry], capture_output=True, text=True)
        return r.returncode == 0, f"exit {r.returncode} {r.stderr[-300:]}"
    except (OSError, KeyError, ValueError, tarfile.TarError, subprocess.SubprocessError) as e:
        return False, repr(e)
