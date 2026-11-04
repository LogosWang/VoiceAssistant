"""What differs between macOS and Windows, so the rest of the service reads the same on both.
Standard library only: the VS Code extension's agent runs on this too."""
import contextlib
import os
import re
import shutil
import subprocess
import sys
import time

IS_WIN = sys.platform == "win32"
HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "bin")  # helpers the Windows installer ships

if IS_WIN:
    import ctypes
    import msvcrt
    from ctypes import wintypes
else:
    import fcntl


# ---------- file locks ----------

def lock(f):
    """Blocks until this open file is locked, across processes."""
    if IS_WIN:
        f.seek(0)
        while True:
            try:  # LK_LOCK gives up after ten seconds; other processes may hold it for longer
                return msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            except OSError:
                pass
    else:
        fcntl.flock(f, fcntl.LOCK_EX)


def unlock(f):
    if IS_WIN:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(f, fcntl.LOCK_UN)


def replace(source, target):
    """os.replace; on Windows it fails while another process has the target open for a moment."""
    for _ in range(40 if IS_WIN else 0):
        try:
            return os.replace(source, target)
        except PermissionError:
            time.sleep(0.05)
    os.replace(source, target)


def utf8_stdio():
    """A Windows pipe defaults to the system's code page; JSON with Chinese in it goes through these.
    Stdin also moves to a handle of its own, with NUL as the standard input children inherit: while a
    thread sits in a read on that pipe, starting any program that inherits it blocks forever."""
    if not IS_WIN:
        return
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")
    with contextlib.suppress(OSError, ValueError):
        private = os.dup(0)
        nul = os.open(os.devnull, os.O_RDONLY)
        os.dup2(nul, 0)
        ctypes.windll.kernel32.SetStdHandle(wintypes.DWORD(-10 & 0xFFFFFFFF), wintypes.HANDLE(msvcrt.get_osfhandle(0)))
        sys.stdin = open(private, encoding="utf-8", errors="replace")


# ---------- running other programs ----------

_SHIM_TARGET = re.compile(r'"%dp0%\\([^"]+)"\s+%\*', re.I)


def argv(cmd):
    """The command as Windows can start it. npm's commands are .cmd files, and through cmd.exe a
    message would be cut at 8191 characters and could be read as shell syntax. So what the shim
    runs is started directly; only a .cmd that can't be read this way (npm itself, whose arguments
    are ours) goes through as it is."""
    if not IS_WIN:
        return cmd
    exe = shutil.which(cmd[0]) or cmd[0]
    if not exe.lower().endswith((".cmd", ".bat")):
        return [exe, *cmd[1:]]
    try:
        with open(exe, encoding="utf-8", errors="replace") as f:
            found = _SHIM_TARGET.findall(f.read())
    except OSError:
        found = []
    if found:
        target = os.path.join(os.path.dirname(exe), found[-1])
        if target.lower().endswith(".exe"):
            return [target, *cmd[1:]]
        beside = os.path.join(os.path.dirname(exe), "node.exe")
        node = beside if os.path.exists(beside) else shutil.which("node")
        if node and os.path.exists(target):
            return [node, target, *cmd[1:]]
    return [exe, *cmd[1:]]


def curl():
    """curl, by full path. On Windows a network that can't reach a certificate's revocation server,
    common behind firewalls, is no reason to refuse the connection."""
    if IS_WIN:
        return [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "curl.exe"), "--ssl-revoke-best-effort"]
    return ["/usr/bin/curl"]


def stop_process(proc):
    """Stops a process and what it started: on Windows terminate() alone would leave the CLI's own
    children running, the very thing a stop command is for."""
    if proc.poll() is not None:
        return
    if IS_WIN:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.terminate()


def kill_tree(proc):
    """Kills a process at once, with what it started on Windows: killing npm's node there leaves the
    native program it launched running."""
    if IS_WIN and proc.poll() is None:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()


