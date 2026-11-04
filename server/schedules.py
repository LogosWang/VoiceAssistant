"""Tasks the AI runs by itself at set times, managed from the app's settings window.

They run while the app is running and the Mac is awake, one at a time, each as a fresh session of the AI,
written to one conversation per task. Nobody is there to approve anything, so they go no further than `auto`.
"""
import contextlib
import datetime
import fcntl
import json
import os
import subprocess
import threading
import time
import uuid

import agent_core
import events
import providers
import sessions
import shared

STORE = os.path.join(shared.SCHEDULES_DIR, "tasks.json")
LOCK = os.path.join(shared.SCHEDULES_DIR, ".lock")
OLD_STORE = os.path.join(shared.HOME, "schedules.json")   # where the first version kept them
MODES = ("plan", "acceptEdits", "auto")
REPEATS = ("days", "every", "once")
TICK = 5
ON_TIME = 180          # seconds late that still counts as on time, whatever catch_up says
TIMEOUT = 3600
MAX_OUTPUT = 8000
SYSTEM = """这是一个定时任务，由 Carnot 按设定的时间自动发起，此刻可能没有人在电脑前。
1. 不要提问，也不要等确认。缺信息时按最合理的理解做，并在结果里说明你的假设。
2. 需要确认才能做的操作会被拒绝。遇到这种情况就跳过，并在结果里写清楚哪一步没做。
3. 不要新建或修改定时任务。
4. 最后用几句话写明做了什么、结果如何，这段话会给用户看。"""


def _parse_time(text):
    hour, minute = str(text).split(":")[:2]
    return datetime.time(int(hour), int(minute))


def next_run(task, after):
    """The first time this task is due after `after` (epoch seconds), or None when it never will be."""
    repeat = task["repeat"]
    if repeat == "once":
        return task["at"] if task["at"] > after else None
    if repeat == "every":
        return after + task["every"] * 60
    start = datetime.datetime.fromtimestamp(after)
    clock = _parse_time(task["time"])
    for offset in range(8):
        day = start.date() + datetime.timedelta(days=offset)
        when = datetime.datetime.combine(day, clock)
        if day.weekday() in task["days"] and when.timestamp() > after:
            return when.timestamp()
    return None


def clean(raw, default_dir):
    """A task as the settings page sent it, checked. Raises ValueError with what to tell the user."""
    name, prompt = str(raw.get("name", "")).strip(), str(raw.get("prompt", "")).strip()
    if not prompt:
        raise ValueError("还没写要做什么")
    repeat = raw.get("repeat")
    if repeat not in REPEATS:
        raise ValueError("重复方式不对")
    workdir = os.path.expanduser(str(raw.get("workdir") or "").strip()) or default_dir
    if not os.path.isdir(workdir):
        raise ValueError("工作文件夹不存在")
    provider = raw.get("provider") or None
    if provider is not None and provider not in providers.PROVIDERS:
        raise ValueError("没有这个 AI")
    task = {"name": name[:60] or prompt[:24], "prompt": prompt, "repeat": repeat, "workdir": workdir,
            "provider": provider, "permission": raw.get("permission") if raw.get("permission") in MODES else "auto",
            "catch_up": bool(raw.get("catch_up", True))}
    try:
        if repeat == "days":
            task["time"] = _parse_time(raw.get("time")).strftime("%H:%M")
            task["days"] = sorted({int(d) for d in raw.get("days", []) if 0 <= int(d) <= 6})
        elif repeat == "every":
            task["every"] = int(raw.get("every"))
        else:
            at = raw.get("at")
            task["at"] = at if isinstance(at, (int, float)) else datetime.datetime.fromisoformat(str(at)).timestamp()
    except (TypeError, ValueError, AttributeError):
        raise ValueError("时间格式不对") from None
    if repeat == "days" and not task["days"]:
        raise ValueError("至少选一天")
    if repeat == "every" and not 1 <= task["every"] <= 60 * 24 * 30:
        raise ValueError("间隔要在 1 分钟到 30 天之间")
    if repeat == "once" and task["at"] <= time.time():
        raise ValueError("这个时间已经过了")
    return task


