// Carnot for VS Code: a chat view over server/carnot_agent.py, the same code the app runs, so
// connections, memory and skills are the same ones.
const vscode = require("vscode");
const cp = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");

const MODES = ["plan", "manual", "acceptEdits", "auto", "bypassPermissions"];
const DIFF_SCHEME = "carnot-diff";
const EDIT_TOOLS = ["Edit", "MultiEdit", "Write"];
const SKIP_FILES = "**/{node_modules,.git,.venv,venv,dist,build,__pycache__}/**";

function permissionMode() {
  const set = vscode.workspace.getConfiguration("carnot").get("permissionMode");
  return MODES.includes(set) ? set : "auto";  // "default" was offered once; Claude Code has no such mode
}

function serverDir() {
  const set = vscode.workspace.getConfiguration("carnot").get("serverDir");
  if (set) return set;
  const carnotHome = process.env.CARNOT_HOME || path.join(os.homedir(), ".carnot");
  try { return fs.readFileSync(path.join(carnotHome, "server_dir"), "utf8").trim(); } catch { return ""; }
}

/** What the native diff view shows for text that isn't a file on disk. */
const diffTexts = new Map();
const diffProvider = { provideTextDocumentContent: uri => diffTexts.get(uri.toString()) ?? "" };
let diffSeq = 0;
function diffUri(name, text) {
  const uri = vscode.Uri.from({ scheme: DIFF_SCHEME, path: "/" + name, query: String(++diffSeq) });
  diffTexts.set(uri.toString(), text);
  return uri;
}

/** Where the files on the system clipboard are; Finder may hand them over as file reference URLs. */
function clipboardFiles() {
  const script = `ObjC.import("AppKit");
    const items = $.NSPasteboard.generalPasteboard.pasteboardItems, out = [];
    for (let i = 0; i < items.count; i++) {
      const url = items.objectAtIndex(i).stringForType("public.file-url");
      if (url.js) out.push($.NSURL.URLWithString(url).filePathURL.path.js);
    }
    JSON.stringify(out)`;
  return new Promise(resolve => cp.execFile("/usr/bin/osascript", ["-l", "JavaScript", "-e", script], { timeout: 5000 }, (err, stdout) => {
    try { resolve(err ? [] : JSON.parse(stdout).filter(p => fs.existsSync(p))); } catch { resolve([]); }
  }));
}

/** A preview of an edit means nothing once it has been answered. */
function closePreviews() {
  const stale = vscode.window.tabGroups.all.flatMap(g => g.tabs).filter(t =>
    t.input instanceof vscode.TabInputTextDiff && t.input.modified.scheme === DIFF_SCHEME && t.label.includes("待确认"));
  if (stale.length) vscode.window.tabGroups.close(stale, true);
}

/** Runs carnot_agent.py, calling onEvent for each JSON line. Resolves with the exit code. */
function agent(args, { input = "", keepOpen = false, onEvent = () => {}, onSpawn = () => {} } = {}) {
  return new Promise((resolve, reject) => {
    const dir = serverDir();
    const python = path.join(dir, ".venv", "bin", "python");
    if (!dir || !fs.existsSync(python)) return reject(new Error("找不到 Carnot 的服务目录，请在设置里填写 carnot.serverDir"));
    const child = cp.spawn(python, [path.join(dir, "carnot_agent.py"), ...args], { cwd: dir, env: process.env });
    onSpawn(child);
    let buf = "", err = "";
    child.stdout.setEncoding("utf8");  // a character can fall across two chunks
    child.stdout.on("data", d => {
      buf += d;
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 1);
        let event;
        try { event = JSON.parse(line); } catch { continue; /* not ours */ }
        try { onEvent(event); } catch (e) { console.error("carnot: event failed", event.type, e); }
      }
    });
    child.stderr.on("data", d => { err += d; });
    child.on("error", reject);
    child.on("close", code => code === 0 || code === null ? resolve(code) : reject(new Error(err.slice(-400) || `exit ${code}`)));
    child.stdin.on("error", () => {});  // an answer can arrive just after it exits
    if (keepOpen) child.stdin.write(input); else child.stdin.end(input);
  });
}

