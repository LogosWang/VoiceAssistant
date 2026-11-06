"""MCP server (stdio) with the computer-use tools. Each call goes to the Carnot app over
~/.carnot/computer.sock; see computer.py and app/computer.swift. One of these runs per CLI turn,
so an approved takeover lasts for that task only."""
import json
import os
import socket
import sys

SOCK = os.environ.get("CARNOT_COMPUTER_SOCK") or os.path.join(os.path.expanduser(os.environ.get("CARNOT_HOME", "~/.carnot")), "computer.sock")
FILE = os.path.join(os.path.dirname(SOCK), "computer.json")

S, I, B, A = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}, {"type": "number"}


def tool(name, desc, props=None, required=()):
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": props or {}, "required": list(required)}}


APP = {"app": {**S, "description": "程序名、bundle id 或 pid"}}
BACKGROUND = [
    tool("list_apps", "列出正在运行的程序。后台操作。"),
    tool("list_windows", "列出窗口和编号，可只看一个程序。包括被挡住的窗口。后台操作。", {"app": S}),
    tool("capture_window", "截一个窗口的画面，窗口被挡住也行，不用切到前台。", {"window": I}, ["window"]),
    tool("read_ui", "读一个程序窗口里的控件（按钮、输入框、文字…），每个带编号，供 press / set_text 用。默认读它的当前窗口，"
         "也可指定 list_windows 的窗口编号。query 只列包含这段文字的控件。后台操作。",
         {**APP, "window": I, "query": S, "depth": I}, ["app"]),
    tool("press", "按 read_ui 列出的某个控件（按钮、复选框、标签页、列表项…），不动鼠标。action 可选，如 AXShowMenu。",
         {**APP, "element": I, "action": S}, ["app", "element"]),
    tool("set_text", "把 read_ui 列出的输入框内容直接设成 text，不动键盘焦点。", {**APP, "element": I, "text": S}, ["app", "element", "text"]),
    tool("choose_menu", "选程序菜单栏里的菜单项，path 如 [\"文件\", \"导出…\"]。后台操作。",
         {**APP, "path": {"type": "array", "items": S}}, ["app", "path"]),
    tool("app_keys", "只给某个程序发打字（text）和/或按键（keys，如 \"cmd+s\"、\"return\"、多个用空格隔开），不切到前台。有些程序会忽略。",
         {**APP, "text": S, "keys": S}, ["app"]),
    tool("click_in_window", "后台点击窗口里的一点，x、y 用 capture_window 那张图的像素。不动鼠标，但很多程序不接受，点完必须截图核对。",
         {"window": I, "x": A, "y": A, "button": {"type": "string", "enum": ["left", "right"]}, "double": B}, ["window", "x", "y"]),
    tool("request_full_control", "后台办不到时，请求接管鼠标和键盘。会弹窗问用户，写清原因。批准后才能用 screenshot、click 等工具。",
         {"reason": S}, ["reason"]),
]
FULL = [
    tool("screenshot", "截整个屏幕（接管后）。display 是第几块屏幕，默认主屏。", {"display": I}),
    tool("click", "移动鼠标并点击（接管后），x、y 用最近一张 screenshot 的像素。",
         {"x": A, "y": A, "button": {"type": "string", "enum": ["left", "right"]}, "double": B}, ["x", "y"]),
    tool("move_mouse", "移动鼠标（接管后）。", {"x": A, "y": A}, ["x", "y"]),
    tool("drag", "按住左键从 (x, y) 拖到 (to_x, to_y)（接管后）。", {"x": A, "y": A, "to_x": A, "to_y": A}, ["x", "y", "to_x", "to_y"]),
    tool("scroll", "在 (x, y) 处滚动（接管后），down、right 是行数，负数反向。", {"x": A, "y": A, "down": I, "right": I}, ["x", "y"]),
    tool("type_text", "在当前焦点打字（接管后）。", {"text": S}, ["text"]),
    tool("press_keys", "按键（接管后），如 \"cmd+c\"、\"return\"、\"cmd+shift+4\"，多个用空格隔开。", {"keys": S}, ["keys"]),
    tool("activate_app", "把一个程序切到前台（接管后）。", APP, ["app"]),
]
OPS = {"list_apps": "apps", "list_windows": "windows", "capture_window": "capture", "read_ui": "ui", "press": "press",
       "set_text": "set", "choose_menu": "menu", "app_keys": "app_keys", "click_in_window": "wclick",
       "request_full_control": "grant", "screenshot": "screen", "click": "click", "move_mouse": "move", "drag": "drag",
       "scroll": "scroll", "type_text": "type", "press_keys": "key", "activate_app": "activate"}
FULL_NAMES = {t["name"] for t in FULL}
grant = None


def mode():
    try:
        with open(FILE) as f:
            return json.load(f).get("mode", "off")
    except (OSError, ValueError):
        return "off"


def ask_app(req, wait):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(wait)
    try:
        s.connect(SOCK)
    except OSError:
        return {"ok": False, "error": "Carnot 应用没有在运行，计算机使用要靠它。请用户打开 Carnot 后再试"}
    with s:
        s.sendall(json.dumps(req, ensure_ascii=False).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(1 << 20)
            if not chunk:
                break
            buf += chunk
    try:
        return json.loads(buf)
    except ValueError:
        return {"ok": False, "error": "Carnot 应用没有回应"}


def call(name, args):
    global grant
    if name not in OPS:
        return {"ok": False, "error": f"没有工具 {name}"}
    if name in FULL_NAMES and not grant:
        if mode() != "full":
            return {"ok": False, "error": "这是接管鼠标键盘的操作，要先调用 request_full_control 说明原因，用户同意后才能用"}
        r = ask_app({"op": "grant", "reason": ""}, 30)
        if not r.get("ok"):
            return r
        grant = r.get("grant")
    req = {**args, "op": OPS[name], "grant": grant}
    r = ask_app(req, 150 if name == "request_full_control" else 60)
    if name == "request_full_control" and r.get("ok"):
        grant = r.get("grant")
    elif name in FULL_NAMES and not r.get("ok") and "request_full_control" in (r.get("error") or ""):
        grant = None  # taken back with Esc, or timed out
    return r


def answer(rid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": rid}
    msg.update({"error": error} if error else {"result": result})
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        rid, method = m.get("id"), m.get("method")
        if rid is None:
            continue  # a notification
        if method == "initialize":
            answer(rid, {"protocolVersion": (m.get("params") or {}).get("protocolVersion", "2025-06-18"),
                         "capabilities": {"tools": {}}, "serverInfo": {"name": "carnot-computer", "version": "1"}})
        elif method == "tools/list":
            answer(rid, {"tools": BACKGROUND + FULL})
        elif method == "tools/call":
            p = m.get("params") or {}
            r = call(p.get("name"), p.get("arguments") or {})
            content = [{"type": "text", "text": r.get("text") or r.get("error") or ""}]
            if r.get("image"):
                content.append({"type": "image", "data": r["image"], "mimeType": r.get("mime", "image/jpeg")})
            answer(rid, {"content": content, "isError": not r.get("ok")})
        elif method == "ping":
            answer(rid, {})
        else:
            answer(rid, error={"code": -32601, "message": f"unknown method {method}"})


if __name__ == "__main__":
    main()
