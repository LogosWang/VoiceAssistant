"""Runs one turn through the selected AI's CLI and reports what happens. Used by both the voice
app (claude_bridge.py) and the VS Code extension (carnot_agent.py)."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

import attachments
import compat
import computer
import model_choice
import providers
import shared

MAX_OUTPUT = 30000     # characters of a tool's output kept for showing
AUTH_ERROR = re.compile(r"/login|authentication_failed|not logged in|oauth token|\b401\b|unauthori[sz]ed"
                        r"|api key not valid|invalid api key|auth method", re.I)


class Agent:
    """on_text gets reply text as it arrives; on_flush marks the end of a block of it. on_step gets
    the rest as it happens: thinking and tool calls with their input and output. parts holds it all
    in the order it happened, for the transcript. on_permission, Claude Code only, answers the
    questions it would otherwise deny: given the request, it returns {"allow": bool, ...}."""

    def __init__(self, provider, workdir, permission, system="", add_dirs=(), session=None,
                 on_text=None, on_flush=None, on_log=None, on_session=None, on_tool=None, on_step=None, on_permission=None, may_schedule=True):
        self.provider = provider
        self.stopping = False
        self.may_schedule = may_schedule   # a scheduled task doesn't get to make more of them
        self.workdir = workdir
        self.permission = permission
        self.system = system
        self.add_dirs = list(add_dirs)
        self.session = session
        self._text_out = on_text or (lambda t: None)
        self._step_out = on_step or (lambda step: None)
        self.on_permission = on_permission
        self.usage = {}
        self.parts = []
        self._tools = {}
        self.on_flush = on_flush or (lambda: None)
        self.log = on_log or (lambda m: None)
        self.on_session = on_session or (lambda s: None)
        self.on_tool = on_tool or (lambda name, detail: None)
        self.proc = None

    # ---------- what happens, in order ----------

    def on_text(self, text):
        self._append("text", text)
        self._text_out(text)

    def _thinking(self, text):
        if not text:        # some models keep their thinking to themselves
            return
        self._append("thinking", text)
        self._step_out({"kind": "thinking", "text": text})

    def _append(self, kind, text):
        if self.parts and self.parts[-1]["type"] == kind:
            self.parts[-1]["text"] += text
        else:
            self.parts.append({"type": kind, "text": text})

    def _tool_start(self, tool_id, name, tool_input):
        if tool_id in self._tools:
            return
        part = {"type": "tool", "id": tool_id, "name": name, "input": tool_input}
        self._tools[tool_id] = part
        self.parts.append(part)
        self.on_tool(name, brief(tool_input))
        self._step_out({"kind": "tool_start", "id": tool_id, "name": name, "input": tool_input})

    def _tool_end(self, tool_id, output, error=False, patch=None):
        output = output or ""
        if len(output) > MAX_OUTPUT:
            output = output[:MAX_OUTPUT] + f"\n…（输出太长，已截断，共 {len(output)} 字符）"
        part = self._tools.get(tool_id)
        if part is not None:
            part.update(output=output, error=bool(error), done=True)
            if patch:
                part["patch"] = patch
        self._step_out({"kind": "tool_end", "id": tool_id, "output": output, "error": bool(error), "patch": patch})

    def _usage(self, **values):
        self.usage.update({k: v for k, v in values.items() if v is not None})
        self._step_out({"kind": "usage", **self.usage})

    def _context(self, u):
        total = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
        if total:
            self._usage(context=total)

    def _answer(self, ev):
        """A question from Claude Code. Whatever goes wrong, it gets an answer, or the turn would hang."""
        req = ev.get("request") or {}
        rid = ev.get("request_id")
        if req.get("subtype") != "can_use_tool":
            reply = {"subtype": "error", "request_id": rid, "error": "not supported"}
        else:
            try:
                d = self.on_permission({"id": rid, "tool": req.get("tool_name"), "input": req.get("input") or {},
                                        "description": req.get("description"), "tool_use_id": req.get("tool_use_id"),
                                        "suggestions": req.get("permission_suggestions") or []}) or {}
            except Exception as e:
                d = {"allow": False, "message": f"确认时出错：{type(e).__name__}"}
            if d.get("allow"):
                body = {"behavior": "allow", "updatedInput": d.get("input") or req.get("input") or {}}
                updates = d.get("updates") or (req.get("permission_suggestions") if d.get("always") else None)
                if updates:
                    body["updatedPermissions"] = updates
            else:
                body = {"behavior": "deny", "message": d.get("message") or "用户拒绝了这个操作。"}
            reply = {"subtype": "success", "request_id": rid, "response": body}
        try:
            self.proc.stdin.write(json.dumps({"type": "control_response", "response": reply}, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            pass

    def _saw_session(self, sid):
        if sid:
            self.session = sid
            self.on_session(sid)

    # ---------- command lines ----------

    def command(self, dirs):
        p = providers.PROVIDERS[self.provider]
        asked = self.permission == "manual" and self.on_permission and p["engine"] == "claude"
        # nothing can ask on the other CLIs from here, so "ask each time" means change nothing
        mode = "manual" if asked else "plan" if self.permission == "manual" and self.on_permission else providers.normalize_permission(self.permission)
        sid = self.session
        model, effort = model_choice.resolve(self.provider)
        cli = shutil.which(p["cli"]) or p["cli"]
        dirs = [shared.MEMORY_DIR] + dirs
        if p["engine"] == "codex":
            cmd = [cli, "exec"] + (["resume", sid] if sid else [])
            cmd += ["--json", "--skip-git-repo-check"]
            if model:
                cmd += ["-c", f'model="{model}"']
            if effort:
                cmd += ["-c", f'model_reasoning_effort="{effort}"']
            if computer.mode() != "off":
                spec = computer.server_spec()
                cmd += ["-c", "mcp_servers.carnot_computer.command=" + json.dumps(spec["command"]),
                        "-c", "mcp_servers.carnot_computer.args=" + json.dumps(spec["args"])]
            if mode == "bypassPermissions":
                cmd.append("--dangerously-bypass-approvals-and-sandbox")
            else:  # -c, because a resumed session takes no --sandbox or --add-dir
                cmd += ["-c", 'sandbox_mode="%s"' % ("read-only" if mode == "plan" else "workspace-write")]
                if mode != "plan":
                    cmd += ["-c", "sandbox_workspace_write.writable_roots=" + json.dumps([*dirs, shared.SCHEDULES_DIR])]
                if mode == "auto" and not sid:  # a resumed session doesn't take this flag
                    cmd.append("--approve-for-me")
            return cmd + ["-"]
        if p["engine"] == "gemini":
            cmd = [cli, "-p", "", "--output-format", "stream-json", "--skip-trust"]
            if model:
                cmd += ["-m", model]
            cmd += ["--approval-mode", {"plan": "plan", "bypassPermissions": "yolo"}.get(mode, "auto_edit")]
            cmd += ["--include-directories", ",".join(dirs)]
            return cmd + (["--resume", sid] if sid else [])
        cu = computer.mode()
        system = "\n\n".join(s for s in (self.system, shared.memory_prompt(), computer.prompt(cu)) if s)
        # Claude Code's own per-project memory would split what's remembered
        cmd = [cli, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
               "--permission-mode", mode, *self._system_flag(system),
               "--settings", json.dumps({"autoMemoryEnabled": False})]
        # these die with the session; the shared list is schedules.py, as the system prompt says
        cmd.append("--disallowedTools=CronCreate,CronDelete,CronList,ScheduleWakeup")
        allowed = []
        if self.may_schedule and mode in ("auto", "acceptEdits"):
            allowed.append(f"Bash({shared.schedules_tool()} schedule-*)")
        if cu != "off":
            cmd += ["--mcp-config", json.dumps({"mcpServers": {computer.SERVER: computer.server_spec()}})]
            if mode in ("auto", "acceptEdits"):  # turned on by the user; taking the mouse still asks
                allowed.append("mcp__" + computer.SERVER)
        if allowed:
            cmd.append("--allowedTools=" + ",".join(allowed))
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        if self.on_permission:   # keeps stdin open, so questions can be answered during the turn
            cmd += ["--input-format", "stream-json", "--permission-prompt-tool", "stdio"]
        for d in [os.path.expanduser(d) for d in self.add_dirs] + dirs:
            cmd += ["--add-dir", d]
        if sid:
            cmd += ["--resume", sid]
        return cmd

    def _system_flag(self, system):
        """A Windows command line ends at 32767 characters, and memory keeps growing."""
        if not compat.IS_WIN or len(system) < 20000:
            return ["--append-system-prompt", system]
        os.makedirs(shared.HOME, exist_ok=True)
        path = os.path.join(shared.HOME, f"system-prompt-{os.getpid()}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(system)
        return ["--append-system-prompt-file", path]

    def _prompt(self, text):
        """CLIs without a system prompt flag get the shared context with the message, every turn,
        because memory may have changed since their session started."""
        if providers.PROVIDERS[self.provider]["engine"] == "claude":
            if self.on_permission:
                return json.dumps({"type": "user", "message": {"role": "user", "content": text}}, ensure_ascii=False) + "\n"
            return text
        parts = [shared.instructions_prompt(), shared.memory_prompt()]
        if providers.PROVIDERS[self.provider]["engine"] == "codex":  # gemini has no flag for an MCP server
            parts.append(computer.prompt())
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
        if t == "system" and ev.get("subtype") == "api_retry":
            self._step_out({"kind": "retry", "attempt": ev.get("attempt"), "max": ev.get("max_retries"),
                            "status": ev.get("error_status"), "error": ev.get("error")})
        if t == "system" and ev.get("subtype") == "init":
            self._saw_session(ev.get("session_id"))
            self._step_out({"kind": "init", "commands": ev.get("slash_commands") or [],
                            "terminal": ev.get("terminal_slash_commands") or [], "model": ev.get("model"),
                            "subscription": self.provider == "claude" and ev.get("apiKeySource") == "none"})
        elif t == "rate_limit_event":
            info = ev.get("rate_limit_info") or {}
            if info.get("status") not in (None, "allowed"):
                self._step_out({"kind": "limit", "status": info.get("status"), "resets": info.get("resetsAt")})
        elif t == "stream_event":
            e = ev.get("event", {})
            if e.get("type") == "message_delta":  # other services only give the count here
                self._context(e.get("usage") or {})
            if e.get("type") == "content_block_delta" and e["delta"].get("type") == "text_delta":
                st["streamed"] = True  # got token deltas; skip the duplicate full-message text
                self.on_text(e["delta"]["text"])
            elif e.get("type") == "content_block_delta" and e["delta"].get("type") == "thinking_delta":
                self._thinking(e["delta"].get("thinking", ""))
            elif e.get("type") in ("content_block_stop", "message_stop"):
                self.on_flush()
        elif t == "assistant":
            u = ev.get("message", {}).get("usage") or {}
            if u:
                self._context(u)
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    self.log(f"[claude] tool {block['name']}: {brief(block.get('input'))}")
                    self._tool_start(block.get("id"), block["name"], block.get("input"))
                elif block.get("type") == "text" and not st.get("streamed"):
                    self.on_text(block["text"])
                    self.on_flush()
        elif t == "user":
            extra = ev.get("tool_use_result")
            for block in ev.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self._tool_end(block.get("tool_use_id"), result_text(block.get("content")), block.get("is_error"),
                                   extra.get("structuredPatch") if isinstance(extra, dict) else None)
        elif t == "result":
            self.on_flush()
            self._saw_session(ev.get("session_id"))
            status = "error" if ev.get("is_error") else "done"
            models = ev.get("modelUsage") or {}
            is_claude = self.provider == "claude"  # what the CLI prices other models at means nothing
            self._usage(cost=ev.get("total_cost_usd") if is_claude else None, window=max((m.get("contextWindow") or 0 for m in models.values()), default=0) or None,
                        input=sum(m.get("inputTokens", 0) + m.get("cacheCreationInputTokens", 0) for m in models.values()),
                        output=sum(m.get("outputTokens", 0) for m in models.values()), done=True)
            self.log(f"[claude] {status} in {time.time() - st['t0']:.0f}s: {ev.get('result', '')}")
            if ev.get("is_error"):
                st["failed"] = str(ev.get("result", ""))[:300]

    def _codex_event(self, ev, st):
        t = ev.get("type")
        if t == "thread.started":
            self._saw_session(ev.get("thread_id"))
        elif t == "item.started":
            item = ev.get("item", {})
            if item.get("type") == "command_execution":
                self._tool_start(item.get("id"), "Bash", {"command": unwrap_shell(item.get("command", ""))})
        elif t == "item.completed":
            item = ev.get("item", {})
            kind = item.get("type")
            if kind == "agent_message":
                self.on_text(item.get("text", "") + "\n\n")
                self.on_flush()
            elif kind == "reasoning":
                self._thinking(item.get("text", "") + "\n")
            elif kind == "command_execution":
                self._tool_start(item.get("id"), "Bash", {"command": unwrap_shell(item.get("command", ""))})
                self.log(f"[codex] command: {str(item.get('command', ''))[:120]}")
                self._tool_end(item.get("id"), item.get("aggregated_output"), item.get("exit_code") not in (0, None))
            elif kind == "file_change":
                self._tool_start(item.get("id"), "Edit", {"changes": item.get("changes", [])})
                self._tool_end(item.get("id"), "", item.get("status") == "failed")
            elif kind != "error":
                self._tool_start(item.get("id"), kind or "tool", {k: v for k, v in item.items() if k not in ("id", "type")})
                self._tool_end(item.get("id"), "", item.get("status") == "failed")
        elif t == "turn.failed":
            st["failed"] = str(ev.get("error", ev))[:300]
        elif t == "turn.completed":
            self.log(f"[codex] done in {time.time() - st['t0']:.0f}s")
            u = ev.get("usage") or {}
            self._usage(input=u.get("input_tokens"), output=u.get("output_tokens"), context=u.get("input_tokens"), done=True)

    def _gemini_event(self, ev, st):
        t = ev.get("type")
        if t == "init":
            self._saw_session(ev.get("session_id"))
        elif t == "message" and ev.get("role") == "assistant":
            self.on_text(ev.get("content", ""))
        elif t == "tool_use":
            self.log(f"[gemini] tool {ev.get('tool_name', '')}")
            self._tool_start(ev.get("tool_id"), ev.get("tool_name", "tool"), ev.get("parameters"))
        elif t == "tool_result":
            self._tool_end(ev.get("tool_id"), result_text(ev.get("output")), ev.get("status") == "error")
        elif t == "result":
            self.on_flush()
            if ev.get("status") == "error":
                st["failed"] = str(ev.get("error", ev))[:300]
            self.log(f"[gemini] {ev.get('status')} in {time.time() - st['t0']:.0f}s")
            stats = ev.get("stats") or {}
            self._usage(input=stats.get("input_tokens"), output=stats.get("output_tokens"), context=stats.get("input_tokens"), done=True)

    # ---------- a turn ----------

    def run(self, text, dirs=()):
        """Returns (status, detail): ok, auth (refused credentials), missing (no CLI), stopped,
        no_conversation (the session to resume is gone) or failed."""
        p = providers.PROVIDERS[self.provider]
        if not shutil.which(p["cli"]):
            providers.fix_path(fresh=True)  # installed since the shell was last asked?
        if not shutil.which(p["cli"]):
            return "missing", p["cli"]
        text, dirs = self._attachments(text, dirs)
        if self.stopping:
            return "stopped", ""
        before = self.session
        st = {"t0": time.time()}
        auth_failed = False
        handler = getattr(self, f"_{p['engine']}_event")
        errf = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(compat.argv(self.command(list(dirs))), cwd=self.workdir, text=True, encoding="utf-8", errors="replace", bufsize=1,
                                     env=providers.apply_env(self.provider), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=errf)
        stays_open = self.on_permission and p["engine"] == "claude"
        self.proc.stdin.write(self._prompt(text))  # via stdin so the prompt can't be parsed as a flag
        if stays_open:
            self.proc.stdin.flush()
        else:
            self.proc.stdin.close()
        for line in self.proc.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "control_request":
                self._answer(ev)
                continue
            if self.is_auth_error(ev):
                auth_failed = True  # it would only retry for a minute with the same credentials
                compat.stop_process(self.proc)
                break  # not waiting for end of file: a child of the CLI may hold the pipe open
            handler(ev, st)
            if stays_open and ev.get("type") == "result":
                self.proc.stdin.close()   # the turn is over; the CLI exits when its input ends
        self.on_flush()
        if stays_open and not self.proc.stdin.closed:
            self.proc.stdin.close()
        rc = self.proc.wait()
        errf.seek(0)
        err = errf.read().strip()
        if "IneligibleTierError" in err:
            providers.gemini_refused()
            return "failed", ("Google 不再允许免费个人账号通过 Gemini CLI 使用，所以网页登录成功了也连不上。"
                              "请在连接设置里填 Gemini API Key（aistudio.google.com/apikey）。")
        stopped = self.stopping or rc in (-15, 143)  # 143: the CLI caught the signal and exited itself
        if auth_failed or (rc != 0 and not stopped and AUTH_ERROR.search(err)):
            self.session = before  # that turn didn't happen; keep the conversation
            return "auth", err[-300:]
        if stopped:
            return "stopped", ""
        if "No conversation found" in err:
            return "no_conversation", err[-300:]
        if st.get("failed") or rc != 0:
            return "failed", f"exit {rc}: {st.get('failed', '')} {err[-300:]}".strip()
        return "ok", ""

    def _attachments(self, text, dirs):
        if not attachments.targets(text):
            return text, dirs
        self._tool_start("attachments", "处理附件", {"files": [os.path.basename(p) for p in attachments.targets(text)]})
        text, allowed, summary = attachments.expand(text)
        self._tool_end("attachments", summary or "可以直接读取", False, None)
        return text, [*dirs, *allowed]

    def stop(self):
        self.stopping = True
        if self.proc:
            compat.stop_process(self.proc)


def result_text(content):
    """Tool output: a string, or blocks of which only the text can be shown."""
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return content if isinstance(content, str) else ""


def unwrap_shell(command):
    """Codex reports commands wrapped in a login shell. What it ran is the inside."""
    m = re.fullmatch(r"""(?:/bin/z?sh -l?c|\S*(?:powershell|pwsh)(?:\.exe)?"? (?:-NoProfile )?-Command) (['"]?)(.*)\1""", command, re.S | re.I)
    return m.group(2) if m else command


def brief(inp):
    if not isinstance(inp, dict):
        return ""
    for k in ("command", "file_path", "pattern", "url", "description"):
        if k in inp:
            return str(inp[k])[:120]
    return json.dumps(inp, ensure_ascii=False)[:120]
