//! Computer use on Windows, the counterpart of app/computer.swift. The AI's tools (server/computer_mcp.py)
//! reach it over 127.0.0.1, with the port and a token in ~/.carnot/computer.port, one JSON line each way.
//! The app does this rather than the tool itself because it stays running: it holds a takeover for the
//! task, watches for Esc, and remembers which control a number in read_ui meant.
//!
//! Background actions leave the mouse, the keyboard focus and the screen to you: they read and press
//! controls through UI Automation, capture single windows even when covered, and send keys to one
//! program's window. Full control moves the real mouse and types as you would. It needs the mode set to
//! "full", or a takeover you approve in a dialog, and Esc takes it back.

use base64::Engine;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};
use windows::core::{Interface, BOOL, BSTR, HSTRING, PCWSTR, PWSTR};
use windows::Win32::Foundation::*;
use windows::Win32::Graphics::Dwm::*;
use windows::Win32::Graphics::Gdi::*;
use windows::Win32::Security::Cryptography::*;
use windows::Win32::Storage::Xps::{PrintWindow, PRINT_WINDOW_FLAGS};
use windows::Win32::System::Com::*;
use windows::Win32::System::LibraryLoader::{GetModuleHandleW, GetProcAddress};
use windows::Win32::System::Threading::*;
use windows::Win32::UI::Accessibility::*;
use windows::Win32::UI::HiDpi::*;
use windows::Win32::UI::Input::Ime::ImmGetDefaultIMEWnd;
use windows::Win32::UI::Input::KeyboardAndMouse::*;
use windows::Win32::UI::WindowsAndMessaging::*;

/// On every input we send, so the Esc watcher can tell ours from yours
const MARK: usize = 0x0CA4_0707;
const FULL_OPS: &[&str] = &["screen", "click", "move", "drag", "scroll", "type", "key", "activate"];
const EM_REPLACESEL: u32 = 0x00C2;

struct Fail(String);
type R<T> = Result<T, Fail>;
fn fail<T>(text: impl Into<String>) -> R<T> {
    Err(Fail(text.into()))
}
impl From<windows::core::Error> for Fail {
    fn from(e: windows::core::Error) -> Self {
        Fail(format!("系统调用失败：{}", e.message()))
    }
}

/// A control from read_ui. UI Automation's objects are free-threaded once COM is up in the
/// multithreaded apartment, which every connection's thread joins.
#[derive(Clone)]
struct Elem(IUIAutomationElement);
unsafe impl Send for Elem {}
unsafe impl Sync for Elem {}

pub struct Computer {
    home: PathBuf,
    notify: Box<dyn Fn(String) + Send + Sync>,
    elements: Mutex<HashMap<u32, Vec<Elem>>>,
    grant: Mutex<Option<(String, Instant, u32)>>, // token, last used, the task's tool process that holds it
    hook_thread: Mutex<Option<u32>>,
    screen_map: Mutex<(f64, f64, f64)>, // the last screenshot: its pixels -> screen pixels
}

static CU: OnceLock<Arc<Computer>> = OnceLock::new();

pub fn start(home: PathBuf, notify: Box<dyn Fn(String) + Send + Sync>) {
    unsafe {
        let _ = SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2); // screen pixels everywhere
    }
    let cu = Arc::new(Computer {
        home: home.clone(),
        notify,
        elements: Mutex::new(HashMap::new()),
        grant: Mutex::new(None),
        hook_thread: Mutex::new(None),
        screen_map: Mutex::new((0.0, 0.0, 1.0)),
    });
    if CU.set(cu.clone()).is_err() {
        return;
    }
    let Ok(listener) = TcpListener::bind(("127.0.0.1", 0)) else { return };
    let Ok(addr) = listener.local_addr() else { return };
    let token = random_token();
    let _ = std::fs::create_dir_all(&home);
    if std::fs::write(home.join("computer.port"), format!("{} {token}\n", addr.port())).is_err() {
        return;
    }
    std::thread::spawn(move || {
        for c in listener.incoming().flatten() {
            let (cu, token) = (cu.clone(), token.clone());
            std::thread::spawn(move || cu.serve(c, &token));
        }
    });
}

fn random_token() -> String {
    let mut b = [0u8; 16];
    unsafe {
        let _ = BCryptGenRandom(None, &mut b, BCRYPT_USE_SYSTEM_PREFERRED_RNG);
    }
    b.iter().map(|x| format!("{x:02x}")).collect()
}

fn ok(text: impl Into<String>) -> Value {
    json!({"ok": true, "text": text.into()})
}

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(Some(0)).collect()
}

fn bstr(r: windows::core::Result<BSTR>) -> String {
    r.map(|b| b.to_string()).unwrap_or_default()
}

fn cut(s: &str) -> String {
    let one = s.replace(['\n', '\r'], " ");
    if one.chars().count() > 80 { one.chars().take(80).collect::<String>() + "…" } else { one }
}

impl Computer {
    // ---------- requests ----------

    fn serve(&self, c: TcpStream, token: &str) {
        let _ = c.set_read_timeout(Some(Duration::from_secs(30)));
        let mut line = String::new();
        if BufReader::new(&c).read_line(&mut line).is_err() {
            return;
        }
        let reply = match serde_json::from_str::<Value>(&line) {
            Ok(req) if req["token"].as_str() == Some(token) => {
                unsafe {
                    let _ = CoInitializeEx(None, COINIT_MULTITHREADED);
                }
                match self.handle(&req) {
                    Ok(v) => v,
                    Err(Fail(t)) => json!({"ok": false, "error": t}),
                }
            }
            Ok(_) => json!({"ok": false, "error": "bad token"}),
            Err(_) => json!({"ok": false, "error": "bad request"}),
        };
        let mut out = reply.to_string();
        out.push('\n');
        let mut c = c;
        let _ = c.write_all(out.as_bytes());
    }

    fn mode(&self) -> String {
        std::fs::read_to_string(self.home.join("computer.json"))
            .ok()
            .and_then(|s| serde_json::from_str::<Value>(&s).ok())
            .and_then(|v| v["mode"].as_str().map(String::from))
            .unwrap_or_else(|| "off".into())
    }

    fn handle(&self, r: &Value) -> R<Value> {
        let op = r["op"].as_str().unwrap_or_default();
        let m = self.mode();
        if op == "status" {
            let held = self.grant.lock().unwrap().is_some();
            return Ok(ok(format!("模式 {m}；{}", if held { "接管中" } else { "没有接管" })));
        }
        if m == "off" {
            return fail("用户已经关掉了计算机使用");
        }
        if op == "grant" {
            return self.request_grant(r["reason"].as_str().unwrap_or_default(), m == "full", r["pid"].as_u64().unwrap_or(0) as u32);
        }
        if FULL_OPS.contains(&op) {
            self.check_grant(r["grant"].as_str(), m == "full")?;
        }
        match op {
            "apps" => Ok(ok(apps())),
            "windows" => Ok(ok(windows_text(r["app"].as_str()))),
            "capture" => capture_window(r),
            "ui" => Ok(ok(self.ui(r)?)),
            "press" => Ok(ok(self.press(r)?)),
            "set" => Ok(ok(self.set_text(r)?)),
            "menu" => Ok(ok(menu(r)?)),
            "app_keys" => Ok(ok(app_keys(r)?)),
            "wclick" => Ok(ok(window_click(r)?)),
            "screen" => self.screen(r),
            "click" | "move" | "drag" | "scroll" => Ok(ok(self.mouse(op, r)?)),
            "type" => Ok(ok(type_text(r["text"].as_str().unwrap_or_default())?)),
            "key" => {
                let keys = r["keys"].as_str().unwrap_or_default();
                send_combo(keys)?;
                Ok(ok(format!("已按下 {keys}")))
            }
            "activate" => Ok(ok(activate(r["app"].as_str().unwrap_or_default())?)),
            _ => fail(format!("不认识的操作 {op}")),
        }
    }

    // ---------- full control: the takeover and Esc ----------