async function agentResult(args, input) {
  let last = null;
  await agent(args, { input, onEvent: e => { last = e; } });
  return last;
}

class Chat {
  constructor(context, folder = null) {
    this.context = context;
    this.folder = folder;  // an editor-tab chat works here; the sidebar one in the workspace
    this.host = null;      // the sidebar view or the editor tab
    this.conv = null;      // the open conversation: { id, title, client }
    this.messages = [];
    this.child = null;
    this.provider = context.globalState.get("provider", null);
    this.pendingFiles = [];
  }

  // ---------- webview ----------

  resolveWebviewView(view) {
    this.attach(view);
  }

  attach(view) {
    this.host = view;
    const media = vscode.Uri.joinPath(this.context.extensionUri, "media");
    view.webview.options = { enableScripts: true, localResourceRoots: [media] };
    const nonce = crypto.randomBytes(16).toString("hex");
    const css = view.webview.asWebviewUri(vscode.Uri.joinPath(media, "chat.css"));
    const js = view.webview.asWebviewUri(vscode.Uri.joinPath(media, "chat.js"));
    view.webview.html = `<!doctype html><html lang="zh"><head><meta charset="utf-8">
      <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${view.webview.cspSource}; script-src 'nonce-${nonce}'; img-src ${view.webview.cspSource} https: data:;">
      <link rel="stylesheet" href="${css}"></head><body><div id="log"></div><div id="bottom"></div>
      <script nonce="${nonce}" src="${js}"></script></body></html>`;
    view.webview.onDidReceiveMessage(m => this.handle(m).catch(e => this.post({ type: "error", text: e.message })));
  }

  post(m) { if (this.host) this.host.webview.postMessage(m); }

  async handle(m) {
    if (m.type === "ready") { await this.importOldChats(); return this.refresh(); }
    if (m.type === "permissionAnswer") return this.answer(m);
    if (m.type === "previewEdit") return this.previewEdit(m.tool, m.input);
    if (m.type === "viewChange") return this.viewChange(m);
    if (m.type === "undoFile") return this.undo(m, [m.path]);
    if (m.type === "undoTurn") return this.undo(m, null);
    if (m.type === "rewind") return this.rewind(m.ordinal);
    if (m.type === "mention") return this.mention(m.q);
    if (m.type === "pasteFiles") return this.pasteFiles();
    if (m.type === "pasteImage") return this.pasteImage(m.data, m.mime);
    if (m.type === "dropped") return this.dropped(m.uris);
    if (m.type === "copy") return vscode.env.clipboard.writeText(m.text);
    if (m.type === "openFile") return this.openFile(m.path, m.line);
    if (m.type === "openSession") return this.open(m.id);
    if (m.type === "renameSession") return this.rename(m.id, m.title);
    if (m.type === "deleteSession") return this.remove(m.id, m.title);
    if (m.type === "newChat") return this.newChat();
    if (m.type === "history") return this.history();
    if (m.type === "send") return this.send(m.text, m.paths || [], m.skill, m.withFile);
    if (m.type === "stop") { if (this.child) this.child.kill("SIGTERM"); return; }
    if (m.type === "provider") { this.provider = m.id; await this.context.globalState.update("provider", m.id); return this.refresh(); }
    if (m.type === "permission") return this.setPermission(m.mode);
    if (m.type === "setModel") return this.showModels(await agentResult(["model-set", m.provider, m.model || "", m.effort || ""]));
    if (m.type === "attach") return this.pick();
    if (m.type === "connections") return this.connections();
  }

  async refresh() {
    const [status, skills, commands, models] = await Promise.all([agentResult(["status"]), agentResult(["skills"]), agentResult(["commands"]), agentResult(["models"])]);
    if (!this.provider) this.provider = status.current;
    this.status = status;
    this.skills = skills.items;
    this.commands = commands.items;
    this.models = models.items;
    this.postState();
    agentResult(["models", "--refresh"]).then(r => this.showModels(r)).catch(() => {});  // a vendor may have new ones
  }

