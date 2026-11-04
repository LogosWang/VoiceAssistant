"""The AI services Carnot can work through, and how each one signs in.

Claude, ChatGPT and Gemini run through their own official CLI, signed in with a subscription or an
API key. ZCode (GLM Coding Plan), DeepSeek and Doubao (Ark Coding Plan) take an API key and run
through Claude Code against their Anthropic-compatible endpoints, so tools keep working.
Keys live in the system's own store (macOS keychain, Windows Credential Manager), never in a file, and reach the CLI only through its environment.
"""
import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

import compat

HERE = os.path.dirname(os.path.abspath(__file__))
CURRENT_FILE = os.path.join(compat.state_dir(), ".provider")
PERMISSION_FILE = os.path.join(compat.state_dir(), ".permission")
# What the AI may do unasked, in Claude Code's words. Nobody can answer a prompt from here, so a
# mode that would ask ("manual") is left out: it would only fail.
PERMISSIONS = ("plan", "acceptEdits", "auto", "bypassPermissions")
DEFAULT_PERMISSION = "auto"
KEYCHAIN = "carnot-voice"
PATH_CACHE_SECONDS = 86400
MIRROR_FILE = os.path.join(HERE, "mirror.json")   # only the China edition has it
CLAUDE_CODE = "@anthropic-ai/claude-code"

PROVIDERS = {
    "claude": {"name": "Claude", "engine": "claude", "cli": "claude", "login": True, "package": CLAUDE_CODE, "key_env": "ANTHROPIC_API_KEY",
               "key_url": "https://console.anthropic.com/settings/keys"},
    "chatgpt": {"name": "ChatGPT", "engine": "codex", "cli": "codex", "login": True, "package": "@openai/codex",
                "key_env": "CODEX_API_KEY", "key_url": "https://platform.openai.com/api-keys"},
    "gemini": {"name": "Gemini", "engine": "gemini", "cli": "gemini", "login": True, "package": "@google/gemini-cli",
               "key_env": "GEMINI_API_KEY", "key_url": "https://aistudio.google.com/apikey"},
    "zcode": {"name": "ZCode", "engine": "claude", "cli": "claude", "login": False, "package": CLAUDE_CODE, "key_env": "ANTHROPIC_AUTH_TOKEN",
              "endpoints": [("https://api.z.ai/api/anthropic", "海外 Z.ai"), ("https://open.bigmodel.cn/api/anthropic", "国内智谱")],
              "model": "glm-5.3", "newest": r"glm-(\d+(?:\.\d+)*)",
              "key_url": "https://z.ai/manage-apikey/apikey-list", "key_url_china": "https://open.bigmodel.cn/usercenter/proj-mgmt/apikeys"},
    "deepseek": {"name": "DeepSeek", "engine": "claude", "cli": "claude", "login": False, "package": CLAUDE_CODE, "key_env": "ANTHROPIC_AUTH_TOKEN",
                 "base_url": "https://api.deepseek.com/anthropic", "model": "deepseek-v4-pro", "newest": r"deepseek-v(\d+(?:\.\d+)*)-pro",
                 "key_url": "https://platform.deepseek.com/api_keys"},
    "doubao": {"name": "豆包", "engine": "claude", "cli": "claude", "login": False, "package": CLAUDE_CODE, "key_env": "ANTHROPIC_AUTH_TOKEN",
               "base_url": "https://ark.cn-beijing.volces.com/api/coding", "model": "ark-code-latest",
               "key_url": "https://console.volcengine.com/ark"},
}


def vendor_request(url, key, data=None):
    """(status, body), 0 if it can't be reached. curl because the key goes in over stdin, out of any
    process list, and it trusts what the system trusts."""
    config = f'header = "x-api-key: {key}"\nheader = "Authorization: Bearer {key}"\n'
    cmd = [*compat.curl(), "-sS", "--max-time", "12", "-K", "-", "-w", "\n%{http_code}", "-H", "anthropic-version: 2023-06-01"]
    if data is not None:
        cmd += ["-H", "content-type: application/json", "--data-binary", json.dumps(data)]
    try:
        r = subprocess.run(cmd + [url], input=config, capture_output=True, encoding="utf-8", errors="replace", timeout=20)
        body, _, code = r.stdout.rpartition("\n")
        return int(code or 0), body
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0, ""


def _endpoints(pid):
    """Mainland networks and accounts first in the China edition."""
    found = PROVIDERS[pid].get("endpoints") or []
    return list(reversed(found)) if os.path.exists(MIRROR_FILE) else list(found)


