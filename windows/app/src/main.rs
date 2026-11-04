// Carnot desktop app on Windows, what app/main.swift is on a Mac: a floating character with subtitles,
// on top of everything. It runs server/voice_server.py as a child process and restarts it if it dies;
// the server reports over stdout ("@event {json}" lines, see events.py) and the rest goes to the log.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde_json::{json, Value};
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicIsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::menu::{CheckMenuItem, Menu, MenuBuilder, MenuItem, PredefinedMenuItem, Submenu};
use tauri::tray::{TrayIcon, TrayIconBuilder};
use tauri::webview::PageLoadEvent;
use tauri::{AppHandle, DragDropEvent, Manager, PhysicalPosition, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent, Wry};
use tauri_plugin_autostart::ManagerExt;
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};
use windows::core::{HSTRING, PCWSTR};
use windows::Win32::Foundation::{HWND, POINT, RECT};
use windows::Win32::UI::Shell::ShellExecuteW;
use windows::Win32::UI::WindowsAndMessaging::*;

const WIN: (f64, f64) = (250.0, 266.0);
// The parts that take the mouse, x y w h from the window's top left: the character, the input box
// under it, and the attachments just above the box, if any.
const CHAR_RECT: (f64, f64, f64, f64) = (45.0, 12.0, 160.0, 208.0);
const BAR_RECT: (f64, f64, f64, f64) = (0.0, 220.0, 250.0, 46.0);
const CHIPS_RECT: (f64, f64, f64, f64) = (0.0, 194.0, 250.0, 26.0);
const NO_WINDOW: u32 = 0x0800_0000;

/// How pages reach the app: the Mac bridge's name, so index.html and settings.html stay as they are.
const BRIDGE: &str = r#"
window.webkit = {messageHandlers: {carnot: {postMessage: m => window.__TAURI__.core.invoke("native", {msg: m})}}};
"#;

/// The transparent layer over the character: click for the input box, drag to move, right-click for the
/// menu. Native on a Mac, a page element here, since the web view takes the mouse before the window does.
const HANDLE: &str = r#"
addEventListener("DOMContentLoaded", () => {
  const send = m => window.__TAURI__.core.invoke("native", {msg: m});
  const h = document.createElement("div");
  h.id = "carnot-handle";
  h.style.cssText = "position:fixed;left:45px;top:12px;width:160px;height:208px;z-index:2147483647;background:transparent;-webkit-user-select:none";
  document.body.append(h);
  let start = null;
  h.addEventListener("mousedown", e => { if (e.button === 0) start = [e.screenX, e.screenY]; });
  h.addEventListener("mousemove", e => {
    if (start && Math.hypot(e.screenX - start[0], e.screenY - start[1]) >= 4) { start = null; send({type: "drag"}); }
  });
  h.addEventListener("mouseup", e => { if (start && e.button === 0) { start = null; send({type: "openInput"}); } });
  window.carnotHandleBelow = px => { h.style.height = (208 - px) + "px"; };
});
addEventListener("contextmenu", e => {
  e.preventDefault();
  if (e.target && e.target.id === "carnot-handle") window.__TAURI__.core.invoke("native", {msg: {type: "menu"}});
});
"#;