  showModels(r) {
    if (!r) return;
    this.models = r.items;
    this.post({ type: "models", items: r.items });
  }

  /** The app may have changed the choice: it lives in one place, for both. */
  async reloadModels() { this.showModels(await agentResult(["models"])); }

  /** Switching conversations changes none of what refresh() asks for, and asking takes seconds. */
  showConversation() { return this.status ? this.postState() : this.refresh(); }

  postState() {
    this.post({ type: "state", provider: this.provider, providers: this.status.items, skills: this.skills, commands: this.commands, models: this.models,
                messages: this.messages, conv: this.conv, busy: !!this.child || !!this.sending, file: this.fileLabel(), permission: permissionMode() });
  }

  async setPermission(mode) {
    if (!MODES.includes(mode)) return;
    if (mode === "bypassPermissions") {
      if (!await this.confirmBypass()) return this.post({ type: "permission", mode: permissionMode() });
    }
    await vscode.workspace.getConfiguration("carnot").update("permissionMode", mode, vscode.ConfigurationTarget.Global);
    this.post({ type: "permission", mode });
  }

  workdir() {
    return this.folder || vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || os.homedir();
  }

  // ---------- questions from the AI, answered here ----------

  answer(m) {
    if (!this.child) return;
    const { type, ...reply } = m;
    this.child.stdin.write(JSON.stringify(reply) + "\n");
    closePreviews();
  }

  /** Never on top of this chat: a chat in a tab keeps its group, diffs use the next one. */
  diffColumn() {
    const column = this.host && this.host.viewColumn;
    return column === vscode.ViewColumn.One ? vscode.ViewColumn.Two : vscode.ViewColumn.One;
  }

  /** What a pending edit would do, in VS Code's own diff view. */
  async previewEdit(tool, input) {
    const file = path.isAbsolute(input.file_path) ? input.file_path : path.join(this.workdir(), input.file_path);
    let now = "";
    try { now = fs.readFileSync(file, "utf8"); } catch { /* a new file */ }
    let next = now;
    const apply = e => { next = e.replace_all ? next.split(e.old_string).join(e.new_string) : next.replace(e.old_string, () => e.new_string); };
    if (tool === "Write") next = input.content ?? "";
    else if (tool === "MultiEdit") (input.edits || []).forEach(apply);
    else apply(input);
    const name = path.basename(file);
    await vscode.commands.executeCommand("vscode.diff", diffUri(name, now), diffUri(name, next), `${name}（待确认的改动）`,
      { preview: true, preserveFocus: true, viewColumn: this.diffColumn() });
  }

  /** A turn's change to one file: the snapshot from before it against the file as it is now. */
  async viewChange({ cwd, before, path: rel, status }) {
    const old = status === "A" ? "" : (await agentResult(["cp-show", "--cwd", cwd, before, rel])).text;
    const full = path.join(cwd, rel), name = path.basename(rel);
    const right = fs.existsSync(full) ? vscode.Uri.file(full) : diffUri(name, "");
    await vscode.commands.executeCommand("vscode.diff", diffUri(name, old), right, `${name}（这一轮的改动）`, { preview: true, viewColumn: this.diffColumn() });
  }

  async undo({ cwd, before, card }, paths) {
    if (!paths) {
      const yes = "撤销";
      const pick = await vscode.window.showWarningMessage("把这一轮改动的文件全部恢复到这一轮之前？之后你手动做的修改也会被覆盖。", { modal: true }, yes);
      if (pick !== yes) return;
    }
    const r = await agentResult(["cp-restore", "--cwd", cwd, before, ...(paths || [])]);
    this.post({ type: "undone", card, paths: r.restored });
  }