def base_url(pid):
    p = PROVIDERS[pid]
    if not p.get("endpoints"):
        return p.get("base_url")
    urls = [u for u, _ in _endpoints(pid)]
    with contextlib.suppress(OSError, ValueError, AttributeError):
        with open(ENDPOINTS_FILE, encoding="utf-8") as f:
            saved = json.load(f).get(pid)
        if saved in urls:
            return saved
    return urls[0]


def detect_endpoint(pid):
    """A key belongs to one of a service's platforms, or to both: the first that takes it is kept."""
    key, reachable = get_key(pid), None
    for url, _ in _endpoints(pid):
        code, _ = vendor_request(url + "/v1/messages", key, {"model": PROVIDERS[pid]["model"], "max_tokens": 1,
                                                              "messages": [{"role": "user", "content": "hi"}]})
        if code == 200:
            reachable = url
            break
        if code and code not in (401, 403) and not reachable:   # the key is fine, the plan or model isn't
            reachable = url
    if reachable:
        os.makedirs(CARNOT_HOME, exist_ok=True)
        saved = {}
        with contextlib.suppress(OSError, ValueError):
            with open(ENDPOINTS_FILE, encoding="utf-8") as f:
                saved = json.load(f)
        saved[pid] = reachable
        with open(ENDPOINTS_FILE, "w", encoding="utf-8") as f:
            json.dump(saved, f)
    return reachable


def endpoint_label(pid):
    return dict(PROVIDERS[pid].get("endpoints") or []).get(base_url(pid), "")


def _shell_path(fresh):
    """What an interactive shell says, remembered for a day: asking costs most of a second, every turn."""
    cache = os.path.join(CARNOT_HOME, "shell_path")
    if not fresh:
        with contextlib.suppress(OSError):
            if time.time() - os.path.getmtime(cache) < PATH_CACHE_SECONDS:
                with open(cache, encoding="utf-8") as f:
                    return f.read().strip().split(":")
    try:
        r = subprocess.run(["/bin/zsh", "-ilc", 'print -r -- "__PATH__$PATH"'], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    m = re.search(r"__PATH__(.*)", r.stdout)
    if not m:
        return []
    with contextlib.suppress(OSError):
        with open(cache, "w", encoding="utf-8") as f:
            f.write(m.group(1))
    return m.group(1).split(":")


def fix_path(fresh=False):
    """An app started from Finder or at login has a bare PATH, and a login shell doesn't help when
    the user's PATH is set in ~/.zshrc. Ask an interactive shell, then add the usual places."""
    if compat.IS_WIN:
        merged = compat.user_path() + os.environ.get("PATH", "").split(os.pathsep)
        os.environ["PATH"] = os.pathsep.join(dict.fromkeys(p for p in merged if p))
        return
    found = _shell_path(fresh)
    home = os.path.expanduser("~")
    usual = ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", f"{home}/.local/bin", f"{home}/bin",
             f"{home}/.npm-global/bin"]
    merged = found + os.environ.get("PATH", "").split(":") + usual
    os.environ["PATH"] = ":".join(dict.fromkeys(p for p in merged if p))


def current():
    try:
        pid = open(CURRENT_FILE).read().strip()
    except OSError:
        return "claude"
    return pid if pid in PROVIDERS else "claude"


def set_current(pid):
    with open(CURRENT_FILE, "w") as f:
        f.write(pid)


def normalize_permission(mode):
    return mode if mode in PERMISSIONS else DEFAULT_PERMISSION


def current_permission(default=DEFAULT_PERMISSION):
    """The choice made in the app, else the config file's."""
    try:
        return normalize_permission(open(PERMISSION_FILE).read().strip())
    except OSError:
        return normalize_permission(default)


def set_permission(mode):
    with open(PERMISSION_FILE, "w") as f:
        f.write(normalize_permission(mode))


def get_key(pid):
    if compat.IS_WIN:
        return compat.cred_get(KEYCHAIN, pid)
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN, "-a", pid, "-w"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def set_key(pid, key):
    """Goes in over stdin, so the key never shows up in a process list."""
    key = key.strip()
    if not re.fullmatch(r"[\x21\x23-\x5b\x5d-\x7e]+", key):  # printable, no quote or backslash
        raise ValueError("bad key")
    if compat.IS_WIN:
        return compat.cred_set(KEYCHAIN, pid, key)
    cmd = f'add-generic-password -U -s {KEYCHAIN} -a {pid} -w "{key}"\n'
    subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True, check=True)


def del_key(pid):
    if compat.IS_WIN:
        return compat.cred_del(KEYCHAIN, pid)
    subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN, "-a", pid], capture_output=True)


