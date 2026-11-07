// The chat view. Everything shown from a reply goes through esc() first.
const vs = acquireVsCodeApi();
const log = document.getElementById("log"), bottom = document.getElementById("bottom");
const CLIENTS = { app: "App", vscode: "VS Code", telegram: "Telegram", feishu: "飞书", schedule: "定时任务" };
document.body.insertAdjacentHTML("afterbegin", `<div id="bar"><span id="convTitle"></span><button id="historyBtn" title="全部会话">历史</button><button id="newBtn" title="新对话">新对话</button></div>
  <div id="history" hidden><input id="historySearch" placeholder="搜索标题"><div id="historyList"></div></div>`);
bottom.innerHTML = `<div id="menu" hidden></div><div id="chips"></div>
  <textarea id="input" placeholder="给 Carnot 发消息。/ 命令和 skill，@ 引用文件，可粘贴图片。Enter 发送，Shift+Enter 换行"></textarea>
  <div id="row"><button id="attach" class="icon" title="附加文件或文件夹（也可以粘贴图片、输入 @）"><svg viewBox="0 0 16 16"><path d="M8 3v10M3 8h10"/></svg></button><div id="pickers"><select id="provider" title="使用的 AI"></select><select id="model" title="模型，从下一条消息开始生效"></select><select id="effort" title="Effort（思考力度），从下一条消息开始生效"></select><select id="permission" title="AI 的权限，从下一条消息开始生效"><option value="plan">计划模式</option><option value="manual">每次询问</option><option value="acceptEdits">可改文件</option><option value="auto">自动</option><option value="bypassPermissions">完全放行</option></select><select id="computer" title="计算机使用：让 AI 操作这台电脑上的程序，从下一条消息开始生效"><option value="off">不用电脑</option><option value="background">后台用电脑</option><option value="full">接管电脑</option></select></div><span id="usage"></span><button id="send" class="icon main" title="发送"></button></div>`;
const ICON_SEND = '<svg viewBox="0 0 16 16"><path d="M8 13V3M3.500 7.500 8 3l4.500 4.500"/></svg>';
const ICON_STOP = '<svg viewBox="0 0 16 16"><rect x="4" y="4" width="8" height="8" rx="1" class="solid"/></svg>';
const $ = id => document.getElementById(id);
const input = $("input");
let commands = [], mentionItems = [], mentionQ = null, userCount = 0, cardSeq = 0, skills = [], paths = [], skill = null, withFile = true, file = "", busy = false, providers = [], current = null, live = null, menuIndex = 0;

const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function inline(s) {   // s is already escaped
  return s.replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2">$1</a>');
}

function codeBlock(lang, code) {
  return `<div class="code"><div class="codehead"><span>${esc(lang || "")}</span><button class="copy">复制</button></div><pre><code>${esc(code)}</code></pre></div>`;
}