  /** Back to before one of your messages: the code, the conversation, or both. */
  async rewind(ordinal) {
    if (this.child || !this.conv) return;
    const h = (await agentResult(["history", this.conv.id])).messages;
    let seen = -1, at = -1;
    h.some((m, i) => { if (m.role === "user" && ++seen === ordinal) { at = i; return true; } return false; });
    if (at < 0) return;
    const point = h.slice(at + 1).find(m => m.checkpoint)?.checkpoint;
    const both = "代码和对话", talk = "仅对话", code = "仅代码";
    const buttons = point ? [both, talk, code] : [talk];
    const pick = await vscode.window.showWarningMessage("回退到这条消息之前？", { modal: true, detail: point ? "代码会恢复到当时的状态，之后的修改（包括你手动改的）都会被覆盖。" : "这之后没有文件改动，只回退对话。" }, ...buttons);
    if (!pick) return;
    if (pick !== talk && point) await agentResult(["cp-restore", "--cwd", point.cwd, point.before]);
    if (pick !== code) {
      await agentResult(["rewind", this.conv.id, String(at - h.length)]);  // from the end: the list shown can be a tail
      await this.open(this.conv.id);
      this.post({ type: "setInput", text: h[at].text });
    }
    else vscode.window.showInformationMessage("代码已恢复，对话保持不变。");
  }

  // ---------- what can go in the box ----------

  async mention(q) {
    const seen = new Set(), items = [];
    const add = uri => {
      if (uri.scheme !== "file" || seen.has(uri.fsPath)) return;
      seen.add(uri.fsPath);
      items.push({ path: uri.fsPath, label: vscode.workspace.asRelativePath(uri, false) });
    };
    const clean = q.replace(/[{}[\]*?]/g, "");
    for (const t of vscode.window.tabGroups.all.flatMap(g => g.tabs)) {
      const uri = t.input && t.input.uri;
      if (uri && (!clean || uri.fsPath.toLowerCase().includes(clean.toLowerCase()))) add(uri);
    }
    const base = new vscode.RelativePattern(this.workdir(), clean ? `**/*${clean}*` : "**/*");
    (await vscode.workspace.findFiles(base, SKIP_FILES, 30)).forEach(add);
    this.post({ type: "mentions", q, items: items.slice(0, 30) });
  }

  async pasteFiles() {
    const paths = await clipboardFiles();
    this.post(paths.length ? { type: "attached", paths } : { type: "noPastedFiles" });
  }

  pasteImage(data, mime) {
    const home = process.env.CARNOT_HOME || path.join(os.homedir(), ".carnot");
    const dir = path.join(home, "inbox", "vscode");
    fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    const ext = { "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp", "image/heic": "heic", "image/heif": "heif", "image/tiff": "tiff" }[mime] || "png";
    const file = path.join(dir, `${new Date().toISOString().replace(/[:.]/g, "-")}-${crypto.randomBytes(3).toString("hex")}.${ext}`);
    fs.writeFileSync(file, Buffer.from(data, "base64"));
    this.post({ type: "attached", paths: [file] });
  }

  dropped(uris) {
    const paths = uris.map(u => { try { return vscode.Uri.parse(u).fsPath; } catch { return null; } }).filter(p => p && fs.existsSync(p));
    if (paths.length) this.post({ type: "attached", paths });
  }

  async confirmBypass() {
    const yes = "确认放行";
    return await vscode.window.showWarningMessage("完全放行后，AI 可以不经确认执行任何操作，包括删除文件和对外发送内容。确定吗？", { modal: true }, yes) === yes;
  }

  fileLabel() {
    const e = vscode.window.activeTextEditor;
    return e && e.document.uri.scheme === "file" ? path.basename(e.document.uri.fsPath) : "";
  }

  fileNote() {
    const e = vscode.window.activeTextEditor;
    if (!e || e.document.uri.scheme !== "file") return "";
    const s = e.selection;
    const lines = s.isEmpty ? "" : `，选中第 ${s.start.line + 1}-${s.end.line + 1} 行`;
    return `\n\n[当前文件：${e.document.uri.fsPath}${lines}]`;
  }

  async pick() {
    const picked = await vscode.window.showOpenDialog({ canSelectFiles: true, canSelectFolders: true, canSelectMany: true, title: "附加文件或文件夹" });
    if (picked) this.post({ type: "attached", paths: picked.map(u => u.fsPath) });
  }

  // ---------- conversations, shared with the app and the bots ----------

