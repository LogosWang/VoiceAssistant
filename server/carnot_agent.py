"""Command line front of agent_core for the VS Code extension. One process per turn or action,
JSON lines out. Needs nothing beyond the standard library.

    carnot_agent.py run --provider claude --cwd DIR --conversation ID [--permission MODE] [--dir D]...
        (stdin: the message as one JSON line {"text": ...}, then one line per answer to a question)
    carnot_agent.py commands
    carnot_agent.py cp-show --cwd DIR TREE PATH | cp-restore --cwd DIR TREE [PATH]...
    carnot_agent.py rewind ID KEEP
    carnot_agent.py status | skills | seed | sessions
    carnot_agent.py history ID | session-new | session-delete ID | session-rename ID TITLE
    carnot_agent.py import-chats   (old extension chats as JSON on stdin)
    carnot_agent.py login|logout|del-key|install PROVIDER
    carnot_agent.py set-key PROVIDER   (the key comes on stdin)
"""
import argparse
import contextlib
import json
import os
import queue
import signal
import subprocess
import sys
import threading

import agent_core
import checkpoints
import cli_install
import model_choice
import compat
import providers
import schedules
import sessions
import shared

HERE = os.path.dirname(os.path.abspath(__file__))
COMMANDS_CACHE = os.path.join(shared.HOME, "claude_commands.json")
# in print mode, what these do has no meaning or needs a terminal
NOT_IN_CHAT = {"exit", "quit", "login", "logout", "vim", "terminal-setup", "ide", "theme", "help", "doctor", "resume", "rewind"}
SYSTEM = """你正在 VS Code 里和用户对话，帮用户写代码、改文件、回答问题。用 Markdown 回复，引用文件时写明路径。
消息末尾的 [附件文件：路径]、[附件文件夹：路径]、[当前文件：路径] 是用户附加的内容，需要时读取。"""