function markdown(src) {
  const out = [], parts = src.split(/```/);
  parts.forEach((part, i) => {
    if (i % 2) {  // inside a fence (an unfinished one, while streaming, counts too)
      const nl = part.indexOf("\n");
      out.push(codeBlock(nl < 0 ? "" : part.slice(0, nl).trim(), nl < 0 ? "" : part.slice(nl + 1).replace(/\n$/, "")));
      return;
    }
    let list = null, para = [], table = [];
    const close = () => { if (list) { out.push(`</${list}>`); list = null; } };
    const flush = () => { if (para.length) { out.push(`<p>${inline(esc(para.join("\n"))).replace(/\n/g, "<br>")}</p>`); para = []; } };
    const flushTable = () => {
      if (!table.length) return;
      const cells = l => l.trim().replace(/^\||\|$/g, "").split("|").map(c => inline(esc(c.trim())));
      const body = table.slice(2).map(l => `<tr>${cells(l).map(c => `<td>${c}</td>`).join("")}</tr>`).join("");
      out.push(`<div class="tablewrap"><table><tr>${cells(table[0]).map(c => `<th>${c}</th>`).join("")}</tr>${body}</table></div>`);
      table = [];
    };
    const lines = part.split("\n");
    lines.forEach((line, n) => {
      const isRow = /^\s*\|.*\|\s*$/.test(line);
      if (isRow && (table.length || /^\s*\|[\s:|-]+\|\s*$/.test(lines[n + 1] || ""))) { flush(); close(); table.push(line); return; }
      flushTable();
      const h = line.match(/^(#{1,3})\s+(.*)/), ul = line.match(/^\s*[-*]\s+(.*)/), ol = line.match(/^\s*\d+\.\s+(.*)/), q = line.match(/^>\s?(.*)/);
      if (h) { flush(); close(); out.push(`<h${h[1].length}>${inline(esc(h[2]))}</h${h[1].length}>`); }
      else if (q) { flush(); close(); out.push(`<blockquote>${inline(esc(q[1]))}</blockquote>`); }
      else if (ul || ol) {
        flush();
        const kind = ul ? "ul" : "ol";
        if (list !== kind) { close(); out.push(`<${kind}>`); list = kind; }
        out.push(`<li>${inline(esc((ul || ol)[1]))}</li>`);
      }
      else if (!line.trim()) { flush(); close(); }
      else { close(); para.push(line); }
    });
    flushTable(); flush(); close();
  });
  return out.join("");
}

// ---------- tool calls: what went in, what came out ----------

const PATH_TOOLS = ["Read", "Edit", "Write", "MultiEdit", "NotebookEdit"];
function toolTitle(p) {
  const i = p.input || {};
  if (PATH_TOOLS.includes(p.name) && i.file_path) return i.file_path;
  return i.description || i.pattern || i.query || i.url || i.prompt || (typeof i.command === "string" ? i.command.split("\n")[0] : "") || "";
}

/** Line diff by longest common subsequence; big edits just show old then new. */
function lineDiff(oldText, newText) {
  const a = (oldText || "").split("\n"), b = (newText || "").split("\n");
  if (a.length * b.length > 250000) return [...a.map(l => "-" + l), ...b.map(l => "+" + l)];
  const t = Array.from({ length: a.length + 1 }, () => new Int32Array(b.length + 1));
  for (let i = a.length - 1; i >= 0; i--) for (let j = b.length - 1; j >= 0; j--) t[i][j] = a[i] === b[j] ? t[i + 1][j + 1] + 1 : Math.max(t[i + 1][j], t[i][j + 1]);
  const out = []; let i = 0, j = 0;
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) { out.push(" " + a[i]); i++; j++; }
    else if (i < a.length && (j === b.length || t[i + 1][j] >= t[i][j + 1])) out.push("-" + a[i++]);
    else out.push("+" + b[j++]);
  }
  return out;
}

function diffLines(p) {
  const i = p.input || {};
  if (p.patch && p.patch.length) return p.patch.flatMap((h, n) => [...(n ? ["…"] : []), ...h.lines]);
  if (p.name === "Edit" && i.old_string !== undefined) return lineDiff(i.old_string, i.new_string);
  if (p.name === "MultiEdit" && i.edits) return i.edits.flatMap((e, n) => [...(n ? ["…"] : []), ...lineDiff(e.old_string, e.new_string)]);
  if (p.name === "Write" && typeof i.content === "string") return i.content.split("\n").map(l => "+" + l);
  return null;
}

function block(label, text, cls, openByDefault) {
  const d = document.createElement("div"); d.className = "tblock " + (cls || "");
  const l = document.createElement("span"); l.className = "tlabel"; l.textContent = label;
  const body = document.createElement("pre"); body.className = "tbody"; body.textContent = text;
  const c = document.createElement("button"); c.className = "copy"; c.textContent = "复制"; c.dataset.text = text;
  d.append(l, body, c);
  if (text.split("\n").length > 6 && !openByDefault) {
    body.classList.add("short");
    const more = document.createElement("button"); more.className = "more"; more.textContent = "展开";
    more.onclick = () => { const s = body.classList.toggle("short"); more.textContent = s ? "展开" : "收起"; };
    d.append(more);
  }
  return d;
}

function drawTool(card, p) {
  card.textContent = "";
  card.className = "tool " + (!p.done ? "running" : p.error ? "failed" : "ok");
  const head = document.createElement("div"); head.className = "thead";
  const dot = document.createElement("span"); dot.className = "dot";
  const name = document.createElement("span"); name.className = "tname"; name.textContent = p.name;
  const tt = toolTitle(p);
  const isPath = PATH_TOOLS.includes(p.name) && (p.input || {}).file_path;
  const title = document.createElement("span"); title.className = "ttitle"; title.title = tt;
  title.textContent = isPath ? tt.split(/[\\/]/).filter(Boolean).slice(-3).join("/") : tt;
  head.append(dot, name, title);
  const i = p.input || {};
  if (PATH_TOOLS.includes(p.name) && i.file_path) {
    title.classList.add("link");
    title.onclick = () => vs.postMessage({ type: "openFile", path: i.file_path, line: p.patch && p.patch[0] ? p.patch[0].newStart : i.offset });
  }
  card.append(head);
  if (p.name === "ExitPlanMode" || p.name === "AskUserQuestion") {  // the question card shows these; afterwards the plan folds
    if (i.plan) { const d = document.createElement("details"); d.className = "tplan"; d.innerHTML = `<summary>计划</summary><div class="body">${markdown(i.plan)}</div>`; card.append(d); }
    return;
  }
  const body = document.createElement("div"); body.className = "tcontent";
  const diff = diffLines(p);
  if (diff && !p.error) {
    const added = diff.filter(l => l[0] === "+").length, gone = diff.filter(l => l[0] === "-").length;
    const stat = document.createElement("span"); stat.className = "tstat"; stat.textContent = `+${added} −${gone}`; head.append(stat);
    const box = document.createElement("div"); box.className = "diff" + (diff.length > 24 ? " short" : "");
    for (const l of diff) {
      const row = document.createElement("div");
      row.className = l[0] === "+" ? "add" : l[0] === "-" ? "del" : l === "…" ? "gap" : "ctx";
      row.textContent = l === "…" ? "⋯" : l[0] === "+" || l[0] === "-" ? l[0] + " " + l.slice(1) : "  " + l.slice(1);
      box.append(row);
    }
    body.append(box);
    if (diff.length > 24) {
      const more = document.createElement("button"); more.className = "more"; more.textContent = "展开";
      more.onclick = () => { const s = box.classList.toggle("short"); more.textContent = s ? "展开" : "收起"; };
      body.append(more);
    }
  } else if (typeof i.command === "string") {
    body.append(block("IN", i.command, "in", true));
  } else if (i.changes) {
    body.append(block("IN", i.changes.map(c => `${c.kind || "change"} ${c.path || ""}`).join("\n"), "in", true));
  } else if (p.name !== "Read" && Object.keys(i).length) {
    body.append(block("IN", JSON.stringify(i, null, 2), "in"));
  }
  if (p.done && p.output && !(diff && !p.error)) {
    if (p.name === "Read" && !p.error) {
      const d = document.createElement("details"); const s = document.createElement("summary");
      s.textContent = `${p.output.split("\n").length} 行`; d.append(s, block("OUT", p.output, "out", true)); body.append(d);
    } else body.append(block("OUT", p.output, p.error ? "out err" : "out"));
  }
  if (body.children.length) card.append(body);
}

function nameOf(id) { return (providers.find(p => p.id === id) || {}).name || id; }

function addUser(m) {
  const d = document.createElement("div"); d.className = "msg user";
  const tags = [...(m.skill ? [`skill：${m.skill}`] : []), ...(m.paths || []).map(p => "📎 " + p.split(/[\\/]/).filter(Boolean).pop())];
  d.innerHTML = tags.map(t => `<span class="tag">${esc(t)}</span>`).join("") + `<div>${esc(m.text).replace(/\n/g, "<br>")}</div>`;
  const back = document.createElement("button"); back.className = "rewind"; back.textContent = "回退到这里"; back.title = "把代码和对话退回这条消息之前";
  const ordinal = userCount++;
  back.onclick = () => vs.postMessage({ type: "rewind", ordinal });
  d.append(back);
  log.append(d);
}

// A small beam engine, the same linkage as the app's: the flywheel's angle fixes the crank pin, the rod
// and the beam's reach fix the beam, and the beam fixes the piston.
const ENGINE_SVG = `<svg class="engine" viewBox="0 0 92 46" aria-hidden="true"><path class="frame" d="M4 44h84M36 44V13M30 44l6-31 6 31M6 44V28M18 44V28"/><rect class="cyl" x="5" y="26" width="14" height="18" rx="1"/><rect class="pst" x="7" y="27" width="10" height="4"/><path class="prod"/><path class="beam"/><path class="rod"/><g class="wheel"><circle cx="0" cy="0" r="10"/><path d="M-10 0H10M0-10V10"/></g><circle class="pin" r="1.6"/><circle class="pivot" cx="36" cy="12" r="1.8"/></svg>`;
const ENG = { px: 36, py: 12, reach: 26, cx: 62, cy: 32, crank: 6, rod: 17 };
function drawEngineAt(svg, theta) {
  const g = ENG, pinX = g.cx + g.crank * Math.cos(theta), pinY = g.cy + g.crank * Math.sin(theta);
  const dx = pinX - g.px, dy = pinY - g.py, dist = Math.hypot(dx, dy);
  const along = (g.reach * g.reach - g.rod * g.rod + dist * dist) / (2 * dist);
  const up = Math.sqrt(Math.max(0, g.reach * g.reach - along * along));
  const endX = g.px + (along * dx + up * dy) / dist, endY = g.py + (along * dy - up * dx) / dist;   // the upper meeting point
  const leftX = 2 * g.px - endX, leftY = 2 * g.py - endY;
  const pistonY = 35 + (leftY - 12) * 0.9;
  const at = s => svg.querySelector(s);
  at(".beam").setAttribute("d", `M${leftX} ${leftY}L${endX} ${endY}`);
  at(".rod").setAttribute("d", `M${endX} ${endY}L${pinX} ${pinY}`);
  at(".prod").setAttribute("d", `M12 ${pistonY}L${leftX} ${leftY}`);
  at(".pst").setAttribute("y", pistonY);
  at(".pin").setAttribute("cx", pinX); at(".pin").setAttribute("cy", pinY);
  at(".wheel").setAttribute("transform", `translate(${g.cx} ${g.cy}) rotate(${theta * 180 / Math.PI})`);
}
let engineTheta = -0.9, engineLast = 0;
function turnEngine(now) {
  const svg = live && live.el.querySelector("svg.engine");
  if (!svg) { engineLast = 0; return; }
  if (engineLast) engineTheta += (now - engineLast) / 1700 * 2 * Math.PI;   // one turn in 1.7 s, as in the app
  engineLast = now;
  drawEngineAt(svg, engineTheta);
  requestAnimationFrame(turnEngine);
}

function addReply(provider) {
  const d = document.createElement("div"); d.className = "msg";
  d.innerHTML = `<div class="who">${esc(nameOf(provider))}</div><div class="parts"></div><div class="dots">${ENGINE_SVG}<span class="dtext">…</span></div>`;
  log.append(d);
  return { el: d, box: d.querySelector(".parts"), parts: [], text: "", cards: {} };
}

/** Text, thinking and tools appear in the order they happened. */
function lastPart(r, type) {
  const p = r.parts[r.parts.length - 1];
  if (p && p.type === type) return p;
  const el = document.createElement("div");
  el.className = type === "text" ? "body" : "thinking";
  const part = { type, text: "", el };
  if (type === "thinking") {
    el.innerHTML = `<details><summary>思考过程</summary><div class="ttext"></div></details>`;
  }
  r.box.append(el); r.parts.push(part);
  return part;
}
function addText(r, text) { const p = lastPart(r, "text"); p.text += text; p.el.innerHTML = markdown(p.text); scroll(); }
function addThinking(r, text) { if (!text) return; const p = lastPart(r, "thinking"); p.text += text; p.el.querySelector(".ttext").textContent = p.text; scroll(); }
function addTool(r, p) {
  const card = document.createElement("div"); r.box.append(card);
  const part = Object.assign({ type: "tool", el: card }, p);
  r.parts.push(part); r.cards[p.id] = part;
  drawTool(card, part); scroll();
}
function endTool(r, e) {
  const part = r.cards[e.id]; if (!part) return;
  Object.assign(part, { output: e.output, error: e.error, patch: e.patch, done: true });
  drawTool(part.el, part); scroll();
}
/** The turn is over: a tool that never reported back didn't finish. */
function settleTools(r) {
  for (const p of r.parts) if (p.type === "tool" && !p.done) { Object.assign(p, { done: true, error: true }); drawTool(p.el, p); }
}
function addError(r, text) {
  const e = document.createElement("div"); e.className = "error"; e.textContent = text;
  (r ? r.el : log).append(e); scroll();
}
// ---------- questions from the AI ----------

function settle(card, text, cls) {
  card.className = "approval done " + (cls || "");
  card.textContent = text;
  live && live.el && scroll();
}
function askButton(label, cls, fn) {
  const b = document.createElement("button"); b.textContent = label; if (cls) b.className = cls; b.onclick = fn; return b;
}
const pendingCards = new Set();
function addPermission(r, q) {
  const card = document.createElement("div"); card.className = "approval"; pendingCards.add(card);
  const reply = (data, text, cls) => { vs.postMessage({ type: "permissionAnswer", id: q.id, ...data }); pendingCards.delete(card); settle(card, text, cls); };
  const row = document.createElement("div"); row.className = "aactions";
  const reason = document.createElement("input"); reason.className = "areason";
  if (q.tool === "ExitPlanMode") {
    const t = document.createElement("div"); t.className = "atitle"; t.textContent = "计划已就绪，请审阅";
    const plan = document.createElement("div"); plan.className = "aplan body"; plan.innerHTML = markdown(q.input.plan || "");
    reason.placeholder = "想改哪里？写在这里，点“继续修改”";
    row.append(
      askButton("批准并自动改文件", "main", () => reply({ allow: true, updates: [{ type: "setMode", mode: "acceptEdits", destination: "session" }] }, "已批准计划，自动修改文件", "ok")),
      askButton("批准，每步确认", "", () => reply({ allow: true, updates: [{ type: "setMode", mode: "default", destination: "session" }] }, "已批准计划，每步确认", "ok")),
      askButton("继续修改", "", () => reply({ allow: false, message: reason.value.trim() || "先别动手，继续修改计划。" }, "已退回计划" + (reason.value.trim() ? "：" + reason.value.trim() : ""), "no")));
    card.append(t, plan, reason, row);
  } else if (q.tool === "AskUserQuestion") {
    const answers = {};
    const qs = q.input.questions || [];
    const finish = () => reply({ allow: true, input: { ...q.input, answers } }, Object.entries(answers).map(([k, v]) => `${k} ${v}`).join("；"), "ok");
    qs.forEach(item => {
      const t = document.createElement("div"); t.className = "atitle"; t.textContent = item.question; card.append(t);
      const opts = document.createElement("div"); opts.className = "aactions";
      (item.options || []).forEach(o => {
        const b = askButton(o.label, "", () => {
          if (item.multiSelect) { b.classList.toggle("main"); answers[item.question] = [...opts.querySelectorAll("button.main")].map(x => x.textContent).join(", "); return; }
          answers[item.question] = o.label; [...opts.children].forEach(x => x.classList.remove("main")); b.classList.add("main");
          if (Object.keys(answers).length === qs.length) finish();
        });
        b.title = o.description || ""; opts.append(b);
      });
      card.append(opts);
    });
    reason.placeholder = "或者直接写答案";
    row.append(askButton("提交", "main", () => {
      if (reason.value.trim()) qs.forEach(i => { if (!answers[i.question]) answers[i.question] = reason.value.trim(); });
      if (Object.keys(answers).length) finish();
    }));
    card.append(reason, row);
  } else {
    const i = q.input || {};
    const t = document.createElement("div"); t.className = "atitle"; t.textContent = `允许 ${q.tool}？`;
    card.append(t);
    if (q.description && !i.file_path) { const d = document.createElement("div"); d.className = "adesc"; d.textContent = q.description; card.append(d); }
    if (typeof i.command === "string") card.append(block("IN", i.command, "in", true));
    else if (i.file_path) {
      const f = document.createElement("div"); f.className = "adesc"; f.textContent = i.file_path; card.append(f);
      row.append(askButton("差异视图", "", () => vs.postMessage({ type: "previewEdit", tool: q.tool, input: i })));
    } else card.append(block("IN", JSON.stringify(i, null, 2), "in"));
    reason.placeholder = "拒绝的理由（可不填）";
    row.prepend(
      askButton("允许", "main", () => reply({ allow: true }, `已允许 ${q.tool}`, "ok")),
      ...(q.suggestions && q.suggestions.length ? [askButton("这次对话都允许", "", () => reply({ allow: true, always: true }, `已允许 ${q.tool}，这次对话不再询问`, "ok"))] : []),
      askButton("拒绝", "", () => reply({ allow: false, message: reason.value.trim() || "用户拒绝了这个操作。" }, `已拒绝 ${q.tool}` + (reason.value.trim() ? "：" + reason.value.trim() : ""), "no")));
    card.append(reason, row);
  }
  r.box.append(card); r.parts.push({ type: "approval", el: card });
  follow = true; scroll();
}
function dropPending() { for (const c of pendingCards) settle(c, "已结束，没有回答", "no"); pendingCards.clear(); }

// ---------- what a turn changed ----------

function addChanges(r, point) {
  if (!point || !point.files || !point.files.length) return;
  const card = document.createElement("div"); card.className = "changes"; card.dataset.card = ++cardSeq;
  const head = document.createElement("div"); head.className = "chead";
  const label = document.createElement("span"); label.textContent = `这一轮改了 ${point.files.length} 个文件`;
  const keep = askButton("保留", "", () => card.classList.add("kept"));
  const undo = askButton("全部撤销", "", () => vs.postMessage({ type: "undoTurn", cwd: point.cwd, before: point.before, card: card.dataset.card }));
  head.append(label, keep, undo); card.append(head);
  for (const f of point.files) {
    const row = document.createElement("div"); row.className = "cfile"; row.dataset.path = f.path;
    const name = document.createElement("span"); name.className = "cname"; name.textContent = f.path; name.title = "查看差异";
    name.onclick = () => vs.postMessage({ type: "viewChange", cwd: point.cwd, before: point.before, path: f.path, status: f.status });
    const stat = document.createElement("span"); stat.className = "tstat"; stat.textContent = f.status === "A" ? `新建 +${f.added}` : f.status === "D" ? `删除 −${f.removed}` : `+${f.added} −${f.removed}`;
    const u = askButton("撤销", "", () => vs.postMessage({ type: "undoFile", cwd: point.cwd, before: point.before, path: f.path, card: card.dataset.card }));
    row.append(name, stat, u); card.append(row);
  }
  r.box.append(card); scroll();
}

// ---------- usage ----------

const short = n => n >= 1e6 ? (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M" : n >= 1000 ? Math.round(n / 1000) + "k" : String(n || 0);
function drawUsage(u) {
  const el = $("usage");
  if (!u || (!u.context && !u.input)) { el.textContent = ""; el.title = ""; return; }
  const pct = u.window && u.context ? Math.min(100, Math.round(100 * u.context / u.window)) : null;
  el.textContent = pct !== null ? `上下文 ${pct}%` : `${short(u.context || u.input)} token`;
  el.className = pct !== null && pct >= 80 ? "warn" : "";
  el.title = [
    u.context ? `上下文 ${short(u.context)}${u.window ? " / " + short(u.window) : ""}` : "",
    u.input !== undefined ? `这一轮 输入 ${short(u.input)}，输出 ${short(u.output)}` : "",
    u.cost ? `这一轮费用 $${u.cost.toFixed(4)}${subscription ? "（订阅额度内为估算）" : ""}` : "",
    "点击查看详情 (/context)",
  ].filter(Boolean).join("\n");
}
let subscription = false;
$("usage").onclick = () => { if (!busy) { input.value = "/context"; send(); } };

function nearBottom() { return log.scrollHeight - log.scrollTop - log.clientHeight < 80; }
let follow = true;
const jump = document.createElement("button");
jump.id = "jump"; jump.hidden = true; jump.textContent = "↓ 下面有新内容";
jump.onclick = () => { follow = true; scroll(); };
bottom.append(jump);
log.addEventListener("scroll", () => { follow = nearBottom(); if (follow) jump.hidden = true; });
const scroll = () => { if (follow) { log.scrollTop = log.scrollHeight; jump.hidden = true; } else jump.hidden = false; };

function drawEmpty() {
  if (log.children.length) return;
  log.innerHTML = `<div class="empty">和 Carnot App 共用同一套记忆和 skill。<br>输入 / 选择命令或 skill，@ 引用文件，右上角可连接其他 AI。</div>`;
}

/** Returns the last reply, still open, when `running`: the page was reloaded in the middle of a turn. */
function drawHistory(messages, running) {
  log.textContent = "";
  follow = true;
  userCount = 0;
  drawUsage([...messages].reverse().find(m => m.usage)?.usage);
  let open = null;
  for (const [at, m] of messages.entries()) {
    if (m.role === "user") { addUser(m); continue; }
    const r = addReply(m.provider);
    if (running && at === messages.length - 1) open = r;
    else r.el.querySelector(".dots").remove();
    const parts = m.parts || [...(m.tools || []).map(t => ({ type: "tool", id: t.name, name: t.name, input: t.detail ? { description: t.detail } : {}, done: true })), { type: "text", text: m.text || "" }];
    for (const p of parts) {
      if (p.type === "text") addText(r, p.text);
      else if (p.type === "thinking") addThinking(r, p.text);
      else addTool(r, p);
    }
    if (r === open) continue;
    settleTools(r);
    addChanges(r, m.checkpoint);
    if (m.error) addError(r, m.error);
  }
  drawEmpty(); scroll();
  return open;
}

function drawConv(conv) {
  const t = $("convTitle");
  t.textContent = conv ? `${conv.client && conv.client !== "vscode" ? (CLIENTS[conv.client] || conv.client) + " · " : ""}${conv.title || ""}` : "";
  t.title = t.textContent;
}

// ---------- every conversation, from the app, the bots and here ----------

let historyItems = [], openId = null;
function when(t) {
  const d = new Date(t * 1000), now = new Date();
  return d.toDateString() === now.toDateString() ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : d.toLocaleDateString();
}
function drawHistory_() {
  const q = $("historySearch").value.trim().toLowerCase(), box = $("historyList");
  box.textContent = "";
  const items = historyItems.filter(i => !q || i.title.toLowerCase().includes(q));
  if (!items.length) { const e = document.createElement("div"); e.className = "hempty"; e.textContent = q ? "没有匹配的会话" : "还没有会话"; box.append(e); }
  for (const i of items) {
    const row = document.createElement("div"); row.className = "hitem" + (i.id === openId ? " on" : "");
    const main = document.createElement("div"); main.className = "hmain";
    const title = document.createElement("div"); title.className = "htitle"; title.textContent = i.title;
    const meta = document.createElement("div"); meta.className = "hmeta"; meta.textContent = `${CLIENTS[i.client] || i.client} · ${when(i.updated)}`;
    main.append(title, meta);
    main.onclick = () => { $("history").hidden = true; log.hidden = false; vs.postMessage({ type: "openSession", id: i.id }); };
    const ren = document.createElement("button"); ren.textContent = "改名"; ren.onclick = () => vs.postMessage({ type: "renameSession", id: i.id, title: i.title });
    const del = document.createElement("button"); del.textContent = "删除"; del.onclick = () => vs.postMessage({ type: "deleteSession", id: i.id, title: i.title });
    row.append(main, ren, del); box.append(row);
  }
}
$("historySearch").addEventListener("input", drawHistory_);
$("historyBtn").onclick = () => {
  if (!$("history").hidden) { $("history").hidden = true; log.hidden = false; return; }
  vs.postMessage({ type: "history" });
};
$("newBtn").onclick = () => { $("history").hidden = true; log.hidden = false; vs.postMessage({ type: "newChat" }); };

const EFFORT_NAMES = { low: "低", medium: "中", high: "高", xhigh: "超高", max: "最高" };
let models = {};
function drawModels() {
  const c = models[current];
  $("model").hidden = !c;
  $("effort").hidden = !c || !c.efforts.length;
  if (!c) return;
  $("model").innerHTML = c.models.map(m => `<option value="${esc(m.id)}" title="${esc(m.note || "")}">${esc(m.name)}</option>`).join("");
  $("model").value = c.selected;
  $("effort").innerHTML = c.efforts.map(e => `<option value="${esc(e)}">Effort ${EFFORT_NAMES[e] || esc(e)}${c.mapped[e] ? `（按${EFFORT_NAMES[c.mapped[e]]}）` : ""}</option>`).join("");
  $("effort").value = c.shown_effort || "";
}
// a new model starts from its own default effort, unless one was picked on purpose
const pickModel = () => vs.postMessage({ type: "setModel", provider: current, model: $("model").value, effort: models[current].effort || "" });
const pickEffort = () => vs.postMessage({ type: "setModel", provider: current, model: $("model").value, effort: $("effort").value });

function drawProviders() {
  $("provider").innerHTML = providers.map(p => `<option value="${esc(p.id)}"${p.id === current ? " selected" : ""}>${esc(p.name)}${p.ready ? "" : "（未连接）"}</option>`).join("");
}

function drawChips() {
  const box = $("chips"); box.textContent = "";
  const chip = (text, onX, cls = "") => {
    const c = document.createElement("div"); c.className = "chip " + cls;
    const s = document.createElement("span"); s.textContent = text; c.append(s);
    if (onX) { const x = document.createElement("b"); x.textContent = "×"; x.onclick = onX; c.append(x); }
    box.append(c); return c;
  };
  if (skill) chip(`skill：${skill}`, () => { skill = null; drawChips(); });
  if (file) {
    const c = chip(`当前文件：${file}`, null, "toggle" + (withFile ? "" : " off"));
    c.title = "点击切换是否附带当前文件";
    c.onclick = () => { withFile = !withFile; drawChips(); };
  }
  paths.forEach(p => chip("📎 " + p.split(/[\\/]/).filter(Boolean).pop(), () => { paths = paths.filter(q => q !== p); drawChips(); }));
}

// A model can think for minutes without sending anything: say so, rather than look frozen.
let turnStart = 0, lastHeard = 0, retry = "", ticker = null;
const clock = ms => { const s = Math.floor(ms / 1000); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
function heard(m) {
  if (!["delta", "step", "permission"].includes(m.type)) return;
  lastHeard = Date.now();
  retry = m.type === "step" && m.step.kind === "retry" ? `连接不上模型${m.step.status ? `（${m.step.status}）` : ""}，第 ${m.step.attempt}${m.step.max ? "/" + m.step.max : ""} 次重试` : "";
}
function drawWaiting() {
  const el = live && live.el.querySelector(".dtext");
  if (!el) return;
  const now = Date.now(), silent = now - lastHeard, total = clock(now - turnStart);
  let text = `工作中 ${total}`;
  if (pendingCards.size) text = "等你确认";
  else if (retry) text = `${retry}（${total}）`;
  else if (live.el.querySelector(".tool.running")) text = `工具运行中 ${total}`;
  else if (silent >= 10000) text = `模型思考中，${clock(silent)} 没有新输出（共 ${total}）${silent >= 180000 ? "，想中断就点停止" : ""}`;
  el.textContent = text;
}

function setBusy(on) {
  busy = on;
  clearInterval(ticker); ticker = null; retry = "";
  if (on) { turnStart = lastHeard = Date.now(); ticker = setInterval(drawWaiting, 1000); }
  $("send").innerHTML = on ? ICON_STOP : ICON_SEND;
  $("send").className = "icon " + (on ? "busy" : "main");
  $("send").title = on ? "停止" : "发送";
}

function send() {
  if (busy) return vs.postMessage({ type: "stop" });
  const text = input.value.trim();
  if (!text && !paths.length) return;
  if (/^\/(clear|new)$/.test(text)) { input.value = ""; hideMenu(); return vs.postMessage({ type: "newChat" }); }
  vs.postMessage({ type: "send", text, paths, skill, withFile: withFile && !!file && !text.startsWith("/") });
  input.value = ""; paths = []; skill = null; hideMenu(); drawChips();
}

// ---------- the menus: / for commands and skills, @ for files ----------

function usesClaudeCode() { const p = providers.find(x => x.id === current); return !p || p.needs === "Claude Code"; }
function token() {  // the @word the caret is in
  const m = input.value.slice(0, input.selectionStart).match(/(?:^|\s)@([^\s@]*)$/);
  return m ? m[1] : null;
}
function matches() {
  const m = input.value.match(/^\/(\S*)$/);
  if (m) {
    const q = m[1].toLowerCase(), skillNames = new Set(skills.map(s => s.name));
    const local = [{ kind: "local", name: "clear", description: "开始新对话" }];
    const native = usesClaudeCode() ? commands.filter(c => !skillNames.has(c)).map(c => ({ kind: "command", name: c, description: "Claude Code 命令" })) : [];
    return [...local, ...native, ...skills.map(s => ({ kind: "skill", name: s.name, description: s.description }))].filter(i => i.name.toLowerCase().includes(q));
  }
  if (token() !== null) return mentionItems.map(i => ({ kind: "file", name: i.label, description: "", path: i.path }));
  return null;
}
function hideMenu() { $("menu").hidden = true; }
function drawMenu() {
  const list = matches(), menu = $("menu");
  if (!list || !list.length) return hideMenu();
  menuIndex = Math.min(menuIndex, list.length - 1);
  menu.hidden = false; menu.textContent = "";
  list.slice(0, 40).forEach((s, i) => {
    const d = document.createElement("div"); d.className = i === menuIndex ? "sel" : "";
    d.textContent = (s.kind === "skill" || s.kind === "file" ? "" : "/") + s.name;
    if (s.kind === "skill") { const t = document.createElement("span"); t.className = "mkind"; t.textContent = " skill"; d.append(t); }
    if (s.description) { const small = document.createElement("small"); small.textContent = s.description; d.append(small); }
    d.onmousedown = e => { e.preventDefault(); choose(s); };
    menu.append(d);
  });
  [...menu.children][menuIndex]?.scrollIntoView({ block: "nearest" });
}
function choose(s) {
  if (s.kind === "skill") { skill = s.name; input.value = ""; }
  else if (s.kind === "file") {
    const end = input.selectionStart, head = input.value.slice(0, end).replace(/@[^\s@]*$/, "");
    input.value = head + input.value.slice(end);
    if (!paths.includes(s.path)) paths.push(s.path);
    mentionItems = [];
  } else input.value = "/" + s.name + " ";
  hideMenu(); drawChips(); input.focus();
}
function refreshMenu() {
  menuIndex = 0;
  const q = token();
  if (q !== null && q !== mentionQ) { mentionQ = q; vs.postMessage({ type: "mention", q }); }
  if (q === null) { mentionQ = null; mentionItems = []; }
  drawMenu();
}

input.addEventListener("input", refreshMenu);
input.addEventListener("keydown", e => {
  const list = !$("menu").hidden && matches();
  if (list && list.length) {
    const n = Math.min(list.length, 40);
    if (e.key === "ArrowDown") { menuIndex = (menuIndex + 1) % n; drawMenu(); return e.preventDefault(); }
    if (e.key === "ArrowUp") { menuIndex = (menuIndex + n - 1) % n; drawMenu(); return e.preventDefault(); }
    if (e.key === "Enter" || e.key === "Tab") { choose(list[menuIndex]); return e.preventDefault(); }
    if (e.key === "Escape") return hideMenu();
  }
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
});
// pictures can be pasted or dropped; files and folders can be dropped
// a page sees a copied file's name but not where it is, so the extension asks the system clipboard
let pasted = null;
input.addEventListener("paste", e => {
  const data = e.clipboardData;
  if (!data) return;
  const line = data.getData("text/plain");  // one line may be all a copied file shows; longer text pastes as usual
  if (!data.types.includes("Files") && (!line || line.includes("\n"))) return;
  e.preventDefault();
  const image = [...data.items].find(i => i.type.startsWith("image/"));
  pasted = { text: data.getData("text/plain"), mime: image && image.type, image: image && image.getAsFile() };
  vs.postMessage({ type: "pasteFiles" });
});
function pasteContents() {  // no file behind it: rich text keeps its text, a screenshot is the picture
  const { text, image, mime } = pasted || {};
  pasted = null;
  if (text) { input.focus(); return document.execCommand("insertText", false, text); }
  if (!image) return;
  const reader = new FileReader();
  reader.onload = () => vs.postMessage({ type: "pasteImage", mime, data: String(reader.result).split(",")[1] });
  reader.readAsDataURL(image);
}
bottom.addEventListener("dragover", e => e.preventDefault());
bottom.addEventListener("drop", e => {
  e.preventDefault();
  const uris = (e.dataTransfer.getData("text/uri-list") || "").split(/\r?\n/).filter(u => u && !u.startsWith("#"));
  if (uris.length) vs.postMessage({ type: "dropped", uris });
  for (const f of e.dataTransfer.files || []) if (f.type.startsWith("image/") && !uris.length) {
    const reader = new FileReader();
    reader.onload = () => vs.postMessage({ type: "pasteImage", mime: f.type, data: String(reader.result).split(",")[1] });
    reader.readAsDataURL(f);
  }
});
$("send").onclick = send;
$("attach").onclick = () => vs.postMessage({ type: "attach" });
$("provider").onchange = e => { current = e.target.value; drawModels(); vs.postMessage({ type: "provider", id: current }); };
$("model").onchange = pickModel;
$("effort").onchange = pickEffort;
$("computer").onchange = e => vs.postMessage({ type: "setComputer", mode: e.target.value });
$("permission").onchange = e => vs.postMessage({ type: "permission", mode: e.target.value });

// every copy button: code blocks and tool input/output
document.addEventListener("click", e => {
  const b = e.target.closest && e.target.closest("button.copy");
  if (!b) return;
  const code = b.closest(".code");
  vs.postMessage({ type: "copy", text: b.dataset.text !== undefined ? b.dataset.text : code.querySelector("code").textContent });
  b.textContent = "已复制"; setTimeout(() => { b.textContent = "复制"; }, 1200);
});

window.addEventListener("message", ({ data: m }) => {
  heard(m);
  if (m.type === "state") {
    providers = m.providers; current = m.provider; skills = m.skills; commands = m.commands || []; file = m.file; models = m.models || {}; drawProviders(); drawModels(); drawChips();
    $("permission").value = m.permission;
    $("computer").value = m.computer || "off";
    drawConv(m.conv);
    if (!busy || (m.busy && !live)) { live = drawHistory(m.messages, m.busy); if (live) requestAnimationFrame(turnEngine); }
    setBusy(m.busy);
  } else if (m.type === "models") {
    models = m.items; drawModels();
    if (m.computer) $("computer").value = m.computer;
  } else if (m.type === "historyList") {
    historyItems = m.items; openId = m.open; $("history").hidden = false; log.hidden = true; drawHistory_(); $("historySearch").focus();
  } else if (m.type === "user") {
    log.querySelector(".empty")?.remove(); addUser(m); scroll();
  } else if (m.type === "busy") {
    setBusy(m.on);
    if (m.on) { live = addReply(m.provider); scroll(); requestAnimationFrame(turnEngine); }
    else { dropPending(); if (live) { settleTools(live); live.el.querySelector(".dots")?.remove(); live = null; } }
  } else if (m.type === "delta") {
    if (live) addText(live, m.text);
  } else if (m.type === "permission") {
    if (live) addPermission(live, m.request);
  } else if (m.type === "checkpoint") {
    if (live) addChanges(live, m.point);
  } else if (m.type === "undone") {
    const card = document.querySelector(`.changes[data-card="${CSS.escape(String(m.card))}"]`);
    if (card) for (const row of card.querySelectorAll(".cfile")) if (m.paths.includes(row.dataset.path)) row.classList.add("undone");
  } else if (m.type === "noPastedFiles") {
    pasteContents();
  } else if (m.type === "mentions") {
    if (m.q === mentionQ) { mentionItems = m.items; drawMenu(); }
  } else if (m.type === "setInput") {
    input.value = m.text || ""; input.focus();
  } else if (m.type === "step") {
    if (m.step.kind === "usage") drawUsage(m.step);
    else if (m.step.kind === "init") { subscription = m.step.subscription; const h = new Set(m.step.terminal || []); commands = (m.step.commands || []).filter(c => !h.has(c)); }
    else if (m.step.kind === "limit" && live && m.step.status !== "allowed") addError(live, `用量提醒：${m.step.status === "rejected" ? "额度已用完" : "额度快用完了"}${m.step.resets ? "，" + new Date(m.step.resets * 1000).toLocaleString() + " 恢复" : ""}`);
    if (live) {
      const e = m.step;
      if (e.kind === "thinking") addThinking(live, e.text);
      else if (e.kind === "tool_start") addTool(live, e);
      else if (e.kind === "tool_end") endTool(live, e);
    }
  } else if (m.type === "error") {
    addError(live, m.text);
  } else if (m.type === "attached") {
    for (const p of m.paths) if (!paths.includes(p)) paths.push(p);
    drawChips();
  } else if (m.type === "permission") {
    $("permission").value = m.mode;
  } else if (m.type === "file") {
    file = m.file; drawChips();
  } else if (m.type === "focus") {
    withFile = !!m.withFile; drawChips(); input.focus();
  }
});
drawEmpty(); drawChips(); setBusy(false);
vs.postMessage({ type: "ready" });
