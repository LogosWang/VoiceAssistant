"""Snapshots of a working folder before and after each turn, so any turn's changes can be seen and
undone, however they were made: an edit tool or a shell command. They live in a separate git
directory under ~/.carnot, one per folder. The project's own .git is never touched, and its
.gitignore is respected. Whatever can't be snapshotted just goes without: this must never get in
the way of the turn itself."""
import hashlib
import os
import shutil
import subprocess

import shared

ROOT = os.path.join(shared.HOME, "checkpoints")
TIMEOUT = 20
ALWAYS_SKIP = ["node_modules/", ".venv/", "venv/", "__pycache__/", ".DS_Store", "*.onnx", "*.safetensors", "*.ckpt", "dist/", "build/"]


def available():
    """/usr/bin/git on a Mac without developer tools only opens an install dialog."""
    if not shutil.which("git"):
        return False
    return subprocess.run(["xcode-select", "-p"], capture_output=True).returncode == 0 or shutil.which("git") != "/usr/bin/git"


def _too_broad(cwd):
    real = os.path.realpath(cwd)
    return real in ("/", os.path.realpath(os.path.expanduser("~")), "/Users", "/tmp", "/private/tmp")


def _git(cwd, *args, text=True, env_extra=None):
    git_dir = os.path.join(ROOT, hashlib.sha256(os.path.realpath(cwd).encode()).hexdigest()[:16])
    env = dict(os.environ, GIT_DIR=git_dir, GIT_WORK_TREE=cwd, GIT_INDEX_FILE=os.path.join(git_dir, "carnot-index"))
    env.update(env_extra or {})
    if not os.path.exists(os.path.join(git_dir, "HEAD")):
        os.makedirs(git_dir, exist_ok=True)
        subprocess.run(["git", "init", "-q", "--bare", git_dir], env={k: v for k, v in env.items() if not k.startswith("GIT_")},
                       capture_output=True, timeout=TIMEOUT, check=True)
        for key, value in (("core.bare", "false"), ("gc.auto", "0"), ("core.autocrlf", "false"), ("advice.addEmbeddedRepo", "false")):
            subprocess.run(["git", "config", key, value], env=env, capture_output=True, timeout=TIMEOUT)
        os.makedirs(os.path.join(git_dir, "info"), exist_ok=True)
        with open(os.path.join(git_dir, "info", "exclude"), "w") as f:
            f.write("\n".join(ALWAYS_SKIP) + "\n")
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=text, timeout=TIMEOUT)


def snapshot(cwd):
    """The folder's state as a tree id, or None if it can't or shouldn't be taken."""
    if not available() or _too_broad(cwd) or not os.path.isdir(cwd):
        return None
    try:
        if _git(cwd, "add", "-A", "--ignore-errors", ".").returncode not in (0, 1):
            return None
        r = _git(cwd, "write-tree")
        return r.stdout.strip() if r.returncode == 0 else None
    except (subprocess.SubprocessError, OSError):
        return None


def changes(cwd, before, after):
    """[{path, status, added, removed}] between two snapshots."""
    if not before or not after or before == after:
        return []
    try:
        names = _git(cwd, "diff-tree", "-r", "--no-renames", "--name-status", before, after).stdout
        counts = _git(cwd, "diff-tree", "-r", "--no-renames", "--numstat", before, after).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    numbers = {}
    for line in counts.splitlines():
        a, r, path = line.split("\t", 2)
        numbers[path] = (0 if a == "-" else int(a), 0 if r == "-" else int(r))
    out = []
    for line in names.splitlines():
        status, path = line.split("\t", 1)
        added, removed = numbers.get(path, (0, 0))
        out.append({"path": path, "status": status[0], "added": added, "removed": removed})
    return out


def _inside(cwd, path):
    full = os.path.realpath(os.path.join(cwd, path))
    return full if full.startswith(os.path.realpath(cwd) + os.sep) else None


def show(cwd, tree, path):
    """A file's text in a snapshot; empty if it wasn't there."""
    r = _git(cwd, "show", f"{tree}:{path}", text=False)
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""


def restore(cwd, tree, paths=None):
    """Puts files back as they were in the snapshot; ones that didn't exist then are removed.
    paths None means everything that differs from it now. Returns what changed."""
    now = snapshot(cwd)
    if paths is None:
        paths = [c["path"] for c in changes(cwd, tree, now)]
    done = []
    for path in paths:
        full = _inside(cwd, path)
        if not full:
            continue
        r = _git(cwd, "show", f"{tree}:{path}", text=False)
        if r.returncode == 0:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as f:
                f.write(r.stdout)
        elif os.path.exists(full):
            os.remove(full)
        done.append(path)
    return done
