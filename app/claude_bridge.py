"""Runs voice commands through headless Claude Code (`claude -p`) and streams the reply to TTS."""
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time

from tts import SentenceSplitter

SYSTEM_PROMPT = """你正在通过语音与用户交互：用户的指令来自语音识别，可能有同音错字，请结合上下文理解。你输出的所有文字都会被 TTS 朗读出来。
要求：
1. 直接动手完成指令，用户不在屏幕前，不要反问细节，自己做出合理判断。
2. 开始较长的操作前，先用一句话说明你要做什么。
3. 用简短口语化的中文回复，不用 Markdown、表格、代码块，不要念长路径、URL 或代码。
4. 完成后用一两句话汇报结果。
5. 删除重要文件、git push、对外发送消息等不可逆操作，先口头说明并请用户确认，等用户下一句话同意后再执行。
6. 消息末尾的 [摄像头画面：路径] 或 [屏幕画面：路径] 是用户说这句话那一刻的画面，先用 Read 工具查看图片再回答，像面对面聊天一样自然地说你看到了什么。"""

SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".claude_session")


class ClaudeBridge:
    def __init__(self, cfg, speaker, on_change, log):
        self.cfg = cfg
        self.speaker = speaker
        self.on_change = on_change
        self.log = log
        self.claude = shutil.which("claude") or "claude"
        self.workdir = os.path.expanduser(cfg["workdir"])
        self.session_id = None
        if cfg.get("keep_session", True) and os.path.exists(SESSION_FILE):
            self.session_id = open(SESSION_FILE).read().strip() or None
        self._q = queue.Queue()
        self._proc = None
        self._running = False
        self._muted = False  # barge-in: the rest of the current reply is not spoken
        threading.Thread(target=self._loop, daemon=True).start()

    @property
    def busy(self):
        return self._running or not self._q.empty()

    def submit(self, text):
        self._q.put(text)
        self.on_change()

    def mute(self):
        """Silences the rest of the running reply; the task itself keeps going."""
        if self._running:
            self._muted = True
            self.log("[claude] reply muted (task keeps running, say 停止 to kill it)")

    def stop(self):
        while not self._q.empty():
            self._q.get_nowait()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        self.on_change()

    def new_session(self):
        self.session_id = None
        if os.path.exists(SESSION_FILE):
            os.remove(SESSION_FILE)

    def wait_idle(self):
        while self.busy:
            time.sleep(0.2)

    def _loop(self):
        while True:
            text = self._q.get()
            self._running = True
            self.on_change()
            try:
                self._run(text)
            except Exception as e:
                self.log(f"[claude] error: {e}")
                self.speaker.say("调用 Claude 出错了")
            finally:
                self._running = False
                self.on_change()

    def _cmd(self):
        cmd = [self.claude, "-p", "--output-format", "stream-json", "--verbose",
               "--include-partial-messages",
               "--permission-mode", self.cfg["permission_mode"],
               "--append-system-prompt", SYSTEM_PROMPT]
        if self.cfg.get("model"):
            cmd += ["--model", self.cfg["model"]]
        for d in self.cfg.get("add_dirs", []):
            cmd += ["--add-dir", os.path.expanduser(d)]
        if self.session_id and self.cfg.get("keep_session", True):
            cmd += ["--resume", self.session_id]
        return cmd

    def _run(self, text):
        self.log(f"[claude] >>> {text}")
        self._muted = False
        t0 = time.time()
        splitter = SentenceSplitter(self._speak)
        streamed = False  # got token deltas; skip the duplicate full-message text
        errf = tempfile.TemporaryFile(mode="w+")
        self._proc = subprocess.Popen(self._cmd(), cwd=self.workdir, text=True, bufsize=1,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=errf)
        self._proc.stdin.write(text)  # via stdin so the prompt can't be parsed as a flag
        self._proc.stdin.close()

        for line in self._proc.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "system" and ev.get("subtype") == "init":
                self._save_session(ev.get("session_id"))
            elif t == "stream_event":
                e = ev.get("event", {})
                if e.get("type") == "content_block_delta" and e["delta"].get("type") == "text_delta":
                    streamed = True
                    splitter.feed(e["delta"]["text"])
                elif e.get("type") in ("content_block_stop", "message_stop"):
                    splitter.flush()
            elif t == "assistant":
                for block in ev.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        self.log(f"[claude] tool {block['name']}: {_brief(block.get('input'))}")
                    elif block.get("type") == "text" and not streamed:
                        splitter.feed(block["text"])
                        splitter.flush()
            elif t == "result":
                splitter.flush()
                self._save_session(ev.get("session_id"))
                status = "error" if ev.get("is_error") else "done"
                self.log(f"[claude] {status} in {time.time() - t0:.0f}s: {ev.get('result', '')}")

        rc = self._proc.wait()
        if rc not in (0, -15):  # -15 = stopped by voice command
            errf.seek(0)
            err = errf.read().strip()
            self.log(f"[claude] exit {rc}: {err}")
            if self.session_id and "No conversation found" in err:
                self.new_session()
                return self._run(text)
            self.speaker.say("Claude 执行失败了，详情请看终端")

    def _speak(self, sentence):
        if self._muted:
            self.log(f"[muted] {sentence}")
            return
        self.log(f"[speak] {sentence}")
        self.speaker.say(sentence)

    def _save_session(self, sid):
        if not sid or not self.cfg.get("keep_session", True):
            return
        self.session_id = sid
        with open(SESSION_FILE, "w") as f:
            f.write(sid)


def _brief(inp):
    if not isinstance(inp, dict):
        return ""
    for k in ("command", "file_path", "pattern", "url", "description"):
        if k in inp:
            return str(inp[k])[:120]
    return json.dumps(inp, ensure_ascii=False)[:120]