def out(**ev):
    sys.stdout.write(json.dumps(ev, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def run(args):
    sid, pid = args.conversation, args.provider
    shared.turn_started()
    answers = queue.SimpleQueue()  # put() is safe from the signal handler
    said = []
    def on_text(t):
        said.append(t)
        out(type="text", text=t)
    def on_step(step):
        if step.get("kind") == "init":
            remember_commands(step)
        out(type="step", **step)
    def on_permission(request):
        out(type="permission", **request)
        while True:
            answer = answers.get()
            if answer is None:         # the extension went away: nothing was approved
                return {"allow": False, "message": "没有人回答这个确认。"}
            if answer.get("id") == request["id"]:
                return answer
    first = json.loads(sys.stdin.readline() or "{}")
    def read_answers():
        for line in sys.stdin:
            with contextlib.suppress(ValueError):
                message = json.loads(line)
                if message.get("type") == "stop":  # Windows has no SIGTERM to catch
                    stop()
                else:
                    answers.put(message)
        answers.put(None)
    agent = agent_core.Agent(
        pid, args.cwd, args.permission, system=SYSTEM, session=sessions.native(sid, pid),
        on_text=on_text, on_session=lambda s: sessions.set_native(sid, pid, s), on_step=on_step, on_permission=on_permission)
    def stop(*_):
        agent.stop()
        answers.put(None)      # a question still open would hold the turn up forever
    signal.signal(signal.SIGTERM, stop)
    threading.Thread(target=read_answers, daemon=True).start()
    text = first.get("text", "")
    if not args.retry:                # a retry repeats a turn already written down
        sessions.touch(sid, text)
        sessions.record(sid, "user", first.get("shown", text))
    before = sessions.native(sid, pid)
    tree = checkpoints.snapshot(args.cwd)
    status, detail = agent.run(sessions.with_handoff(sid, pid, text), args.dir)
    after = checkpoints.snapshot(args.cwd) if tree else None
    point = {"before": tree, "after": after, "cwd": args.cwd, "files": checkpoints.changes(args.cwd, tree, after)} if tree else None
    if point:
        out(type="checkpoint", **point)
    if status == "no_conversation":   # the CLI lost it: the next try starts anew, with the handoff
        sessions.set_native(sid, pid, None)
    elif status == "auth" and before:
        sessions.set_native(sid, pid, before)
    if status != "no_conversation":
        sessions.record(sid, "assistant", "".join(said).strip(), provider=pid, parts=agent.parts,
                        checkpoint=point if point and point["files"] else None, usage=agent.usage or None)
    out(type="done", status=status, detail=detail)


def remember_commands(step):
    """Claude Code says what commands it has when a session starts; the menu needs them before that."""
    hidden = set(step.get("terminal") or []) | NOT_IN_CHAT
    names = [c for c in step.get("commands") or [] if c not in hidden]
    if names:
        tmp = COMMANDS_CACHE + f".{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(names, f, ensure_ascii=False)
        compat.replace(tmp, COMMANDS_CACHE)


def known_commands():
    try:
        with open(COMMANDS_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return ["compact", "context", "cost", "model", "init", "review", "memory", "agents"]


def schedule_command(args):
    """How any AI reaches the one list of scheduled tasks. Nothing it asks for goes past clean()."""
    here = os.getcwd()
    try:
        if args.action == "schedule-list":
            out(type="schedules", items=schedules.listing())
        elif args.action == "schedule-add":
            out(type="ok", task=schedules.add(json.loads(args.task), here, source="ai"))
        elif args.action == "schedule-update":
            out(type="ok", task=schedules.update(args.id, json.loads(args.changes), here))
        elif args.action == "schedule-remove":
            schedules.remove(args.id)
            out(type="ok")
        elif args.action == "schedule-enable":
            out(type="ok", task=schedules.set_enabled(args.id, args.state == "on"))
        elif args.action == "schedule-run":
            schedules.request_run(args.id)
            out(type="ok", note="Carnot App 开着的话，几秒内开始运行")
    except ValueError as e:     # includes bad JSON
        out(type="error", text=str(e))
        sys.exit(1)


def main():
    compat.utf8_stdio()
    providers.fix_path()
    shared.seed_memory()
    shared.sync_skills()
    sessions.migrate(HERE)
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="action", required=True)
    r = sub.add_parser("run")
    r.add_argument("--provider", default=providers.current(), choices=list(providers.PROVIDERS))
    r.add_argument("--cwd", default=os.path.expanduser("~"))
    r.add_argument("--conversation", required=True)
    r.add_argument("--retry", action="store_true")
    r.add_argument("--permission", default=providers.DEFAULT_PERMISSION)
    r.add_argument("--dir", action="append", default=[])
    sub.add_parser("schedule-list")
    sub.add_parser("schedule-add").add_argument("task")
    su = sub.add_parser("schedule-update")
    su.add_argument("id")
    su.add_argument("changes")
    for name in ("schedule-remove", "schedule-run"):
        sub.add_parser(name).add_argument("id")
    se = sub.add_parser("schedule-enable")
    se.add_argument("id")
    se.add_argument("state", choices=["on", "off"])
    for name in ("status", "skills", "seed", "sessions", "session-new", "import-chats", "commands"):
        sub.add_parser(name)
    cs = sub.add_parser("cp-show")
    cs.add_argument("--cwd", required=True)
    cs.add_argument("tree")
    cs.add_argument("path")
    cr = sub.add_parser("cp-restore")
    cr.add_argument("--cwd", required=True)
    cr.add_argument("tree")
    cr.add_argument("paths", nargs="*")
    rw = sub.add_parser("rewind")
    rw.add_argument("id")
    rw.add_argument("keep", type=int)
    sub.add_parser("history").add_argument("id")
    sub.add_parser("session-delete").add_argument("id")
    rn = sub.add_parser("session-rename")
    rn.add_argument("id")
    rn.add_argument("title")
    sub.add_parser("models").add_argument("--refresh", action="store_true")
    ms = sub.add_parser("model-set")
    ms.add_argument("provider", choices=list(providers.PROVIDERS))
    ms.add_argument("model", nargs="?", default="")
    ms.add_argument("effort", nargs="?", default="")
    for name in ("login", "logout", "set-key", "del-key", "install"):
        sub.add_parser(name).add_argument("provider", choices=list(providers.PROVIDERS))
    args = ap.parse_args()

    if args.action == "run":
        run(args)
    elif args.action == "status":
        out(**providers.statuses())
    elif args.action.startswith("schedule-"):
        schedule_command(args)
    elif args.action == "commands":
        out(type="commands", items=known_commands())
    elif args.action == "cp-show":
        out(type="file", text=checkpoints.show(args.cwd, args.tree, args.path))
    elif args.action == "cp-restore":
        out(type="ok", restored=checkpoints.restore(args.cwd, args.tree, args.paths or None))
    elif args.action == "rewind":
        out(type="ok", kept=len(sessions.truncate(args.id, args.keep)))
    elif args.action == "sessions":
        out(type="sessions", **sessions.listing(sessions.KEEP))
    elif args.action == "history":
        out(type="history", id=args.id, title=(sessions.get(args.id) or {}).get("title", ""), messages=sessions.history(args.id))
    elif args.action == "session-new":
        out(type="session", id=sessions.create(client="vscode"))
    elif args.action == "session-delete":
        sessions.delete(args.id)
        out(type="ok")
    elif args.action == "session-rename":
        sessions.rename(args.id, args.title)
        out(type="ok")
    elif args.action == "import-chats":
        out(type="ok", imported=sessions.import_chats(json.load(sys.stdin)))
    elif args.action == "skills":
        out(type="skills", items=shared.list_skills())
    elif args.action == "models":
        if args.refresh:
            model_choice.refresh_stale()
        out(type="models", items=model_choice.listing())
    elif args.action == "model-set":
        model_choice.set_choice(args.provider, args.model, args.effort)
        out(type="models", items=model_choice.listing())
    elif args.action == "seed":
        out(type="memory", dir=shared.MEMORY_DIR)
    elif args.action == "set-key":
        providers.set_key(args.provider, sys.stdin.read())
        if providers.PROVIDERS[args.provider].get("endpoints"):
            providers.detect_endpoint(args.provider)
        model_choice.refresh(args.provider)
        out(type="ok")
    elif args.action == "del-key":
        providers.del_key(args.provider)
        out(type="ok")
    elif args.action == "install":
        ok, detail = cli_install.install(args.provider)
        out(type="ok" if ok else "failed", rc=0 if ok else 1, detail=detail)
    elif args.action == "login":
        cmd = providers.login_command(args.provider)
        if not cmd:
            providers.open_terminal(providers.PROVIDERS[args.provider]["cli"])
            out(type="terminal")
        else:
            rc = subprocess.run(compat.argv(cmd), env=providers.apply_env(args.provider), stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=600).returncode
            out(type="ok" if rc == 0 else "failed", rc=rc)
    elif args.action == "logout":
        if args.provider == "claude":
            subprocess.run(compat.argv(["claude", "auth", "logout"]), capture_output=True)
        elif args.provider == "chatgpt":
            subprocess.run(compat.argv(["codex", "logout"]), capture_output=True)
        elif args.provider == "gemini":
            path = os.path.expanduser("~/.gemini/oauth_creds.json")
            if os.path.exists(path):
                os.remove(path)
        out(type="ok")


if __name__ == "__main__":
    main()