  /** Chats from when the extension kept its own come into the shared list, once. */
  async importOldChats() {
    const old = this.context.globalState.get("chats", []);
    if (!old.length) return;
    await agentResult(["import-chats"], JSON.stringify(old));
    await this.context.globalState.update("chats", undefined);
  }

  newChat() {
    if (this.child) this.child.kill("SIGTERM");
    this.conv = null;
    this.messages = [];
    return this.showConversation();
  }

  /** The list of every conversation, from any client, in the chat view. */
  async history() {
    const list = await agentResult(["sessions"]);
    this.post({ type: "historyList", items: list.items, open: this.conv && this.conv.id });
  }

  async open(id) {
    if (this.child) return this.post({ type: "error", text: "当前回答还没结束，停止后再切换。" });
    const [h, list] = await Promise.all([agentResult(["history", id]), agentResult(["sessions"])]);
    this.conv = list.items.find(i => i.id === id) || { id, title: h.title };
    this.messages = h.messages;
    await this.showConversation();
  }

  async rename(id, title) {
    const next = await vscode.window.showInputBox({ prompt: "重命名这个对话", value: title });
    if (!next) return;
    await agentResult(["session-rename", id, next]);
    if (this.conv && this.conv.id === id) { this.conv.title = next; await this.refresh(); }
    return this.history();
  }

  async remove(id, title) {
    const yes = "删除";
    const pick = await vscode.window.showWarningMessage(`删除“${title}”？之后不能再继续这个对话。`, { modal: true }, yes);
    if (pick !== yes) return;
    await agentResult(["session-delete", id]);
    if (this.conv && this.conv.id === id) await this.newChat();
    return this.history();
  }

  // ---------- a turn ----------

  async send(text, paths, skill, withFile) {
    if (this.child || this.sending) return;
    if (!this.provider) await this.refresh();
    const pid = this.provider;
    let prompt = text || "请看附件。";
    const dirs = [];
    for (const p of paths) {
      const isDir = fs.statSync(p).isDirectory();
      prompt += `\n\n[附件${isDir ? "文件夹" : "文件"}：${p}]`;
      const d = isDir ? p : path.dirname(p);
      if (!dirs.includes(d)) dirs.push(d);
    }
    if (withFile) prompt += this.fileNote();
    const shown = prompt;
    if (skill) {
      const s = (this.skills || []).find(x => x.name === skill);
      if (s) prompt = `[请按这个 skill 的说明完成任务：${s.name}，说明文件：${s.path}，先读取它]\n\n${prompt}`;
    }
    this.messages.push({ role: "user", text, paths, skill });
    const reply = { role: "assistant", text: "", parts: [], provider: pid };
    this.messages.push(reply);
    this.post({ type: "user", text, paths, skill });
    this.post({ type: "busy", on: true, provider: pid });
    this.sending = true;  // no child yet while the conversation is being made
    try {
      if (!this.conv) {
        const made = await agentResult(["session-new"]);
        this.conv = { id: made.id, title: text.slice(0, 28) || "新对话", client: "vscode" };
      }
      await this.turn(pid, prompt, shown, dirs, reply, false);
    } finally {
      this.child = null;
      this.sending = false;
      this.reloadModels().catch(() => {});
      closePreviews();
      this.post({ type: "busy", on: false });
    }
  }

