"""Which models each AI service offers, and which one and what effort the user picked.

Every list comes from the vendor at run time, so a new model shows up without a new Carnot:
Claude Code and Codex report theirs over their own protocols, the Anthropic-compatible services answer
/v1/models, and Gemini with a key answers its own. Until the user picks something else, what's selected
is whatever the vendor or the CLI's own settings say now, so it moves when they do.
One choice per service, shared by the app, VS Code, the bots and scheduled tasks. It lives in
~/.carnot, read on every turn, so it takes effect from the next message.
"""
import contextlib
import fcntl
import http.server
import json
import os
import re
import shutil
import subprocess
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor

import providers

CATALOG = os.path.join(providers.CARNOT_HOME, "models.json")
CHOICES = os.path.join(providers.CARNOT_HOME, "model_choice.json")
LOCK = CATALOG + ".lock"
MAX_AGE = 6 * 3600
ASK_TIMEOUT = 25
EFFORTS = ("low", "medium", "high", "xhigh", "max")
GEMINI_ALIASES = [("auto", "自动"), ("pro", "Pro"), ("flash", "Flash"), ("flash-lite", "Flash-Lite")]  # the CLI keeps these current
SAFE_ID = re.compile(r"[\w.:/\[\]-]{1,120}")


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _edit(path, change):
    os.makedirs(providers.CARNOT_HOME, exist_ok=True)
    with open(LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _load(path)
        change(data)
        tmp = f"{path}.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)


def _item(mid, name=None, efforts=(), note=""):
    return {"id": mid, "name": name or mid, "efforts": [e for e in efforts if e in EFFORTS], "note": note}


# ---------- asking each vendor ----------

def _ask_claude(pid):
    """Claude Code's own start-up handshake. No message is sent, so no model runs."""
    p = subprocess.Popen([shutil.which("claude"), "-p", "--output-format", "stream-json", "--verbose", "--input-format", "stream-json"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                         env=providers.apply_env(pid), cwd=providers.CARNOT_HOME)
    killer = threading.Timer(ASK_TIMEOUT, p.kill)
    killer.start()
    try:
        p.stdin.write(json.dumps({"type": "control_request", "request_id": "models", "request": {"subtype": "initialize"}}) + "\n")
        p.stdin.flush()
        for line in p.stdout:
            with contextlib.suppress(ValueError):
                ev = json.loads(line)
                if ev.get("type") == "control_response":
                    found = (ev["response"].get("response") or {}).get("models") or []
                    break
        else:
            found = []
    finally:
        killer.cancel()
        p.kill()
    items = [{**_item(m.get("value"), m.get("displayName"), m.get("supportedEffortLevels") or [], m.get("description", "")),
              "resolved": m.get("resolvedModel")} for m in found if m.get("value") != "default"]
    if not items:
        return [], None
    with ThreadPoolExecutor(4) as pool:
        for item, effort in zip(items, pool.map(_cli_default_effort, items)):
            item["default_effort"] = effort
    resolved = next((m.get("resolvedModel") for m in found if m.get("value") == "default"), None)
    configured = str(_json_file("~/.claude/settings.json").get("model") or "").split("[")[0]  # opus[1m] is opus
    default = next((i for i in items if i["id"] == configured), None) or next((i for i in items if i["resolved"] == resolved), items[0])
    return items, default["id"]


def _cli_default_effort(item):
    """The effort Claude Code would send for this model, which it reports nowhere but in the request:
    it runs once against a throwaway local server that refuses everything. Nothing leaves this Mac."""
    if not item["efforts"]:
        return None
    got = []
    done = threading.Event()
    class Answer(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            effort = (body.get("output_config") or {}).get("effort")
            if effort and ("haiku" not in body.get("model", "") or "haiku" in item["id"]):
                got.append(effort)
                done.set()
            self.send_response(400)
            self.end_headers()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Answer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {k: v for k, v in providers.apply_env("claude").items() if k != "ANTHROPIC_API_KEY"}
    env.update(ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_address[1]}", ANTHROPIC_AUTH_TOKEN="local", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    p = subprocess.Popen([shutil.which("claude"), "-p", "--no-session-persistence", "--model", item["id"], "hi"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, cwd=providers.CARNOT_HOME)
    done.wait(15)
    p.kill()
    p.wait()
    server.shutdown()
    return got[0] if got else None


def _json_file(path):
    return _load(os.path.expanduser(path))


def _ask_codex(pid):
    p = subprocess.Popen([shutil.which("codex"), "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, env=providers.apply_env(pid), cwd=providers.CARNOT_HOME)
    killer = threading.Timer(ASK_TIMEOUT, p.kill)
    killer.start()
    found = []
    try:
        for msg in ({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "carnot", "version": "1"}}},
                    {"method": "initialized"}, {"id": 2, "method": "model/list", "params": {}}):
            p.stdin.write(json.dumps(msg) + "\n")
        p.stdin.flush()
        for line in p.stdout:
            with contextlib.suppress(ValueError):
                ev = json.loads(line)
                if ev.get("id") == 2:
                    found = (ev.get("result") or {}).get("data") or []
                    break
    finally:
        killer.cancel()
        p.kill()
    own = {}
    with contextlib.suppress(OSError, tomllib.TOMLDecodeError):
        with open(os.path.expanduser("~/.codex/config.toml"), "rb") as f:
            own = tomllib.load(f)
    items, default = [], None
    for m in found:
        if m.get("hidden"):
            continue
        efforts = [e.get("reasoningEffort") for e in m.get("supportedReasoningEfforts") or []]
        item = _item(m.get("model") or m.get("id"), m.get("displayName"), efforts)
        mine = own.get("model_reasoning_effort")
        item["default_effort"] = mine if mine in efforts else m.get("defaultReasoningEffort")
        items.append(item)
        if m.get("isDefault") and not default:
            default = item["id"]
    return items, own["model"] if own.get("model") in [i["id"] for i in items] else default


def _ask_gemini(pid):
    key = providers.get_key(pid)
    if not key:
        return [_item(i, n) for i, n in GEMINI_ALIASES], GEMINI_ALIASES[0][0]
    code, body = providers.vendor_request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200", key)
    if code != 200:
        return [], None
    items = [_item(m["name"].split("/", 1)[-1], m.get("displayName")) for m in json.loads(body).get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", []) and "gemini" in m.get("name", "")]
    return [_item(*GEMINI_ALIASES[0]), *items], GEMINI_ALIASES[0][0]


def _nearest(effort, allowed):
    above = EFFORTS[EFFORTS.index(effort):]
    return next((e for e in above if e in allowed), next((e for e in reversed(EFFORTS) if e in allowed), "high"))


def _probe_efforts(pid, model):
    """Which levels this model refuses. A refusal costs nothing and comes before any thinking. Once per
    model; if the vendor can't answer now (busy, no quota) it is asked again next time."""
    mapped = {}
    for effort in ("medium", "xhigh"):
        code, body = providers.vendor_request(providers.base_url(pid) + "/v1/messages", providers.get_key(pid), {
            "model": model, "max_tokens": 1, "output_config": {"effort": effort}, "thinking": {"type": "adaptive"},
            "messages": [{"role": "user", "content": "hi"}]})
        if code == 400:
            said = re.search(r"please use ([a-z, ]+)", body)
            if said or "effort" in body.lower():
                allowed = [e for e in EFFORTS if said and re.search(rf"\b{e}\b", said.group(1))]
                mapped[effort] = _nearest(effort, allowed)
        elif code != 200:
            return {"mapped": {}, "probed": False}
    return {"mapped": mapped, "probed": True}


def _ask_compatible(pid):
    """Reached through Claude Code, which passes them the effort. All five levels are offered; a level a
    model refuses (GLM 5.3 takes only low, high and max) goes out as the nearest it takes."""
    p = providers.PROVIDERS[pid]
    code, body = providers.vendor_request(providers.base_url(pid) + "/v1/models", providers.get_key(pid))
    if code != 200:
        return [], None
    items = [{**_item(m["id"], m.get("display_name"), EFFORTS), "default_effort": "high"}  # what Claude Code sends for a model it doesn't know
             for m in json.loads(body).get("data", []) if m.get("id")]
    known = {i["id"]: i for i in (_load(CATALOG).get(pid) or {}).get("models", []) if i.get("probed")}
    with ThreadPoolExecutor(6) as pool:
        for item, tested in zip(items, pool.map(lambda i: known.get(i["id"]) or _probe_efforts(pid, i["id"]), items)):
            item.update(mapped=tested.get("mapped", {}), probed=tested.get("probed", False))
    best, top = None, ()
    for i in items:          # these vendors don't say which is current: the highest version in the family
        m = re.fullmatch(p.get("newest", "$^"), i["id"])
        version = tuple(int(n) for n in m.group(1).split(".")) if m else ()
        if version > top:
            best, top = i["id"], version
    return items, best or next((i["id"] for i in items if i["id"] == p["model"]), items[0]["id"] if items else None)


def _usable(pid):
    p = providers.PROVIDERS[pid]
    if not shutil.which(p["cli"]):
        return False
    return bool(providers.get_key(pid)) or p["login"]


def refresh(pid):
    """Asks the vendor. A failed ask keeps the list we have. Returns whether anything changed."""
    engine = providers.PROVIDERS[pid]["engine"]
    ask = {"codex": _ask_codex, "gemini": _ask_gemini}.get(engine) or (_ask_claude if pid == "claude" else _ask_compatible)
    try:
        items, default = ask(pid)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        items, default = [], None
    now = time.time()
    changed = []
    def store(data):
        old = data.get(pid) or {}
        if items:
            changed.append(old.get("models") != items or old.get("default") != default)
            data[pid] = {"models": items, "default": default, "fetched": now}
        else:           # try again in a while, not on every look
            data[pid] = {**old, "fetched": now, "failed": True}
    _edit(CATALOG, store)
    return bool(changed and changed[0])


def refresh_stale(only=None):
    changed = False
    catalog = _load(CATALOG)
    for pid in providers.PROVIDERS:
        if only and pid != only:
            continue
        if time.time() - (catalog.get(pid) or {}).get("fetched", 0) > MAX_AGE and _usable(pid):
            changed = refresh(pid) or changed
    return changed


# ---------- the choice ----------

def set_choice(pid, model=None, effort=None):
    """Picking what's already the default stores nothing, so it keeps following the vendor."""
    if pid not in providers.PROVIDERS:
        raise ValueError("unknown provider")
    model, effort = model or None, effort or None
    if model and not SAFE_ID.fullmatch(model):
        raise ValueError("bad model")
    if effort and effort not in EFFORTS:
        raise ValueError("bad effort")
    items, default_id = _offered(pid, _load(CATALOG))
    if model == default_id:
        model = None
    active = next((i for i in items if i["id"] == (model or default_id)), {})
    if effort == active.get("default_effort"):
        effort = None
    _edit(CHOICES, lambda data: data.__setitem__(pid, {"model": model, "effort": effort}))


def _offered(pid, catalog):
    """(what can be picked, the id of the one that holds unless the user picks another). Before a vendor
    has answered, that's the one model we know of."""
    entry, p = catalog.get(pid) or {}, providers.PROVIDERS[pid]
    items = entry.get("models") or []
    if entry.get("default") and items:
        return items, entry["default"]
    before = _item(p.get("model") or "", p.get("model") or p["name"], EFFORTS if p["engine"] == "claude" else ())
    before["default_effort"] = "high" if before["efforts"] else None
    return items or [before], items[0]["id"] if items else before["id"]


def _state(pid, catalog, choices):
    """What holds now. A model the vendor retired falls back to the default, an effort it can't do too."""
    items, default_id = _offered(pid, catalog)
    choice = choices.get(pid) or {}
    model = choice.get("model") if choice.get("model") in [i["id"] for i in items] else None
    active = next((i for i in items if i["id"] == (model or default_id)), items[0])
    picked = choice.get("effort") if choice.get("effort") in active["efforts"] else None
    default_effort = active.get("default_effort")
    if default_effort not in active["efforts"]:
        default_effort = None
    return items, default_id, model, picked, picked or default_effort, active


def resolve(pid):
    """(model, effort) for the command line. The effort is always the one on show, so the two can't differ;
    no model leaves it to the CLI, whose own settings then apply."""
    _, default_id, model, _, shown, active = _state(pid, _load(CATALOG), _load(CHOICES))
    p = providers.PROVIDERS[pid]
    if not model and (p.get("base_url") or p.get("endpoints")):  # Claude Code has no default of its own for these
        model = default_id or p["model"]
    return model, (active.get("mapped") or {}).get(shown, shown)


def listing():
    catalog, choices = _load(CATALOG), _load(CHOICES)
    out = {}
    for pid in providers.PROVIDERS:
        items, default_id, model, picked, shown, active = _state(pid, catalog, choices)
        out[pid] = {"models": [{k: i[k] for k in ("id", "name", "note", "efforts")} for i in items],
                    "selected": model or default_id, "model": model, "effort": picked, "shown_effort": shown,
                    "efforts": active["efforts"], "mapped": active.get("mapped") or {}}
    return out
