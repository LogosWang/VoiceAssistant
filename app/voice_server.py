"""VoiceInput server: ESP32 mic stream -> wake word -> ASR -> Claude Code -> spoken reply.

    python voice_server.py              # listen for the ESP32
    python voice_server.py --mic        # use the Mac's microphone instead (testing)
    python voice_server.py --wav a.wav  # feed a 16 kHz mono wav file (testing)
    python voice_server.py --dry-run    # print transcripts, don't run Claude
"""
import argparse
import glob
import os
import re
import socket
import subprocess
import threading
import time
import tomllib
import wave

import numpy as np
import sherpa_onnx
from pypinyin import lazy_pinyin

from aec import EchoCanceller
from claude_bridge import ClaudeBridge
from tts import Speaker
from vision import Vision

SR = 16000
HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(HERE, "models")
KWS_DIR = os.path.join(MODELS, "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20")  # zh + en keywords
ASR_DIR = os.path.join(MODELS, "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17")
VAD_MODEL = os.path.join(MODELS, "silero_vad.onnx")
PREROLL = int(0.4 * SR)  # audio kept from just before the wake word fired

STOP_WORDS = ("停止", "取消", "别说了", "闭嘴", "停下", "安静")
NEW_SESSION_WORDS = ("新对话", "新会话", "重新开始")
FOLLOW_UP_END = ("没事了", "不用了", "就这样", "谢谢", "好了", "没了")
# "off" lists are checked first: "别看屏幕" contains "看屏幕"
CAMERA_OFF = ("关闭摄像头", "关掉摄像头", "关摄像头", "关闭相机", "关掉相机")
CAMERA_ON = ("打开摄像头", "开启摄像头", "开摄像头", "打开相机", "开启相机")
SCREEN_OFF = ("停止看屏幕", "别看屏幕", "不用看屏幕", "不要看屏幕", "关闭屏幕共享", "停止屏幕共享", "关闭屏幕录制", "停止屏幕录制", "关掉屏幕录制")
SCREEN_ON = ("看屏幕", "看我的屏幕", "看一下屏幕", "看看屏幕", "打开屏幕共享", "开启屏幕共享", "共享屏幕")
BEEP_WAKE = "/System/Library/Sounds/Tink.aiff"
BEEP_DONE = "/System/Library/Sounds/Pop.aiff"
BEEP_CANCEL = "/System/Library/Sounds/Basso.aiff"


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def beep(path):
    subprocess.Popen(["afplay", path])


def kws_model(part):
    # e.g. encoder-epoch-13-avg-2-chunk-16-left-64.onnx (the fp32 one; int8 is less accurate)
    files = glob.glob(os.path.join(KWS_DIR, f"{part}-epoch-13-avg-2-chunk-16-left-64.onnx"))
    assert files, f"{part} model missing in {KWS_DIR}, run ./download_models.sh"
    return files[0]


def is_cjk(s):
    return any("\u4e00" <= c <= "\u9fff" for c in s)


def strip_wake(text, names):
    """Drop the wake name (or its tail, if the pre-roll cut it) from the start of the transcript."""
    for name in names:
        if not is_cjk(name):
            m = re.match(rf"\W*{re.escape(name)}(?![A-Za-z])", text, re.I)
            if m:
                return text[m.end():].lstrip(" ，,。.!！?？、")
    han = [(i, c) for i, c in enumerate(text) if "一" <= c <= "鿿"]
    for name in filter(is_cjk, names):
        target = lazy_pinyin(name)
        for k in range(len(target), 1, -1):
            if len(han) < k:
                continue
            got = lazy_pinyin("".join(c for _, c in han[:k]))
            same = sum(a == b for a, b in zip(got, target[-k:]))
            if same == k or (k == len(target) >= 3 and same >= k - 1):
                text = text[han[k - 1][0] + 1:]
                return text.lstrip(" ，,。.!！?？、")
    return text.strip()


class DryRunBridge:
    busy = False

    def submit(self, text):
        log(f"[dry-run] would send to Claude: {text}")

    def mute(self): pass
    def stop(self): pass
    def new_session(self): pass
    def wait_idle(self): pass