def env(pid):
    """What the CLI's environment needs for this provider; None values must be removed."""
    p = PROVIDERS[pid]
    out = {}
    if p["engine"] == "claude":  # a key left in the shell must not send this to the wrong service
        out = {"ANTHROPIC_API_KEY": None, "ANTHROPIC_AUTH_TOKEN": None, "ANTHROPIC_BASE_URL": None}
    key = get_key(pid)
    if key:
        out[p["key_env"]] = key
        if pid == "chatgpt":
            out["OPENAI_API_KEY"] = key
        if pid == "gemini":
            out["GEMINI_CLI_HOME"] = gemini_key_home()
    if p.get("base_url") or p.get("endpoints"):
        out["ANTHROPIC_BASE_URL"] = base_url(pid)
        out["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return out


CARNOT_HOME = os.path.expanduser(os.environ.get("CARNOT_HOME", "~/.carnot"))
ENDPOINTS_FILE = os.path.join(CARNOT_HOME, "endpoints.json")
GEMINI_CREDS = os.path.expanduser("~/.gemini/oauth_creds.json")
GEMINI_REFUSED = os.path.join(CARNOT_HOME, "gemini_refused")


def gemini_key_home():
    """Gemini CLI obeys the sign-in saved in ~/.gemini over any key, so a key runs from its own home."""
    home = os.path.join(CARNOT_HOME, "gemini")
    os.makedirs(os.path.join(home, ".gemini"), exist_ok=True)
    with open(os.path.join(home, ".gemini", "settings.json"), "w") as f:
        json.dump({"security": {"auth": {"selectedType": "gemini-api-key"}}}, f)
    skills = os.path.join(home, ".agents")
    if not os.path.lexists(skills) and os.path.isdir(os.path.expanduser("~/.agents")):
        compat.link_dir(os.path.expanduser("~/.agents"), skills)
    return home


def gemini_refused():
    """Google turned this sign-in away: the browser login succeeds, the CLI then can't use it."""
    with contextlib.suppress(OSError):
        with open(GEMINI_REFUSED, "w") as f:
            f.write(str(os.path.getmtime(GEMINI_CREDS)))


def _gemini_signed_in():
    if not os.path.exists(GEMINI_CREDS):
        return False
    with contextlib.suppress(OSError):
        with open(GEMINI_REFUSED) as f:
            return f.read() != str(os.path.getmtime(GEMINI_CREDS))  # a new sign-in gets a new try
    return True


def apply_env(pid):
    e = dict(os.environ)
    for k, v in env(pid).items():
        if v is None:
            e.pop(k, None)
        else:
            e[k] = v
    return e


def login_command(pid):
    """Argv of a command that opens the browser login and waits, or None if there isn't one."""
    return {"claude": ["claude", "auth", "login"], "chatgpt": ["codex", "login"]}.get(pid)


def open_terminal(command):
    """For a CLI whose sign-in only works in its own interactive screen (Gemini)."""
    if compat.IS_WIN:  # `command` is one of the CLI names in PROVIDERS, never anything from outside
        subprocess.Popen(["cmd", "/c", "start", "Carnot", "cmd", "/k", command], stdin=subprocess.DEVNULL)
        return
    fd, path = tempfile.mkstemp(suffix=".command", prefix="carnot-")
    with os.fdopen(fd, "w") as f:
        f.write(f"#!/bin/zsh -l\n{command}\n")
    os.chmod(path, 0o700)
    subprocess.run(["open", path])


def logged_in(pid):
    if pid == "claude" and shutil.which("claude"):
        r = subprocess.run(compat.argv(["claude", "auth", "status"]), capture_output=True, encoding="utf-8", errors="replace")
        try:
            return bool(json.loads(r.stdout).get("loggedIn"))
        except ValueError:
            return False
    if pid == "chatgpt" and shutil.which("codex"):
        return subprocess.run(compat.argv(["codex", "login", "status"]), capture_output=True).returncode == 0
    if pid == "gemini":
        return _gemini_signed_in()
    return False


def statuses():
    items = []
    for pid, p in PROVIDERS.items():
        installed = bool(shutil.which(p["cli"]))
        has_key = bool(get_key(pid))
        signed_in = installed and p["login"] and logged_in(pid)
        items.append({"id": pid, "name": p["name"], "installed": installed, "login": p["login"],
                      "signed_in": signed_in, "has_key": has_key, "ready": installed and (has_key or signed_in),
                      "key_url": (os.path.exists(MIRROR_FILE) and p.get("key_url_china")) or p["key_url"],
                      "endpoint": endpoint_label(pid), "package": p.get("package", ""),
                      "needs": "Claude Code" if p["cli"] == "claude" else p["cli"].capitalize()})
    return {"current": current(), "items": items}
