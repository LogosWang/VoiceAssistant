"""Echo cancellation: removes whatever the Mac is playing from the mic, before wake word and ASR.

syscap (ScreenCaptureKit) delivers a copy of everything the Mac plays: Carnot's replies, music,
videos, without changing the output device or touching its sound. That copy is the reference for
a linear echo canceller (Speex MDF). Linear on purpose: it subtracts the predicted echo and leaves
your voice alone (WebRTC's AEC3 and Apple's voice processing also squash the voice or duck other
apps' audio).

Alignment uses hardware timestamps: syscap stamps each buffer with its presentation time and the
Mac mic's callback carries the ADC time of its first sample, both on the host clock that
time.monotonic() reads. Each 10 ms mic frame is paired with the reference that was playing at
the moment it was recorded, minus `delay`; the remaining acoustic delay (speaker -> room -> mic)
is learned by the filter. As a safety net the mic is cross-correlated with the reference it was
paired with, and if the echo ever sits outside the filter's reach, `delay` is moved.
The ESP32 stream has no timestamps; its sample times come from arrival times (Clock).
"""
import ctypes
import ctypes.util
import os
import struct
import subprocess
import threading
import time

import numpy as np

SR = 16000
FRAME = SR // 100        # 10 ms
REF_KEEP = 4 * SR        # reference history kept for alignment
MAX_WAIT = 0.3           # a mic frame waits at most this long for its reference, then gets silence
LEAD = 0.05              # where a realignment puts the echo, behind the paired reference (s)
EST_LEN = 3 * SR         # audio used for one delay check
EST_RANGE = int(0.5 * SR)
HERE = os.path.dirname(os.path.abspath(__file__))
SYSCAP = os.path.join(HERE, "syscap", "syscap")


class Clock:
    """Sample times of a stream without timestamps (ESP32 over Wi-Fi), from arrival times."""

    WINDOW = 20.0  # seconds; the least-delayed arrival over the last 1-2 windows

    def __init__(self):
        self.best = self.prev = None
        self.since = None

    def update(self, n_end, now):
        off = now - n_end / SR
        if self.since is None or now - self.since > self.WINDOW:
            self.prev, self.best, self.since = self.best, off, now
        self.best = min(self.best, off)

    @property
    def offset(self):
        return self.best if self.prev is None else min(self.best, self.prev)