class Assistant:
    def __init__(self, cfg, dry_run=False, save_dir=None):
        self.cfg = cfg
        self.save_dir = save_dir
        if save_dir:
            os.makedirs(save_dir, exist_ok=True)
        self.label = cfg["wake"]["name"]
        self.names = [self.label] + cfg["wake"]["asr_aliases"]
        self.lock = threading.RLock()
        self.conn = None
        self.state = None
        self.recording = None
        self.history = np.zeros(0, np.float32)
        self.dc = 0.0
        self.resting = False  # rest mode: ignore commands until told to wake up
        self.follow_up_due = False   # reply finished: listen once more without the wake word
        self.skip_follow_up = False  # set by commands that end the conversation, cleared on next wake

        log("loading models...")
        self.kws = sherpa_onnx.KeywordSpotter(
            tokens=os.path.join(KWS_DIR, "tokens.txt"),
            encoder=kws_model("encoder"), decoder=kws_model("decoder"), joiner=kws_model("joiner"),
            keywords_file=self._write_keywords(), num_threads=1,
            max_active_paths=cfg["wake"]["max_active_paths"],
            keywords_score=cfg["wake"]["score"], keywords_threshold=cfg["wake"]["threshold"])
        self.kws_stream = self.kws.create_stream()
        self.asr = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=os.path.join(ASR_DIR, "model.int8.onnx"), tokens=os.path.join(ASR_DIR, "tokens.txt"),
            num_threads=2, use_itn=True, language="auto")
        self.vad_cfg = sherpa_onnx.VadModelConfig()
        self.vad_cfg.silero_vad.model = VAD_MODEL
        self.vad_cfg.silero_vad.min_silence_duration = cfg["record"]["end_silence"]
        self.vad_cfg.silero_vad.min_speech_duration = 0.25
        self.vad_cfg.sample_rate = SR
        # Standby VAD: after each utterance the KWS stream is reset, otherwise decoding state
        # left over from ordinary speech can make it miss the next wake word.
        idle_cfg = sherpa_onnx.VadModelConfig()
        idle_cfg.silero_vad.model = VAD_MODEL
        idle_cfg.silero_vad.min_silence_duration = 1.0
        idle_cfg.sample_rate = SR
        self.idle_vad = sherpa_onnx.VoiceActivityDetector(idle_cfg, buffer_size_in_seconds=30)
        self.idle_pending = np.zeros(0, np.float32)
        self.idle_speech = False

        self.speaker = Speaker(cfg["tts"], self.refresh_state)
        self.vision = Vision(cfg["vision"], log)
        self.aec = None
        if cfg["aec"]["enabled"]:
            try:
                self.aec = EchoCanceller(cfg["aec"], log)
            except Exception as e:
                log(f"[aec] off: {e}")
        self.aec_warned = False
        self.aec_peak_since = None
        self.reply_audio = []
        self.bridge = DryRunBridge() if dry_run else ClaudeBridge(
            cfg["claude"], self.speaker, self.refresh_state, log)
        log(f"ready, wake word: {self.label}")

    def _write_keywords(self):
        """One keyword line per pronunciation: Chinese as characters, English as CMU phones."""
        tokens_txt = os.path.join(KWS_DIR, "tokens.txt")
        vocab = {line.split()[0] for line in open(tokens_txt)}
        lines = []
        for p in self.cfg["wake"]["pronunciations"]:
            toks = (sherpa_onnx.text2token([p], tokens=tokens_txt, tokens_type="ppinyin")[0]
                    if is_cjk(p) else p.upper().split())
            bad = [t for t in toks if t not in vocab]
            assert not bad, f"pronunciation {p!r}: unknown tokens {bad}"
            lines.append(f"{' '.join(toks)} @{self.label}")
        path = os.path.join(MODELS, "keywords.generated.txt")
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        return path

    # ---------- state / LED ----------

    def refresh_state(self):
        with self.lock:
            if self.recording is not None:
                s = "L"
            elif self.speaker.busy:
                s = "S"
            elif self.bridge.busy:
                s = "B"
            else:
                s = "I"
            if s == self.state:
                return
            if s == "I" and self.state in ("S", "B") and self.cfg["record"]["follow_up"] \
                    and not self.skip_follow_up:
                self.follow_up_due = True  # picked up by the audio thread in process()
            self.state = s
            if self.conn:
                try:
                    self.conn.sendall(s.encode())
                except OSError:
                    pass

    # ---------- audio pipeline ----------

    def feed(self, samples, t0=None):
        """Live audio entry point: removes what the Mac is playing, then processes.
        t0: host time the first sample was recorded (None: estimated from arrival)."""
        if self.aec:
            samples = self.aec.process(samples, t0)
            self._check_route()
            if not len(samples):  # waiting for the matching system audio
                return
        self.process(samples)

    def _check_route(self):
        # Carnot is talking but the system audio capture hears nothing
        if self.aec_warned or not self.speaker.busy:
            self.aec_peak_since = None
            return
        now = time.monotonic()
        if self.aec_peak_since is None:
            self.aec_peak_since = now
            self.aec.check()
        elif now - self.aec_peak_since > 3:
            if self.aec.check() < 1e-3:
                log("[aec] system audio capture hears nothing while Carnot speaks, "
                    "echo cancellation isn't working (see README)")
                self.aec_warned = True
            self.aec_peak_since = None

    def process(self, samples):
        samples = samples * self.cfg["record"]["gain"]
        self.dc = 0.95 * self.dc + 0.05 * float(samples.mean())
        samples = samples - self.dc
        if self.save_dir:  # debugging barge-in: keep what the mic heard while Carnot was talking
            if self.speaker.busy:
                self.reply_audio.append(samples)
            elif self.reply_audio:
                self._save(np.concatenate(self.reply_audio), "during_reply")
                self.reply_audio = []
        if self.follow_up_due and self.recording is None:
            self.follow_up_due = False
            if self.aec and self.aec.media_playing():
                log("[follow-up] skipped: something is playing, say the wake word")
            elif not (self.speaker.busy or self.bridge.busy):
                log("[follow-up] listening, no wake word needed")
                self._start_recording(np.zeros(0, np.float32), self.cfg["record"]["follow_up_window"],
                                      follow_up=True)
        if self.recording is not None:
            self._record(samples)
            return
        self.history = np.concatenate([self.history, samples])[-PREROLL:]
        self._idle_vad(samples)
        self.kws_stream.accept_waveform(SR, samples)
        while self.kws.is_ready(self.kws_stream):
            self.kws.decode_stream(self.kws_stream)
            kw = self.kws.get_result(self.kws_stream)
            if kw:
                self.kws.reset_stream(self.kws_stream)
                if self.speaker.busy and not self.cfg["wake"]["barge_in"]:
                    continue
                self._on_wake(kw)
                return

    def _idle_vad(self, samples):
        self.idle_pending = np.concatenate([self.idle_pending, samples])
        win = self.vad_cfg.silero_vad.window_size
        while len(self.idle_pending) >= win:
            self.idle_vad.accept_waveform(self.idle_pending[:win])
            self.idle_pending = self.idle_pending[win:]
            speech = self.idle_vad.is_speech_detected()
            if self.idle_speech and not speech:
                self.kws.reset_stream(self.kws_stream)
            self.idle_speech = speech
        while not self.idle_vad.empty():
            self.idle_vad.pop()

    def _on_wake(self, kw):
        log(f"[wake] {kw}")
        self.follow_up_due = False
        self.bridge.mute()  # otherwise the reply still streaming in would keep talking
        self.speaker.stop()
        self._start_recording(self.history, self.cfg["record"]["start_timeout"])

    def _start_recording(self, preroll, start_timeout, follow_up=False):
        beep(BEEP_WAKE)
        self.skip_follow_up = False
        self.recording = {
            "audio": [preroll], "n": 0, "started": False, "start_timeout": start_timeout,
            "follow_up": follow_up,
            "vad": sherpa_onnx.VoiceActivityDetector(self.vad_cfg, buffer_size_in_seconds=60),
            "pending": np.zeros(0, np.float32),
        }
        self.history = np.zeros(0, np.float32)
        self.refresh_state()

    def _record(self, samples):
        r = self.recording
        r["audio"].append(samples)
        r["n"] += len(samples)
        r["pending"] = np.concatenate([r["pending"], samples])
        win = self.vad_cfg.silero_vad.window_size
        while len(r["pending"]) >= win:
            r["vad"].accept_waveform(r["pending"][:win])
            r["pending"] = r["pending"][win:]
            if r["vad"].is_speech_detected():
                r["started"] = True
            elif r["started"]:
                return self._finish()
        secs = r["n"] / SR
        rc = self.cfg["record"]
        if not r["started"] and secs > r["start_timeout"]:
            log("[wake] no speech, back to standby")
            if not r["follow_up"]:  # a follow-up just ends quietly
                beep(BEEP_CANCEL)
            self._save(np.concatenate(r["audio"]), "nospeech")
            self._end_recording()
        elif secs > rc["max_seconds"]:
            self._finish()

    def _end_recording(self):
        self.recording = None
        self.kws.reset_stream(self.kws_stream)
        self.refresh_state()

    def _save(self, audio, tag):
        """Logs the level of a recording and, with --save-dir, writes it to a wav for inspection."""
        peak = float(np.abs(audio).max()) if len(audio) else 0.0
        rms = float(np.sqrt(np.mean(audio ** 2))) if len(audio) else 0.0
        log(f"[level] {tag}: {len(audio) / SR:.1f}s peak {peak:.3f} rms {rms:.4f}")
        if not self.save_dir:
            return
        path = os.path.join(self.save_dir, f"{time.strftime('%H%M%S')}_{tag}.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())

    def _finish(self):
        audio = np.concatenate(self.recording["audio"])
        self._save(audio, "command")
        self._end_recording()
        beep(BEEP_DONE)
        s = self.asr.create_stream()
        s.accept_waveform(SR, audio)
        self.asr.decode_stream(s)
        raw = s.result.text.strip()
        text = strip_wake(raw, self.names)
        log(f"[asr] {raw!r} -> {text!r}")
        self.handle(text)

    def handle_rest(self, short):
        """TODO(you): voice rest mode, see "动手练习" in README.md.

        `short` is the transcript with punctuation removed, e.g. "休息一下".
        Return True if you handled the command (it won't go to Claude), False otherwise.
        """
        return False

    def handle(self, text):
        short = re.sub(r"[^\w]", "", text)
        if self.handle_rest(short):
            return
        if not short:
            self.speaker.say("没听清，请再说一遍")
        elif self._vision_command(short, text):
            pass
        elif len(short) <= 5 and any(w in short for w in FOLLOW_UP_END):
            self.skip_follow_up = True
            self.speaker.say("好的")
        elif len(short) <= 6 and any(w in short for w in STOP_WORDS):
            self.skip_follow_up = True
            self.bridge.stop()
            self.speaker.stop()
            self.speaker.say("好的，已停止")
        elif len(short) <= 8 and any(w in short for w in NEW_SESSION_WORDS):
            self.bridge.stop()
            self.bridge.new_session()
            self.speaker.say("好的，开始新对话")
        else:
            self._submit(text)

    def _vision_command(self, short, text):
        """Camera/screen on/off. A longer sentence ("打开摄像头看看这是什么") is also sent to Claude."""
        hit = lambda words: any(w in short for w in words)
        ask = len(short) > 8
        if hit(CAMERA_OFF):
            self.vision.camera_off()
            self.speaker.say("摄像头已关闭")
        elif hit(SCREEN_OFF):
            self.vision.screen_off()
            self.speaker.say("好的，不再看屏幕")
        elif hit(CAMERA_ON):
            # opening the camera takes a moment; don't block the audio thread
            threading.Thread(target=self._camera_on, args=(text, ask), daemon=True).start()
        elif hit(SCREEN_ON):
            self.vision.screen_on()
            self._submit(text) if ask else self.speaker.say("好的，我在看你的屏幕")
        else:
            return False
        return True

    def _camera_on(self, text, ask):
        if not self.vision.camera_on():
            self.speaker.say("摄像头打开失败，详情请看终端")
        elif ask:
            self._submit(text)
        else:
            self.speaker.say("摄像头已打开")

    def _submit(self, text):
        """Sends a command to Claude, with a snapshot from the camera/screen if either is on."""
        shots = self.vision.snapshot()
        if shots:
            text += "\n\n" + "\n".join(f"[{label}画面：{path}]" for label, path in shots)
        self.bridge.submit(text)

    # ---------- audio sources ----------

    def serve_esp(self):
        net = self.cfg["network"]
        threading.Thread(target=self._discovery, args=(net["discovery_port"],), daemon=True).start()
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", net["audio_port"]))
        srv.listen(1)
        log(f"waiting for ESP32 on TCP {net['audio_port']}")
        while True:
            conn, addr = srv.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.settimeout(5)  # the ESP32 streams nonstop; 5 s of silence = it's gone
            log(f"ESP32 connected from {addr[0]}")
            with self.lock:
                self.conn, self.state = conn, None
            self.refresh_state()
            if self.aec:
                self.aec.reset_mic()
            leftover = b""
            try:
                while data := conn.recv(4096):
                    data = leftover + data
                    cut = len(data) & ~1
                    leftover = data[cut:]
                    self.feed(np.frombuffer(data[:cut], "<i2").astype(np.float32) / 32768)
            except OSError as e:
                log(f"ESP32 connection lost: {e}")
            with self.lock:
                self.conn = None
            conn.close()
            log("ESP32 disconnected")

    def _discovery(self, port):
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        udp.bind(("0.0.0.0", port))
        while True:
            msg, addr = udp.recvfrom(64)
            if msg == b"VOICEINPUT?":
                udp.sendto(b"VOICEINPUT!", addr)

    def serve_mic(self):
        import queue
        import sounddevice as sd
        want = self.cfg.get("mic", {}).get("device", "")
        dev = next((i for i, d in enumerate(sd.query_devices())
                    if d["max_input_channels"] and want.lower() in d["name"].lower()), None) if want else None
        if want and dev is None:
            log(f"[mic] no input device matching {want!r}, using the system default")
        log(f"using microphone: {sd.query_devices(dev, kind='input')['name']}")
        q = queue.Queue()
        # the callback carries the hardware time of each block, which echo cancellation aligns on
        cb = lambda data, frames, t, status: q.put((data[:, 0].copy(), t.inputBufferAdcTime))
        with sd.InputStream(device=dev, samplerate=SR, channels=1, dtype="float32", blocksize=512, callback=cb):
            while True:
                self.feed(*q.get())

    def serve_wav(self, path):
        with wave.open(path) as w:
            assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (SR, 1, 2), \
                "need 16 kHz mono 16-bit wav"
            audio = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
        audio = np.concatenate([audio, np.zeros(3 * SR, np.float32)])
        for i in range(0, len(audio), 512):
            self.process(audio[i:i + 512])
        self.bridge.wait_idle()
        while self.speaker.busy:
            time.sleep(0.2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.toml"))
    ap.add_argument("--mic", action="store_true", help="use the Mac microphone instead of the ESP32")
    ap.add_argument("--wav", help="feed a 16 kHz mono wav file")
    ap.add_argument("--dry-run", action="store_true", help="don't send commands to Claude")
    ap.add_argument("--save-dir", help="save every recording as a wav here (debugging)")
    args = ap.parse_args()
    with open(args.config, "rb") as f:
        cfg = tomllib.load(f)
    a = Assistant(cfg, args.dry_run, args.save_dir)
    try:
        if args.wav:
            a.serve_wav(args.wav)
        elif args.mic:
            a.serve_mic()
        else:
            a.serve_esp()
    except KeyboardInterrupt:
        a.bridge.stop()
        a.speaker.stop()
    finally:
        a.vision.close()
        if a.aec:
            a.aec.close()


if __name__ == "__main__":
    main()