    fn check_grant(&self, token: Option<&str>, full: bool) -> R<()> {
        let expired = {
            let g = self.grant.lock().unwrap();
            g.as_ref().is_some_and(|(_, used, _)| used.elapsed() > Duration::from_secs(600))
        };
        if expired {
            self.end_grant("十分钟没有操作，已收回鼠标键盘");
        }
        let mut g = self.grant.lock().unwrap();
        match g.as_mut() {
            Some((t, used, _)) if Some(t.as_str()) == token => {
                *used = Instant::now();
                Ok(())
            }
            _ => fail(if full {
                "还没有接管：先调用 request_full_control"
            } else {
                "这是接管鼠标键盘的操作，后台模式下要先调用 request_full_control 说明原因，用户同意后才能用"
            }),
        }
    }

    /// A takeover is for the task that asked: `owner` is that task's tool process, and when it exits,
    /// because the task ended or was stopped, the mouse and keyboard go back. Another task asks afresh.
    fn request_grant(&self, reason: &str, full: bool, owner: u32) -> R<Value> {
        let held_by_other = {
            let mut g = self.grant.lock().unwrap();
            match g.as_mut() {
                Some((t, used, who)) if *who == owner && owner != 0 => {
                    *used = Instant::now();
                    return Ok(json!({"ok": true, "text": "已经在接管中", "grant": t.clone()}));
                }
                held => held.is_some(),
            }
        };
        if held_by_other {
            self.end_grant("上一个任务的接管已收回");
        }
        if !full && !ask(reason) {
            return fail("用户没有同意接管鼠标键盘。换个后台办法，或者在对话里问用户");
        }
        let token = random_token();
        *self.grant.lock().unwrap() = Some((token.clone(), Instant::now(), owner));
        watch_esc();
        watch_owner(owner, token.clone());
        self.say("Carnot 正在操作鼠标和键盘 · 按 Esc 收回", 600_000);
        Ok(json!({"ok": true, "text": "已获准接管鼠标键盘。用户按 Esc 会收回，这次任务结束时也会交还。", "grant": token}))
    }

    fn end_grant(&self, why: &str) {
        if self.grant.lock().unwrap().take().is_none() {
            return;
        }
        if let Some(tid) = self.hook_thread.lock().unwrap().take() {
            unsafe {
                let _ = PostThreadMessageW(tid, WM_QUIT, WPARAM(0), LPARAM(0));
            }
        }
        self.say(why, 4000);
    }

    fn say(&self, text: &str, hold: u64) {
        (self.notify)(json!({"type": "notice", "text": text, "hold": hold}).to_string());
    }

    // ---------- UI Automation ----------

    fn ui(&self, r: &Value) -> R<String> {
        let (pid, app) = find_app(r["app"].as_str().unwrap_or_default())?;
        let w = target_window(pid, r)?;
        let a = automation()?;
        let root = unsafe { a.ElementFromHandle(w.hwnd) }?;
        let walker = unsafe { a.ControlViewWalker() }?;
        let query = r["query"].as_str().unwrap_or_default().to_lowercase();
        let max_depth = r["depth"].as_i64().unwrap_or(30) as usize;
        let mut walk = Walk { found: vec![], lines: vec![], skipped: 0, query, max_depth, origin: (w.rect.left, w.rect.top) };
        walk.visit(&walker, &root, 0, 0);
        let title = bstr(unsafe { root.CurrentName() });
        let (found, lines, skipped) = (walk.found, walk.lines, walk.skipped);
        let empty = found.is_empty();
        self.elements.lock().unwrap().insert(pid, found);
        if lines.is_empty() {
            return Ok(if walk.query.is_empty() || empty {
                "这个窗口没有可读的控件（自绘界面）。后台做不到的话，可以 capture_window 看画面后试 click_in_window，或者 request_full_control".into()
            } else {
                format!("没有包含“{}”的控件", walk.query)
            });
        }
        let mut out = format!("{app} 窗口“{title}”的控件（坐标是相对窗口左上角的像素）：\n{}", lines.join("\n"));
        if skipped > 0 {
            out += &format!("\n…还有 {skipped} 个没列出，用 query 筛选");
        }
        Ok(out)
    }

    fn element(&self, r: &Value) -> R<IUIAutomationElement> {
        let (pid, _) = find_app(r["app"].as_str().unwrap_or_default())?;
        let n = r["element"].as_u64().or_else(|| r["element"].as_str().and_then(|s| s.parse().ok())).unwrap_or(0) as usize;
        let list = self.elements.lock().unwrap();
        match list.get(&pid).and_then(|l| n.checked_sub(1).and_then(|i| l.get(i))) {
            Some(e) => Ok(e.0.clone()),
            None => fail(format!("没有编号 {n} 的控件，先用 read_ui 读一遍这个程序")),
        }
    }

    fn press(&self, r: &Value) -> R<String> {
        let e = self.element(r)?;
        let wanted = r["action"].as_str().unwrap_or_default().to_lowercase().trim_start_matches("ax").to_string();
        let order: Vec<&str> = if wanted.is_empty() { vec!["invoke", "toggle", "select", "expand", "default"] } else { vec![wanted.as_str()] };
        unsafe {
            for act in &order {
                let done = match *act {
                    "invoke" | "press" => e.GetCurrentPatternAs::<IUIAutomationInvokePattern>(UIA_InvokePatternId).map(|p| p.Invoke()),
                    "toggle" => e.GetCurrentPatternAs::<IUIAutomationTogglePattern>(UIA_TogglePatternId).map(|p| p.Toggle()),
                    "select" | "pick" => e.GetCurrentPatternAs::<IUIAutomationSelectionItemPattern>(UIA_SelectionItemPatternId).map(|p| p.Select()),
                    "expand" | "collapse" | "showmenu" => e
                        .GetCurrentPatternAs::<IUIAutomationExpandCollapsePattern>(UIA_ExpandCollapsePatternId)
                        .map(|p| {
                            let open = p.CurrentExpandCollapseState().map(|s| s != ExpandCollapseState_Collapsed).unwrap_or(false);
                            if *act == "collapse" || (*act != "expand" && open) { p.Collapse() } else { p.Expand() }
                        }),
                    "default" | "confirm" | "open" => e
                        .GetCurrentPatternAs::<IUIAutomationLegacyIAccessiblePattern>(UIA_LegacyIAccessiblePatternId)
                        .map(|p| p.DoDefaultAction()),
                    "focus" => Ok(e.SetFocus()),
                    other => return fail(format!("不认识的动作 {other}，可用 invoke、toggle、select、expand、collapse、default")),
                };
                match done {
                    Ok(Ok(())) => return Ok(format!("已执行 {act}。用 read_ui 或 capture_window 核对结果")),
                    Ok(Err(err)) => return fail(format!("执行 {act} 失败（{}）。可以换个控件，或者 request_full_control 后直接点", err.message())),
                    Err(_) => continue, // doesn't have that pattern
                }
            }
        }
        fail("这个控件不能按（不支持 invoke、toggle、select、expand 或默认动作）")
    }

    fn set_text(&self, r: &Value) -> R<String> {
        let e = self.element(r)?;
        let text = r["text"].as_str().unwrap_or_default();
        unsafe {
            if e.CurrentIsPassword().map(|b| b.as_bool()).unwrap_or(false) {
                return fail("不能往密码框里填东西，请用户自己输入");
            }
            if let Ok(p) = e.GetCurrentPatternAs::<IUIAutomationValuePattern>(UIA_ValuePatternId) {
                if let Err(err) = p.SetValue(&BSTR::from(text)) {
                    return fail(format!("这个控件不让直接设文字（{}）。可以先 press 它，再用 app_keys 打字", err.message()));
                }
                let now = bstr(p.CurrentValue());
                return Ok(if now == text { "已填入".into() } else { format!("已设置，但读回来的是“{}”，请核对", cut(&now)) });
            }
            if let Ok(p) = e.GetCurrentPatternAs::<IUIAutomationLegacyIAccessiblePattern>(UIA_LegacyIAccessiblePatternId) {
                if p.SetValue(&HSTRING::from(text)).is_ok() {
                    return Ok("已设置，请用 read_ui 核对".into());
                }
            }
        }
        fail("这个控件不让直接设文字。可以先 press 它，再用 app_keys 打字")
    }

