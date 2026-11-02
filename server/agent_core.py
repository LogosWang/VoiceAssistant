"""Runs one turn through the selected AI's CLI and reports what happens. Used by both the voice
app (claude_bridge.py) and the VS Code extension (carnot_agent.py)."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

import providers
import shared

AUTH_ERROR = re.compile(r"/login|authentication_failed|not logged in|oauth token|\b401\b|unauthori[sz]ed"
                        r"|api key not valid|invalid api key|auth method", re.I)


class Agent:
    """on_text gets reply text as it arrives; on_flush marks the end of a block of it."""

    def __init__(self, provider, workdir, permission, system="", model=None, add_dirs=(), session=None,
                 on_text=None, on_flush=None, on_log=None, on_session=None, on_tool=None):
        self.provider = provider
        self.workdir = workdir
        self.permission = permission
        self.system = system
        self.model = model
        self.add_dirs = list(add_dirs)
        self.session = session
        self.on_text = on_text or (lambda t: None)
        self.on_flush = on_flush or (lambda: None)
        self.log = on_log or (lambda m: None)
        self.on_session = on_session or (lambda s: None)
        self.on_tool = on_tool or (lambda name, detail: None)
        self.proc = None

    def _saw_session(self, sid):
        if sid:
            self.session = sid
            self.on_session(sid)

    # ---------- command lines ----------

    def command(self, dirs):
        p = providers.PROVIDERS[self.provider]
        mode = providers.normalize_permission(self.permission)
        sid = self.session
        cli = shutil.which(p["cli"]) or p["cli"]
        dirs = [shared.MEMORY_DIR] + dirs
        if p["engine"] == "codex":
            cmd = [cli, "exec"] + (["resume", sid] if sid else [])
            cmd += ["--json", "--skip-git-repo-check"]
            if mode == "bypassPermissions":
                cmd.append("--dangerously-bypass-approvals-and-sandbox")
            else:  # -c, because a resumed session takes no --sandbox or --add-dir
                cmd += ["-c", 'sandbox_mode="%s"' % ("read-only" if mode == "plan" else "workspace-write")]
                if mode != "plan":
                    cmd += ["-c", "sandbox_workspace_write.writable_roots=" + json.dumps(dirs)]
                if mode == "auto" and not sid:  # a resumed session doesn't take this flag
                    cmd.append("--approve-for-me")
            return cmd + ["-"]
        if p["engine"] == "gemini":
            cmd = [cli, "-p", "", "--output-format", "stream-json", "--skip-trust"]
            cmd += ["--approval-mode", {"plan": "plan", "bypassPermissions": "yolo"}.get(mode, "auto_edit")]
            cmd += ["--include-directories", ",".join(dirs)]
            return cmd + (["--resume", sid] if sid else [])
        system = "\n\n".join(s for s in (self.system, shared.memory_prompt()) if s)
        # Claude Code's own per-project memory would split what's remembered
        cmd = [cli, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
               "--permission-mode", mode, "--append-system-prompt", system,
               "--settings", json.dumps({"autoMemoryEnabled": False})]
        model = p.get("model") or self.model
        if model:
            cmd += ["--model", model]
        for d in [os.path.expanduser(d) for d in self.add_dirs] + dirs:
            cmd += ["--add-dir", d]
        if sid:
            cmd += ["--resume", sid]
        return cmd

    def _prompt(self, text):
        """CLIs without a system prompt flag get the shared context with the message, every turn,
        because memory may have changed since their session started."""
        if providers.PROVIDERS[self.provider]["engine"] == "claude":
            return text
        parts = [shared.instructions_prompt(), shared.memory_prompt()]
        if not self.session and self.system:
            parts.insert(0, self.system)
        return "\n\n".join(p for p in parts if p) + f"\n\n# 用户的消息\n{text}"

    # ---------- events per CLI ----------

    @staticmethod
    def is_auth_error(ev):
        if ev.get("error") == "authentication_failed" or 401 in (ev.get("api_error_status"), ev.get("error_status")):
            return True
        failed = ev.get("type") in ("error", "turn.failed") or (ev.get("type") == "result" and ev.get("status") == "error")
        return failed and bool(AUTH_ERROR.search(json.dumps(ev)))

    def _claude_event(self, ev, st):
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            self._saw_session(ev.get("session_id"))
        elif t == "stream_event":
            e = ev.get("event", {})
            if e.get("type") == "content_block_delta" and e["delta"].get("type") == "text_delta":
                st["streamed"] = True  # got token deltas; skip the duplicate full-message text
                self.on_text(e["delta"]["text"])
            elif e.get("type") in ("content_block_stop", "message_stop"):
                self.on_flush()
        elif t == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    self.log(f"[claude] tool {block['name']}: {brief(block.get('input'))}")
                    self.on_tool(block["name"], brief(block.get("input")))
                elif block.get("type") == "text" and not st.get("streamed"):
                    self.on_text(block["text"])
                    self.on_flush()
        elif t == "result":
            self.on_flush()
            self._saw_session(ev.get("session_id"))
            status = "error" if ev.get("is_error") else "done"
            self.log(f"[claude] {status} in {time.time() - st['t0']:.0f}s: {ev.get('result', '')}")
            if ev.get("is_error"):
                st["failed"] = str(ev.get("result", ""))[:300]

    def _codex_event(self, ev, st):
        t = ev.get("type")
        if t == "thread.started":
            self._saw_session(ev.get("thread_id"))
        elif t == "item.completed":
            item = ev.get("item", {})
            if item.get("type") == "agent_message":
                self.on_text(item.get("text", ""))
                self.on_flush()
            else:
                detail = str(item.get("command", ""))[:120]
                self.log(f"[codex] {item.get('type')}: {detail}")
                self.on_tool(item.get("type", "tool"), detail)
        elif t == "turn.failed":
            st["failed"] = str(ev.get("error", ev))[:300]
        elif t == "turn.completed":
            self.log(f"[codex] done in {time.time() - st['t0']:.0f}s")

    def _gemini_event(self, ev, st):
        t = ev.get("type")
        if t == "init":
            self._saw_session(ev.get("session_id"))
        elif t == "message" and ev.get("role") == "assistant":
            self.on_text(ev.get("content", ""))
        elif t == "tool_use":
            self.log(f"[gemini] tool {ev.get('tool_name', '')}")
            self.on_tool(ev.get("tool_name", "tool"), brief(ev.get("parameters")))
        elif t == "result":
            self.on_flush()
            if ev.get("status") == "error":
                st["failed"] = str(ev.get("error", ev))[:300]
            self.log(f"[gemini] {ev.get('status')} in {time.time() - st['t0']:.0f}s")

    # ---------- a turn ----------

    def run(self, text, dirs=()):
        """Returns (status, detail): ok, auth (refused credentials), missing (no CLI), stopped,
        no_conversation (the session to resume is gone) or failed."""
        p = providers.PROVIDERS[self.provider]
        if not shutil.which(p["cli"]):
            return "missing", p["cli"]
        before = self.session
        st = {"t0": time.time()}
        auth_failed = False
        handler = getattr(self, f"_{p['engine']}_event")
        errf = tempfile.TemporaryFile(mode="w+")
        self.proc = subprocess.Popen(self.command(list(dirs)), cwd=self.workdir, text=True, bufsize=1,
                                     env=providers.apply_env(self.provider), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=errf)
        self.proc.stdin.write(self._prompt(text))  # via stdin so the prompt can't be parsed as a flag
        self.proc.stdin.close()
        for line in self.proc.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if self.is_auth_error(ev):
                auth_failed = True  # it would only retry for a minute with the same credentials
                self.proc.terminate()
                break  # not waiting for end of file: a child of the CLI may hold the pipe open
            handler(ev, st)
        self.on_flush()
        rc = self.proc.wait()
        errf.seek(0)
        err = errf.read().strip()
        if auth_failed or (rc not in (0, -15) and AUTH_ERROR.search(err)):
            self.session = before  # that turn didn't happen; keep the conversation
            return "auth", err[-300:]
        if rc == -15:
            return "stopped", ""
        if "No conversation found" in err:
            return "no_conversation", err[-300:]
        if st.get("failed") or rc != 0:
            return "failed", f"exit {rc}: {st.get('failed', '')} {err[-300:]}".strip()
        return "ok", ""

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def brief(inp):
    if not isinstance(inp, dict):
        return ""
    for k in ("command", "file_path", "pattern", "url", "description"):
        if k in inp:
            return str(inp[k])[:120]
    return json.dumps(inp, ensure_ascii=False)[:120]
