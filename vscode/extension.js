// Carnot for VS Code: a chat view over server/carnot_agent.py, the same code the app runs, so
// connections, memory and skills are the same ones.
const vscode = require("vscode");
const cp = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");

const KEEP_CHATS = 50;
const CONTEXT_ON_SWITCH = 10;  // messages handed over when a chat moves to another AI
const MODES = ["plan", "acceptEdits", "auto", "bypassPermissions"];

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

/** Runs carnot_agent.py, calling onEvent for each JSON line. Resolves with the exit code. */
function agent(args, { input = "", onEvent = () => {}, onSpawn = () => {} } = {}) {
  return new Promise((resolve, reject) => {
    const dir = serverDir();
    const python = path.join(dir, ".venv", "bin", "python");
    if (!dir || !fs.existsSync(python)) return reject(new Error("找不到 Carnot 的服务目录，请在设置里填写 carnot.serverDir"));
    const child = cp.spawn(python, [path.join(dir, "carnot_agent.py"), ...args], { cwd: dir, env: process.env });
    onSpawn(child);
    let buf = "", err = "";
    child.stdout.on("data", d => {
      buf += d;
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 1);
        try { onEvent(JSON.parse(line)); } catch { /* not ours */ }
      }
    });
    child.stderr.on("data", d => { err += d; });
    child.on("error", reject);
    child.on("close", code => code === 0 || code === null ? resolve(code) : reject(new Error(err.slice(-400) || `exit ${code}`)));
    child.stdin.end(input);
  });
}

async function agentResult(args, input) {
  let last = null;
  await agent(args, { input, onEvent: e => { last = e; } });
  return last;
}

class Chat {
  constructor(context) {
    this.context = context;
    this.view = null;
    this.chats = context.globalState.get("chats", []);
    this.chat = null;
    this.child = null;
    this.provider = context.globalState.get("provider", null);
    this.pendingFiles = [];
  }

  // ---------- webview ----------