    // ---------- screen ----------

    fn screen(&self, r: &Value) -> R<Value> {
        let index = r["display"].as_u64().unwrap_or(0) as usize;
        let ds = displays();
        let Some(rect) = ds.get(index).copied() else { return fail(format!("没有第 {index} 块屏幕，一共 {} 块", ds.len())) };
        // Carnot's own windows stay out of the picture
        let own = own_windows();
        unsafe {
            for h in &own {
                let _ = SetWindowDisplayAffinity(*h, WDA_EXCLUDEFROMCAPTURE);
            }
        }
        std::thread::sleep(Duration::from_millis(120));
        let shot = grab_screen(rect);
        unsafe {
            for h in &own {
                let _ = SetWindowDisplayAffinity(*h, WDA_NONE);
            }
        }
        let (px, w, h) = shot?;
        let scale = (1440.0 / w as f64).min(1.0);
        *self.screen_map.lock().unwrap() = (rect.left as f64, rect.top as f64, scale);
        let (image, iw, ih) = jpeg(px, w, h, scale)?;
        Ok(json!({"ok": true, "image": image, "mime": "image/jpeg",
                  "text": format!("屏幕 {index}：{iw}×{ih} 像素。click、drag、scroll 的坐标都用这张图的像素。")}))
    }

    fn point(&self, r: &Value, kx: &str, ky: &str) -> (i32, i32) {
        let (ox, oy, s) = *self.screen_map.lock().unwrap();
        ((ox + r[kx].as_f64().unwrap_or(0.0) / s).round() as i32, (oy + r[ky].as_f64().unwrap_or(0.0) / s).round() as i32)
    }

    fn mouse(&self, op: &str, r: &Value) -> R<String> {
        let (x, y) = self.point(r, "x", "y");
        mouse_at(x, y, MOUSEEVENTF_MOVE, 0);
        std::thread::sleep(Duration::from_millis(30));
        match op {
            "click" => {
                let right = r["button"].as_str() == Some("right");
                let (down, up) = if right { (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP) } else { (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP) };
                for _ in 0..if r["double"].as_bool() == Some(true) { 2 } else { 1 } {
                    mouse_at(x, y, down, 0);
                    std::thread::sleep(Duration::from_millis(30));
                    mouse_at(x, y, up, 0);
                    std::thread::sleep(Duration::from_millis(30));
                }
                Ok("已点击".into())
            }
            "drag" => {
                let (tx, ty) = self.point(r, "to_x", "to_y");
                mouse_at(x, y, MOUSEEVENTF_LEFTDOWN, 0);
                for i in 1..=12 {
                    let f = i as f64 / 12.0;
                    mouse_at(x + ((tx - x) as f64 * f) as i32, y + ((ty - y) as f64 * f) as i32, MOUSEEVENTF_MOVE, 0);
                    std::thread::sleep(Duration::from_millis(16));
                }
                mouse_at(tx, ty, MOUSEEVENTF_LEFTUP, 0);
                Ok("已拖动".into())
            }
            "scroll" => {
                let down = r["down"].as_i64().unwrap_or(0) as i32;
                let right = r["right"].as_i64().unwrap_or(0) as i32;
                if down != 0 {
                    mouse_at(x, y, MOUSEEVENTF_WHEEL, (-down * 120) as u32);
                }
                if right != 0 {
                    mouse_at(x, y, MOUSEEVENTF_HWHEEL, (right * 120) as u32);
                }
                Ok("已滚动".into())
            }
            _ => Ok("已移动鼠标".into()),
        }
    }
}

// ---------- the end of a takeover: the task ends, or Esc ----------

fn watch_owner(pid: u32, token: String) {
    if pid == 0 {
        return; // a tool too old to say who it is: ten idle minutes, or Esc
    }
    std::thread::spawn(move || unsafe {
        let Ok(h) = OpenProcess(PROCESS_SYNCHRONIZE, false, pid) else { return };
        WaitForSingleObject(h, INFINITE);
        let _ = CloseHandle(h);
        let Some(cu) = CU.get() else { return };
        let still = cu.grant.lock().unwrap().as_ref().is_some_and(|(t, _, _)| *t == token);
        if still {
            cu.end_grant("任务结束，已交还鼠标键盘");
        }
    });
}

// ---------- the Esc watcher, while a takeover lasts ----------

fn watch_esc() {
    std::thread::spawn(|| unsafe {
        let Some(cu) = CU.get() else { return };
        let module = GetModuleHandleW(None).ok();
        let Ok(hook) = SetWindowsHookExW(WH_KEYBOARD_LL, Some(esc_proc), module.map(|m| m.into()), 0) else { return };
        *cu.hook_thread.lock().unwrap() = Some(GetCurrentThreadId());
        if cu.grant.lock().unwrap().is_some() {
            let mut msg = MSG::default();
            while GetMessageW(&mut msg, None, 0, 0).as_bool() {}
        }
        let _ = UnhookWindowsHookEx(hook);
    });
}

unsafe extern "system" fn esc_proc(code: i32, w: WPARAM, l: LPARAM) -> LRESULT {
    if code >= 0 && (w.0 as u32 == WM_KEYDOWN || w.0 as u32 == WM_SYSKEYDOWN) {
        let k = &*(l.0 as *const KBDLLHOOKSTRUCT);
        if k.vkCode == VK_ESCAPE.0 as u32 && k.dwExtraInfo != MARK {
            if let Some(cu) = CU.get() {
                let cu = cu.clone();
                std::thread::spawn(move || cu.end_grant("已收回鼠标键盘控制")); // never hold up your keyboard here
            }
        }
    }
    CallNextHookEx(None, code, w, l)
}

/// Yes or no, gone by itself after two minutes (which counts as no).
fn ask(reason: &str) -> bool {
    let text = format!(
        "允许 Carnot 接管鼠标和键盘？\n\n{}\n\n接管期间 Carnot 会移动鼠标、点击和打字，随时按 Esc 收回。\n\n“是”：允许这次任务　“否”：拒绝",
        if reason.is_empty() { "后台方式做不到这一步。".to_string() } else { format!("原因：{reason}") }
    );
    let (text, caption) = (wide(&text), wide("Carnot"));
    let style = MB_YESNO | MB_ICONQUESTION | MB_TOPMOST | MB_SETFOREGROUND | MB_DEFBUTTON2;
    unsafe {
        type Timed = unsafe extern "system" fn(HWND, PCWSTR, PCWSTR, u32, u16, u32) -> i32;
        let user32 = GetModuleHandleW(&HSTRING::from("user32.dll")).ok();
        let timed = user32.and_then(|m| GetProcAddress(m, windows::core::s!("MessageBoxTimeoutW")));
        let answer = match timed {
            Some(f) => std::mem::transmute::<_, Timed>(f)(HWND::default(), PCWSTR(text.as_ptr()), PCWSTR(caption.as_ptr()), style.0, 0, 120_000),
            None => MessageBoxW(None, PCWSTR(text.as_ptr()), PCWSTR(caption.as_ptr()), style).0,
        };
        answer == IDYES.0
    }
}

// ---------- programs and windows ----------

#[derive(Clone)]
struct Win {
    hwnd: HWND,
    pid: u32,
    app: String,
    title: String,
    rect: RECT, // what you see of it, in screen pixels
    minimized: bool,
    other_desktop: bool,
}

fn window_text(h: HWND) -> String {
    let mut buf = [0u16; 512];
    let n = unsafe { GetWindowTextW(h, &mut buf) };
    String::from_utf16_lossy(&buf[..n.max(0) as usize])
}

fn class_name(h: HWND) -> String {
    let mut buf = [0u16; 128];
    let n = unsafe { GetClassNameW(h, &mut buf) };
    String::from_utf16_lossy(&buf[..n.max(0) as usize])
}