fn carnot_home() -> PathBuf {
    std::env::var_os("CARNOT_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(std::env::var_os("USERPROFILE").unwrap_or_default()).join(".carnot"))
}

fn log_path() -> PathBuf {
    carnot_home().join("logs").join("carnot.log")
}

fn state_path() -> PathBuf {
    carnot_home().join("app-state.json")
}

fn read_state() -> Value {
    fs::read_to_string(state_path()).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_else(|| json!({}))
}

fn write_state(key: &str, value: Value) {
    let mut state = read_state();
    state[key] = value;
    let _ = fs::create_dir_all(carnot_home());
    let _ = fs::write(state_path(), state.to_string());
}

fn shell_open(target: &str) {
    unsafe {
        ShellExecuteW(None, &HSTRING::from("open"), &HSTRING::from(target), PCWSTR::null(), PCWSTR::null(), SW_SHOWNORMAL);
    }
}

// ---------- the service ----------

/// voice_server.py as a child process, restarted with a growing delay if it exits.
struct Server {
    dir: PathBuf,
    child: Mutex<Option<Child>>,
    stdin: Mutex<Option<ChildStdin>>,
    stopping: AtomicBool,
    log: Mutex<Option<File>>,
}

impl Server {
    fn new(dir: PathBuf) -> Self {
        let path = log_path();
        let _ = fs::create_dir_all(path.parent().unwrap());
        if fs::metadata(&path).map_or(false, |m| m.len() > 5_000_000) {
            let _ = fs::rename(&path, path.with_extension("log.old"));
        }
        let log = OpenOptions::new().create(true).append(true).open(&path).ok();
        Server { dir, child: Mutex::new(None), stdin: Mutex::new(None), stopping: AtomicBool::new(false), log: Mutex::new(log) }
    }

    fn write_log(&self, text: &str) {
        if let Some(f) = self.log.lock().unwrap().as_mut() {
            let _ = f.write_all(text.as_bytes());
        }
    }

    fn python(&self) -> PathBuf {
        let venv = self.dir.join(".venv");
        let scripts = venv.join("Scripts").join("python.exe");
        if scripts.exists() { scripts } else { venv.join("python.exe") }
    }

    fn start(self: &Arc<Self>, app: &AppHandle) {
        self.stopping.store(false, Ordering::SeqCst);
        self.write_log("--- starting server\n");
        let spawned = Command::new(self.python())
            .args(["-u", "voice_server.py", "--ui", "--mic"])
            .current_dir(&self.dir)
            .env("PYTHONUTF8", "1")
            .env("PYTHONIOENCODING", "utf-8")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .creation_flags(NO_WINDOW)
            .spawn();
        match spawned {
            Ok(mut child) => {
                *self.stdin.lock().unwrap() = child.stdin.take();
                for stream in [child.stdout.take().map(|s| Box::new(s) as Box<dyn std::io::Read + Send>),
                               child.stderr.take().map(|s| Box::new(s) as Box<dyn std::io::Read + Send>)].into_iter().flatten() {
                    let (me, app) = (self.clone(), app.clone());
                    std::thread::spawn(move || me.read_lines(stream, &app));
                }
                *self.child.lock().unwrap() = Some(child);
            }
            Err(e) => self.write_log(&format!("cannot start server: {e}\n")),
        }
    }

    fn read_lines(&self, stream: Box<dyn std::io::Read + Send>, app: &AppHandle) {
        let mut reader = BufReader::new(stream);
        let mut raw = Vec::new();
        while reader.read_until(b'\n', &mut raw).map_or(false, |n| n > 0) {
            let line = String::from_utf8_lossy(&raw).trim_end().to_string();
            raw.clear();
            self.write_log(&line);
            self.write_log("\n");
            if let Some(i) = line.find("@event ") { // a log line from another thread may precede it
                on_event(app, &line[i + 7..]);
            }
        }
    }

    /// Watches the process; run on a thread of its own.
    fn supervise(self: Arc<Self>, app: AppHandle) {
        let mut delay = Duration::from_secs(3);
        let mut started = Instant::now();
        loop {
            std::thread::sleep(Duration::from_millis(250));
            let ended = match self.child.lock().unwrap().as_mut() {
                Some(child) => child.try_wait().ok().flatten().map(|status| status.code().unwrap_or(-1)),
                None => Some(-1),
            };
            let Some(status) = ended else { continue };
            *self.child.lock().unwrap() = None;
            if self.stopping.load(Ordering::SeqCst) {
                std::thread::sleep(Duration::from_millis(250));
                continue;
            }
            self.write_log(&format!("--- server exited with status {status}\n"));
            send(&app, r#"{"type":"state","state":"offline"}"#);
            if started.elapsed() > Duration::from_secs(60) {
                delay = Duration::from_secs(3);
            }
            std::thread::sleep(delay);
            delay = (delay * 2).min(Duration::from_secs(60));
            if !self.stopping.load(Ordering::SeqCst) {
                started = Instant::now();
                self.start(&app);
            }
        }
    }

    /// A command from the app (typed text, settings). False if the service isn't running.
    /// These lines aren't logged: API keys pass through here.
    fn send(&self, command: &Value) -> bool {
        match self.stdin.lock().unwrap().as_mut() {
            Some(pipe) => writeln!(pipe, "{command}").and_then(|_| pipe.flush()).is_ok(),
            None => false,
        }
    }

    /// Closing its stdin is how the service is asked to quit; it stops what it started and exits.
    fn stop(&self) {
        self.stopping.store(true, Ordering::SeqCst);
        *self.stdin.lock().unwrap() = None;
        let deadline = Instant::now() + Duration::from_secs(3);
        while Instant::now() < deadline {
            let done = self.child.lock().unwrap().as_mut().map_or(true, |c| !matches!(c.try_wait(), Ok(None)));
            if done {
                break;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        if let Some(child) = self.child.lock().unwrap().as_mut() {
            let _ = child.kill();
        }
        *self.child.lock().unwrap() = None;
    }
}

/// One microphone, one listener: a server left over in a terminal would answer as well.
fn stop_leftover_servers(dir: &Path) {
    let script = "Get-CimInstance Win32_Process -Filter \"Name like 'python%' and CommandLine like '%voice_server.py%'\" | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($env:CARNOT_SERVER, 'OrdinalIgnoreCase') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }";
    let _ = Command::new("powershell")
        .args(["-NoProfile", "-NonInteractive", "-Command", script])
        .env("CARNOT_SERVER", strip_verbatim(dir.to_path_buf()))
        .creation_flags(NO_WINDOW)
        .status();
}

fn find_server_dir(app: &AppHandle) -> PathBuf {
    if let Some(dir) = std::env::var_os("CARNOT_SERVER_DIR") {
        return PathBuf::from(dir);
    }
    if let Ok(resources) = app.path().resource_dir() {
        let installed = resources.join("server");
        if installed.join("voice_server.py").exists() {
            return installed;
        }
    }
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../server").canonicalize().map(strip_verbatim).unwrap_or_default()
}

fn strip_verbatim(path: PathBuf) -> PathBuf {
    let text = path.to_string_lossy();
    PathBuf::from(text.strip_prefix(r"\\?\").unwrap_or(&text).to_string())
}

// ---------- what the app remembers ----------

#[derive(Default)]
struct Ui {
    page_ready: bool,
    pending: Vec<String>,
    settings_ready: bool,
    input_open: bool,
    input_since: Option<Instant>,
    chips_shown: bool,
    sessions: Vec<Value>,
    current_session: String,
    tray: Option<TrayIcon>,
    ignoring: Option<bool>,
    moved_at: Option<Instant>,
    position: Option<(i32, i32)>,
    picks: Vec<Value>, // what each item of the page's menu does
}

struct Carnot {
    server: Arc<Server>,
    ui: Mutex<Ui>,
    last_other_window: AtomicIsize, // where the keyboard goes back to when the input box closes
}

fn state(app: &AppHandle) -> tauri::State<'_, Carnot> {
    app.state::<Carnot>()
}

fn main_window(app: &AppHandle) -> Option<WebviewWindow> {
    app.get_webview_window("main")
}

fn hwnd_of(window: &WebviewWindow) -> Option<HWND> {
    window.hwnd().ok().map(|h| HWND(h.0 as *mut _))
}

fn send(app: &AppHandle, json: &str) {
    let carnot = state(app);
    let mut ui = carnot.ui.lock().unwrap();
    if !ui.page_ready {
        ui.pending.push(json.to_string());
        return;
    }
    let settings_ready = ui.settings_ready;
    drop(ui);
    let script = format!("carnot({json})");
    if let Some(w) = main_window(app) {
        let _ = w.eval(&script);
    }
    if settings_ready {
        if let Some(w) = app.get_webview_window("settings") {
            let _ = w.eval(&script);
        }
    }
}

fn on_event(app: &AppHandle, json: &str) {
    send(app, json);
    let Ok(event) = serde_json::from_str::<Value>(json) else { return };
    match event["type"].as_str() {
        Some("open_settings") => {
            let app = app.clone();
            let _ = app.clone().run_on_main_thread(move || open_settings(&app));
        }
        Some("sessions") => {
            let carnot = state(app);
            let mut ui = carnot.ui.lock().unwrap();
            ui.sessions = event["items"].as_array().cloned().unwrap_or_default();
            ui.current_session = event["current"].as_str().unwrap_or_default().to_string();
            drop(ui);
            refresh_tray(app);
        }
        _ => {}
    }
}

// ---------- the floating window ----------

fn inside(rect: (f64, f64, f64, f64), x: f64, y: f64) -> bool {
    x >= rect.0 && y >= rect.1 && x < rect.0 + rect.2 && y < rect.1 + rect.3
}

/// The window lets clicks through to whatever is behind it, except over the character, and only takes
/// the keyboard for the input box. Twenty times a second, as on a Mac.
fn watch_mouse(app: AppHandle) {
    let mut ticks = 0u32;
    loop {
        std::thread::sleep(Duration::from_millis(50));
        ticks += 1;
        let Some(window) = main_window(&app) else { continue };
        let Some(hwnd) = hwnd_of(&window) else { continue };
        let Ok(origin) = window.outer_position() else { continue };
        let scale = window.scale_factor().unwrap_or(1.0);
        let mut cursor = POINT::default();
        unsafe {
            let _ = GetCursorPos(&mut cursor);
        }
        let x = (cursor.x - origin.x) as f64 / scale;
        let y = (cursor.y - origin.y) as f64 / scale;
        let foreground = unsafe { GetForegroundWindow() };
        let carnot = state(&app);
        if foreground != hwnd && !foreground.is_invalid() {
            carnot.last_other_window.store(foreground.0 as isize, Ordering::Relaxed);
        }

        let mut ui = carnot.ui.lock().unwrap();
        let over_bar = inside(BAR_RECT, x, y);
        let over = inside(CHAR_RECT, x, y) || over_bar || (ui.chips_shown && inside(CHIPS_RECT, x, y));
        if ui.ignoring != Some(!over) {
            ui.ignoring = Some(!over);
            let _ = window.set_ignore_cursor_events(!over);
        }
        let wants = ui.input_open || over_bar; // so the click on the box can give it the keyboard
        apply_styles(hwnd, wants);
        // clicked elsewhere; a moment's grace, because getting the focus isn't instant
        let settled = ui.input_since.map_or(true, |t| t.elapsed() > Duration::from_millis(600));
        let lost = ui.input_open && settled && foreground != hwnd;
        // a move is saved once it has stopped
        let save = ui.moved_at.map_or(false, |t| t.elapsed() > Duration::from_millis(500));
        if save {
            ui.moved_at = None;
        }
        let position = ui.position;
        drop(ui);
        if lost {
            set_input(&app, false, false);
        }
        if save {
            if let Some((x, y)) = position {
                write_state("origin", json!([x, y]));
            }
        }
        if ticks % 100 == 0 {
            let app2 = app.clone();
            let _ = app.run_on_main_thread(move || place_if_off_screen(&app2)); // a display went away
        }
        if ticks % 10 == 0 {
            let app2 = app.clone();
            let _ = app.run_on_main_thread(move || follow_virtual_desktop(&app2));
        }
    }
}

/// Not in Alt-Tab, and takes the focus only when asked. Checked on every tick, because the window library
/// sets the extended style afresh whenever it changes something else, like letting clicks through.
fn apply_styles(hwnd: HWND, wants_keyboard: bool) {
    unsafe {
        let style = GetWindowLongPtrW(hwnd, GWL_EXSTYLE);
        let no_activate = WS_EX_NOACTIVATE.0 as isize;
        let tool = style | WS_EX_TOOLWINDOW.0 as isize;
        let want = if wants_keyboard { tool & !no_activate } else { tool | no_activate };
        if want != style {
            SetWindowLongPtrW(hwnd, GWL_EXSTYLE, want);
        }
    }
}

fn work_area() -> RECT {
    let mut area = RECT::default();
    unsafe {
        let _ = SystemParametersInfoW(SPI_GETWORKAREA, 0, Some(&mut area as *mut _ as *mut _), SYSTEM_PARAMETERS_INFO_UPDATE_FLAGS(0));
    }
    area
}

fn on_a_screen(window: &WebviewWindow, x: i32, y: i32) -> bool {
    let scale = window.scale_factor().unwrap_or(1.0);
    let (cx, cy) = (x + (CHAR_RECT.0 * scale) as i32, y + (CHAR_RECT.1 * scale) as i32);
    let (cw, ch) = ((CHAR_RECT.2 * scale) as i32, (CHAR_RECT.3 * scale) as i32);
    window.available_monitors().unwrap_or_default().iter().any(|m| {
        let (p, s) = (m.position(), m.size());
        let width = (cx + cw).min(p.x + s.width as i32) - cx.max(p.x);
        let height = (cy + ch).min(p.y + s.height as i32) - cy.max(p.y);
        width > 60 && height > 60
    })
}

/// Saved position if any of the character is still on a screen, else the main screen's corner.
fn place(window: &WebviewWindow) {
    let saved = read_state()["origin"].as_array().map(|o| (o[0].as_i64().unwrap_or(0) as i32, o[1].as_i64().unwrap_or(0) as i32));
    if let Some((x, y)) = saved {
        if on_a_screen(window, x, y) {
            let _ = window.set_position(PhysicalPosition::new(x, y));
            return;
        }
    }
    let area = work_area();
    let scale = window.scale_factor().unwrap_or(1.0);
    let x = area.right - ((WIN.0 + 12.0) * scale) as i32;
    let y = area.bottom - (WIN.1 * scale) as i32;
    let _ = window.set_position(PhysicalPosition::new(x, y));
}

fn place_if_off_screen(app: &AppHandle) {
    if let Some(window) = main_window(app) {
        if let Ok(p) = window.outer_position() {
            if !on_a_screen(&window, p.x, p.y) {
                place(&window);
            }
        }
    }
}

/// Windows has no "show on all desktops" for an app's own window. So when the user changes desktop, the
/// window is moved after them, using a throwaway window to learn which desktop that is.
fn follow_virtual_desktop(app: &AppHandle) {
    use windows::Win32::System::Com::{CoCreateInstance, CLSCTX_ALL};
    use windows::Win32::UI::Shell::{IVirtualDesktopManager, VirtualDesktopManager};
    let Some(window) = main_window(app) else { return };
    let Some(hwnd) = hwnd_of(&window) else { return };
    unsafe {
        let Ok(manager) = CoCreateInstance::<_, IVirtualDesktopManager>(&VirtualDesktopManager, None, CLSCTX_ALL) else { return };
        if manager.IsWindowOnCurrentVirtualDesktop(hwnd).map_or(true, |on| on.as_bool()) {
            return;
        }
        let Ok(probe) = CreateWindowExW(WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, &HSTRING::from("STATIC"), &HSTRING::from(""),
                                        WS_POPUP, -32000, -32000, 1, 1, None, None, None, None) else { return };
        let _ = ShowWindow(probe, SW_SHOWNOACTIVATE);
        if let Ok(desktop) = manager.GetWindowDesktopId(probe) {
            if desktop != windows::core::GUID::zeroed() {
                let _ = manager.MoveWindowToDesktop(hwnd, &desktop);
            }
        }
        let _ = DestroyWindow(probe);
    }
}

fn set_input(app: &AppHandle, open: bool, give_back: bool) {
    let carnot = state(app);
    {
        let mut ui = carnot.ui.lock().unwrap();
        ui.input_open = open;
        if open {
            ui.input_since = Some(Instant::now());
        }
    }
    let Some(window) = main_window(app) else { return };
    if open {
        if let Some(hwnd) = hwnd_of(&window) {
            apply_styles(hwnd, true);
        }
        let _ = window.set_focus();
    } else if give_back {
        let other = carnot.last_other_window.load(Ordering::Relaxed);
        if other != 0 {
            unsafe {
                let _ = SetForegroundWindow(HWND(other as *mut _));
            }
        }
    }
    send(app, &json!({"type": "input", "open": open}).to_string());
}

fn attach(app: &AppHandle, paths: Vec<String>) {
    if !paths.is_empty() {
        send(app, &json!({"type": "attached", "paths": paths}).to_string());
    }
}

fn paths_of(picked: Vec<tauri_plugin_dialog::FilePath>) -> Vec<String> {
    picked.into_iter().filter_map(|p| p.into_path().ok()).map(|p| p.to_string_lossy().to_string()).collect()
}

/// Windows' dialogs pick files or a folder, not both, so the button asks which.
fn pick(app: &AppHandle, folder: bool) {
    let app = app.clone();
    std::thread::spawn(move || {
        let mut dialog = app.dialog().file().set_title("附加");
        if let Some(w) = main_window(&app) {
            dialog = dialog.set_parent(&w);
        }
        let picked = if folder {
            dialog.blocking_pick_folders().unwrap_or_default()
        } else {
            dialog.blocking_pick_files().unwrap_or_default()
        };
        attach(&app, paths_of(picked));
    });
}

// ---------- the other windows ----------

fn open_settings(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("settings") {
        state(app).server.send(&json!({"type": "providers"}));
        let _ = w.unminimize();
        let _ = w.set_focus();
        return;
    }
    let handle = app.clone();
    let built = WebviewWindowBuilder::new(app, "settings", WebviewUrl::App("settings.html".into()))
        .title("Carnot 连接设置")
        .inner_size(560.0, 680.0)
        .center()
        .always_on_top(true)
        .initialization_script(BRIDGE)
        .on_page_load(move |_, payload| {
            if payload.event() == PageLoadEvent::Finished {
                state(&handle).ui.lock().unwrap().settings_ready = true;
            }
        })
        .build();
    if let Ok(w) = built {
        let handle = app.clone();
        w.on_window_event(move |event| {
            if matches!(event, WindowEvent::Destroyed) {
                state(&handle).ui.lock().unwrap().settings_ready = false;
            }
        });
    }
}

fn open_settings_at(app: &AppHandle, section: &'static str) {
    open_settings(app);
    let app = app.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(500));
        if let Some(w) = app.get_webview_window("settings") {
            let _ = w.eval(&format!("document.getElementById('{section}').scrollIntoView()"));
        }
    });
}

fn rename_session(app: &AppHandle, id: &str, title: &str) {
    if let Some(w) = app.get_webview_window("rename") {
        let _ = w.close();
    }
    let data = json!({"id": id, "title": title});
    let _ = WebviewWindowBuilder::new(app, "rename", WebviewUrl::App("rename.html".into()))
        .title("重命名这个对话")
        .inner_size(380.0, 130.0)
        .resizable(false)
        .minimizable(false)
        .maximizable(false)
        .center()
        .always_on_top(true)
        .initialization_script(&format!("{BRIDGE}\nwindow.carnotRename = {data};"))
        .build();
}

fn delete_session(app: &AppHandle, id: String, title: &str) {
    let handle = app.clone();
    app.dialog()
        .message("只从列表里移除，之后不能再继续这个对话。")
        .title(format!("删除“{title}”？"))
        .buttons(MessageDialogButtons::OkCancelCustom("删除".into(), "取消".into()))
        .show(move |yes| {
            if yes {
                state(&handle).server.send(&json!({"type": "session_delete", "id": id}));
            }
        });
}

// ---------- menu (tray and right-click on the character) ----------

fn menu_text(text: &str) -> String {
    text.replace('&', "&&")
}

fn build_menu(app: &AppHandle) -> tauri::Result<Menu<Wry>> {
    let carnot = state(app);
    let (sessions, current) = {
        let ui = carnot.ui.lock().unwrap();
        (ui.sessions.clone(), ui.current_session.clone())
    };
    let clients = [("telegram", "Telegram · "), ("feishu", "飞书 · "), ("vscode", "VS Code · "), ("schedule", "定时 · ")];
    let conversations = Submenu::new(app, "会话", true)?;
    conversations.append(&MenuItem::with_id(app, "new", "新对话", true, None::<&str>)?)?;
    if !sessions.is_empty() {
        conversations.append(&PredefinedMenuItem::separator(app)?)?;
    }
    for s in sessions.iter().take(20) {
        let Some(id) = s["id"].as_str() else { continue };
        let title = s["title"].as_str().unwrap_or("对话");
        let client = s["client"].as_str().unwrap_or_default();
        let prefix = clients.iter().find(|c| c.0 == client).map_or("", |c| c.1);
        let mark = if id == current { "● " } else { "" };
        let one = Submenu::new(app, menu_text(&format!("{mark}{prefix}{title}")), true)?;
        one.append(&MenuItem::with_id(app, format!("use:{id}"), "继续这个对话", true, None::<&str>)?)?;
        one.append(&MenuItem::with_id(app, format!("rename:{id}"), "重命名…", true, None::<&str>)?)?;
        one.append(&MenuItem::with_id(app, format!("delete:{id}"), "删除…", true, None::<&str>)?)?;
        conversations.append(&one)?;
    }
    let login = app.autolaunch().is_enabled().unwrap_or(false);
    MenuBuilder::new(app)
        .text("input", "输入文字…")
        .item(&conversations)
        .text("settings", "连接设置…")
        .text("schedules", "定时任务…")
        .text("bots", "远程机器人…")
        .text("toggle", "显示 / 隐藏人物")
        .text("restart", "重启语音服务")
        .item(&CheckMenuItem::with_id(app, "login", "开机自启", true, login, None::<&str>)?)
        .separator()
        .text("config", "打开设置文件")
        .text("log", "打开日志")
        .separator()
        .text("quit", "退出 Carnot")
        .build()
}

/// A menu the page describes: it can't be cut off by the small floating window.
fn build_pick_menu(app: &AppHandle, items: &[Value]) -> tauri::Result<Menu<Wry>> {
    let menu = Menu::new(app)?;
    let mut picks = Vec::new();
    for item in items {
        if item.get("separator").is_some() {
            menu.append(&PredefinedMenuItem::separator(app)?)?;
            continue;
        }
        let title = menu_text(item["title"].as_str().unwrap_or_default());
        let id = format!("pick:{}", picks.len());
        let action = item.get("payload").or_else(|| item.get("action")).cloned().unwrap_or(Value::Null);
        let enabled = !action.is_null();
        picks.push(action);
        if item["on"].as_bool() == Some(true) {
            menu.append(&CheckMenuItem::with_id(app, id, title, enabled, true, None::<&str>)?)?;
        } else {
            menu.append(&MenuItem::with_id(app, id, title, enabled, None::<&str>)?)?;
        }
    }
    state(app).ui.lock().unwrap().picks = picks;
    Ok(menu)
}

fn refresh_tray(app: &AppHandle) {
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        let carnot = state(&handle);
        let tray = carnot.ui.lock().unwrap().tray.clone();
        if let (Some(tray), Ok(menu)) = (tray, build_menu(&handle)) {
            let _ = tray.set_menu(Some(menu));
        }
    });
}

