"""Command line front of agent_core for the VS Code extension. One process per turn or action,
JSON lines out. Needs nothing beyond the standard library.

    carnot_agent.py run --provider claude --cwd DIR [--session ID] [--permission MODE] [--dir D]...
        (the message comes on stdin)
    carnot_agent.py status | skills | seed
    carnot_agent.py login|logout|del-key|install PROVIDER
    carnot_agent.py set-key PROVIDER   (the key comes on stdin)
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import tomllib

import agent_core
import providers
import shared

HERE = os.path.dirname(os.path.abspath(__file__))
SYSTEM = """你正在 VS Code 里和用户对话，帮用户写代码、改文件、回答问题。用 Markdown 回复，引用文件时写明路径。
消息末尾的 [附件文件：路径]、[附件文件夹：路径]、[当前文件：路径] 是用户附加的内容，需要时读取。"""


def out(**ev):
    sys.stdout.write(json.dumps(ev, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def configured_model():
    try:
        with open(os.path.join(HERE, "config.toml"), "rb") as f:
            return tomllib.load(f)["claude"].get("model")
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return None


def run(args):
    agent = agent_core.Agent(
        args.provider, args.cwd, args.permission, system=SYSTEM, model=configured_model(), session=args.session,
        on_text=lambda t: out(type="text", text=t), on_session=lambda s: out(type="session", id=s),
        on_tool=lambda name, detail: out(type="tool", name=name, detail=detail))
    signal.signal(signal.SIGTERM, lambda *_: agent.stop())
    status, detail = agent.run(sys.stdin.read(), args.dir)
    out(type="done", status=status, detail=detail, session=agent.session)


def main():
    providers.fix_path()
    shared.seed_memory()
    shared.sync_skills()
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="action", required=True)
    r = sub.add_parser("run")
    r.add_argument("--provider", default=providers.current(), choices=list(providers.PROVIDERS))
    r.add_argument("--cwd", default=os.path.expanduser("~"))
    r.add_argument("--session")
    r.add_argument("--permission", default="auto")
    r.add_argument("--dir", action="append", default=[])
    for name in ("status", "skills", "seed"):
        sub.add_parser(name)
    for name in ("login", "logout", "set-key", "del-key", "install"):
        sub.add_parser(name).add_argument("provider", choices=list(providers.PROVIDERS))
    args = ap.parse_args()

    if args.action == "run":
        run(args)
    elif args.action == "status":
        out(**providers.statuses())
    elif args.action == "skills":
        out(type="skills", items=shared.list_skills())
    elif args.action == "seed":
        out(type="memory", dir=shared.MEMORY_DIR)
    elif args.action == "set-key":
        providers.set_key(args.provider, sys.stdin.read())
        out(type="ok")
    elif args.action == "del-key":
        providers.del_key(args.provider)
        out(type="ok")
    elif args.action == "install":
        rc = subprocess.run(["npm", "install", "-g", providers.PROVIDERS[args.provider]["package"]],
                            capture_output=True).returncode
        out(type="ok" if rc == 0 else "failed", rc=rc)
    elif args.action == "login":
        cmd = providers.login_command(args.provider)
        if not cmd:
            providers.open_terminal(providers.PROVIDERS[args.provider]["cli"])
            out(type="terminal")
        else:
            rc = subprocess.run(cmd, env=providers.apply_env(args.provider), stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=600).returncode
            out(type="ok" if rc == 0 else "failed", rc=rc)
    elif args.action == "logout":
        if args.provider == "claude":
            subprocess.run(["claude", "auth", "logout"], capture_output=True)
        elif args.provider == "chatgpt":
            subprocess.run(["codex", "logout"], capture_output=True)
        elif args.provider == "gemini":
            path = os.path.expanduser("~/.gemini/oauth_creds.json")
            if os.path.exists(path):
                os.remove(path)
        out(type="ok")


if __name__ == "__main__":
    main()
