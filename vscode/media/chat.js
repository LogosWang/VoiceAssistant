// The chat view. Everything shown from a reply goes through esc() first.
const vs = acquireVsCodeApi();
const log = document.getElementById("log"), bottom = document.getElementById("bottom");
bottom.innerHTML = `<div id="menu" hidden></div><div id="chips"></div>
  <textarea id="input" placeholder="给 Carnot 发消息，输入 / 选择 skill，Enter 发送，Shift+Enter 换行"></textarea>
  <div id="row"><select id="provider" title="使用的 AI"></select><select id="permission" title="AI 的权限，从下一条消息开始生效"><option value="plan">只读规划</option><option value="acceptEdits">可改文件</option><option value="auto">自动</option><option value="bypassPermissions">完全放行</option></select><button id="attach" title="附加文件或文件夹">＋ 附件</button><span class="sp"></span><button id="send" class="main">发送</button></div>`;
const $ = id => document.getElementById(id);
const input = $("input");
let skills = [], paths = [], skill = null, withFile = true, file = "", busy = false, providers = [], current = null, live = null, menuIndex = 0;

const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function inline(s) {   // s is already escaped
  return s.replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2">$1</a>');
}

function markdown(src) {
  const out = [], parts = src.split(/```/);
  parts.forEach((part, i) => {
    if (i % 2) {  // inside a fence (an unfinished one, while streaming, counts too)
      out.push(`<pre><code>${esc(part.replace(/^[^\n]*\n/, ""))}</code></pre>`);
      return;
    }
    let list = null;
    const close = () => { if (list) { out.push(`</${list}>`); list = null; } };
    let para = [];
    const flush = () => { if (para.length) { out.push(`<p>${inline(esc(para.join("\n"))).replace(/\n/g, "<br>")}</p>`); para = []; } };
    for (const line of part.split("\n")) {
      const h = line.match(/^(#{1,3})\s+(.*)/), ul = line.match(/^\s*[-*]\s+(.*)/), ol = line.match(/^\s*\d+\.\s+(.*)/);
      if (h) { flush(); close(); out.push(`<h${h[1].length}>${inline(esc(h[2]))}</h${h[1].length}>`); }
      else if (ul || ol) {
        flush();
        const kind = ul ? "ul" : "ol";
        if (list !== kind) { close(); out.push(`<${kind}>`); list = kind; }
        out.push(`<li>${inline(esc((ul || ol)[1]))}</li>`);
      }
      else if (!line.trim()) { flush(); close(); }
      else { close(); para.push(line); }
    }
    flush(); close();
  });
  return out.join("");
}

function nameOf(id) { return (providers.find(p => p.id === id) || {}).name || id; }

function addUser(m) {
  const d = document.createElement("div"); d.className = "msg user";
  const tags = [...(m.skill ? [`skill：${m.skill}`] : []), ...(m.paths || []).map(p => "📎 " + p.split("/").filter(Boolean).pop())];
  d.innerHTML = tags.map(t => `<span class="tag">${esc(t)}</span>`).join("") + `<div>${esc(m.text).replace(/\n/g, "<br>")}</div>`;
  log.append(d);
}

function addReply(provider) {
  const d = document.createElement("div"); d.className = "msg";
  d.innerHTML = `<div class="who">${esc(nameOf(provider))}</div><div class="tools"></div><div class="body"></div><div class="dots">…</div>`;
  log.append(d);
  return { el: d, text: "" };
}

function drawReply(r) { r.el.querySelector(".body").innerHTML = markdown(r.text); scroll(); }
function addTool(r, name, detail) {
  const t = document.createElement("div"); t.className = "tool"; t.title = detail || "";
  t.textContent = `▸ ${name}${detail ? " " + detail : ""}`;
  r.el.querySelector(".tools").append(t); scroll();
}
function addError(r, text) {
  const e = document.createElement("div"); e.className = "error"; e.textContent = text;
  (r ? r.el : log).append(e); scroll();
}
const scroll = () => { log.scrollTop = log.scrollHeight; };

function drawEmpty() {
  if (log.children.length) return;
  log.innerHTML = `<div class="empty">和 Carnot App 共用同一套记忆和 skill。<br>输入 / 选择 skill，右上角可连接其他 AI。</div>`;
}

function drawHistory(messages) {
  log.textContent = "";
  for (const m of messages) {
    if (m.role === "user") { addUser(m); continue; }
    const r = addReply(m.provider); r.text = m.text;
    r.el.querySelector(".dots").remove();
    (m.tools || []).forEach(t => addTool(r, t.name, t.detail));
    drawReply(r);
    if (m.error) addError(r, m.error);
  }
  drawEmpty(); scroll();
}

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
  paths.forEach(p => chip("📎 " + p.split("/").filter(Boolean).pop(), () => { paths = paths.filter(q => q !== p); drawChips(); }));
}

function setBusy(on) {
  busy = on;
  $("send").textContent = on ? "停止" : "发送";
  $("send").className = on ? "" : "main";
}

function send() {
  if (busy) return vs.postMessage({ type: "stop" });
  const text = input.value.trim();
  if (!text && !paths.length) return;
  vs.postMessage({ type: "send", text, paths, skill, withFile: withFile && !!file });
  input.value = ""; paths = []; skill = null; hideMenu(); drawChips();
}

// ---------- the / menu ----------

function matches() {
  const m = input.value.match(/^\/(\S*)$/);
  return m ? skills.filter(s => s.name.toLowerCase().includes(m[1].toLowerCase())) : null;
}
function hideMenu() { $("menu").hidden = true; }
function drawMenu() {
  const list = matches(), menu = $("menu");
  if (!list || !list.length) return hideMenu();
  menuIndex = Math.min(menuIndex, list.length - 1);
  menu.hidden = false; menu.textContent = "";
  list.forEach((s, i) => {
    const d = document.createElement("div"); d.className = i === menuIndex ? "sel" : "";
    d.textContent = s.name;
    const small = document.createElement("small"); small.textContent = s.description; d.append(small);
    d.onmousedown = e => { e.preventDefault(); choose(s); };
    menu.append(d);
  });
}
function choose(s) { skill = s.name; input.value = ""; hideMenu(); drawChips(); input.focus(); }

input.addEventListener("input", () => { menuIndex = 0; drawMenu(); });
input.addEventListener("keydown", e => {
  const list = !$("menu").hidden && matches();
  if (list && list.length) {
    if (e.key === "ArrowDown") { menuIndex = (menuIndex + 1) % list.length; drawMenu(); return e.preventDefault(); }
    if (e.key === "ArrowUp") { menuIndex = (menuIndex + list.length - 1) % list.length; drawMenu(); return e.preventDefault(); }
    if (e.key === "Enter" || e.key === "Tab") { choose(list[menuIndex]); return e.preventDefault(); }
    if (e.key === "Escape") return hideMenu();
  }
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
});
$("send").onclick = send;
$("attach").onclick = () => vs.postMessage({ type: "attach" });
$("provider").onchange = e => vs.postMessage({ type: "provider", id: e.target.value });
$("permission").onchange = e => vs.postMessage({ type: "permission", mode: e.target.value });

window.addEventListener("message", ({ data: m }) => {
  if (m.type === "state") {
    providers = m.providers; current = m.provider; skills = m.skills; file = m.file; drawProviders(); drawChips();
    $("permission").value = m.permission;
    if (!busy) drawHistory(m.messages);
    setBusy(m.busy);
  } else if (m.type === "user") {
    log.querySelector(".empty")?.remove(); addUser(m); scroll();
  } else if (m.type === "busy") {
    setBusy(m.on);
    if (m.on) { live = addReply(m.provider); scroll(); }
    else if (live) { live.el.querySelector(".dots")?.remove(); live = null; }
  } else if (m.type === "delta") {
    if (live) { live.text += m.text; drawReply(live); }
  } else if (m.type === "tool") {
    if (live) addTool(live, m.name, m.detail);
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
drawEmpty(); drawChips();
vs.postMessage({ type: "ready" });
