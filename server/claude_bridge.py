"""Runs voice commands through headless Claude Code (`claude -p`) and streams the reply to TTS."""
import os
import queue
import subprocess
import tempfile
import threading
import time


import agent_core
import events
import providers
import sessions
from tts import SentenceSplitter

SYSTEM_PROMPT = """你正在通过语音与用户交互：用户的指令来自语音识别，可能有同音错字，请结合上下文理解。你输出的所有文字都会被 TTS 朗读出来。
要求：
1. 直接动手完成指令，用户不在屏幕前，不要反问细节，自己做出合理判断。
2. 开始较长的操作前，先用一句话说明你要做什么。
3. 用简短口语化的中文回复，不用 Markdown、表格、代码块，不要念长路径、URL 或代码。
4. 完成后用一两句话汇报结果。
5. 删除重要文件、git push、对外发送消息等不可逆操作，先口头说明并请用户确认，等用户下一句话同意后再执行。
6. 消息末尾的 [摄像头画面：路径] 或 [屏幕画面：路径] 是用户说这句话那一刻的画面，先用 Read 工具查看图片再回答，像面对面聊天一样自然地说你看到了什么。
7. 消息末尾的 [附件文件：路径] 或 [附件文件夹：路径] 是用户附加的内容，先读取它们再回答。"""