@contextlib.contextmanager
def _locked():
    os.makedirs(shared.SCHEDULES_DIR, mode=0o700, exist_ok=True)
    with open(LOCK, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        if os.path.exists(OLD_STORE) and not os.path.exists(STORE):
            os.replace(OLD_STORE, STORE)
        yield


def _read():
    try:
        with open(STORE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _write(tasks):
    tmp = STORE + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STORE)


@contextlib.contextmanager
def _edit():
    """The app's service and any AI's command line both change this list, so each change starts from the file."""
    with _locked():
        tasks = _read()
        yield tasks
        _write(tasks)


def listing():
    with _locked():
        return _read()


def _find(tasks, tid):
    task = next((t for t in tasks if t["id"] == tid), None)
    if task is None:
        raise ValueError("没有这个定时任务")
    return task


def add(raw, default_dir, source="app"):
    task = clean(raw, default_dir)
    task.update(id=uuid.uuid4().hex[:12], enabled=True, created=time.time(), source=source)
    task["next_run"] = next_run(task, time.time())
    with _edit() as tasks:
        tasks.append(task)
    return task


def update(tid, changes, default_dir):
    with _edit() as tasks:
        task = _find(tasks, tid)
        fresh = clean({**task, **changes}, default_dir)
        for gone in {"time", "days", "every", "at"} - set(fresh):
            task.pop(gone, None)
        task.update(fresh)
        task["next_run"] = next_run(task, time.time()) if task["enabled"] else None
    return task


def remove(tid):
    with _edit() as tasks:
        tasks.remove(_find(tasks, tid))


def set_enabled(tid, on):
    with _edit() as tasks:
        task = _find(tasks, tid)
        due = next_run(task, time.time()) if on else None
        if on and due is None:
            raise ValueError("这个任务的时间已经过了，请先修改时间")
        task.update(enabled=bool(on), next_run=due)
    return task


def request_run(tid):
    """The service picks this up on its next tick."""
    with _edit() as tasks:
        _find(tasks, tid)["run_requested"] = True


class Manager:
    def __init__(self, claude_cfg, log, on_sessions, dry_run=False):
        self.workdir = os.path.expanduser(claude_cfg["workdir"])
        self.add_dirs = claude_cfg.get("add_dirs", [])
        self.log = log
        self.on_sessions = on_sessions
        self.dry_run = dry_run
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.queue = []
        self.current = None     # (task id, agent)
        self.seen = None        # the file's state when last reported
        with _edit() as tasks:
            for t in tasks:
                if t.get("last_status") == "running":   # the app went away in the middle of it
                    t["last_status"] = "interrupted"

    def report(self):
        with self.lock:
            running = self.current[0] if self.current else None
            queued = list(self.queue)
        items = [{**t, "running": t["id"] == running, "queued": t["id"] in queued} for t in listing()]
        events.emit("schedules", items=items)

    @property
    def active(self):
        return self.current is not None or bool(self.queue)

    # ---------- what the settings page asks for ----------

    def save(self, raw):
        if raw.get("id"):
            task = update(raw["id"], {k: v for k, v in raw.items() if k != "id"}, self.workdir)
        else:
            task = add(raw, self.workdir)
        self.log(f"[schedule] saved {task['id']}")
        self.report()

    def remove(self, tid):
        self.stop_task(tid)
        remove(tid)
        self.report()

    def enable(self, tid, on):
        try:
            set_enabled(tid, on)
        except ValueError as e:
            events.emit("notice", text=str(e), hold=8000)
        self.report()

    def run_now(self, tid):
        if self.dry_run:
            self.log(f"[schedule] dry run, not running {tid}")
            return
        with self.lock:
            if tid not in self.queue and not (self.current and self.current[0] == tid):
                self.queue.append(tid)
        self.wake.set()
        self.report()

    def stop_task(self, tid):
        with self.lock:
            self.queue = [q for q in self.queue if q != tid]
            if self.current and self.current[0] == tid:
                self.current[1].stop()
        self.report()

    def stop(self):
        with self.lock:
            self.queue = []
            if self.current:
                self.current[1].stop()

    # ---------- running ----------

    def start(self):
        threading.Thread(target=self._clock, daemon=True).start()
        threading.Thread(target=self._worker, daemon=True).start()

    def _clock(self):
        while True:
            try:
                self._due(time.time())
            except Exception as e:
                self.log(f"[schedule] clock: {type(e).__name__}")
            time.sleep(TICK)

    def _due(self, now):
        """Queues what's due. However many times a task was missed, it runs at most once.
        Also notices what an AI's command line changed: new tasks are announced, so none goes unseen."""
        changed, announce = False, []
        with _locked():
            tasks = _read()
            ids = {t["id"] for t in tasks}
            for t in tasks:
                if t.get("source") == "ai" and not t.get("announced"):
                    t["announced"] = True
                    announce.append(t["name"])
                    changed = True
                if t.pop("run_requested", False):
                    changed = True
                    if not self.dry_run:
                        self._enqueue(t["id"])
                when = t.get("next_run")
                if not t.get("enabled") or when is None or when > now:
                    continue
                late = now - when
                t["next_run"] = next_run(t, now)
                if t["next_run"] is None:
                    t["enabled"] = False
                changed = True
                if late > ON_TIME and not t.get("catch_up", True):
                    self.log(f"[schedule] {t['id']} missed its time, skipped")
                    t["last_status"], t["last_run"] = "missed", now
                elif not self.dry_run:
                    self._enqueue(t["id"])
            if changed:
                _write(tasks)
            state = os.stat(STORE).st_mtime_ns if os.path.exists(STORE) else None
        with self.lock:                 # removed from elsewhere while it was running
            if self.current and self.current[0] not in ids:
                self.current[1].stop()
        for name in announce:
            self.log("[schedule] an AI added a task")
            events.emit("notice", text=f"AI 新建了定时任务“{name}”，可在定时任务里查看", hold=8000)
            _notify(f"AI 新建了定时任务“{name}”")
        if state != self.seen:
            self.seen = state
            self.report()

    def _enqueue(self, tid):
        with self.lock:
            if tid not in self.queue and not (self.current and self.current[0] == tid):
                self.queue.append(tid)
                self.wake.set()

    def _worker(self):
        while True:
            self.wake.wait()
            with self.lock:
                tid = self.queue.pop(0) if self.queue else None
                task = next((dict(t) for t in listing() if t["id"] == tid), None)
                if not self.queue and not task:
                    self.wake.clear()
            if task:
                try:
                    self._run(task)
                except Exception as e:
                    self.log(f"[schedule] {tid} failed: {type(e).__name__}")
                    self._finish(tid, "failed", f"{type(e).__name__}: {str(e)[:200]}", "", None)

    def _conversation(self, task):
        sid = task.get("session")
        if sid and sessions.get(sid):
            return sid
        return sessions.create(client="schedule", title=task["name"])

    def _run(self, task):
        tid = task["id"]
        sid = self._conversation(task)
        pid = task.get("provider") or providers.current()
        sessions.touch(sid, task["prompt"])
        sessions.record(sid, "user", task["prompt"])
        said = []
        agent = agent_core.Agent(pid, task["workdir"], task["permission"], system=SYSTEM,
                                 add_dirs=self.add_dirs, on_text=said.append, on_log=self.log, may_schedule=False,
                                 on_session=lambda s: sessions.set_native(sid, pid, s))
        with self.lock:
            self.current = (tid, agent)
        with _edit() as tasks:
            for t in tasks:
                if t["id"] == tid:
                    t.update(last_status="running", last_run=time.time(), session=sid)
        self.report()
        self.on_sessions()
        limit = threading.Timer(TIMEOUT, agent.stop)
        limit.daemon = True
        limit.start()
        started = time.time()
        status, detail = agent.run(task["prompt"])
        limit.cancel()
        if status == "stopped" and time.time() - started >= TIMEOUT:
            status, detail = "timeout", f"超过 {TIMEOUT // 60} 分钟，已停止"
        text = "".join(said).strip()
        sessions.record(sid, "assistant", text, provider=pid, parts=agent.parts, usage=agent.usage or None,
                        error=None if status == "ok" else detail or status)
        self._finish(tid, status, detail, text, task["name"])
        self.on_sessions()

    def _finish(self, tid, status, detail, text, name):
        with self.lock:
            self.current = None
        with _edit() as tasks:
            for t in tasks:
                if t["id"] == tid:
                    t.update(last_status=status, last_detail=str(detail)[-500:], last_output=text[:MAX_OUTPUT])
        self.log(f"[schedule] {tid}: {status}")
        self.report()
        if name and status != "stopped":
            word = "完成了" if status == "ok" else "没有完成"
            events.emit("notice", text=f"定时任务“{name}”{word}", hold=8000)
            _notify(f"定时任务“{name}”{word}")


def _notify(text):
    """The name of the task is all that shows; the result stays in the app."""
    script = 'on run argv\ndisplay notification (item 1 of argv) with title "Carnot"\nend run'
    subprocess.Popen(["/usr/bin/osascript", "-e", script, text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