  resolveWebviewView(view) {
    this.view = view;
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

  post(m) { if (this.view) this.view.webview.postMessage(m); }

  async handle(m) {
    if (m.type === "ready") return this.refresh();
    if (m.type === "send") return this.send(m.text, m.paths || [], m.skill, m.withFile);
    if (m.type === "stop") { if (this.child) this.child.kill("SIGTERM"); return; }
    if (m.type === "provider") { this.provider = m.id; await this.context.globalState.update("provider", m.id); return this.refresh(); }
    if (m.type === "permission") return this.setPermission(m.mode);
    if (m.type === "attach") return this.pick();
    if (m.type === "connections") return this.connections();
  }

  async refresh() {
    const status = await agentResult(["status"]);
    const skills = await agentResult(["skills"]);
    if (!this.provider) this.provider = status.current;
    this.status = status;
    this.skills = skills.items;
    this.post({ type: "state", provider: this.provider, providers: status.items, skills: skills.items,
                messages: this.chat ? this.chat.messages : [], busy: !!this.child, file: this.fileLabel(), permission: permissionMode() });
  }

  async setPermission(mode) {
    if (!MODES.includes(mode)) return;
    if (mode === "bypassPermissions") {
      if (!await this.confirmBypass()) return this.post({ type: "permission", mode: permissionMode() });
    }
    await vscode.workspace.getConfiguration("carnot").update("permissionMode", mode, vscode.ConfigurationTarget.Global);
    this.post({ type: "permission", mode });
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

  // ---------- chats ----------

  newChat() {
    if (this.child) this.child.kill("SIGTERM");
    this.chat = null;
    return this.refresh();
  }

  async history() {
    const pick = await vscode.window.showQuickPick(
      this.chats.map(c => ({ label: c.title, description: new Date(c.at).toLocaleString(), chat: c })),
      { placeHolder: "选择要继续的对话" });
    if (pick) { this.chat = pick.chat; await this.refresh(); }
  }

  save() {
    this.chat.at = Date.now();
    this.chats = [this.chat, ...this.chats.filter(c => c.id !== this.chat.id)].slice(0, KEEP_CHATS);
    return this.context.globalState.update("chats", this.chats);
  }

  // ---------- a turn ----------

  async send(text, paths, skill, withFile) {
    if (this.child) return;
    if (!this.chat) this.chat = { id: crypto.randomUUID(), title: text.slice(0, 40) || "新对话", messages: [], sessions: {}, lastProvider: null, at: Date.now() };
    const chat = this.chat, pid = this.provider;
    let prompt = text || "请看附件。";
    const dirs = [];
    for (const p of paths) {
      const isDir = fs.statSync(p).isDirectory();
      prompt += `\n\n[附件${isDir ? "文件夹" : "文件"}：${p}]`;
      const d = isDir ? p : path.dirname(p);
      if (!dirs.includes(d)) dirs.push(d);
    }
    if (withFile) prompt += this.fileNote();
    if (skill) {
      const s = (this.skills || []).find(x => x.name === skill);
      if (s) prompt = `[请按这个 skill 的说明完成任务：${s.name}，说明文件：${s.path}，先读取它]\n\n${prompt}`;
    }
    // Each CLI has its own session, so moving to another AI hands over the recent messages.
    if (chat.lastProvider && chat.lastProvider !== pid && !chat.sessions[pid] && chat.messages.length) {
      const recent = chat.messages.slice(-CONTEXT_ON_SWITCH).map(m => `${m.role === "user" ? "用户" : "AI"}：${m.text}`).join("\n\n");
      prompt = `以下是这段对话之前的内容，由另一个 AI 回答，请接着继续：\n\n${recent}\n\n---\n\n${prompt}`;
    }
    chat.messages.push({ role: "user", text, paths, skill });
    const reply = { role: "assistant", text: "", tools: [], provider: pid };
    chat.messages.push(reply);
    this.post({ type: "user", text, paths, skill });
    this.post({ type: "busy", on: true, provider: pid });
    try {
      await this.turn(chat, pid, prompt, dirs, reply, false);
    } finally {
      chat.lastProvider = pid;
      this.child = null;
      await this.save();
      this.post({ type: "busy", on: false });
    }
  }

  async turn(chat, pid, prompt, dirs, reply, retried) {
    const cwd = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || os.homedir();
    const args = ["run", "--provider", pid, "--cwd", cwd, "--permission", permissionMode()];
    if (chat.sessions[pid]) args.push("--session", chat.sessions[pid]);
    for (const d of dirs) args.push("--dir", d);
    let done = null;
    await agent(args, {
      input: prompt,
      onSpawn: c => { this.child = c; },
      onEvent: e => {
        if (e.type === "text") { reply.text += e.text; this.post({ type: "delta", text: e.text }); }
        else if (e.type === "tool") { reply.tools.push(e); this.post({ type: "tool", name: e.name, detail: e.detail }); }
        else if (e.type === "session") chat.sessions[pid] = e.id;
        else if (e.type === "done") done = e;
      },
    });
    if (!done) return;
    if (done.session) chat.sessions[pid] = done.session;
    if (done.status === "no_conversation" && !retried) {
      delete chat.sessions[pid];
      return this.turn(chat, pid, prompt, dirs, reply, true);
    }
    if (done.status === "auth" && !retried) {
      if (await this.signIn(pid)) return this.turn(chat, pid, prompt, dirs, reply, true);
      return this.fail(reply, "登录或 API Key 无效，请在连接设置里更新。");
    }
    if (done.status === "auth") return this.fail(reply, "登录之后还是连不上。");
    if (done.status === "missing") {
      this.fail(reply, `还没安装 ${done.detail}，请在连接设置里安装。`);
      return this.connections();
    }
    if (done.status === "failed") this.fail(reply, `执行失败：${done.detail}`);
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

function activate(context) {
  const chat = new Chat(context);
  const reg = (name, fn) => context.subscriptions.push(vscode.commands.registerCommand(name, fn));
  context.subscriptions.push(vscode.window.registerWebviewViewProvider("carnot.chat", chat, { webviewOptions: { retainContextWhenHidden: true } }));
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
  context.subscriptions.push(vscode.window.onDidChangeActiveTextEditor(() => chat.post({ type: "file", file: chat.fileLabel() })));
  return { chat, agent, agentResult };
}

module.exports = { activate, deactivate() {} };