def process_stamp(pid):
    """None if no process has this id; otherwise text that changes if the id goes to another process.
    Not os.kill(pid, 0): on Windows signal 0 is CTRL_C_EVENT, which sends a Ctrl+C instead of asking.
    And Windows soon gives an id out again, so being alive isn't enough to know it's the same process."""
    if not IS_WIN:
        try:
            os.kill(pid, 0)
            return ""
        except ProcessLookupError:
            return None
        except PermissionError:
            return "another user's"
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return "another user's" if ctypes.get_last_error() == 5 else None  # ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
            return None
        if not kernel.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return None
        return f"{times[0].dwHighDateTime}.{times[0].dwLowDateTime}"
    finally:
        kernel.CloseHandle(handle)


def wait_for_parent_exit():
    """Returns when the process that started this one has gone."""
    parent = os.getppid()
    if IS_WIN:  # getppid never changes here, so wait on the process itself
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        handle = kernel.OpenProcess(0x00100000, False, parent)  # SYNCHRONIZE
        if handle:
            kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
        return
    while os.getppid() == parent:
        time.sleep(2)


def host_time():
    """The clock audio timestamps are on: time.monotonic() on a Mac, the performance counter here
    (what WASAPI stamps its buffers with; monotonic() only uses it from Python 3.13)."""
    if not IS_WIN:
        return time.monotonic()
    count = ctypes.c_longlong()
    ctypes.windll.kernel32.QueryPerformanceCounter(ctypes.byref(count))
    return count.value / _qpc_frequency()


_frequency = None


def _qpc_frequency():
    global _frequency
    if _frequency is None:
        value = ctypes.c_longlong()
        ctypes.windll.kernel32.QueryPerformanceFrequency(ctypes.byref(value))
        _frequency = value.value
    return _frequency


def tool(name):
    """A helper: the copy the installer ships, else whatever is on PATH."""
    shipped = os.path.join(BIN, name + ".exe")
    return shipped if IS_WIN and os.path.exists(shipped) else name


def python_path():
    """This Python in a form every shell reads the same, for commands an AI is told to run."""
    return sys.executable.replace("\\", "/") if IS_WIN else sys.executable


# ---------- paths ----------

