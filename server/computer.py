"""Computer use: whether it's on, and how the CLIs get its tools.

off         no tools
background  works without your mouse, keyboard focus or screen; asks you before taking them over
full        may move the mouse and type straight away; Esc still takes it back

The tools are computer_mcp.py, which passes each call to the Carnot app (app/computer.swift on a Mac,
since macOS gives the app, not a terminal or VS Code, the permissions they need; windows/app/src/computer.rs
on Windows, where the app is what stays running to hold a takeover and watch for Esc)."""
import json
import os
import sys

import compat
import shared

FILE = os.path.join(shared.HOME, "computer.json")
MCP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "computer_mcp.py")
MODES = ("off", "background", "full")
SERVER = "carnot-computer"


def mode():
    try:
        with open(FILE) as f:
            m = json.load(f).get("mode")
        return m if m in MODES else "off"
    except (OSError, ValueError):
        return "off"


def set_mode(m):
    if m not in MODES:
        raise ValueError("unknown computer-use mode")
    os.makedirs(shared.HOME, exist_ok=True)
    tmp = FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"mode": m}, f)
    os.replace(tmp, FILE)


def server_spec():
    return {"command": sys.executable, "args": [MCP]}


def prompt(m=None):
    m = m or mode()
    if m == "off":
        return ""
    if compat.IS_WIN:
        return _prompt_windows(m)
    common = """# 计算机使用（用户已开启）
你有 carnot-computer 工具，可以操作这台 Mac 上的图形界面程序。
- 先用 list_apps / list_windows 找到目标；read_ui 读控件，press / set_text / choose_menu / app_keys 操作，capture_window 截单个窗口核对。这些都在后台进行，不动用户的鼠标，也不切换窗口，窗口被挡住也行。
- 程序没开就用 Bash 运行 open -g -a "程序名"（-g 不切到前台）。命令行或 AppleScript 能直接办的事，优先用它们。
- 每做一步都核对结果，别假设成功了。
- 发送消息、付款、删除、提交、改系统设置这类做了就收不回的操作，先在对话里征得用户同意。不读也不填密码框。"""
    if m == "full":
        return common + """
- 用户允许你直接接管鼠标键盘：后台办不到时，先调用 request_full_control（会立即批准），再用 screenshot 看屏幕，click / type_text / press_keys / scroll / drag 操作，坐标用最近一张 screenshot 的像素。用户按 Esc 会收回控制，收回后不要再抢。"""
    return common + """
- 后台确实办不到时（没有控件树、后台点击没反应等），调用 request_full_control 写明原因，用户在弹窗里同意后，才能用 screenshot / click / type_text / press_keys / scroll / drag 接管鼠标键盘，坐标用最近一张 screenshot 的像素。用户拒绝就换办法或问用户；按 Esc 会收回控制，收回后不要再抢。"""


def _prompt_windows(m):
    common = """# 计算机使用（用户已开启）
你有 carnot-computer 工具，可以操作这台 Windows 电脑上的图形界面程序。
- 先用 list_apps / list_windows 找到目标；read_ui 读控件，press / set_text / choose_menu / app_keys 操作，capture_window 截单个窗口核对。这些都在后台进行，不动用户的鼠标，也不切换窗口，窗口被挡住也行；最小化的窗口能读控件，但截不到画面。
- 程序没开就用 PowerShell 的 Start-Process 打开（新窗口一般会出现在前台）。命令行或 PowerShell 能直接办的事，优先用它们。
- 快捷键写 ctrl，不是 cmd。
- 每做一步都核对结果，别假设成功了。
- 发送消息、付款、删除、提交、改系统设置这类做了就收不回的操作，先在对话里征得用户同意。不读也不填密码框。"""
    if m == "full":
        return common + """
- 用户允许你直接接管鼠标键盘：后台办不到时，先调用 request_full_control（会立即批准），再用 screenshot 看屏幕，click / type_text / press_keys / scroll / drag 操作，坐标用最近一张 screenshot 的像素。用户按 Esc 会收回控制，收回后不要再抢。"""
    return common + """
- 后台确实办不到时（没有控件、后台点击或按键没反应等），调用 request_full_control 写明原因，用户在弹窗里同意后，才能用 screenshot / click / type_text / press_keys / scroll / drag 接管鼠标键盘，坐标用最近一张 screenshot 的像素。用户拒绝就换办法或问用户；按 Esc 会收回控制，收回后不要再抢。"""
