"""What every client and every AI shares: one memory folder and one set of skills.

Memory is ~/.carnot/memory: a MEMORY.md index plus one file per fact. It is handed to whichever AI
runs, on every turn, so a fact saved from VS Code under one model is there in the app under another.
Skills stay where they are, in ~/.claude/skills and ~/.agents/skills (read by Codex and Gemini); each
side gets a symlink to what the other has. Nothing existing is moved or overwritten.
"""
import os
import re
import shutil

HOME = os.path.expanduser(os.environ.get("CARNOT_HOME", "~/.carnot"))
MEMORY_DIR = os.path.join(HOME, "memory")
INDEX = os.path.join(MEMORY_DIR, "MEMORY.md")
SEEDED = os.path.join(HOME, ".memory_seeded")
PERSONA = os.path.join(MEMORY_DIR, "carnot-persona.md")
PERSONA_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "persona.md")
PORTRAIT = os.path.join(MEMORY_DIR, "carnot-portrait.png")
PORTRAIT_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portrait.png")
KNOWLEDGE = os.path.join(MEMORY_DIR, "knowledge")
KNOWLEDGE_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge")
CLAUDE_HOME = os.path.expanduser("~/.claude")
SKILL_DIRS = (os.path.join(CLAUDE_HOME, "skills"), os.path.expanduser("~/.agents/skills"))
TEMP_PROJECTS = ("scratch-workspaces", "-private-tmp")


def _project_label(folder):
    return folder.rsplit("-", 1)[-1] if folder.count("-") > 1 else folder.strip("-")


def seed_memory():
    """First run: copies what Claude Code already remembers, per project, into the shared folder.
    One-way and once; the originals stay. Scratch workspaces are skipped."""
    os.makedirs(MEMORY_DIR, exist_ok=True)
    if os.path.exists(SEEDED):
        return 0
    copied, lines = 0, []
    projects = os.path.join(CLAUDE_HOME, "projects")
    for project in sorted(os.listdir(projects)) if os.path.isdir(projects) else []:
        src = os.path.join(projects, project, "memory")
        if not os.path.isdir(src) or any(t in project for t in TEMP_PROJECTS):
            continue
        label = re.sub(r"[^\w-]", "_", _project_label(project))
        renamed = {}
        for name in sorted(os.listdir(src)):
            if name.endswith(".md") and name != "MEMORY.md":
                renamed[name] = f"{label}__{name}"
                shutil.copy2(os.path.join(src, name), os.path.join(MEMORY_DIR, renamed[name]))
                copied += 1
        index = os.path.join(src, "MEMORY.md")
        if renamed and os.path.exists(index):
            text = open(index, encoding="utf-8").read()
            for old, new in renamed.items():
                text = text.replace(f"({old})", f"({new})")
            lines.append(f"## From project {label}\n{text.strip()}\n")
    if not os.path.exists(INDEX) or lines:
        with open(INDEX, "a", encoding="utf-8") as f:
            f.write("\n".join(lines))
    with open(SEEDED, "w") as f:
        f.write("1")
    return copied


def ensure_persona():
    """Who Carnot is. Lives with the memories so it can be edited like one; never overwritten."""
    os.makedirs(MEMORY_DIR, exist_ok=True)
    if not os.path.exists(PERSONA) and os.path.exists(PERSONA_DEFAULT):
        shutil.copy2(PERSONA_DEFAULT, PERSONA)
        with open(INDEX, "a", encoding="utf-8") as f:
            f.write("\n- [Carnot 的人物设定](carnot-persona.md) — 名字、来历、性格，以及知识体系和分寸\n")
    _add_portrait()
    ensure_knowledge()


def _add_portrait():
    if not os.path.exists(PORTRAIT) and os.path.exists(PORTRAIT_DEFAULT):
        shutil.copy2(PORTRAIT_DEFAULT, PORTRAIT)
        with open(INDEX, "a", encoding="utf-8") as f:
            f.write("- [Carnot 的立绘](carnot-portrait.png) — 自己的样子，被问到长相时打开看\n")