def user_path():
    """Windows: the PATH the user would get in a new terminal, plus where the CLIs usually land."""
    import winreg
    found = []
    for root, key in ((winreg.HKEY_CURRENT_USER, "Environment"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        with contextlib.suppress(OSError):
            with winreg.OpenKey(root, key) as k:
                found += os.path.expandvars(winreg.QueryValueEx(k, "Path")[0]).split(os.pathsep)
    home = os.path.expanduser("~")
    return found + [os.path.expandvars(r"%APPDATA%\npm"), os.path.join(home, ".local", "bin"), BIN,
                    os.path.expandvars(r"%ProgramFiles%\nodejs"), os.path.expandvars(r"%ProgramFiles%\Git\cmd"),
                    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\cmd")]


def add_user_path(folder):
    """Windows: so a new terminal finds what was just installed there."""
    if not IS_WIN:
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
        try:
            value, kind = winreg.QueryValueEx(k, "Path")
        except OSError:
            value, kind = "", winreg.REG_EXPAND_SZ
        parts = [os.path.normcase(os.path.expandvars(p)).rstrip("\\") for p in value.split(os.pathsep)]
        if os.path.normcase(folder).rstrip("\\") not in parts:
            winreg.SetValueEx(k, "Path", 0, kind, f"{value};{folder}".lstrip(";"))


def same_path(path):
    """A path in the form to compare: case doesn't count on Windows."""
    return os.path.normcase(path)


def link_dir(source, target):
    """A link to a folder. On Windows symlinks need developer mode, junctions need nothing."""
    if IS_WIN:
        import _winapi
        _winapi.CreateJunction(source, target)
    else:
        os.symlink(source, target)


def state_dir():
    """Where the service keeps what it changes at run time. Windows: outside the install folder, so
    upgrading and uninstalling only ever touch files the installer put there."""
    if not IS_WIN:
        return HERE
    home = os.path.normpath(os.path.expanduser(os.environ.get("CARNOT_HOME", "~/.carnot")))
    os.makedirs(home, exist_ok=True)
    return home


def config_path():
    default = os.path.join(HERE, "config.toml")
    if not IS_WIN:
        return default
    home = state_dir()
    path = os.path.join(home, "config.toml")
    if not os.path.exists(path) and os.path.exists(default):
        os.makedirs(home, exist_ok=True)
        shutil.copy2(default, path)
    return path if os.path.exists(path) else default


# ---------- sound and notifications ----------

_BEEPS = {"wake": ("Tink.aiff", "Speech On.wav"), "done": ("Pop.aiff", "Speech Sleep.wav"),
          "cancel": ("Basso.aiff", "Speech Misrecognition.wav")}


def beep(kind):
    mac, win = _BEEPS[kind]
    if IS_WIN:
        import winsound
        path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Media", win)
        with contextlib.suppress(RuntimeError):
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    else:
        subprocess.Popen(["afplay", f"/System/Library/Sounds/{mac}"])


def play_command(path):
    """Plays an audio file and exits; stopping the process stops the sound."""
    if IS_WIN:
        return [tool("ffplay"), "-nodisp", "-autoexit", "-nostats", "-loglevel", "quiet", path]
    return ["afplay", path]


def say_command(text, cfg, folder, name):
    """The offline voice: macOS `say`, or Windows SAPI. On Windows the text goes through a file, so
    nothing an AI wrote is ever parsed as PowerShell."""
    if not IS_WIN:
        return ["say", "-v", cfg["say_voice"], "-r", str(cfg["say_rate"]), text]
    path = os.path.join(folder, f"{name}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", os.path.join(HERE, "say_win.ps1"), path]


def notify(title, text):
    """A system notification. Windows: the text goes in through the environment, not the script."""
    if IS_WIN:
        script = ("[void][Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime];"
                  "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(1);"
                  "$t=$x.GetElementsByTagName('text');"
                  "[void]$t.Item(0).AppendChild($x.CreateTextNode($env:CARNOT_NOTE_TITLE+': '+$env:CARNOT_NOTE_TEXT));"
                  "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Carnot')"
                  ".Show([Windows.UI.Notifications.ToastNotification]::new($x))")
        subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                         env=dict(os.environ, CARNOT_NOTE_TITLE=title, CARNOT_NOTE_TEXT=text),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        script = 'on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run'
        subprocess.Popen(["/usr/bin/osascript", "-e", script, title, text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------- keys: Windows Credential Manager ----------

class _Credential(ctypes.Structure if IS_WIN else object):
    if IS_WIN:
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]


_GENERIC = 1
_LOCAL_MACHINE = 2


def _advapi():
    lib = ctypes.WinDLL("advapi32", use_last_error=True)
    lib.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_Credential))]
    lib.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    lib.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    lib.CredFree.argtypes = [ctypes.c_void_p]
    return lib


def cred_get(service, name):
    lib = _advapi()
    found = ctypes.POINTER(_Credential)()
    if not lib.CredReadW(f"{service}/{name}", _GENERIC, 0, ctypes.byref(found)):
        return ""
    try:
        c = found.contents
        return bytes(c.CredentialBlob[:c.CredentialBlobSize]).decode("utf-8")
    finally:
        lib.CredFree(found)


def cred_set(service, name, value):
    data = value.encode("utf-8")
    blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    c = _Credential(Type=_GENERIC, TargetName=f"{service}/{name}", UserName=name, CredentialBlobSize=len(data),
                    CredentialBlob=blob, Persist=_LOCAL_MACHINE)
    if not _advapi().CredWriteW(ctypes.byref(c), 0):
        raise OSError(ctypes.get_last_error(), "CredWrite failed")


def cred_del(service, name):
    _advapi().CredDeleteW(f"{service}/{name}", _GENERIC, 0)