fn session_title(app: &AppHandle, id: &str) -> String {
    let carnot = state(app);
    let ui = carnot.ui.lock().unwrap();
    ui.sessions.iter().find(|s| s["id"].as_str() == Some(id)).and_then(|s| s["title"].as_str()).unwrap_or("对话").to_string()
}

fn on_menu(app: &AppHandle, id: &str) {
    let carnot = state(app);
    match id {
        "input" => set_input(app, true, false),
        "new" => { carnot.server.send(&json!({"type": "session_new"})); }
        "settings" => open_settings(app),
        "schedules" => open_settings_at(app, "schedules"),
        "bots" => open_settings_at(app, "bots"),
        "attach_files" => pick(app, false),
        "attach_folder" => pick(app, true),
        "toggle" => {
            if let Some(w) = main_window(app) {
                let _ = if w.is_visible().unwrap_or(true) { w.hide() } else { w.show() };
            }
        }
        "restart" => {
            let (server, app) = (carnot.server.clone(), app.clone());
            std::thread::spawn(move || {
                server.stop();
                server.start(&app);
            });
        }
        "login" => {
            let auto = app.autolaunch();
            let _ = if auto.is_enabled().unwrap_or(false) { auto.disable() } else { auto.enable() };
            refresh_tray(app);
        }
        "config" => {
            let home = carnot_home().join("config.toml");
            let path = if home.exists() { home } else { carnot.server.dir.join("config.toml") };
            shell_open(&path.to_string_lossy());
        }
        "log" => shell_open(&log_path().to_string_lossy()),
        "quit" => {
            let (server, app) = (carnot.server.clone(), app.clone());
            std::thread::spawn(move || {
                server.stop();
                app.exit(0);
            });
        }
        other => {
            if let Some(pick) = other.strip_prefix("pick:") {
                let action = pick.parse::<usize>().ok().and_then(|i| carnot.ui.lock().unwrap().picks.get(i).cloned());
                match action.as_ref().and_then(|a| a.as_str()) {
                    Some("settings") => open_settings(app),
                    Some("schedules") => open_settings_at(app, "schedules"),
                    _ => {
                        if let Some(payload) = action.filter(|a| a.is_object()) {
                            carnot.server.send(&payload);
                        }
                    }
                }
            } else if let Some(sid) = other.strip_prefix("use:") {
                carnot.server.send(&json!({"type": "session_use", "id": sid}));
            } else if let Some(sid) = other.strip_prefix("rename:") {
                rename_session(app, sid, &session_title(app, sid));
            } else if let Some(sid) = other.strip_prefix("delete:") {
                delete_session(app, sid.to_string(), &session_title(app, sid));
            }
        }
    }
}