  async turn(pid, prompt, shown, dirs, reply, retried) {
    const cwd = this.workdir();
    const args = ["run", "--provider", pid, "--cwd", cwd, "--permission", permissionMode(), "--conversation", this.conv.id];
    if (retried) args.push("--retry");
    for (const d of dirs) args.push("--dir", d);
    let done = null;
    await agent(args, {
      input: JSON.stringify({ text: prompt, shown }) + "\n",
      keepOpen: true,
      onSpawn: c => { this.child = c; },
      onEvent: e => {
        if (e.type === "text") {
          reply.text += e.text;
          const last = reply.parts[reply.parts.length - 1];
          if (last && last.type === "text") last.text += e.text; else reply.parts.push({ type: "text", text: e.text });
          this.post({ type: "delta", text: e.text });
        } else if (e.type === "step") {
          this.applyStep(reply, e);
          this.post({ type: "step", step: e });
        } else if (e.type === "permission") {
          this.post({ type: "permission", request: e });
          if (EDIT_TOOLS.includes(e.tool) && e.input.file_path) this.previewEdit(e.tool, e.input).catch(() => {});
        } else if (e.type === "checkpoint") {
          reply.checkpoint = e;
          this.post({ type: "checkpoint", point: e });
        }
        else if (e.type === "done") { done = e; this.child && this.child.stdin.end(); }
      },
    });
    if (!done) return;
    if (done.status === "no_conversation" && !retried) return this.turn(pid, prompt, shown, dirs, reply, true);
    if (done.status === "auth" && !retried) {
      if (await this.signIn(pid)) return this.turn(pid, prompt, shown, dirs, reply, true);
      return this.fail(reply, "登录或 API Key 无效，请在连接设置里更新。");
    }
    if (done.status === "auth") return this.fail(reply, "登录之后还是连不上。");
    if (done.status === "missing") {
      this.fail(reply, `还没安装 ${done.detail}，请在连接设置里安装。`);
      return this.connections();
    }
    if (done.status === "failed") this.fail(reply, `执行失败：${done.detail}`);
  }

  async openFile(file, line) {
    const full = path.isAbsolute(file) ? file : path.join(this.folder || vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || os.homedir(), file);
    const doc = await vscode.window.showTextDocument(vscode.Uri.file(full), { preview: true, viewColumn: vscode.ViewColumn.One });
    if (line) {
      const at = new vscode.Position(Math.max(0, line - 1), 0);
      doc.selection = new vscode.Selection(at, at);
      doc.revealRange(new vscode.Range(at, at), vscode.TextEditorRevealType.InCenter);
    }
  }

  /** Keeps the message in order, as the page shows it, so it can be drawn again. */
  applyStep(reply, e) {
    const parts = reply.parts;
    if (e.kind === "thinking") {
      const last = parts[parts.length - 1];
      if (last && last.type === "thinking") last.text += e.text; else parts.push({ type: "thinking", text: e.text });
    } else if (e.kind === "usage") {
      reply.usage = e;
    } else if (e.kind === "tool_start") {
      parts.push({ type: "tool", id: e.id, name: e.name, input: e.input });
    } else if (e.kind === "tool_end") {
      const t = parts.find(p => p.type === "tool" && p.id === e.id);
      if (t) Object.assign(t, { output: e.output, error: e.error, patch: e.patch, done: true });
    }
  }

  fail(reply, text) {
    reply.error = text;
    this.post({ type: "error", text });
  }

  /** A refused subscription login opens the browser and waits; a refused key needs a new key. */
  async signIn(pid) {
    await this.refresh();
    const p = this.status.items.find(i => i.id === pid);
    if (p.has_key || !p.login) {
      vscode.window.showWarningMessage(`${p.name} 的 API Key 无效或已过期`, "更新 Key").then(a => a && this.setKey(p));
      return false;
    }
    vscode.window.showInformationMessage(`${p.name} 需要登录，已打开浏览器`);
    const r = await agentResult(["login", pid]);
    return !!r && r.type === "ok";
  }

  // ---------- connections ----------