fn frame(h: HWND) -> RECT {
    let mut r = RECT::default();
    unsafe {
        if DwmGetWindowAttribute(h, DWMWA_EXTENDED_FRAME_BOUNDS, &mut r as *mut _ as *mut _, std::mem::size_of::<RECT>() as u32).is_err() {
            let _ = GetWindowRect(h, &mut r);
        }
    }
    r
}

fn process_name(pid: u32) -> String {
    unsafe {
        let Ok(h) = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid) else { return format!("pid {pid}") };
        let mut buf = [0u16; 1024];
        let mut len = buf.len() as u32;
        let got = QueryFullProcessImageNameW(h, PROCESS_NAME_WIN32, PWSTR(buf.as_mut_ptr()), &mut len);
        let _ = CloseHandle(h);
        if got.is_err() {
            return format!("pid {pid}");
        }
        let path = String::from_utf16_lossy(&buf[..len as usize]);
        let file = path.rsplit('\\').next().unwrap_or(&path);
        file.strip_suffix(".exe").or_else(|| file.strip_suffix(".EXE")).unwrap_or(file).to_string()
    }
}

/// Every window a person would call a program's window, front to back.
fn window_list() -> Vec<Win> {
    unsafe extern "system" fn each(h: HWND, l: LPARAM) -> BOOL {
        (*(l.0 as *mut Vec<HWND>)).push(h);
        TRUE
    }
    let mut all: Vec<HWND> = Vec::new();
    unsafe {
        let _ = EnumWindows(Some(each), LPARAM(&mut all as *mut _ as isize));
    }
    let me = std::process::id();
    let mut names: HashMap<u32, String> = HashMap::new();
    all.into_iter()
        .filter_map(|h| unsafe {
            if !IsWindowVisible(h).as_bool() {
                return None;
            }
            let ex = GetWindowLongPtrW(h, GWL_EXSTYLE) as u32;
            if ex & WS_EX_TOOLWINDOW.0 != 0 || GetWindow(h, GW_OWNER).is_ok_and(|o| !o.is_invalid()) {
                return None;
            }
            let title = window_text(h);
            let mut pid = 0u32;
            GetWindowThreadProcessId(h, Some(&mut pid));
            if title.is_empty() || pid == me {
                return None;
            }
            let mut cloaked = 0u32;
            let _ = DwmGetWindowAttribute(h, DWMWA_CLOAKED, &mut cloaked as *mut _ as *mut _, 4);
            if cloaked != 0 && cloaked != DWM_CLOAKED_SHELL {
                return None; // a program's own hidden frame, not a window anyone sees
            }
            let rect = frame(h);
            let minimized = IsIconic(h).as_bool();
            if !minimized && (rect.right - rect.left < 80 || rect.bottom - rect.top < 60) {
                return None;
            }
            let app = names.entry(pid).or_insert_with(|| process_name(pid)).clone();
            Some(Win { hwnd: h, pid, app, title, rect, minimized, other_desktop: cloaked == DWM_CLOAKED_SHELL })
        })
        .collect()
}

fn foreground_pid() -> u32 {
    let mut pid = 0u32;
    unsafe {
        GetWindowThreadProcessId(GetForegroundWindow(), Some(&mut pid));
    }
    pid
}