fn popup(app: &AppHandle, menu: tauri::Result<Menu<Wry>>) {
    if let (Some(w), Ok(menu)) = (main_window(app), menu) {
        let _ = w.popup_menu(&menu);
    }
}

// ---------- what the pages ask for ----------

// Not on the main thread: there it would run inside WebView2's own callback, where creating a window deadlocks
#[tauri::command(async)]
fn native(app: AppHandle, msg: Value) {
    let carnot = state(&app);
    match msg["type"].as_str().unwrap_or_default() {
        "closeInput" => set_input(&app, false, true),
        "focused" => {
            let mut ui = carnot.ui.lock().unwrap();
            ui.input_open = true;
            ui.input_since = Some(Instant::now());
        }
        "openInput" => set_input(&app, true, false),
        "attach" => {
            let handle = app.clone();
            let _ = app.run_on_main_thread(move || {
                let menu = (|| {
                    MenuBuilder::new(&handle).text("attach_files", "附加文件…").text("attach_folder", "附加文件夹…").build()
                })();
                popup(&handle, menu);
            });
        }
        "chips" => {
            let shown = msg["count"].as_i64().unwrap_or(0) > 0;
            carnot.ui.lock().unwrap().chips_shown = shown;
            if let Some(w) = main_window(&app) {
                let _ = w.eval(&format!("window.carnotHandleBelow&&window.carnotHandleBelow({})", if shown { CHIPS_RECT.3 } else { 0.0 }));
            }
        }
        "settings" => {
            let handle = app.clone();
            let _ = app.run_on_main_thread(move || open_settings(&handle));
        }
        "schedules" => {
            let handle = app.clone();
            let _ = app.run_on_main_thread(move || open_settings_at(&handle, "schedules"));
        }
        "open" => {
            if let Some(url) = msg["url"].as_str().filter(|u| u.starts_with("https://")) {
                shell_open(url);
            }
        }
        "server" => {
            if !carnot.server.send(&msg["payload"]) {
                send(&app, r#"{"type":"notice","text":"语音服务还没启动好，稍等一下再试","hold":5000}"#);
            }
        }
        "drag" => {
            if let Some(w) = main_window(&app) {
                let _ = w.start_dragging();
            }
        }
        "menu" => {
            let handle = app.clone();
            let items = msg["items"].as_array().cloned();
            let _ = app.run_on_main_thread(move || {
                let menu = match items {
                    Some(items) => build_pick_menu(&handle, &items),
                    None => build_menu(&handle), // the right-click on the character
                };
                popup(&handle, menu);
            });
        }
        "closeRename" => {
            if let Some(w) = app.get_webview_window("rename") {
                let _ = w.close();
            }
        }
        _ => {}
    }
}

fn make_main_window(app: &AppHandle) -> tauri::Result<()> {
    let ready = app.clone();
    let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
        .title("Carnot")
        .inner_size(WIN.0, WIN.1)
        .decorations(false)
        .transparent(true)
        .shadow(false)
        .resizable(false)
        .always_on_top(true)
        .skip_taskbar(true)
        .focused(false) // shown without activating: the app you're in stays frontmost
        .initialization_script(BRIDGE)
        .initialization_script(HANDLE)
        .on_page_load(move |_, payload| {
            if payload.event() == PageLoadEvent::Finished {
                let carnot = state(&ready);
                let pending = {
                    let mut ui = carnot.ui.lock().unwrap();
                    ui.page_ready = true;
                    std::mem::take(&mut ui.pending)
                };
                drop(carnot);
                for json in pending {
                    send(&ready, &json);
                }
            }
        })
        .build()?;
    if let Some(hwnd) = hwnd_of(&window) {
        apply_styles(hwnd, false);
    }
    place(&window);
    let handle = app.clone();
    window.on_window_event(move |event| match event {
        WindowEvent::CloseRequested { api, .. } => api.prevent_close(), // Alt-F4 on the character
        WindowEvent::Moved(p) => {
            let carnot = state(&handle);
            let mut ui = carnot.ui.lock().unwrap();
            ui.position = Some((p.x, p.y));
            ui.moved_at = Some(Instant::now());
        }
        WindowEvent::DragDrop(DragDropEvent::Drop { paths, .. }) => {
            attach(&handle, paths.iter().map(|p| p.to_string_lossy().to_string()).collect());
        }
        _ => {}
    });
    Ok(())
}

fn main() {
    let mut builder = tauri::Builder::default();
    // One copy per home folder: a test with a home of its own runs beside the real one and leaves it alone
    if std::env::var_os("CARNOT_HOME").is_none() {
        builder = builder.plugin(tauri_plugin_single_instance::init(|app, _, _| set_input(app, true, false)));
    }
    builder
        .plugin(tauri_plugin_autostart::init(tauri_plugin_autostart::MacosLauncher::LaunchAgent, None))
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![native])
        .setup(|app| {
            let handle = app.handle().clone();
            // For the build: the pages and pictures as they are inside this exe, to compare with the originals
            let args: Vec<String> = std::env::args().collect();
            if let Some(i) = args.iter().position(|a| a == "--dump-assets") {
                let target = PathBuf::from(args.get(i + 1).cloned().unwrap_or_default());
                for name in ["beam", "char", "conrod", "engine", "engine_front", "eyes", "flywheel", "mouth", "pedestal", "piston"] {
                    let file = format!("{name}.webp");
                    let bytes = app.asset_resolver().get(format!("art/{file}")).map(|a| a.bytes().to_vec()).unwrap_or_default();
                    let _ = fs::create_dir_all(&target);
                    let _ = fs::write(target.join(file), bytes);
                }
                std::process::exit(0);
            }
            let server = Arc::new(Server::new(find_server_dir(&handle)));
            app.manage(Carnot { server: server.clone(), ui: Mutex::new(Ui::default()), last_other_window: AtomicIsize::new(0) });
            make_main_window(&handle)?;

            if read_state()["login_set"].as_bool() != Some(true) { // on by default, once; after that it's the user's
                let _ = handle.autolaunch().enable();
                write_state("login_set", json!(true));
            }
            let menu = build_menu(&handle)?;
            let mut tray = TrayIconBuilder::with_id("carnot").tooltip("Carnot").menu(&menu).show_menu_on_left_click(true);
            if let Some(icon) = handle.default_window_icon() {
                tray = tray.icon(icon.clone());
            }
            let tray = tray.build(app)?;
            state(&handle).ui.lock().unwrap().tray = Some(tray);
            app.on_menu_event(|app, event| on_menu(app, event.id().as_ref()));

            let (first, watcher) = (handle.clone(), handle.clone());
            std::thread::spawn(move || watch_mouse(watcher));
            std::thread::spawn(move || {
                let server = state(&first).server.clone();
                stop_leftover_servers(&server.dir);
                server.start(&first);
                server.supervise(first);
            });
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Carnot");
}
