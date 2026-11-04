"""Conversations: what the menu and the VS Code extension list, and what bots continue. Each one
remembers, per AI, the id of the CLI's own session, so it can move between AIs and clients.
What was said is read back from a transcript each client appends to, and for turns from before
transcripts existed, from the CLIs' own logs. The app and the extension are separate processes, so
every change to the store happens under a file lock."""
import contextlib
import glob
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime

import compat
import providers
import shared

FILE = os.path.join(shared.HOME, "sessions.json")
TRANSCRIPTS = os.path.join(shared.HOME, "transcripts")
UNTITLED = "新对话"
KEEP = 200
HANDOFF_MESSAGES = 10
_thread_lock = threading.RLock()
_depth = threading.local()


@contextlib.contextmanager
def _lock():
    """Across threads and processes. Re-entrant, because the functions below call each other."""
    with _thread_lock:
        depth = getattr(_depth, "n", 0)
        if depth == 0:
            os.makedirs(shared.HOME, exist_ok=True)
            _depth.f = open(FILE + ".lock", "w")
            compat.lock(_depth.f)
        _depth.n = depth + 1
        try:
            yield
        finally:
            _depth.n -= 1
            if _depth.n == 0:
                compat.unlock(_depth.f)
                _depth.f.close()


def _read():
    try:
        with open(FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"current": None, "items": {}}


def _write(data):
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    tmp = f"{FILE}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    compat.replace(tmp, FILE)


def _trim(data):
    items = data["items"]
    for sid in sorted(items, key=lambda s: items[s]["updated"])[:max(0, len(items) - KEEP)]:
        if sid != data["current"]:
            del items[sid]


def create(client="app", title=UNTITLED, make_current=False):
    with _lock():
        data = _read()
        sid = uuid.uuid4().hex[:12]
        data["items"][sid] = {"id": sid, "title": title, "client": client, "native": {}, "updated": time.time()}
        if make_current:
            data["current"] = sid
        _trim(data)
        _write(data)
        return sid


def get(sid):
    with _lock():
        return _read()["items"].get(sid)


def current():
    """The app's conversation; there's always one."""
    with _lock():
        data = _read()
        if data["current"] in data["items"]:
            return data["current"]
        return create(make_current=True)


def set_current(sid):
    with _lock():
        data = _read()
        if sid not in data["items"]:
            return False
        data["current"] = sid
        data["items"][sid]["updated"] = time.time()
        _write(data)
        return True


def native(sid, provider):
    item = get(sid)
    return item["native"].get(provider) if item else None


def set_native(sid, provider, value):
    """value None forgets it: that AI's side of the conversation starts over."""
    with _lock():
        data = _read()
        item = data["items"].get(sid)
        if not item:
            return
        if value:
            item["native"][provider] = value
        else:
            item["native"].pop(provider, None)
        item["updated"] = time.time()
        _write(data)


def touch(sid, text=""):
    """A message went in: the first one names the conversation."""
    with _lock():
        data = _read()
        item = data["items"].get(sid)
        if not item:
            return
        if item["title"] == UNTITLED and text.strip():
            line = text.strip().splitlines()[0]
            item["title"] = line[:28] + ("…" if len(line) > 28 else "")
        item["updated"] = time.time()
        _write(data)


def rename(sid, title):
    title = title.strip()[:60]
    with _lock():
        data = _read()
        if title and sid in data["items"]:
            data["items"][sid]["title"] = title
            _write(data)


def delete(sid):
    """Forgets the entry and its transcript. What each CLI keeps of the conversation stays on disk."""
    with _lock():
        data = _read()
        data["items"].pop(sid, None)
        if data["current"] == sid:
            data["current"] = None
        _write(data)
        with contextlib.suppress(OSError):
            os.remove(_transcript_path(sid))
    current()


def _transcript_path(sid):
    return os.path.join(TRANSCRIPTS, re.sub(r"[^\w-]", "", sid) + ".jsonl")


