"""Sentence-queued TTS, interruptible. Neural voices via edge-tts, macOS `say` as fallback."""
import os
import queue
import re
import subprocess
import tempfile
import threading


class Speaker:
    """say() queues a sentence; one thread synthesizes sentence N+1 while another plays sentence N."""

    def __init__(self, cfg, on_change=None):
        self.cfg = cfg
        self.on_change = on_change or (lambda: None)
        self._synth_q = queue.Queue()
        self._play_q = queue.Queue()
        self._proc = None
        self._lock = threading.Lock()
        self._pending = 0  # sentences queued, synthesizing or playing
        self._gen = 0      # bumped by stop(); work from an older generation is dropped
        self._tmp = tempfile.mkdtemp(prefix="voiceinput-tts-")
        if cfg["engine"] == "edge":
            import edge_tts
            self._edge = edge_tts
        threading.Thread(target=self._synth_loop, daemon=True).start()
        threading.Thread(target=self._play_loop, daemon=True).start()

    @property
    def busy(self):
        return self._pending > 0

    def say(self, text):
        text = clean_for_speech(text)
        if not text:
            return
        with self._lock:
            self._pending += 1
            self._synth_q.put((self._gen, text))
        self.on_change()

    def stop(self):
        with self._lock:
            self._gen += 1
            for q in (self._synth_q, self._play_q):
                while not q.empty():
                    q.get_nowait()
                    self._pending -= 1
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
        self.on_change()

    def _drop(self):
        with self._lock:
            self._pending -= 1
        self.on_change()

    def _synth_loop(self):
        n = 0
        while True:
            gen, text = self._synth_q.get()
            cmd = ["say", "-v", self.cfg["say_voice"], "-r", str(self.cfg["say_rate"]), text]
            if self.cfg["engine"] == "edge":
                n += 1
                path = os.path.join(self._tmp, f"{n}.mp3")
                try:
                    self._edge.Communicate(text, self.cfg["edge_voice"],
                                           rate=self.cfg["edge_rate"]).save_sync(path)
                    cmd = ["afplay", path]
                except Exception as e:
                    print(f"[tts] edge-tts failed ({e}), falling back to say", flush=True)
            with self._lock:
                if gen == self._gen:
                    self._play_q.put((gen, cmd))
                    continue
            self._drop()

    def _play_loop(self):
        while True:
            gen, cmd = self._play_q.get()
            with self._lock:
                stale = gen != self._gen
                if not stale:
                    self._proc = subprocess.Popen(cmd)
            if not stale:
                self._proc.wait()
            if cmd[0] == "afplay":
                os.remove(cmd[1])
            self._drop()


def clean_for_speech(text):
    text = re.sub(r"https?://\S+", "链接", text)
    text = re.sub(r"[`*#>|_~]+", " ", text)
    text = re.sub(r"^\s*[-•]\s+", "", text)
    if not re.search(r"[^\W_]", text):  # punctuation only, e.g. a closing quote split off a sentence
        return ""
    return text.strip()


class SentenceSplitter:
    """Turns streamed text deltas into speakable sentences; skips fenced code blocks."""

    _END = re.compile(r"(.+?(?:[。！？!?；;]|\.(?=\s)|$))", re.S)

    def __init__(self, emit):
        self.emit = emit
        self.buf = ""
        self.in_code = False

    def feed(self, delta):
        self.buf += delta
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self._line(line)
        # speak complete sentences of the unfinished line early
        if not self.in_code and not self.buf.lstrip().startswith("`"):
            m = re.search(r"^(.*[。！？!?；;])", self.buf, re.S)
            if m:
                self._line(m.group(1))
                self.buf = self.buf[m.end():]

    def flush(self):
        if self.buf:
            self._line(self.buf)
        self.buf = ""
        self.in_code = False

    def _line(self, line):
        if line.lstrip().startswith("```"):
            self.in_code = not self.in_code
            return
        if self.in_code or not line.strip():
            return
        for s in self._END.findall(line):
            if s.strip():
                self.emit(s.strip())