fn apps() -> String {
    let front = foreground_pid();
    let mut seen: Vec<(u32, String, Vec<Win>)> = vec![];
    for w in window_list() {
        match seen.iter_mut().find(|s| s.0 == w.pid) {
            Some(s) => s.2.push(w),
            None => seen.push((w.pid, w.app.clone(), vec![w])),
        }
    }
    if seen.is_empty() {
        return "没有找到有窗口的程序".into();
    }
    seen.iter()
        .map(|(pid, app, ws)| {
            let titles: Vec<String> = ws.iter().take(3).map(|w| format!("“{}”", cut(&w.title))).collect();
            format!("{app}  pid {pid}  {}{}", titles.join(" "), if *pid == front { "  [前台]" } else { "" })
                + if ws.iter().all(|w| w.minimized) { "  [最小化]" } else { "" }
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn find_app(name: &str) -> R<(u32, String)> {
    let list = window_list();
    let n = name.trim().to_lowercase();
    let n = n.strip_suffix(".exe").unwrap_or(&n).to_string();
    let hit = n
        .parse::<u32>()
        .ok()
        .and_then(|pid| list.iter().find(|w| w.pid == pid))
        .or_else(|| list.iter().find(|w| w.app.to_lowercase() == n))
        .or_else(|| list.iter().find(|w| w.title.to_lowercase() == n))
        .or_else(|| list.iter().find(|w| w.app.to_lowercase().contains(&n)))
        .or_else(|| list.iter().find(|w| w.title.to_lowercase().contains(&n)));
    match hit {
        Some(w) if !n.is_empty() => Ok((w.pid, w.app.clone())),
        _ => fail(format!("没有在运行的程序叫 {name}。用 list_apps 看看有哪些；要先打开它可以用 PowerShell 运行 Start-Process")),
    }
}

fn windows_text(app: Option<&str>) -> String {
    let mut list = window_list();
    if let Some(a) = app.filter(|a| !a.is_empty()) {
        if let Ok((pid, _)) = find_app(a) {
            let n = a.trim().to_lowercase();
            let titled = list.iter().any(|w| w.pid == pid && w.title.to_lowercase().contains(&n));
            list.retain(|w| w.pid == pid && (!titled || w.title.to_lowercase().contains(&n)));
        }
    }
    if list.is_empty() {
        return "没有找到窗口".into();
    }
    list.iter()
        .take(60)
        .map(|w| {
            format!("窗口 {}  {}  “{}”  {}×{}", w.hwnd.0 as isize, w.app, cut(&w.title), w.rect.right - w.rect.left, w.rect.bottom - w.rect.top)
                + if w.minimized { "  [最小化]" } else if w.other_desktop { "  [在别的虚拟桌面]" } else { "" }
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn window_by_id(r: &Value) -> R<Win> {
    let id = r["window"].as_i64().or_else(|| r["window"].as_str().and_then(|s| s.parse().ok())).unwrap_or(0);
    match window_list().into_iter().find(|w| w.hwnd.0 as isize as i64 == id) {
        Some(w) => Ok(w),
        None => fail(format!("没有编号 {id} 的窗口，用 list_windows 重新看看")),
    }
}

/// The window named in the request; or the one whose title the program was named by, since current
/// Windows apps all run in one process (ApplicationFrameHost), so Calculator and Settings share it;
/// or the program's front one.
fn target_window(pid: u32, r: &Value) -> R<Win> {
    if !r["window"].is_null() {
        let w = window_by_id(r)?;
        return if w.pid == pid { Ok(w) } else { fail(format!("窗口 {} 不属于这个程序", w.hwnd.0 as isize)) };
    }
    let list: Vec<Win> = window_list().into_iter().filter(|w| w.pid == pid).collect();
    let n = r["app"].as_str().unwrap_or_default().trim().to_lowercase();
    let front = unsafe { GetForegroundWindow() };
    let titled = |w: &&Win| !n.is_empty() && w.title.to_lowercase().contains(&n);
    list.iter()
        .find(|w| !n.is_empty() && w.title.to_lowercase() == n)
        .or_else(|| list.iter().find(titled))
        .or_else(|| list.iter().find(|w| w.hwnd == front))
        .or_else(|| list.iter().find(|w| !w.minimized))
        .or(list.first())
        .cloned()
        .ok_or_else(|| Fail("这个程序没有能读的窗口".into()))
}

fn own_windows() -> Vec<HWND> {
    unsafe extern "system" fn each(h: HWND, l: LPARAM) -> BOOL {
        let mut pid = 0u32;
        GetWindowThreadProcessId(h, Some(&mut pid));
        if pid == std::process::id() && IsWindowVisible(h).as_bool() {
            (*(l.0 as *mut Vec<HWND>)).push(h);
        }
        TRUE
    }
    let mut own: Vec<HWND> = Vec::new();
    unsafe {
        let _ = EnumWindows(Some(each), LPARAM(&mut own as *mut _ as isize));
    }
    own
}

// ---------- UI Automation ----------

fn automation() -> R<IUIAutomation> {
    unsafe {
        let a: IUIAutomation = CoCreateInstance(&CUIAutomation8, None, CLSCTX_INPROC_SERVER)
            .or_else(|_| CoCreateInstance(&CUIAutomation, None, CLSCTX_INPROC_SERVER))?;
        if let Ok(a2) = a.cast::<IUIAutomation2>() {
            let _ = a2.SetConnectionTimeout(3000); // a program that has stopped answering can't hold us up
            let _ = a2.SetTransactionTimeout(5000);
        }
        Ok(a)
    }
}

fn control_name(id: i32) -> &'static str {
    const NAMES: [&str; 41] = [
        "Button", "Calendar", "CheckBox", "ComboBox", "Edit", "Hyperlink", "Image", "ListItem", "List", "Menu", "MenuBar",
        "MenuItem", "ProgressBar", "RadioButton", "ScrollBar", "Slider", "Spinner", "StatusBar", "Tab", "TabItem", "Text",
        "ToolBar", "ToolTip", "Tree", "TreeItem", "Custom", "Group", "Thumb", "DataGrid", "DataItem", "Document",
        "SplitButton", "Window", "Pane", "Header", "HeaderItem", "Table", "TitleBar", "Separator", "SemanticZoom", "AppBar",
    ];
    NAMES.get((id - 50000) as usize).copied().unwrap_or("Control")
}

const CONTAINERS: &[&str] = &["Pane", "Group", "Custom", "Separator", "Thumb", "ScrollBar", "Control"];

struct Walk {
    found: Vec<Elem>,
    lines: Vec<String>,
    skipped: usize,
    query: String,
    max_depth: usize,
    origin: (i32, i32),
}

impl Walk {
    fn visit(&mut self, walker: &IUIAutomationTreeWalker, e: &IUIAutomationElement, depth: usize, indent: usize) {
        if depth > self.max_depth || self.found.len() >= 3000 {
            return;
        }
        let mut shown = indent;
        unsafe {
            let role = control_name(e.CurrentControlType().map(|c| c.0).unwrap_or(0));
            let secure = e.CurrentIsPassword().map(|b| b.as_bool()).unwrap_or(false);
            let name = bstr(e.CurrentName());
            let help = bstr(e.CurrentHelpText());
            let value = if secure {
                String::new()
            } else {
                e.GetCurrentPatternAs::<IUIAutomationValuePattern>(UIA_ValuePatternId).map(|p| bstr(p.CurrentValue())).unwrap_or_default()
            };
            let text = format!("{name} {help} {value}");
            let worth = !text.trim().is_empty() || !CONTAINERS.contains(&role);
            if worth {
                self.found.push(Elem(e.clone())); // numbered whether shown or not, so a filtered read keeps the numbers
            }
            if worth && (self.query.is_empty() || text.to_lowercase().contains(&self.query)) {
                if self.lines.len() < 500 {
                    let mut l = format!("{}[{}] {role}", "  ".repeat(indent), self.found.len());
                    if secure {
                        l += " 密码框";
                    }
                    if !name.is_empty() {
                        l += &format!(" “{}”", cut(&name));
                    }
                    if !value.is_empty() && value != name {
                        l += &format!(" = “{}”", cut(&value));
                    }
                    if !help.is_empty() && help != name {
                        l += &format!(" 提示“{}”", cut(&help));
                    }
                    if let Ok(b) = e.CurrentBoundingRectangle() {
                        if b.right > b.left {
                            l += &format!(" @{},{} {}×{}", b.left - self.origin.0, b.top - self.origin.1, b.right - b.left, b.bottom - b.top);
                        }
                    }
                    if e.CurrentIsEnabled().is_ok_and(|b| !b.as_bool()) {
                        l += " [不可用]";
                    }
                    if e.CurrentHasKeyboardFocus().is_ok_and(|b| b.as_bool()) {
                        l += " [焦点]";
                    }
                    self.lines.push(l);
                } else {
                    self.skipped += 1;
                }
                shown = indent + 1;
            }
            let mut child = walker.GetFirstChildElement(e);
            while let Ok(c) = child {
                self.visit(walker, &c, depth + 1, shown);
                child = walker.GetNextSiblingElement(&c);
            }
        }
    }
}

fn clean_menu_text(s: &str) -> String {
    s.split('\t').next().unwrap_or_default().replace('&', "").trim().to_lowercase()
}

/// A classic menu bar is chosen from without opening anything: the command it would send goes to the
/// window. Other menus go through UI Automation, which may open them on screen for a moment.
fn menu(r: &Value) -> R<String> {
    let (pid, _) = find_app(r["app"].as_str().unwrap_or_default())?;
    let w = target_window(pid, r)?;
    let path: Vec<String> = r["path"].as_array().map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect()).unwrap_or_default();
    if path.is_empty() {
        return fail("要给出菜单路径，如 [\"文件\", \"另存为\"]");
    }
    unsafe {
        let mut current = GetMenu(w.hwnd);
        if !current.is_invalid() {
            for (i, name) in path.iter().enumerate() {
                let n = clean_menu_text(name);
                let count = GetMenuItemCount(Some(current)).max(0);
                let mut items = vec![];
                for pos in 0..count {
                    let mut buf = [0u16; 256];
                    let len = GetMenuStringW(current, pos as u32, Some(&mut buf), MF_BYPOSITION);
                    items.push((pos, String::from_utf16_lossy(&buf[..len.max(0) as usize])));
                }
                let hit = items.iter().find(|(_, t)| clean_menu_text(t) == n).or_else(|| items.iter().find(|(_, t)| clean_menu_text(t).contains(&n)));
                let Some((pos, _)) = hit else {
                    let names: Vec<String> = items.iter().map(|(_, t)| clean_menu_text(t)).filter(|t| !t.is_empty()).collect();
                    return fail(format!("“{name}”下没找到，有：{}", names.join("、")));
                };
                if i == path.len() - 1 {
                    let state = GetMenuState(current, *pos as u32, MF_BYPOSITION);
                    if state & (MF_GRAYED.0 | MF_DISABLED.0) != 0 {
                        return fail(format!("菜单项“{name}”现在不可用"));
                    }
                    let id = GetMenuItemID(current, *pos);
                    let _ = PostMessageW(Some(w.hwnd), WM_COMMAND, WPARAM(id as usize), LPARAM(0));
                    return Ok(format!("已选菜单 {}", path.join(" → ")));
                }
                current = GetSubMenu(current, *pos);
                if current.is_invalid() {
                    return fail(format!("“{name}”没有下一级菜单"));
                }
            }
        }
    }
    menu_by_automation(&w, &path)
}

fn menu_by_automation(w: &Win, path: &[String]) -> R<String> {
    let a = automation()?;
    unsafe {
        let walker = a.ControlViewWalker()?;
        let root = a.ElementFromHandle(w.hwnd)?;
        let mut scope = find_all(&walker, &root, 5, |e| e.CurrentControlType().map(|c| c.0) == Ok(UIA_MenuBarControlTypeId.0));
        if scope.is_empty() {
            return fail("读不到这个程序的菜单栏");
        }
        for (i, name) in path.iter().enumerate() {
            let n = clean_menu_text(name);
            let named = |e: &IUIAutomationElement| clean_menu_text(&bstr(e.CurrentName()));
            let mut items = vec![];
            for s in &scope {
                items.extend(find_all(&walker, s, 3, |e| e.CurrentControlType().map(|c| c.0) == Ok(UIA_MenuItemControlTypeId.0)));
            }
            if items.is_empty() {
                // an opened menu can be a window of its own
                let desktop = a.GetRootElement()?;
                for m in find_all(&walker, &desktop, 1, |e| e.CurrentControlType().map(|c| c.0) == Ok(UIA_MenuControlTypeId.0)) {
                    items.extend(find_all(&walker, &m, 2, |e| e.CurrentControlType().map(|c| c.0) == Ok(UIA_MenuItemControlTypeId.0)));
                }
            }
            let hit = items.iter().find(|e| named(e) == n).or_else(|| items.iter().find(|e| named(e).contains(&n))).cloned();
            let Some(hit) = hit else {
                let names: Vec<String> = items.iter().map(named).filter(|t| !t.is_empty()).collect();
                return fail(format!("“{name}”下没找到，有：{}", names.join("、")));
            };
            if i == path.len() - 1 {
                if hit.CurrentIsEnabled().is_ok_and(|b| !b.as_bool()) {
                    return fail(format!("菜单项“{name}”现在不可用"));
                }
                if let Ok(p) = hit.GetCurrentPatternAs::<IUIAutomationInvokePattern>(UIA_InvokePatternId) {
                    p.Invoke()?;
                } else if let Ok(p) = hit.GetCurrentPatternAs::<IUIAutomationLegacyIAccessiblePattern>(UIA_LegacyIAccessiblePatternId) {
                    p.DoDefaultAction()?;
                } else {
                    return fail(format!("菜单项“{name}”按不了"));
                }
                return Ok(format!("已选菜单 {}（这个程序的菜单可能在屏幕上短暂打开过）", path.join(" → ")));
            }
            if let Ok(p) = hit.GetCurrentPatternAs::<IUIAutomationExpandCollapsePattern>(UIA_ExpandCollapsePatternId) {
                let _ = p.Expand();
                std::thread::sleep(Duration::from_millis(400));
            }
            scope = vec![hit];
        }
    }
    fail("没有选到菜单")
}

unsafe fn find_all(walker: &IUIAutomationTreeWalker, from: &IUIAutomationElement, depth: usize, want: impl Fn(&IUIAutomationElement) -> bool + Copy) -> Vec<IUIAutomationElement> {
    let mut out = vec![];
    if depth == 0 {
        return out;
    }
    let mut child = walker.GetFirstChildElement(from);
    while let Ok(c) = child {
        if want(&c) {
            out.push(c.clone());
        } else {
            out.extend(find_all(walker, &c, depth - 1, want));
        }
        child = walker.GetNextSiblingElement(&c);
    }
    out
}

// ---------- keys ----------

/// (virtual key, extended) for a key's name. Mac names work too: cmd is Ctrl, option is Alt.
fn key_code(name: &str) -> Option<(u16, bool, bool)> {
    let named = match name {
        "return" | "enter" => 0x0D,
        "tab" => 0x09,
        "space" => 0x20,
        "backspace" => 0x08,
        "delete" | "del" | "forwarddelete" => return Some((0x2E, true, false)),
        "esc" | "escape" => 0x1B,
        "insert" | "ins" => return Some((0x2D, true, false)),
        "home" => return Some((0x24, true, false)),
        "end" => return Some((0x23, true, false)),
        "pageup" | "pgup" => return Some((0x21, true, false)),
        "pagedown" | "pgdn" => return Some((0x22, true, false)),
        "left" => return Some((0x25, true, false)),
        "up" => return Some((0x26, true, false)),
        "right" => return Some((0x27, true, false)),
        "down" => return Some((0x28, true, false)),
        "capslock" => 0x14,
        "printscreen" => return Some((0x2C, true, false)),
        "menu" | "apps" => return Some((0x5D, true, false)),
        _ => 0,
    };
    if named != 0 {
        return Some((named, false, false));
    }
    if let Some(n) = name.strip_prefix('f').and_then(|d| d.parse::<u16>().ok()).filter(|n| (1..=24).contains(n)) {
        return Some((0x70 + n - 1, false, false));
    }
    let mut chars = name.chars();
    let (Some(c), None) = (chars.next(), chars.next()) else { return None };
    if c.is_ascii_alphanumeric() {
        return Some((c.to_ascii_uppercase() as u16, false, false));
    }
    let scan = unsafe { VkKeyScanW(c as u16) };
    if scan == -1 {
        return None;
    }
    Some(((scan & 0xFF) as u16, false, scan & 0x100 != 0))
}

fn modifier(name: &str) -> Option<(u16, bool)> {
    match name {
        "ctrl" | "control" | "cmd" | "command" => Some((0x11, false)),
        "shift" => Some((0x10, false)),
        "alt" | "option" | "opt" => Some((0x12, false)),
        "win" | "windows" | "meta" | "super" => Some((0x5B, true)),
        _ => None,
    }
}

/// [(modifiers, key)] for "ctrl+s return": modifiers go down as real keys and come up again, so
/// nothing is left held.
fn parse_combo(combo: &str) -> R<Vec<(Vec<(u16, bool)>, (u16, bool))>> {
    let mut out = vec![];
    for one in combo.split_whitespace() {
        let mut mods = vec![];
        let mut key = None;
        for part in one.to_lowercase().split('+') {
            if let Some(m) = modifier(part) {
                mods.push(m);
                continue;
            }
            let Some((vk, ext, shift)) = key_code(part) else { return fail(format!("不认识的键 {part}")) };
            if shift && !mods.iter().any(|m| m.0 == 0x10) {
                mods.push((0x10, false));
            }
            key = Some((vk, ext));
        }
        // a modifier alone ("shift", to switch an input method between Chinese and English) is the key itself
        let Some(k) = key.or_else(|| mods.pop()) else { return fail(format!("“{one}”里没有键")) };
        out.push((mods, k));
    }
    Ok(out)
}

fn key_input(vk: u16, ext: bool, up: bool) -> INPUT {
    let scan = unsafe { MapVirtualKeyW(vk as u32, MAPVK_VK_TO_VSC) } as u16;
    let mut flags = KEYBD_EVENT_FLAGS(0);
    if ext {
        flags |= KEYEVENTF_EXTENDEDKEY;
    }
    if up {
        flags |= KEYEVENTF_KEYUP;
    }
    INPUT {
        r#type: INPUT_KEYBOARD,
        Anonymous: INPUT_0 { ki: KEYBDINPUT { wVk: VIRTUAL_KEY(vk), wScan: scan, dwFlags: flags, time: 0, dwExtraInfo: MARK } },
    }
}

fn send_inputs(inputs: &[INPUT]) {
    unsafe {
        SendInput(inputs, std::mem::size_of::<INPUT>() as i32);
    }
}

/// What is typed with this key, rather than a shortcut: a Chinese input method takes such a key as the
/// start of a word, so "f" for a program's zoom becomes a list of candidates instead.
fn types_a_character(mods: &[(u16, bool)], vk: u16) -> bool {
    mods.iter().all(|m| m.0 == 0x10) && (vk == 0x20 || (0x30..=0x5A).contains(&vk) || (0xBA..=0xE2).contains(&vk))
}

/// Closes the input method of the window in front, for one key; returns how to put it back.
fn close_input_method() -> Option<(HWND, usize)> {
    const WM_IME_CONTROL: u32 = 0x0283;
    const IMC_GETOPENSTATUS: usize = 5;
    const IMC_SETOPENSTATUS: usize = 6;
    unsafe {
        let front = GetForegroundWindow();
        let mut gi = GUITHREADINFO { cbSize: std::mem::size_of::<GUITHREADINFO>() as u32, ..Default::default() };
        let tid = GetWindowThreadProcessId(front, None);
        let focus = if GetGUIThreadInfo(tid, &mut gi).is_ok() && !gi.hwndFocus.is_invalid() { gi.hwndFocus } else { front };
        let ime = ImmGetDefaultIMEWnd(focus);
        if ime.is_invalid() {
            return None;
        }
        let mut open = 0usize;
        SendMessageTimeoutW(ime, WM_IME_CONTROL, WPARAM(IMC_GETOPENSTATUS), LPARAM(0), SMTO_ABORTIFHUNG, 500, Some(&mut open));
        if open == 0 {
            return None;
        }
        SendMessageTimeoutW(ime, WM_IME_CONTROL, WPARAM(IMC_SETOPENSTATUS), LPARAM(0), SMTO_ABORTIFHUNG, 500, None);
        Some((ime, open))
    }
}

fn send_combo(combo: &str) -> R<()> {
    for (mods, (vk, ext)) in parse_combo(combo)? {
        let reopen = if types_a_character(&mods, vk) { close_input_method() } else { None };
        for (m, e) in &mods {
            send_inputs(&[key_input(*m, *e, false)]);
            std::thread::sleep(Duration::from_millis(8));
        }
        send_inputs(&[key_input(vk, ext, false)]);
        std::thread::sleep(Duration::from_millis(8));
        send_inputs(&[key_input(vk, ext, true)]);
        for (m, e) in mods.iter().rev() {
            std::thread::sleep(Duration::from_millis(8));
            send_inputs(&[key_input(*m, *e, true)]);
        }
        if let Some((ime, open)) = reopen {
            std::thread::sleep(Duration::from_millis(60)); // the program handles the key before the method comes back
            unsafe {
                SendMessageTimeoutW(ime, 0x0283, WPARAM(6), LPARAM(open as isize), SMTO_ABORTIFHUNG, 500, None);
            }
        }
    }
    Ok(())
}

fn focused_is_password() -> bool {
    automation()
        .ok()
        .and_then(|a| unsafe { a.GetFocusedElement() }.ok())
        .is_some_and(|e| unsafe { e.CurrentIsPassword() }.is_ok_and(|b| b.as_bool()))
}

/// Typed as characters, not keys, so an input method has nothing to hold as unconfirmed text.
fn type_text(text: &str) -> R<String> {
    if focused_is_password() {
        return fail("焦点在密码框里，不能替用户输入密码");
    }
    let units: Vec<u16> = text.encode_utf16().collect();
    for chunk in units.chunks(16) {
        let mut inputs = vec![];
        for &u in chunk {
            for up in [false, true] {
                let flags = if up { KEYEVENTF_UNICODE | KEYEVENTF_KEYUP } else { KEYEVENTF_UNICODE };
                inputs.push(INPUT {
                    r#type: INPUT_KEYBOARD,
                    Anonymous: INPUT_0 { ki: KEYBDINPUT { wVk: VIRTUAL_KEY(0), wScan: u, dwFlags: flags, time: 0, dwExtraInfo: MARK } },
                });
            }
        }
        send_inputs(&inputs);
        std::thread::sleep(Duration::from_millis(12));
    }
    Ok("已输入".into())
}

/// Keys and text for one program's window, through its message queue: the program needn't be in front.
fn app_keys(r: &Value) -> R<String> {
    let (pid, app) = find_app(r["app"].as_str().unwrap_or_default())?;
    let w = target_window(pid, r)?;
    let combos = parse_combo(r["keys"].as_str().unwrap_or_default())?;
    // A program in the background reads Ctrl, Shift and Alt from the real keyboard, not from messages:
    // ctrl+a would arrive as a plain "a" and be typed. So none are sent, rather than the wrong key.
    if combos.iter().any(|(mods, _)| !mods.is_empty()) {
        return fail("后台发不了带 ctrl、shift、alt 的组合键：Windows 程序在后台读不到这些键，会当成只按了后面那个键。请改用 choose_menu 或 press 做同样的事；确实需要时 request_full_control 后用 press_keys");
    }
    let target = unsafe {
        let tid = GetWindowThreadProcessId(w.hwnd, None);
        let mut gi = GUITHREADINFO { cbSize: std::mem::size_of::<GUITHREADINFO>() as u32, ..Default::default() };
        if GetGUIThreadInfo(tid, &mut gi).is_ok() && !gi.hwndFocus.is_invalid() { gi.hwndFocus } else { w.hwnd }
    };
    let mut how = String::new();
    if let Some(text) = r["text"].as_str().filter(|t| !t.is_empty()) {
        let class = class_name(target).to_lowercase();
        let ime_free = unsafe {
            if class.starts_with("edit") || class.contains("richedit") {
                let s = wide(text);
                let mut result = 0usize;
                SendMessageTimeoutW(target, EM_REPLACESEL, WPARAM(1), LPARAM(s.as_ptr() as isize), SMTO_ABORTIFHUNG, 3000, Some(&mut result)).0 != 0
            } else {
                false
            }
        };
        if ime_free {
            how = "已在光标处输入。".into();
        } else {
            for u in text.encode_utf16() {
                unsafe {
                    let _ = PostMessageW(Some(target), WM_CHAR, WPARAM(u as usize), LPARAM(1));
                }
            }
            how = "已按字符发给它。".into();
        }
    }
    for (_, (vk, ext)) in combos {
        for up in [false, true] {
            unsafe {
                let scan = MapVirtualKeyW(vk as u32, MAPVK_VK_TO_VSC);
                let mut l = 1 | (scan << 16) | if ext { 1 << 24 } else { 0 };
                if up {
                    l |= (1 << 30) | (1 << 31);
                }
                let _ = PostMessageW(Some(target), if up { WM_KEYUP } else { WM_KEYDOWN }, WPARAM(vk as usize), LPARAM(l as isize));
            }
        }
    }
    Ok(format!("{how}已发给 {app}。后台发的键有些程序会忽略（浏览器和新式应用常这样），用 read_ui 或 capture_window 核对"))
}

// ---------- capture ----------

/// The scale capture_window uses for a window, which click_in_window works back from.
fn capture_scale(r: &RECT) -> f64 {
    (1400.0 / (r.right - r.left).max(r.bottom - r.top).max(1) as f64).min(1.0)
}

fn capture_window(r: &Value) -> R<Value> {
    let w = window_by_id(r)?;
    if w.minimized {
        return fail(format!("窗口 {} 最小化了，截不到它的画面。可以用 read_ui 读它的控件；要看画面得把它恢复，这需要用户同意（request_full_control）", w.hwnd.0 as isize));
    }
    let mut whole = RECT::default();
    unsafe {
        let _ = GetWindowRect(w.hwnd, &mut whole);
    }
    // A program that doesn't know about display scaling draws itself at its own, smaller size, which
    // Windows then stretches on screen: so it's captured at that size and stretched the same way.
    let f = unsafe {
        let (mut dx, mut dy) = (96u32, 96u32);
        let screen_dpi = if GetDpiForMonitor(MonitorFromWindow(w.hwnd, MONITOR_DEFAULTTONEAREST), MDT_EFFECTIVE_DPI, &mut dx, &mut dy).is_ok() { dx } else { 96 };
        (screen_dpi as f64 / GetDpiForWindow(w.hwnd).max(1) as f64).max(1.0)
    };
    let logical = |v: i32| (v as f64 / f).round() as i32;
    let (px, ww, wh) = grab_window(w.hwnd, logical(whole.right - whole.left), logical(whole.bottom - whole.top))?;
    // the visible frame inside the window's full rectangle, which has invisible borders round it
    let (fx, fy) = (logical(w.rect.left - whole.left).max(0) as u32, logical(w.rect.top - whole.top).max(0) as u32);
    let (fw, fh) = ((logical(w.rect.right - w.rect.left) as u32).min(ww - fx), (logical(w.rect.bottom - w.rect.top) as u32).min(wh - fy));
    let mut cropped = Vec::with_capacity((fw * fh * 4) as usize);
    for y in fy..fy + fh {
        let start = ((y * ww + fx) * 4) as usize;
        cropped.extend_from_slice(&px[start..start + (fw * 4) as usize]);
    }
    let (image, iw, ih) = jpeg(cropped, fw, fh, capture_scale(&w.rect) * f)?;
    Ok(json!({"ok": true, "image": image, "mime": "image/jpeg",
              "text": format!("窗口 {} “{}”：{iw}×{ih} 像素，click_in_window 的坐标用这张图的像素。", w.hwnd.0 as isize, w.title)}))
}

fn bitmap_pixels(dc: HDC, bmp: HBITMAP, w: i32, h: i32) -> Vec<u8> {
    let mut bi = BITMAPINFO {
        bmiHeader: BITMAPINFOHEADER {
            biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
            biWidth: w,
            biHeight: -h, // top row first
            biPlanes: 1,
            biBitCount: 32,
            biCompression: BI_RGB.0,
            ..Default::default()
        },
        ..Default::default()
    };
    let mut px = vec![0u8; (w * h * 4) as usize];
    unsafe {
        GetDIBits(dc, bmp, 0, h as u32, Some(px.as_mut_ptr() as *mut _), &mut bi, DIB_RGB_COLORS);
    }
    px
}

/// BGRA pixels of a window, covered or not: the window draws itself into our bitmap.
fn grab_window(h: HWND, w: i32, ht: i32) -> R<(Vec<u8>, u32, u32)> {
    if w <= 0 || ht <= 0 {
        return fail("这个窗口没有大小");
    }
    unsafe {
        let screen = GetDC(None);
        let mem = CreateCompatibleDC(Some(screen));
        let bmp = CreateCompatibleBitmap(screen, w, ht);
        let old = SelectObject(mem, bmp.into());
        let drawn = PrintWindow(h, mem, PRINT_WINDOW_FLAGS(2)).as_bool(); // PW_RENDERFULLCONTENT: what the GPU draws too
        let px = bitmap_pixels(mem, bmp, w, ht);
        SelectObject(mem, old);
        let _ = DeleteObject(bmp.into());
        let _ = DeleteDC(mem);
        ReleaseDC(None, screen);
        if !drawn {
            return fail("截不到这个窗口");
        }
        Ok((px, w as u32, ht as u32))
    }
}

fn grab_screen(r: RECT) -> R<(Vec<u8>, u32, u32)> {
    let (w, h) = (r.right - r.left, r.bottom - r.top);
    unsafe {
        let screen = GetDC(None);
        let mem = CreateCompatibleDC(Some(screen));
        let bmp = CreateCompatibleBitmap(screen, w, h);
        let old = SelectObject(mem, bmp.into());
        let copied = BitBlt(mem, 0, 0, w, h, Some(screen), r.left, r.top, SRCCOPY | CAPTUREBLT);
        let px = bitmap_pixels(mem, bmp, w, h);
        SelectObject(mem, old);
        let _ = DeleteObject(bmp.into());
        let _ = DeleteDC(mem);
        ReleaseDC(None, screen);
        copied?;
        Ok((px, w as u32, h as u32))
    }
}

/// Screens, the main one first, in screen pixels.
fn displays() -> Vec<RECT> {
    unsafe extern "system" fn each(m: HMONITOR, _: HDC, _: *mut RECT, l: LPARAM) -> BOOL {
        let mut info = MONITORINFO { cbSize: std::mem::size_of::<MONITORINFO>() as u32, ..Default::default() };
        if GetMonitorInfoW(m, &mut info).as_bool() {
            (*(l.0 as *mut Vec<(bool, RECT)>)).push((info.dwFlags & MONITORINFOF_PRIMARY != 0, info.rcMonitor));
        }
        TRUE
    }
    let mut all: Vec<(bool, RECT)> = vec![];
    unsafe {
        let _ = EnumDisplayMonitors(None, None, Some(each), LPARAM(&mut all as *mut _ as isize));
    }
    all.sort_by_key(|(primary, r)| (!primary, r.left, r.top));
    all.into_iter().map(|(_, r)| r).collect()
}

/// (base64 JPEG, width, height), resized by `scale`.
fn jpeg(bgra: Vec<u8>, w: u32, h: u32, scale: f64) -> R<(String, u32, u32)> {
    let mut rgb = Vec::with_capacity((w * h * 3) as usize);
    for p in bgra.chunks_exact(4) {
        rgb.extend_from_slice(&[p[2], p[1], p[0]]);
    }
    let Some(mut img) = image::RgbImage::from_raw(w, h, rgb) else { return fail("截图数据不对") };
    if (scale - 1.0).abs() > 1e-6 {
        let (nw, nh) = (((w as f64 * scale).round() as u32).max(1), ((h as f64 * scale).round() as u32).max(1));
        img = image::imageops::resize(&img, nw, nh, image::imageops::FilterType::Triangle);
    }
    let mut out = vec![];
    image::codecs::jpeg::JpegEncoder::new_with_quality(&mut out, 75)
        .encode_image(&img)
        .map_err(|e| Fail(format!("编码截图失败：{e}")))?;
    Ok((base64::engine::general_purpose::STANDARD.encode(out), img.width(), img.height()))
}

// ---------- the mouse ----------

/// A click into one window through its message queue, at a point of capture_window's picture.
fn window_click(r: &Value) -> R<String> {
    let w = window_by_id(r)?;
    if w.minimized {
        return fail("这个窗口最小化了，后台点不到");
    }
    let s = r["scale"].as_f64().filter(|k| *k > 0.0).unwrap_or_else(|| capture_scale(&w.rect));
    let screen = POINT { x: w.rect.left + (r["x"].as_f64().unwrap_or(0.0) / s).round() as i32, y: w.rect.top + (r["y"].as_f64().unwrap_or(0.0) / s).round() as i32 };
    let mut target = w.hwnd;
    unsafe {
        loop {
            let mut p = screen;
            let _ = ScreenToClient(target, &mut p);
            let child = ChildWindowFromPointEx(target, p, CWP_SKIPINVISIBLE | CWP_SKIPDISABLED | CWP_SKIPTRANSPARENT);
            if child.is_invalid() || child == target {
                break;
            }
            target = child;
        }
        let mut p = screen;
        let _ = ScreenToClient(target, &mut p);
        let at = LPARAM((((p.y as u16 as u32) << 16) | (p.x as u16 as u32)) as isize);
        let right = r["button"].as_str() == Some("right");
        let (down, up, dbl, held) = if right {
            (WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK, 0x0002)
        } else {
            (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK, 0x0001)
        };
        let _ = PostMessageW(Some(target), WM_MOUSEMOVE, WPARAM(0), at);
        let _ = PostMessageW(Some(target), down, WPARAM(held), at);
        let _ = PostMessageW(Some(target), up, WPARAM(0), at);
        if r["double"].as_bool() == Some(true) {
            let _ = PostMessageW(Some(target), dbl, WPARAM(held), at);
            let _ = PostMessageW(Some(target), up, WPARAM(0), at);
        }
    }
    Ok("已向窗口发出后台点击。很多程序不接受后台点击（浏览器和新式应用常这样），一定用 capture_window 核对；没反应就 request_full_control".into())
}

/// The real mouse, at a point in screen pixels.
fn mouse_at(x: i32, y: i32, flags: MOUSE_EVENT_FLAGS, data: u32) {
    unsafe {
        let (vx, vy) = (GetSystemMetrics(SM_XVIRTUALSCREEN), GetSystemMetrics(SM_YVIRTUALSCREEN));
        let (vw, vh) = (GetSystemMetrics(SM_CXVIRTUALSCREEN).max(2), GetSystemMetrics(SM_CYVIRTUALSCREEN).max(2));
        let input = INPUT {
            r#type: INPUT_MOUSE,
            Anonymous: INPUT_0 {
                mi: MOUSEINPUT {
                    dx: ((x - vx) as i64 * 65535 / (vw - 1) as i64) as i32,
                    dy: ((y - vy) as i64 * 65535 / (vh - 1) as i64) as i32,
                    mouseData: data,
                    dwFlags: flags | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK | MOUSEEVENTF_MOVE,
                    time: 0,
                    dwExtraInfo: MARK,
                },
            },
        };
        SendInput(&[input], std::mem::size_of::<INPUT>() as i32);
    }
}

fn activate(name: &str) -> R<String> {
    let (pid, app) = find_app(name)?;
    let w = target_window(pid, &json!({"app": name}))?;
    unsafe {
        if IsIconic(w.hwnd).as_bool() {
            let _ = ShowWindow(w.hwnd, SW_RESTORE);
        }
        if !SetForegroundWindow(w.hwnd).as_bool() {
            // Windows lets a program take the front only just after input: an Alt press counts
            send_inputs(&[key_input(0x12, false, false), key_input(0x12, false, true)]);
            let _ = SetForegroundWindow(w.hwnd);
        }
    }
    Ok(format!("已把 {app} 切到前台"))
}