def record(sid, role, text, provider=None, parts=None, error=None, checkpoint=None, usage=None):
    """One message of a conversation, as it happens. Stays on this Mac."""
    if not text and not parts and not error and not (checkpoint and checkpoint.get("files")):
        return
    os.makedirs(TRANSCRIPTS, mode=0o700, exist_ok=True)
    entry = {"role": role, "text": text, "at": time.time()}
    for k, v in (("provider", provider), ("parts", parts), ("error", error), ("checkpoint", checkpoint), ("usage", usage)):
        if v:
            entry[k] = v
    with _lock(), open(_transcript_path(sid), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _stamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0


def _flatten(content):
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return content if isinstance(content, str) else ""


def _claude_log(native_id, provider):
    """One message per turn of the assistant, with its text, thinking and tools in order."""
    out, tools = [], {}
    for path in glob.glob(os.path.expanduser(f"~/.claude/projects/*/{native_id}.jsonl"))[:1]:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("type") not in ("user", "assistant") or e.get("isMeta") or e.get("isSidechain"):
                    continue
                content = e.get("message", {}).get("content")
                blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
                if e["type"] == "user":
                    extra = e.get("toolUseResult")
                    for b in blocks:
                        if b.get("type") == "tool_result" and b.get("tool_use_id") in tools:
                            t = tools[b["tool_use_id"]]
                            t.update(output=_flatten(b.get("content"))[:30000], error=bool(b.get("is_error")), done=True)
                            if isinstance(extra, dict) and extra.get("structuredPatch"):
                                t["patch"] = extra["structuredPatch"]
                    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
                    if text:
                        out.append({"role": "user", "text": text, "at": _stamp(e.get("timestamp"))})
                    continue
                if not out or out[-1]["role"] != "assistant":
                    out.append({"role": "assistant", "text": "", "at": _stamp(e.get("timestamp")), "provider": provider, "parts": []})
                m = out[-1]
                for b in blocks:
                    if b.get("type") == "text" and b.get("text"):
                        m["text"] += b["text"]
                        m["parts"].append({"type": "text", "text": b["text"]})
                    elif b.get("type") == "thinking" and b.get("thinking"):
                        m["parts"].append({"type": "thinking", "text": b["thinking"]})
                    elif b.get("type") == "tool_use":
                        t = {"type": "tool", "id": b.get("id"), "name": b.get("name", ""), "input": b.get("input"), "done": True}
                        tools[b.get("id")] = t
                        m["parts"].append(t)
    return out


def _codex_log(native_id, provider):
    out, tools = [], {}
    for path in glob.glob(os.path.expanduser(f"~/.codex/sessions/*/*/*/rollout-*-{native_id}.jsonl"))[:1]:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                p = e.get("payload") or {}
                if e.get("type") != "response_item":
                    continue
                kind = p.get("type")
                if kind == "message" and p.get("role") in ("user", "assistant"):
                    text = "".join(b.get("text", "") for b in p.get("content", []) if b.get("type") in ("input_text", "output_text")).strip()
                    if p["role"] == "user":
                        if text.startswith("<"):       # the CLI's own context, not the person
                            continue
                        text = text.rsplit("# 用户的消息\n", 1)[-1].strip()  # what Carnot puts before each message
                        if text:
                            out.append({"role": "user", "text": text, "at": _stamp(e.get("timestamp"))})
                    elif text:
                        m = _codex_turn(out, e, provider)
                        m["text"] += text + "\n\n"
                        m["parts"].append({"type": "text", "text": text + "\n\n"})
                elif kind in ("function_call", "custom_tool_call"):
                    raw = p.get("arguments") or p.get("input") or ""
                    try:
                        args = json.loads(raw)
                    except (TypeError, ValueError):
                        args = {"input": raw}
                    t = {"type": "tool", "id": p.get("call_id"), "name": p.get("name", "tool"), "input": args, "done": True}
                    tools[p.get("call_id")] = t
                    _codex_turn(out, e, provider)["parts"].append(t)
                elif kind in ("function_call_output", "custom_tool_call_output") and p.get("call_id") in tools:
                    o = p.get("output")
                    tools[p["call_id"]]["output"] = (_flatten([dict(b, type="text") for b in o if isinstance(b, dict)]) if isinstance(o, list) else str(o or ""))[:30000]
    return out


def _codex_turn(out, e, provider):
    if not out or out[-1]["role"] != "assistant":
        out.append({"role": "assistant", "text": "", "at": _stamp(e.get("timestamp")), "provider": provider, "parts": []})
    return out[-1]


def history(sid, limit=500):
    """What was said: the CLIs' logs for what's older than the transcript, then the transcript."""
    item = get(sid)
    if not item:
        return []
    recorded = []
    with contextlib.suppress(OSError), open(_transcript_path(sid), encoding="utf-8") as f:
        for line in f:
            with contextlib.suppress(ValueError):
                recorded.append(json.loads(line))
    first = min((m.get("at", 0) for m in recorded), default=float("inf"))
    older = []
    for provider, native_id in item["native"].items():
        engine = providers.PROVIDERS.get(provider, {}).get("engine")
        reader = {"claude": _claude_log, "codex": _codex_log}.get(engine)
        if reader:
            older += [m for m in reader(native_id, provider) if m["at"] < first]
    older.sort(key=lambda m: m["at"])
    for m in older:
        m["text"] = m.get("text", "").strip()
    return (older + recorded)[-limit:]


def truncate(sid, keep):
    """Rewinds a conversation to its first `keep` messages. The CLIs can't forget, so their sessions
    are dropped: the next turn starts a new one and gets the recent messages handed over."""
    with _lock():
        kept = history(sid, 100000)[:keep]
        with open(_transcript_path(sid), "w", encoding="utf-8") as f:
            for m in kept:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        data = _read()
        if sid in data["items"]:
            data["items"][sid]["native"] = {}
            data["items"][sid]["updated"] = time.time()
            _write(data)
    return kept


def with_handoff(sid, provider, text):
    """An AI new to this conversation gets the recent messages, since it has no session of its own."""
    if native(sid, provider) or text.lstrip().startswith("/"):
        return text
    past = history(sid, HANDOFF_MESSAGES + 1)
    if past and past[-1]["role"] == "user" and past[-1]["text"].strip() == text.strip():
        past = past[:-1]
    past = [m for m in past if m.get("text")][-HANDOFF_MESSAGES:]
    if not past:
        return text
    lines = "\n\n".join(f"{'用户' if m['role'] == 'user' else 'AI'}：{m['text']}" for m in past)
    return f"以下是这段对话之前的内容，请接着继续：\n\n{lines}\n\n---\n\n{text}"


def import_chats(chats):
    """The VS Code extension kept its own chats once. Each comes over once, by its old id."""
    with _lock():
        data = _read()
        done = 0
        for c in chats:
            sid = "vs" + re.sub(r"[^\w]", "", str(c.get("id", "")))[:20]
            if not sid or sid in data["items"]:
                continue
            at = (c.get("at") or 0) / 1000 or time.time()
            data["items"][sid] = {"id": sid, "title": c.get("title") or UNTITLED, "client": "vscode",
                                  "native": c.get("sessions") or {}, "updated": at}
            os.makedirs(TRANSCRIPTS, mode=0o700, exist_ok=True)
            with open(_transcript_path(sid), "w", encoding="utf-8") as f:
                for i, m in enumerate(c.get("messages") or []):
                    m = {k: v for k, v in m.items() if k in ("role", "text", "provider", "tools", "error", "paths", "skill")}
                    m["at"] = at - len(c["messages"]) + i
                    f.write(json.dumps(m, ensure_ascii=False) + "\n")
            done += 1
        _write(data)
        return done


def listing(limit=30):
    with _lock():
        data = _read()
    items = sorted(data["items"].values(), key=lambda i: -i["updated"])[:limit]
    return {"current": data["current"], "items": [
        {"id": i["id"], "title": i["title"], "client": i["client"], "updated": i["updated"]} for i in items]}


def migrate(server_dir):
    """The single per-AI session files used before this store existed become one conversation."""
    with _lock():    # several processes can start at once on a first run
        if os.path.exists(FILE):
            return
        found = {}
        for path in glob.glob(os.path.join(server_dir, ".session_*")) + [os.path.join(server_dir, ".claude_session")]:
            try:
                value = open(path).read().strip()
            except OSError:
                continue
            if value:
                found["claude" if path.endswith(".claude_session") else os.path.basename(path)[len(".session_"):]] = value
        sid = create(title="之前的对话" if found else UNTITLED, make_current=True)
        for provider, value in found.items():
            set_native(sid, provider, value)