  async connections() {
    const status = await agentResult(["status"]);
    const picked = await vscode.window.showQuickPick(status.items.map(p => ({
      label: p.name,
      description: !p.installed ? `需要先安装 ${p.needs}` : p.has_key ? "已连接（API Key）" : p.signed_in ? "已连接（订阅登录）" : "未连接",
      p,
    })), { placeHolder: "选择要设置的 AI" });
    if (!picked) return;
    const p = picked.p, actions = [{ label: "使用它", id: "use" }];
    if (!p.installed) actions.push({ label: `安装 ${p.needs}`, id: "install" });
    if (p.installed && p.login) actions.push({ label: p.signed_in ? "重新登录" : "登录订阅", id: "login" });
    if (p.signed_in) actions.push({ label: "退出登录", id: "logout" });
    actions.push({ label: p.has_key ? "更新 API Key" : "填写 API Key", id: "key" });
    if (p.has_key) actions.push({ label: "删除 API Key", id: "delkey" });
    actions.push({ label: "获取 API Key（打开网页）", id: "url" });
    const a = await vscode.window.showQuickPick(actions, { placeHolder: p.name });
    if (!a) return;
    if (a.id === "use") await this.handle({ type: "provider", id: p.id });
    if (a.id === "url") vscode.env.openExternal(vscode.Uri.parse(p.key_url));
    if (a.id === "key") await this.setKey(p);
    if (a.id === "delkey") await agentResult(["del-key", p.id]);
    if (a.id === "logout") await agentResult(["logout", p.id]);
    if (a.id === "login") {
      vscode.window.showInformationMessage(`正在打开 ${p.name} 的登录页面`);
      await agentResult(["login", p.id]);
    }
    if (a.id === "install") {
      await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: `正在安装 ${p.needs}…` }, async () => {
        const r = await agentResult(["install", p.id]);
        if (!r || r.type !== "ok") vscode.window.showErrorMessage(`安装失败（${r ? r.rc : "?"}）`);
      });
    }
    return this.refresh();
  }

  async setKey(p) {
    const key = await vscode.window.showInputBox({ password: true, prompt: `${p.name} 的 API Key，只保存在这台 Mac 的钥匙串里`, ignoreFocusOut: true });
    if (key) { await agentResult(["set-key", p.id], key); await this.refresh(); }
  }
}

/** The folder of the file being edited: its workspace folder if it has one, else where it is. */
function currentFolder() {
  const uri = vscode.window.activeTextEditor?.document.uri;
  if (uri && uri.scheme === "file") {
    const wf = vscode.workspace.getWorkspaceFolder(uri);
    return wf ? wf.uri.fsPath : path.dirname(uri.fsPath);
  }
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || os.homedir();
}

function activate(context) {
  const chat = new Chat(context);
  const tabs = new Set();
  const openHere = () => {
    const folder = currentFolder();
    const panel = vscode.window.createWebviewPanel("carnot.tab", `Carnot · ${path.basename(folder) || folder}`, vscode.ViewColumn.Beside,
      { enableScripts: true, retainContextWhenHidden: true });
    panel.iconPath = vscode.Uri.joinPath(context.extensionUri, "media", "editor-icon.svg");
    const tab = new Chat(context, folder);
    tab.attach(panel);
    tabs.add(tab);
    panel.onDidDispose(() => {
      tabs.delete(tab);
      tab.host = null;
      if (tab.child) tab.child.kill("SIGTERM");
    });
    return tab;
  };
  const reg = (name, fn) => context.subscriptions.push(vscode.commands.registerCommand(name, fn));
  context.subscriptions.push(vscode.workspace.registerTextDocumentContentProvider(DIFF_SCHEME, diffProvider));
  context.subscriptions.push(vscode.window.registerWebviewViewProvider("carnot.chat", chat, { webviewOptions: { retainContextWhenHidden: true } }));
  reg("carnot.openHere", openHere);
  reg("carnot.newChat", () => chat.newChat());
  reg("carnot.history", () => chat.history());
  reg("carnot.connections", () => chat.connections());
  reg("carnot.openMemory", () => {
    const home = process.env.CARNOT_HOME || path.join(os.homedir(), ".carnot");
    return vscode.window.showTextDocument(vscode.Uri.file(path.join(home, "memory", "MEMORY.md")));
  });
  reg("carnot.askSelection", async () => {
    await vscode.commands.executeCommand("carnot.chat.focus");
    chat.post({ type: "focus", withFile: true });
  });
  context.subscriptions.push(vscode.window.onDidChangeActiveTextEditor(() => {
    for (const c of [chat, ...tabs]) c.post({ type: "file", file: c.fileLabel() });
  }));
  context.subscriptions.push(vscode.window.onDidChangeWindowState(s => {
    if (s.focused) for (const c of [chat, ...tabs]) if (c.host) c.reloadModels().catch(() => {});
  }));
  return { chat, tabs, agent, agentResult };
}

module.exports = { activate, deactivate() {} };
