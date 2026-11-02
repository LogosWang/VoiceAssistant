"""Conversations: what the menu lists and what bots continue. Each one remembers, per AI, the id of
the CLI's own session, so a conversation can move between AIs and between clients. Only titles and
ids are stored here; the conversations themselves stay with each CLI."""
import glob
import json
import os
import threading
import time
import uuid

import shared

FILE = os.path.join(shared.HOME, "sessions.json")
UNTITLED = "新对话"
KEEP = 200
_lock = threading.RLock()


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
    os.replace(tmp, FILE)


def _trim(data):
    items = data["items"]
    for sid in sorted(items, key=lambda s: items[s]["updated"])[:max(0, len(items) - KEEP)]:
        if sid != data["current"]:
            del items[sid]


def create(client="app", title=UNTITLED, make_current=False):
    with _lock:
        data = _read()
        sid = uuid.uuid4().hex[:12]
        data["items"][sid] = {"id": sid, "title": title, "client": client, "native": {}, "updated": time.time()}
        if make_current:
            data["current"] = sid
        _trim(data)
        _write(data)
        return sid


def get(sid):
    with _lock:
        return _read()["items"].get(sid)


def current():
    """The app's conversation; there's always one."""
    with _lock:
        data = _read()
        if data["current"] in data["items"]:
            return data["current"]
        return create(make_current=True)


def set_current(sid):
    with _lock:
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
    with _lock:
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
    with _lock:
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
    with _lock:
        data = _read()
        if title and sid in data["items"]:
            data["items"][sid]["title"] = title
            _write(data)


def delete(sid):
    """Only forgets the entry. What the CLI keeps of the conversation stays on disk."""
    with _lock:
        data = _read()
        data["items"].pop(sid, None)
        if data["current"] == sid:
            data["current"] = None
        _write(data)
    current()


def listing(limit=30):
    with _lock:
        data = _read()
    items = sorted(data["items"].values(), key=lambda i: -i["updated"])[:limit]
    return {"current": data["current"], "items": [
        {"id": i["id"], "title": i["title"], "client": i["client"], "updated": i["updated"]} for i in items]}


def migrate(server_dir):
    """The single per-AI session files used before this store existed become one conversation."""
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