def ensure_knowledge():
    """Adds any knowledge file not there yet, and its line in the index. Never overwrites one."""
    os.makedirs(KNOWLEDGE, exist_ok=True)
    for name in sorted(os.listdir(KNOWLEDGE_DEFAULT)) if os.path.isdir(KNOWLEDGE_DEFAULT) else []:
        target = os.path.join(KNOWLEDGE, name)
        if not name.endswith(".md") or os.path.exists(target):
            continue
        shutil.copy2(os.path.join(KNOWLEDGE_DEFAULT, name), target)
        head = open(target, encoding="utf-8").read(600)
        title = re.search(r"^# (.+)$", head, re.M)
        desc = re.search(r"^description:\s*(.+)$", head, re.M)
        with open(INDEX, "a", encoding="utf-8") as f:
            f.write(f"- [{title.group(1) if title else name}](knowledge/{name}) — {desc.group(1) if desc else ''}\n")


def persona_prompt():
    """Handed over whole on every turn: unlike other memories, it can't wait to be looked up."""
    try:
        text = open(PERSONA, encoding="utf-8").read()
    except OSError:
        return ""
    return "# 你的人物设定\n" + re.sub(r"\A---.*?---\s*", "", text, flags=re.S).strip()


def _skill_folders(root):
    if not os.path.isdir(root):
        return {}
    return {n: os.path.join(root, n) for n in sorted(os.listdir(root))
            if os.path.exists(os.path.join(root, n, "SKILL.md"))}


def sync_skills():
    """Links each skill into the other tool's folder, if that name isn't taken. Returns links made."""
    made = 0
    a, b = SKILL_DIRS
    for src_root, dst_root in ((a, b), (b, a)):
        for name, path in _skill_folders(src_root).items():
            target = os.path.join(dst_root, name)
            if not os.path.lexists(target):
                os.makedirs(dst_root, exist_ok=True)
                os.symlink(os.path.realpath(path), target)
                made += 1
    return made


def list_skills():
    found = {}
    for root in SKILL_DIRS:
        for name, path in _skill_folders(root).items():
            if name in found:
                continue
            try:
                head = open(os.path.join(path, "SKILL.md"), encoding="utf-8", errors="replace").read(4000)
            except OSError:
                continue
            m = re.search(r"^description:\s*(.+?)\s*$", head, re.M)
            desc = m.group(1).strip("\"'|> ") if m else ""
            found[name] = {"name": name, "description": desc[:160], "path": os.path.join(path, "SKILL.md")}
    return sorted(found.values(), key=lambda s: s["name"])


def memory_prompt():
    ensure_persona()
    try:
        index = open(INDEX, encoding="utf-8").read().strip()
    except OSError:
        index = ""
    return persona_prompt() + f"""

# 共享记忆
你和用户在不同的软件、不同的 AI 模型下共用一套长期记忆，目录是 {MEMORY_DIR}。
下面是索引 MEMORY.md，话题相关时才读取其中链接的文件，不相关就不要读，也不要提；旧记忆可能过时，用之前先核实。
用户让你记住什么、纠正你、或出现以后还用得上的事实时，在这个目录新建或更新一个 .md 文件（一个文件一件事），并在 MEMORY.md 加一行 `- [标题](文件名) — 一句话`。只用这个目录记忆，不要写到别处。

{index or "（目前还是空的）"}"""


def instructions_prompt():
    """The user's global instructions. Claude Code reads these itself; the other CLIs don't."""
    path = os.path.join(CLAUDE_HOME, "CLAUDE.md")
    try:
        return "# 用户的全局指令\n" + open(path, encoding="utf-8").read().strip()
    except OSError:
        return ""


def skill_note(name):
    """Asked for by name: works the same under every AI, by pointing at the instructions."""
    for s in list_skills():
        if s["name"] == name:
            return f"[请按这个 skill 的说明完成任务：{name}，说明文件：{s['path']}，先读取它]"
    return ""