class SpeexAEC:
    """Linear echo canceller (Speex MDF): subtracts the predicted echo, leaves the voice alone.
    Its residual echo suppressor then damps what is left, only where the canceller predicts echo
    (noise suppression is kept at -1 dB, i.e. off in effect; it lives in the same stage)."""

    def __init__(self, tail_ms):
        path = ctypes.util.find_library("speexdsp") or next(
            (p for p in ("/opt/homebrew/lib/libspeexdsp.dylib", "/usr/local/lib/libspeexdsp.dylib")
             if os.path.exists(p)), None)  # Apple silicon / Intel Homebrew
        if not path:
            raise RuntimeError("libspeexdsp not found, run: brew install speexdsp")
        lib = self.lib = ctypes.CDLL(path)
        lib.speex_echo_state_init.restype = ctypes.c_void_p
        lib.speex_echo_state_init.argtypes = [ctypes.c_int, ctypes.c_int]
        lib.speex_echo_cancellation.argtypes = [ctypes.c_void_p] + [ctypes.c_void_p] * 3
        lib.speex_echo_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        lib.speex_echo_state_reset.argtypes = [ctypes.c_void_p]
        self.st = lib.speex_echo_state_init(FRAME, SR * tail_ms // 1000)
        rate = ctypes.c_int(SR)
        lib.speex_echo_ctl(self.st, 24, ctypes.byref(rate))  # SPEEX_ECHO_SET_SAMPLING_RATE
        lib.speex_preprocess_state_init.restype = ctypes.c_void_p
        lib.speex_preprocess_state_init.argtypes = [ctypes.c_int, ctypes.c_int]
        lib.speex_preprocess_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        lib.speex_preprocess_run.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.pre = lib.speex_preprocess_state_init(FRAME, SR)
        for req, val in ((0, 1), (18, -1)):  # SET_DENOISE on, SET_NOISE_SUPPRESS -1 dB (AGC etc. are off)
            v = ctypes.c_int(val)
            lib.speex_preprocess_ctl(self.pre, req, ctypes.byref(v))
        lib.speex_preprocess_ctl(self.pre, 24, ctypes.c_void_p(self.st))  # SET_ECHO_STATE

    def __call__(self, mic, ref):
        out = np.empty(FRAME, np.int16)
        self.lib.speex_echo_cancellation(self.st, mic.ctypes.data, ref.ctypes.data, out.ctypes.data)
        self.lib.speex_preprocess_run(self.pre, out.ctypes.data)
        return out

    def reset(self):
        self.lib.speex_echo_state_reset(self.st)


def estimate_lag(mic, ref):
    """Lag (samples) at which mic best matches ref (mic[n] ~ ref[n - lag]), and peak sharpness."""
    n = 1 << int(np.ceil(np.log2(len(mic) + EST_RANGE)))
    spec = np.fft.rfft(mic, n) * np.conj(np.fft.rfft(ref, n))
    cc = np.fft.irfft(spec / (np.abs(spec) + 1e-9), n)  # GCC-PHAT: sharp peak for speech and music
    cc = np.concatenate([cc[-EST_RANGE:], cc[:EST_RANGE + 1]])
    i = int(np.argmax(cc))
    return i - EST_RANGE, float(cc[i] / (np.mean(np.abs(cc)) + 1e-12))


class EchoCanceller:
    def __init__(self, cfg, log, start_capture=True):
        self.log = log
        self.engine = SpeexAEC(cfg.get("tail_ms", 400))
        # echo positions the filter handles well; realigning (which resets it) only when outside
        self.ok_lags = (0.003, cfg.get("tail_ms", 400) / 1000 - 0.15)
        self.lock = threading.Lock()
        self.ref = np.zeros(0, np.float32)    # reference samples
        self.ref_t = np.zeros(0, np.float64)  # and the host time each one played at
        self.ref_peak = 0.0  # loudest reference since the last check(), to spot a dead capture
        self.delay = 0.0     # pair the mic sample recorded at t with the reference from t - delay
        self.pending = []    # mic audio waiting for its reference: (samples, time of the first)
        self.mic_n = 0
        self.mic_clock = Clock()  # only for streams without timestamps
        self.hist_mic, self.hist_ref = [], []  # recent raw mic / paired reference, for checks
        self.last_lag = None
        self.proc = None
        if start_capture:
            self._start()

    # ---------- reference: system audio ----------

    def _start(self):
        if not os.path.exists(SYSCAP):
            src = os.path.join(os.path.dirname(SYSCAP), "main.swift")
            r = subprocess.run(["swiftc", "-O", "-o", SYSCAP, src], capture_output=True, text=True)
            if r.returncode:
                raise RuntimeError(f"cannot build syscap (xcode-select --install?): {r.stderr[-300:]}")
        self.proc = subprocess.Popen([SYSCAP], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        line = self.proc.stderr.readline().decode().strip()  # "capturing" or the error
        if line != "capturing":
            self.proc.kill()
            raise RuntimeError(line or "syscap exited")
        threading.Thread(target=self._read, daemon=True).start()
        self.log("[aec] capturing system audio")

    def _read(self):
        f = self.proc.stdout
        while len(head := f.read(12)) == 12:
            pts, n = struct.unpack("<dI", head)
            self.feed_reference(np.frombuffer(f.read(4 * n), np.float32), pts)
        self.log(f"[aec] system audio capture ended: {self.proc.stderr.read().decode().strip()}")

    def feed_reference(self, x, t0):
        """x: reference samples, the first of which played at host time t0."""
        with self.lock:
            self.ref = np.concatenate([self.ref, x])[-REF_KEEP:]
            self.ref_t = np.concatenate([self.ref_t, t0 + np.arange(len(x)) / SR])[-REF_KEEP:]
            if len(x):
                self.ref_peak = max(self.ref_peak, float(np.abs(x).max()))

    def _ref_frame(self, t, now):
        """Reference that played over [t, t + 10 ms); None = not arrived yet, keep waiting."""
        want = t + np.arange(FRAME) / SR
        with self.lock:
            if len(self.ref_t) < 2 or want[-1] > self.ref_t[-1]:
                return None if now - want[-1] < MAX_WAIT else np.zeros(FRAME, np.float32)
            i = np.clip(np.searchsorted(self.ref_t, want), 1, len(self.ref_t) - 1)
            t0, t1 = self.ref_t[i - 1], self.ref_t[i]
            ok = (want >= t0) & (t1 - t0 < 1.5 / SR)  # inside the history, no gap in the capture
            w = np.clip((want - t0) / np.maximum(t1 - t0, 1e-9), 0, 1)
            frame = (1 - w) * self.ref[i - 1] + w * self.ref[i]
        return np.where(ok, frame, 0).astype(np.float32)

    # ---------- mic ----------

    def process(self, x, t0=None, now=None):
        """x: mic samples as they come in, the first recorded at host time t0 (None: estimate it
        from arrival). Returns echo-free audio; it can lag a little while waiting for reference."""
        now = time.monotonic() if now is None else now
        if t0 is None:
            n_end = self.mic_n + sum(len(p[0]) for p in self.pending) + len(x)
            self.mic_clock.update(n_end, now)
            t0 = (n_end - len(x)) / SR + self.mic_clock.offset
        self.pending.append((x, t0))
        out = []
        while (frame := self._take()) is not None:
            mic, t = frame
            ref = self._ref_frame(t - self.delay, now)
            if ref is None:
                self.pending.insert(0, frame)
                break
            self.mic_n += FRAME
            out.append(self.engine(_pcm(mic), _pcm(ref)).astype(np.float32) / 32768)
            self._track(mic, ref)
        return np.concatenate(out) if out else np.zeros(0, np.float32)

    def _take(self):
        """Removes the next 10 ms of pending mic audio: (samples, time of the first), or None."""
        if sum(len(p[0]) for p in self.pending) < FRAME:
            return None
        parts, need, t = [], FRAME, self.pending[0][1]
        while need:
            x, tx = self.pending[0]
            parts.append(x[:need])
            if len(x) > need:
                self.pending[0] = (x[need:], tx + need / SR)
                need = 0
            else:
                self.pending.pop(0)
                need -= len(x)
        return np.concatenate(parts), t

    def reset_mic(self):
        """The mic stream restarted (ESP32 reconnected): its timeline starts over."""
        self.mic_clock = Clock()
        self.pending.clear()
        self.hist_mic.clear()
        self.hist_ref.clear()
        self.engine.reset()

    def _track(self, mic, ref):
        """Every second, check where the echo sits and realign if it is out of the filter's reach."""
        self.hist_mic.append(mic)
        self.hist_ref.append(ref)
        if len(self.hist_mic) * FRAME < EST_LEN:
            return
        m, r = np.concatenate(self.hist_mic), np.concatenate(self.hist_ref)
        del self.hist_mic[:100], self.hist_ref[:100]  # slide by 1 s
        if np.sqrt(np.mean(r ** 2)) < 3e-3 or np.sqrt(np.mean(m ** 2)) < 1e-4:
            return  # nothing playing (or mic silent): nothing to measure
        lag, sharp = estimate_lag(m, r)
        if sharp < 14:  # unrelated sounds score 6-9, a real echo 17 (one beep) to 150+
            self.last_lag = None
            return
        # two estimates in a row must agree before acting on one
        steady = self.last_lag is not None and abs(lag - self.last_lag) <= SR // 500
        self.last_lag = lag
        if steady and not self.ok_lags[0] <= lag / SR <= self.ok_lags[1]:
            self.delay = float(np.clip(self.delay + lag / SR - LEAD, -MAX_WAIT + 0.05, 1.0))
            self.engine.reset()
            self.hist_mic.clear()
            self.hist_ref.clear()
            self.last_lag = None
            self.log(f"[aec] echo came {lag * 1000 // SR} ms after the reference, realigned "
                     f"(delay {self.delay * 1000:.0f} ms)")

    def media_playing(self):
        """Whether the Mac was playing anything a moment ago."""
        now = time.monotonic()
        with self.lock:
            recent = self.ref[(self.ref_t > now - 0.8) & (self.ref_t < now - 0.1)]
        return len(recent) > 0 and float(np.sqrt(np.mean(recent ** 2))) > 1e-3

    def check(self):
        """Peak of the reference since the last call; 0 while something played = capture broken."""
        with self.lock:
            p, self.ref_peak = self.ref_peak, 0.0
        return p

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def _pcm(x):
    return (np.clip(x, -1, 1) * 32767).astype(np.int16)