LOGIN_TIMEOUT = 600  # seconds to finish signing in, in the browser
class ClaudeBridge:
    """Runs commands one at a time through the selected provider's CLI, and speaks the reply."""

    def __init__(self, cfg, speaker, on_change, log):
        self.cfg = cfg
        self.speaker = speaker
        self.on_change = on_change
        self.log = log
        self.workdir = os.path.expanduser(cfg["workdir"])
        self.provider = providers.current()
        self.conversation = sessions.current()
        self.session_id = self._load_session()
        self._q = queue.Queue()
        self._proc = None  # a login in progress
        self._agent = None
        self._running = False
        self._muted = False  # barge-in: the rest of the current reply is not spoken
        threading.Thread(target=self._loop, daemon=True).start()

    @property
    def busy(self):
        return self._running or not self._q.empty()

    @property
    def name(self):
        return providers.PROVIDERS[self.provider]["name"]

    def _load_session(self):
        return sessions.native(self.conversation, self.provider) if self.cfg.get("keep_session", True) else None

    def submit(self, text, dirs=()):
        sessions.touch(self.conversation, text)
        self._q.put((text, list(dirs)))
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
        if self._agent:
            self._agent.stop()
        self.on_change()

    def new_session(self):
        self.use_session(sessions.create(make_current=True))

    def use_session(self, sid):
        """From the next command on; one that is running finishes in the conversation it started in."""
        if sessions.set_current(sid):
            self.conversation = sid
            self.session_id = self._load_session()
            self.log(f"[session] now in {sid}")

    def _forget_native(self):
        self.session_id = None
        sessions.set_native(self.conversation, self.provider, None)

    def switch(self, pid):
        """Takes effect on the next command; a running one finishes on the old service."""
        providers.set_current(pid)
        self.provider = pid
        self.session_id = self._load_session()
        self.log(f"[provider] now using {self.name}")

    def wait_idle(self):
        while self.busy:
            time.sleep(0.2)

    def _loop(self):
        while True:
            text, dirs = self._q.get()
            self._running = True
            self.on_change()
            try:
                self._run(text, dirs)
            except Exception as e:
                self.log(f"[claude] error: {e}")
                self.speaker.say(f"调用 {self.name} 出错了")
            finally:
                self._running = False
                self.on_change()

    # ---------- a run ----------

    def _run(self, text, dirs=(), retried=False):
        self.log(f"[{self.provider}] >>> {text}")
        dirs = list(dirs)
        self._muted = False
        pid = self.provider
        before = self.session_id
        splitter = SentenceSplitter(self._speak)
        self._agent = agent_core.Agent(
            pid, self.workdir, self.cfg["permission_mode"], system=SYSTEM_PROMPT, model=self.cfg.get("model"),
            add_dirs=self.cfg.get("add_dirs", []), session=before if self.cfg.get("keep_session", True) else None,
            on_text=splitter.feed, on_flush=splitter.flush, on_log=self.log, on_session=self._save_session)
        status, detail = self._agent.run(text, dirs)
        self._proc = None
        if status == "missing":
            events.emit("notice", text=f"{self.name} 的命令行工具还没安装，请到连接设置里安装")
            self.speaker.say(f"{self.name} 还没安装，请到连接设置里安装")
        elif status == "auth":
            if before:  # that run didn't happen; keep the conversation
                self.session_id = before
                self._save_session(before)
            else:
                self._forget_native()
            if retried:
                self.speaker.say(f"登录之后 {self.name} 还是连不上，详情请看日志")
            elif self._login():
                return self._run(text, dirs, retried=True)
        elif status == "no_conversation":
            self._forget_native()
            return self._run(text, dirs)
        elif status == "failed":
            self.log(f"[{pid}] failed, {detail}")
            self.speaker.say(f"{self.name} 执行失败了，详情请看日志")

    def _login(self):
        """Signing in expired: opens the browser login and waits. True once signed in."""
        pid = self.provider
        p = providers.PROVIDERS[pid]
        self.log(f"[{pid}] not signed in or the key was refused")
        if providers.get_key(pid) or not p["login"]:  # a key was refused: no browser can fix that
            events.emit("notice", text=f"{p['name']} 的 API Key 无效或已过期，请在连接设置里更新", hold=30000)
            events.emit("open_settings")
            self.speaker.say(f"{p['name']} 的 API Key 不能用，请在连接设置里更新")
            return False
        cmd = providers.login_command(pid)
        if not cmd:  # only signs in on its own screen, so nothing to wait for here
            providers.open_terminal(p["cli"])
            events.emit("notice", text=f"请在弹出的终端里登录 {p['name']}，然后再发一次", hold=60000)
            self.speaker.say(f"请在弹出的终端里登录 {p['name']}，然后再说一次")
            return False
        events.emit("notice", text=f"{p['name']} 登录已过期，已打开浏览器，请完成登录", hold=LOGIN_TIMEOUT * 1000)
        self.speaker.say(f"{p['name']} 登录过期了，我打开了浏览器，请你登录一下")
        ok = self._sign_in(cmd, pid)
        events.emit("notice", text="")
        if ok:
            self.log(f"[{pid}] logged in, running the command again")
            self.speaker.say("登录好了")
            return True
        return False

    def _sign_in(self, cmd, pid):
        out = tempfile.TemporaryFile(mode="w+")
        self._proc = subprocess.Popen(cmd, cwd=self.workdir, text=True, env=providers.apply_env(pid),
                                      stdin=subprocess.DEVNULL, stdout=out, stderr=out)
        try:
            rc = self._proc.wait(timeout=LOGIN_TIMEOUT)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            rc = self._proc.wait()
        if rc == 0:
            return True
        out.seek(0)
        self.log(f"[{pid}] login failed ({rc}): {out.read().strip()[-300:]}")
        if rc != -15:  # -15 = stopped by voice command
            self.speaker.say("登录没有完成，刚才的指令没有执行")
        return False

    def sign_in_now(self, pid):
        """The connection settings' 登录 button."""
        cmd = providers.login_command(pid)
        if not cmd:
            providers.open_terminal(providers.PROVIDERS[pid]["cli"])
            return
        subprocess.run(cmd, cwd=self.workdir, env=providers.apply_env(pid), stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=LOGIN_TIMEOUT)

    def sign_out(self, pid):
        if pid == "claude":
            subprocess.run(["claude", "auth", "logout"], capture_output=True)
        elif pid == "chatgpt":
            subprocess.run(["codex", "logout"], capture_output=True)
        elif pid == "gemini":
            path = os.path.expanduser("~/.gemini/oauth_creds.json")
            if os.path.exists(path):
                os.remove(path)

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
        sessions.set_native(self.conversation, self.provider, sid)
