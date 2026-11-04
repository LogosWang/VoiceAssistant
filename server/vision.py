"""Camera and screen capture for voice commands.

While the camera (or screen) is on, every voice command gets a snapshot taken the moment
you finish speaking; the image path is appended to the prompt and Claude reads it.
"""
import glob
import os
import re
import shutil
import subprocess
import tempfile
import time

import compat

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP_DIR = os.path.join(compat.state_dir(), "snapshots")
KEEP = 50  # snapshots kept on disk


class Vision:
    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log
        self.cam = None      # ffmpeg: camera -> latest.jpg + raw preview stream
        self.preview = None  # ffplay window fed by ffmpeg
        self.screen = False
        os.makedirs(SNAP_DIR, exist_ok=True)
        self.latest = os.path.join(SNAP_DIR, "camera_latest.jpg")

    @property
    def camera(self):
        # closing the preview window kills ffmpeg (broken pipe), which turns the camera off
        return self.cam is not None and self.cam.poll() is None

    def camera_on(self):
        if self.camera:
            return True
        if os.path.exists(self.latest):
            os.remove(self.latest)
        dev = self.cfg["camera"]
        src = (["-f", "lavfi", "-i", dev[6:]] if dev.startswith("lavfi:")  # test pattern
               else self._dshow_source(dev) if compat.IS_WIN
               else ["-f", "avfoundation", "-framerate", str(self.cfg["camera_fps"]), "-i", f"{dev}:none"])
        errf = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
        self.cam = subprocess.Popen(
            [compat.tool("ffmpeg"), "-loglevel", "error", *src,
             "-filter_complex", "[0:v]split=2[a][b];[a]scale=1280:-2[snap];[b]scale=640:-2,fps=15[prev]",
             "-map", "[snap]", "-r", "2", "-update", "1", "-atomic_writing", "1", "-y", self.latest,
             "-map", "[prev]", "-c:v", "rawvideo", "-pix_fmt", "yuv420p", "-f", "nut", "pipe:1"],
            stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, stderr=errf)
        self.preview = subprocess.Popen(
            [compat.tool("ffplay"), "-loglevel", "error", "-window_title", "Carnot 摄像头", "-"],
            stdin=self.cam.stdout)
        self.cam.stdout.close()
        for _ in range(80):  # first frame within 8 s (permission prompt / camera warm-up)
            if os.path.exists(self.latest):
                self.log("[vision] camera on")
                return True
            if self.cam.poll() is not None:
                break
            time.sleep(0.1)
        errf.seek(0)
        self.log(f"[vision] camera failed: {errf.read().strip()[-300:]}")
        self.camera_off()
        return False

    def _dshow_source(self, dev):
        """Windows: `camera` is a position in the device list ("0") or part of a camera's name."""
        r = subprocess.run([compat.tool("ffmpeg"), "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                           capture_output=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
        names = re.findall(r'"([^"]+)" \(video\)', r.stderr)
        name = next((n for n in names if dev.lower() in n.lower()), None)
        if dev.isdigit():
            name = names[int(dev)] if int(dev) < len(names) else None
        return ["-f", "dshow", "-rtbufsize", "64M", "-i", f"video={name or 'none'}"]

    def _grab_screen(self, path):
        """Windows: screencapture's job, display 1 being the main one as it is there."""
        import mss
        from PIL import Image
        with mss.mss() as grab:
            monitors = grab.monitors
            shot = grab.grab(monitors[min(int(self.cfg["screen_display"]), len(monitors) - 1)])
        image = Image.frombytes("RGB", shot.size, shot.rgb)
        image.thumbnail((1600, 1600))
        image.save(path, "JPEG", quality=85)

    def camera_off(self):
        for p in (self.preview, self.cam):
            if p and p.poll() is None:
                p.terminate()
        self.cam = self.preview = None
        self.log("[vision] camera off")

    def screen_on(self):
        self.screen = True
        self.log("[vision] screen on")

    def screen_off(self):
        self.screen = False
        self.log("[vision] screen off")

    def snapshot(self):
        """Grabs the current camera frame and/or screen. Returns [(label, path)]."""
        ts = time.strftime("%Y%m%d-%H%M%S")
        shots = []
        if self.camera and os.path.exists(self.latest):
            path = os.path.join(SNAP_DIR, f"{ts}_camera.jpg")
            shutil.copy(self.latest, path)
            shots.append(("摄像头", path))
        if self.screen:
            path = os.path.join(SNAP_DIR, f"{ts}_screen.jpg")
            if compat.IS_WIN:
                try:
                    self._grab_screen(path)
                except Exception as e:
                    self.log(f"[vision] {type(e).__name__}: {e}")
            else:
                subprocess.run(["screencapture", "-x", "-t", "jpg", "-D", str(self.cfg["screen_display"]), path])
                if os.path.exists(path):
                    subprocess.run(["sips", "-Z", "1600", path], capture_output=True)
            if os.path.exists(path):
                shots.append(("屏幕", path))
            else:
                self.log("[vision] screenshot failed (Screen Recording permission?)")
        for old in sorted(glob.glob(os.path.join(SNAP_DIR, "2*.jpg")))[:-KEEP]:
            os.remove(old)
        return shots

    def close(self):
        self.camera_off()
