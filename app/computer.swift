// Computer use, run by the app because macOS grants Accessibility and Screen Recording to it: the
// AI's tools (server/computer_mcp.py) reach it over a socket in ~/.carnot, one JSON line each way.
//
// Background actions leave the mouse, the keyboard focus and the screen to you: they read and
// press controls through the Accessibility API, capture single windows even when covered, and
// post keys to one app. Full control moves the real mouse and types as you would. It needs the
// mode set to "full", or a grant that you approve in a dialog, and Esc takes it back.
import AppKit
import ApplicationServices
import ScreenCaptureKit

private let ownMark: Int64 = 0x0CA4_0707  // on every event we post, so the Esc watcher can skip ours

private struct Failure: Error { let text: String; init(_ t: String) { text = t } }

final class ComputerUse {
    let path: String
    let notify: (String) -> Void
    private var elements = [pid_t: [AXUIElement]]()
    private let lock = NSLock()
    private var grant: String?
    private var grantUsed = Date.distantPast
    private var escWatch = [Any]()
    private var screenMap = (origin: CGPoint.zero, scale: CGFloat(1))  // the last screenshot's pixels → points

    init(notify: @escaping (String) -> Void) {
        let env = ProcessInfo.processInfo.environment["CARNOT_COMPUTER_SOCK"]
        path = env ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".carnot/computer.sock").path
        self.notify = notify
    }

    // MARK: socket

    func start() {
        try? FileManager.default.createDirectory(atPath: (path as NSString).deletingLastPathComponent, withIntermediateDirectories: true)
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8)
        guard bytes.count < MemoryLayout.size(ofValue: addr.sun_path) else { return }
        withUnsafeMutableBytes(of: &addr.sun_path) { p in
            for (i, b) in bytes.enumerated() { p[i] = b }
        }
        unlink(path)
        let ok = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        guard ok == 0 else { close(fd); return }
        chmod(path, 0o600)  // only you
        listen(fd, 8)
        Thread {
            while true {
                let c = accept(fd, nil, nil)
                if c < 0 { continue }
                Thread { self.serve(c) }.start()
            }
        }.start()
    }

    private func serve(_ c: Int32) {
        defer { close(c) }
        var buf = Data()
        var chunk = [UInt8](repeating: 0, count: 65536)
        while !buf.contains(10) {
            let n = read(c, &chunk, chunk.count)
            if n <= 0 { return }
            buf.append(contentsOf: chunk[0..<n])
        }
        let line = buf.prefix(upTo: buf.firstIndex(of: 10)!)
        var reply: [String: Any]
        if let req = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any], req["op"] as? String == "hold" {
            return hold(c, req["grant"] as? String)
        }
        if let req = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any] {
            do { reply = try handle(req) } catch let f as Failure { reply = ["ok": false, "error": f.text] } catch {
                reply = ["ok": false, "error": "\(error.localizedDescription)"]
            }
        } else { reply = ["ok": false, "error": "bad request"] }
        guard var out = try? JSONSerialization.data(withJSONObject: reply) else { return }
        out.append(10)
        out.withUnsafeBytes { p in
            var off = 0
            while off < p.count {
                let n = write(c, p.baseAddress! + off, p.count - off)
                if n <= 0 { break }
                off += n
            }
        }
    }

    /// The tool server keeps this connection open while it holds a takeover. It closes when the
    /// task ends, whatever way it ends, and the mouse and keyboard come back to you then.
    private func hold(_ c: Int32, _ token: String?) {
        lock.lock(); let valid = token != nil && token == grant; lock.unlock()
        _ = (valid ? "ok\n" : "no\n").withCString { write(c, $0, strlen($0)) }
        guard valid else { return }
        var b = [UInt8](repeating: 0, count: 256)
        while read(c, &b, b.count) > 0 {}
        lock.lock()
        if grant == token { endGrant("任务结束，已交还鼠标键盘") }
        lock.unlock()
    }

    // MARK: requests

    private func mode() -> String {
        let f = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".carnot/computer.json")
        guard let d = try? Data(contentsOf: f), let j = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any]
        else { return "off" }
        return j["mode"] as? String ?? "off"
    }

    private static let fullOps: Set<String> = ["screen", "click", "move", "drag", "scroll", "type", "key", "activate"]

    private func handle(_ r: [String: Any]) throws -> [String: Any] {
        let op = r["op"] as? String ?? ""
        let m = mode()
        if op == "status" {
            return ok("模式 \(m)；辅助功能权限 \(AXIsProcessTrusted() ? "有" : "没有")；屏幕录制权限 \(CGPreflightScreenCaptureAccess() ? "有" : "没有")；\(grant == nil ? "没有接管" : "接管中")")
        }
        guard m != "off" else { throw Failure("用户已经关掉了计算机使用") }
        if op == "grant" { return try requestGrant(r["reason"] as? String ?? "", full: m == "full") }
        if op == "release" {
            lock.lock(); defer { lock.unlock() }
            if let t = r["grant"] as? String, t == grant { endGrant("已交还鼠标键盘") }
            return ok("已交还鼠标键盘")
        }
        if Self.fullOps.contains(op) { try checkGrant(r["grant"] as? String, full: m == "full") }
        switch op {
        case "apps": return ok(apps())
        case "windows": return ok(windows(r["app"] as? String))
        case "capture": return try captureWindow(r)
        case "ui": return ok(try ui(r))
        case "press": return ok(try press(r))
        case "set": return ok(try setText(r))
        case "menu": return ok(try menu(r))
        case "app_keys": return ok(try appKeys(r))
        case "wclick": return ok(try windowClick(r))
        case "screen": return try screen(r)
        case "click", "move", "drag", "scroll": return ok(try mouse(op, r))
        case "type": return ok(try typeText(r["text"] as? String ?? "", pid: nil))
        case "key": try postCombo(r["keys"] as? String ?? "", pid: nil); return ok("已按下 \(r["keys"] as? String ?? "")")
        case "activate": return ok(try activate(r["app"] as? String ?? ""))
        default: throw Failure("不认识的操作 \(op)")
        }
    }

    private func ok(_ text: String) -> [String: Any] { ["ok": true, "text": text] }

    // MARK: full control: grant and Esc

    private func checkGrant(_ token: String?, full: Bool) throws {
        lock.lock(); defer { lock.unlock() }
        if grant != nil, Date().timeIntervalSince(grantUsed) > 600 { endGrant("十分钟没有操作，已收回鼠标键盘") }
        guard let g = grant, token == g else {
            throw Failure(full ? "还没有接管：先调用 request_full_control" : "这是接管鼠标键盘的操作，后台模式下要先调用 request_full_control 说明原因，用户同意后才能用")
        }
        grantUsed = Date()
    }

    private func requestGrant(_ reason: String, full: Bool) throws -> [String: Any] {
        try needAccessibility()
        lock.lock()
        if let g = grant { grantUsed = Date(); lock.unlock(); return ["ok": true, "text": "已经在接管中", "grant": g] }
        lock.unlock()
        var allowed = full
        if !full {
            DispatchQueue.main.sync {
                let a = NSAlert()
                a.messageText = "允许 Carnot 接管鼠标和键盘？"
                a.informativeText = (reason.isEmpty ? "后台方式做不到这一步。" : "原因：\(reason)") + "\n\n接管期间 Carnot 会移动鼠标、点击和打字。随时按 Esc 收回。"
                a.addButton(withTitle: "允许这次任务")
                a.addButton(withTitle: "拒绝")
                a.window.level = .screenSaver
                let t = Timer(timeInterval: 120, repeats: false) { _ in NSApp.abortModal() }
                RunLoop.main.add(t, forMode: .modalPanel)
                NSApp.activate(ignoringOtherApps: true)
                allowed = a.runModal() == .alertFirstButtonReturn
                t.invalidate()
            }
        }
        guard allowed else { throw Failure("用户没有同意接管鼠标键盘。换个后台办法，或者在对话里问用户") }
        let g = UUID().uuidString
        lock.lock(); grant = g; grantUsed = Date(); lock.unlock()
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            let esc: (NSEvent) -> Void = { [weak self] e in
                guard e.keyCode == 53, e.cgEvent?.getIntegerValueField(.eventSourceUserData) != ownMark else { return }
                self?.lock.lock(); self?.endGrant("已收回鼠标键盘控制"); self?.lock.unlock()
            }
            // other apps' keys, and Carnot's own: it may still be the active app after the dialog
            if let g = NSEvent.addGlobalMonitorForEvents(matching: .keyDown, handler: esc) { self.escWatch.append(g) }
            if let l = NSEvent.addLocalMonitorForEvents(matching: .keyDown, handler: { esc($0); return $0 }) { self.escWatch.append(l) }
        }
        say("Carnot 正在操作鼠标和键盘 · 按 Esc 收回", hold: 600_000)
        return ["ok": true, "text": "已获准接管鼠标键盘。用户按 Esc 会收回。", "grant": g]
    }

    /// Called with the lock held.
    private func endGrant(_ why: String) {
        guard grant != nil else { return }
        grant = nil
        DispatchQueue.main.async {
            self.escWatch.forEach(NSEvent.removeMonitor)
            self.escWatch = []
        }
        say(why, hold: 4000)
    }

    private func say(_ text: String, hold: Int) {
        let j = (try? JSONSerialization.data(withJSONObject: ["type": "notice", "text": text, "hold": hold])) ?? Data()
        let s = String(decoding: j, as: UTF8.self)
        DispatchQueue.main.async { self.notify(s) }
    }

    private func needAccessibility() throws {
        if AXIsProcessTrusted() { return }
        DispatchQueue.main.async {
            _ = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
        }
        throw Failure("Carnot 还没有辅助功能权限。请用户在 系统设置 → 隐私与安全性 → 辅助功能 里打开 Carnot，然后重试")
    }

    private func needScreen() throws {
        if CGPreflightScreenCaptureAccess() { return }
        DispatchQueue.main.async { _ = CGRequestScreenCaptureAccess() }
        throw Failure("Carnot 还没有屏幕录制权限。请用户在 系统设置 → 隐私与安全性 → 屏幕与系统录音 里打开 Carnot，然后重试")
    }

    // MARK: apps and windows

    private func regularApps() -> [NSRunningApplication] {
        NSWorkspace.shared.runningApplications.filter { $0.activationPolicy == .regular && $0.processIdentifier != getpid() }
    }

    private func findApp(_ name: String) throws -> NSRunningApplication {
        let apps = regularApps()
        if let pid = Int32(name), let a = apps.first(where: { $0.processIdentifier == pid }) { return a }
        let n = name.lowercased()
        if let a = apps.first(where: { $0.localizedName?.lowercased() == n || $0.bundleIdentifier?.lowercased() == n }) { return a }
        if let a = apps.first(where: { ($0.localizedName?.lowercased().contains(n) ?? false) || ($0.bundleIdentifier?.lowercased().contains(n) ?? false) }) { return a }
        throw Failure("没有在运行的程序叫 \(name)。用 list_apps 看看有哪些；要先打开它可以用 Bash 运行 open -g -a \"名字\"（-g 不会切到前台）")
    }

    private func apps() -> String {
        regularApps().map {
            "\($0.localizedName ?? "?")  pid \($0.processIdentifier)  \($0.bundleIdentifier ?? "")" + ($0.isActive ? "  [前台]" : "") + ($0.isHidden ? "  [隐藏]" : "")
        }.joined(separator: "\n")
    }

    private struct Win { let id: CGWindowID; let pid: pid_t; let app: String; let title: String; let bounds: CGRect; let onScreen: Bool }

    private func windowList() -> [Win] {
        let pids = Set(regularApps().map(\.processIdentifier))
        let info = CGWindowListCopyWindowInfo([.optionAll, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
        return info.compactMap { w in
            guard (w[kCGWindowLayer as String] as? Int) == 0, let pid = w[kCGWindowOwnerPID as String] as? pid_t, pids.contains(pid),
                  let bd = w[kCGWindowBounds as String] as? NSDictionary, let b = CGRect(dictionaryRepresentation: bd),
                  b.width >= 80, b.height >= 60 else { return nil }
            return Win(id: w[kCGWindowNumber as String] as? CGWindowID ?? 0, pid: pid, app: w[kCGWindowOwnerName as String] as? String ?? "",
                       title: w[kCGWindowName as String] as? String ?? "", bounds: b, onScreen: w[kCGWindowIsOnscreen as String] as? Bool ?? false)
        }
    }

    private func windows(_ app: String?) -> String {
        var list = windowList()
        if let app, !app.isEmpty, let a = try? findApp(app) { list = list.filter { $0.pid == a.processIdentifier } }
        if list.isEmpty { return "没有找到窗口" }
        return list.prefix(60).map {
            "窗口 \($0.id)  \($0.app)  “\($0.title)”  \(Int($0.bounds.width))×\(Int($0.bounds.height))" + ($0.onScreen ? "" : "  [不在当前屏幕：最小化或在别的桌面]")
        }.joined(separator: "\n")
    }

    private func window(_ r: [String: Any]) throws -> Win {
        let id = (r["window"] as? NSNumber)?.uint32Value ?? UInt32(r["window"] as? String ?? "") ?? 0
        guard let w = windowList().first(where: { $0.id == id }) else { throw Failure("没有编号 \(id) 的窗口，用 list_windows 重新看看") }
        return w
    }

    // MARK: capture

    private func waitAsync<T>(_ body: @escaping () async throws -> T) throws -> T {
        let sem = DispatchSemaphore(value: 0)
        var result: Result<T, Error> = .failure(Failure("超时"))
        Task {
            do { result = .success(try await body()) } catch { result = .failure(error) }
            sem.signal()
        }
        if sem.wait(timeout: .now() + 20) == .timedOut { throw Failure("截图超时") }
        return try result.get()
    }

    private func jpeg(_ img: CGImage) -> String {
        let rep = NSBitmapImageRep(cgImage: img)
        return rep.representation(using: .jpeg, properties: [.compressionFactor: 0.75])?.base64EncodedString() ?? ""
    }

    private func captureWindow(_ r: [String: Any]) throws -> [String: Any] {
        try needScreen()
        let w = try window(r)
        guard #available(macOS 14.0, *) else { throw Failure("单窗口截图需要 macOS 14") }
        let backing = NSScreen.main?.backingScaleFactor ?? 2
        let scale = min(backing, 1400 / max(w.bounds.width, w.bounds.height))
        if !w.onScreen { throw Failure("窗口 \(w.id) 最小化了或在别的桌面，macOS 截不到它。可以用 read_ui 读它的控件；要看画面得把它挪回当前桌面，这需要用户同意（request_full_control）") }
        let img: CGImage = try waitAsync {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
            guard let sw = content.windows.first(where: { $0.windowID == w.id }) else { throw Failure("截不到这个窗口") }
            let cfg = SCStreamConfiguration()
            cfg.width = Int(w.bounds.width * scale)
            cfg.height = Int(w.bounds.height * scale)
            cfg.showsCursor = false
            cfg.ignoreShadowsSingleWindow = true
            return try await SCScreenshotManager.captureImage(contentFilter: SCContentFilter(desktopIndependentWindow: sw), configuration: cfg)
        }
        return ["ok": true, "image": jpeg(img), "mime": "image/jpeg",
                "text": "窗口 \(w.id) “\(w.title)”：\(img.width)×\(img.height) 像素，click_in_window 的坐标用这张图的像素。"]
    }

    private func screen(_ r: [String: Any]) throws -> [String: Any] {
        try needScreen()
        guard #available(macOS 14.0, *) else { throw Failure("截屏需要 macOS 14") }
        let index = (r["display"] as? Int) ?? 0
        let me = Bundle.main.bundleIdentifier
        let (img, bounds): (CGImage, CGRect) = try waitAsync {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
            let ds = content.displays.sorted { $0.displayID == CGMainDisplayID() ? true : $1.displayID == CGMainDisplayID() ? false : $0.displayID < $1.displayID }
            guard index < ds.count else { throw Failure("没有第 \(index) 块屏幕，一共 \(ds.count) 块") }
            let d = ds[index]
            let b = CGDisplayBounds(d.displayID)
            let s = min(1, 1440 / b.width)
            let cfg = SCStreamConfiguration()
            cfg.width = Int(b.width * s)
            cfg.height = Int(b.height * s)
            cfg.showsCursor = true
            let filter = SCContentFilter(display: d, excludingApplications: content.applications.filter { $0.bundleIdentifier == me }, exceptingWindows: [])
            return (try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: cfg), b)
        }
        lock.lock(); screenMap = (bounds.origin, CGFloat(img.width) / bounds.width); lock.unlock()
        return ["ok": true, "image": jpeg(img), "mime": "image/jpeg",
                "text": "屏幕 \(index)：\(img.width)×\(img.height) 像素。click、drag、scroll 的坐标都用这张图的像素。"]
    }

    // MARK: accessibility

    private func attr(_ e: AXUIElement, _ a: String) -> AnyObject? {
        var v: AnyObject?
        return AXUIElementCopyAttributeValue(e, a as CFString, &v) == .success ? v : nil
    }

    private func str(_ e: AXUIElement, _ a: String) -> String {
        guard let v = attr(e, a) else { return "" }
        if let s = v as? String { return s }
        if let n = v as? NSNumber { return n.stringValue }
        return ""
    }

    private func frame(_ e: AXUIElement) -> CGRect? {
        guard let p = attr(e, kAXPositionAttribute), let s = attr(e, kAXSizeAttribute) else { return nil }
        var pt = CGPoint.zero, sz = CGSize.zero
        AXValueGetValue(p as! AXValue, .cgPoint, &pt)
        AXValueGetValue(s as! AXValue, .cgSize, &sz)
        return CGRect(origin: pt, size: sz)
    }

    private func children(_ e: AXUIElement) -> [AXUIElement] { attr(e, kAXChildrenAttribute) as? [AXUIElement] ?? [] }

    private func appElement(_ a: NSRunningApplication) -> AXUIElement {
        let e = AXUIElementCreateApplication(a.processIdentifier)
        AXUIElementSetMessagingTimeout(e, 3)
        AXUIElementSetAttributeValue(e, "AXManualAccessibility" as CFString, kCFBooleanTrue)  // Electron apps build their tree only when asked
        return e
    }

    private func axWindow(_ app: AXUIElement, _ r: [String: Any]) throws -> AXUIElement {
        let wins = attr(app, kAXWindowsAttribute) as? [AXUIElement] ?? []
        if r["window"] != nil {
            let w = try window(r)
            if let m = wins.first(where: { f in frame(f).map { abs($0.minX - w.bounds.minX) < 3 && abs($0.minY - w.bounds.minY) < 3 && abs($0.width - w.bounds.width) < 3 } ?? false }) { return m }
            if !w.title.isEmpty, let m = wins.first(where: { str($0, kAXTitleAttribute) == w.title }) { return m }
            throw Failure("读不到窗口 \(w.id) 的控件（可能最小化了或在别的桌面）")
        }
        if let f = attr(app, kAXFocusedWindowAttribute) { return f as! AXUIElement }
        if let f = attr(app, kAXMainWindowAttribute) { return f as! AXUIElement }
        if let f = wins.first { return f }
        throw Failure("这个程序没有能读的窗口")
    }

    private static let containers: Set<String> = ["AXGroup", "AXSplitGroup", "AXScrollArea", "AXLayoutArea", "AXUnknown", "AXSplitter", "AXLayoutItem"]

    private func ui(_ r: [String: Any]) throws -> String {
        try needAccessibility()
        let a = try findApp(r["app"] as? String ?? "")
        let app = appElement(a)
        let root = try axWindow(app, r)
        let origin = frame(root)?.origin ?? .zero
        let query = (r["query"] as? String ?? "").lowercased()
        let maxDepth = r["depth"] as? Int ?? 30
        var found = [AXUIElement](), lines = [String](), skipped = 0
        func walk(_ e: AXUIElement, _ depth: Int, _ indent: Int) {
            guard depth <= maxDepth, found.count < 3000 else { return }
            let role = str(e, kAXRoleAttribute)
            let secure = str(e, kAXSubroleAttribute) == "AXSecureTextField"
            let title = str(e, kAXTitleAttribute), desc = str(e, kAXDescriptionAttribute)
            let value = secure ? "" : str(e, kAXValueAttribute), hint = str(e, kAXPlaceholderValueAttribute)
            let text = [title, desc, value, hint].joined(separator: " ")
            var shown = indent
            let worth = !text.trimmingCharacters(in: .whitespaces).isEmpty || !Self.containers.contains(role)
            if worth { found.append(e) }  // numbered whether shown or not, so a filtered read keeps the numbers
            if worth && (query.isEmpty || text.lowercased().contains(query)) {
                if lines.count < 500 {
                    var l = String(repeating: "  ", count: indent) + "[\(found.count)] \(role.replacingOccurrences(of: "AX", with: ""))"
                    if secure { l += " 密码框" }
                    if !title.isEmpty { l += " “\(cut(title))”" }
                    if !desc.isEmpty && desc != title { l += " (\(cut(desc)))" }
                    if !value.isEmpty && value != title { l += " = “\(cut(value))”" }
                    if !hint.isEmpty { l += " 提示“\(cut(hint))”" }
                    if let f = frame(e) { l += " @\(Int(f.minX - origin.x)),\(Int(f.minY - origin.y)) \(Int(f.width))×\(Int(f.height))" }
                    if (attr(e, kAXEnabledAttribute) as? Bool) == false { l += " [不可用]" }
                    if (attr(e, kAXFocusedAttribute) as? Bool) == true { l += " [焦点]" }
                    lines.append(l)
                } else { skipped += 1 }
                shown = indent + 1
            }
            for c in children(e) { walk(c, depth + 1, shown) }
        }
        walk(root, 0, 0)
        lock.lock(); elements[a.processIdentifier] = found; lock.unlock()
        if lines.isEmpty {
            return query.isEmpty || found.isEmpty ? "这个窗口没有可读的控件（自绘界面）。后台做不到的话，可以 capture_window 看画面后试 click_in_window，或者 request_full_control" : "没有包含“\(query)”的控件"
        }
        var out = "\(a.localizedName ?? "") 窗口“\(str(root, kAXTitleAttribute))”的控件（坐标是相对窗口左上角的点）：\n" + lines.joined(separator: "\n")
        if skipped > 0 { out += "\n…还有 \(skipped) 个没列出，用 query 筛选" }
        return out
    }

    private func cut(_ s: String) -> String {
        let one = s.replacingOccurrences(of: "\n", with: " ")
        return one.count > 80 ? String(one.prefix(80)) + "…" : one
    }

    private func element(_ r: [String: Any]) throws -> (NSRunningApplication, AXUIElement) {
        try needAccessibility()
        let a = try findApp(r["app"] as? String ?? "")
        let n = (r["element"] as? Int) ?? Int(r["element"] as? String ?? "") ?? 0
        lock.lock(); let list = elements[a.processIdentifier] ?? []; lock.unlock()
        guard n >= 1, n <= list.count else { throw Failure("没有编号 \(n) 的控件，先用 read_ui 读一遍这个程序") }
        return (a, list[n - 1])
    }

    private func press(_ r: [String: Any]) throws -> String {
        let (_, e) = try element(r)
        var names: CFArray?
        AXUIElementCopyActionNames(e, &names)
        let actions = names as? [String] ?? []
        let wanted = r["action"] as? String
        let pick = wanted.map { w in actions.first { $0 == w || $0 == "AX" + w } ?? w }
            ?? ["AXPress", "AXConfirm", "AXPick", "AXOpen", "AXShowMenu"].first(where: actions.contains)
        guard let act = pick else { throw Failure("这个控件不能按（它支持的动作：\(actions.joined(separator: ", "))）") }
        let rc = AXUIElementPerformAction(e, act as CFString)
        guard rc == .success else { throw Failure("执行 \(act) 失败（\(rc.rawValue)）。可以换个控件，或者 request_full_control 后直接点") }
        return "已执行 \(act)。用 read_ui 或 capture_window 核对结果"
    }

    private func setText(_ r: [String: Any]) throws -> String {
        let (_, e) = try element(r)
        if str(e, kAXSubroleAttribute) == "AXSecureTextField" { throw Failure("不能往密码框里填东西，请用户自己输入") }
        let text = r["text"] as? String ?? ""
        AXUIElementSetAttributeValue(e, kAXFocusedAttribute as CFString, kCFBooleanTrue)
        let rc = AXUIElementSetAttributeValue(e, kAXValueAttribute as CFString, text as CFString)
        guard rc == .success else { throw Failure("这个控件不让直接设文字（\(rc.rawValue)）。可以先 press 它，再用 app_type 打字") }
        let now = str(e, kAXValueAttribute)
        return now == text ? "已填入" : "已设置，但读回来的是“\(cut(now))”，请核对"
    }

    private func menu(_ r: [String: Any]) throws -> String {
        try needAccessibility()
        let a = try findApp(r["app"] as? String ?? "")
        let path = r["path"] as? [String] ?? []
        guard !path.isEmpty, let bar = attr(appElement(a), kAXMenuBarAttribute) else { throw Failure("读不到这个程序的菜单栏") }
        var current = bar as! AXUIElement
        for (i, name) in path.enumerated() {
            var items = [AXUIElement]()
            for c in children(current) {
                if str(c, kAXRoleAttribute) == "AXMenu" { items += children(c) } else { items.append(c) }
            }
            let n = name.lowercased()
            guard let hit = items.first(where: { str($0, kAXTitleAttribute).lowercased() == n })
                    ?? items.first(where: { str($0, kAXTitleAttribute).lowercased().contains(n) }) else {
                let names = items.map { str($0, kAXTitleAttribute) }.filter { !$0.isEmpty }
                throw Failure("“\(name)”下没找到，有：\(names.joined(separator: "、"))")
            }
            if i == path.count - 1 {
                if (attr(hit, kAXEnabledAttribute) as? Bool) == false { throw Failure("菜单项“\(name)”现在不可用") }
                let rc = AXUIElementPerformAction(hit, kAXPressAction as CFString)
                guard rc == .success else { throw Failure("按菜单项失败（\(rc.rawValue)）") }
            }
            current = hit
        }
        return "已选菜单 \(path.joined(separator: " → "))"
    }

    // MARK: keys and mouse

    private static let keyCodes: [String: CGKeyCode] = [
        "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14,
        "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27,
        "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "return": 36, "enter": 36, "l": 37, "j": 38,
        "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "tab": 48, "space": 49, "`": 50,
        "delete": 51, "backspace": 51, "esc": 53, "escape": 53, "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96,
        "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111, "home": 115, "pageup": 116,
        "forwarddelete": 117, "end": 119, "pagedown": 121, "left": 123, "right": 124, "down": 125, "up": 126,
    ]

    private func post(_ e: CGEvent, pid: pid_t?) {
        e.setIntegerValueField(.eventSourceUserData, value: ownMark)
        if let pid { e.postToPid(pid) } else { e.post(tap: .cghidEventTap) }
    }

    private static let modifiers: [(String, CGEventFlags, CGKeyCode)] = [
        ("cmd", .maskCommand, 55), ("command", .maskCommand, 55), ("shift", .maskShift, 56), ("alt", .maskAlternate, 58),
        ("option", .maskAlternate, 58), ("opt", .maskAlternate, 58), ("ctrl", .maskControl, 59), ("control", .maskControl, 59),
        ("fn", .maskSecondaryFn, 63),
    ]

    /// Presses the modifiers as real keys and lets them go again: flags on the key alone can leave
    /// the system thinking ⌘ or Ctrl is still held, and then typing vanishes and clicks open menus.
    private func postCombo(_ combo: String, pid: pid_t?) throws {
        for one in combo.split(separator: " ") {
            var mods = [(CGEventFlags, CGKeyCode)]()
            var key: CGKeyCode?
            for part in one.lowercased().split(separator: "+").map(String.init) {
                if let m = Self.modifiers.first(where: { $0.0 == part }) { mods.append((m.1, m.2)); continue }
                guard let k = Self.keyCodes[part] else { throw Failure("不认识的键 \(part)") }
                key = k
            }
            guard let k = key else { throw Failure("“\(one)”里没有键") }
            var flags = CGEventFlags()
            func modifier(_ code: CGKeyCode) {
                guard let e = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: true) else { return }
                e.type = .flagsChanged
                e.flags = flags
                post(e, pid: pid)
                usleep(8000)
            }
            for (f, code) in mods { flags.insert(f); modifier(code) }
            for down in [true, false] {
                guard let e = CGEvent(keyboardEventSource: nil, virtualKey: k, keyDown: down) else { continue }
                e.flags = flags
                post(e, pid: pid)
                usleep(8000)
            }
            for (f, code) in mods.reversed() { flags.remove(f); modifier(code) }
        }
    }

    /// Typed characters go through the input method, and Chinese pinyin, say, holds them as
    /// unconfirmed text. So first the text goes in at the cursor through Accessibility, then by
    /// pasting (full control), or as key events (a background app, which ignores a pasting ⌘V).
    @discardableResult
    private func typeText(_ text: String, pid: pid_t?) throws -> String {
        let root = pid.map { AXUIElementCreateApplication($0) } ?? AXUIElementCreateSystemWide()
        if let f = attr(root, kAXFocusedUIElementAttribute) {
            let e = f as! AXUIElement
            if str(e, kAXSubroleAttribute) == "AXSecureTextField" { throw Failure("焦点在密码框里，不能替用户输入密码") }
            var settable: DarwinBoolean = false
            AXUIElementIsAttributeSettable(e, kAXSelectedTextAttribute as CFString, &settable)
            if settable.boolValue, AXUIElementSetAttributeValue(e, kAXSelectedTextAttribute as CFString, text as CFString) == .success {
                return "已在光标处输入"
            }
        }
        if pid == nil { try pasteText(text); return "已粘贴输入" }
        let units = Array(text.utf16)
        var i = 0
        while i < units.count {
            let chunk = Array(units[i..<min(i + 16, units.count)])
            for down in [true, false] {
                guard let e = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: down) else { continue }
                e.flags = []
                e.keyboardSetUnicodeString(stringLength: chunk.count, unicodeString: chunk)
                post(e, pid: pid)
            }
            usleep(12000)
            i += 16
        }
        return "已按键输入（中文可能停在输入法里没确认，要核对）"
    }

    /// The clipboard is put back as it was afterwards.
    private func pasteText(_ text: String) throws {
        var saved = [NSPasteboardItem]()
        DispatchQueue.main.sync {
            let pb = NSPasteboard.general
            saved = (pb.pasteboardItems ?? []).map { item in
                let copy = NSPasteboardItem()
                for t in item.types { if let d = item.data(forType: t) { copy.setData(d, forType: t) } }
                return copy
            }
            pb.clearContents()
            let item = NSPasteboardItem()
            item.setString(text, forType: .string)
            item.setData(Data(), forType: NSPasteboard.PasteboardType("org.nspasteboard.TransientType"))  // clipboard managers skip it
            pb.writeObjects([item])
        }
        try postCombo("cmd+v", pid: nil)
        usleep(400_000)  // the app reads the clipboard when it handles the keys, not when they're posted
        DispatchQueue.main.sync {
            let pb = NSPasteboard.general
            pb.clearContents()
            if !saved.isEmpty { pb.writeObjects(saved) }
        }
    }

    private func appKeys(_ r: [String: Any]) throws -> String {
        let a = try findApp(r["app"] as? String ?? "")
        var how = ""
        if let t = r["text"] as? String, !t.isEmpty { how = try typeText(t, pid: a.processIdentifier) + "。" }
        if let k = r["keys"] as? String, !k.isEmpty { try postCombo(k, pid: a.processIdentifier) }
        return "\(how)已发给 \(a.localizedName ?? "")。后台发的键有些程序会忽略，用 read_ui 或 capture_window 核对"
    }

    private func windowClick(_ r: [String: Any]) throws -> String {
        let w = try window(r)
        guard w.onScreen else { throw Failure("这个窗口不在当前屏幕上，后台点不到") }
        let k = r["scale"] as? Double ?? 0
        let backing = Double(NSScreen.main?.backingScaleFactor ?? 2)
        let s = k > 0 ? k : min(backing, 1400 / Double(max(w.bounds.width, w.bounds.height)))  // same as capture_window
        let p = CGPoint(x: w.bounds.minX + CGFloat((r["x"] as? Double ?? 0) / s), y: w.bounds.minY + CGFloat((r["y"] as? Double ?? 0) / s))
        let right = r["button"] as? String == "right"
        let count = (r["double"] as? Bool) == true ? 2 : 1
        for c in 1...count {
            for down in [true, false] {
                let t: CGEventType = right ? (down ? .rightMouseDown : .rightMouseUp) : (down ? .leftMouseDown : .leftMouseUp)
                guard let e = CGEvent(mouseEventSource: nil, mouseType: t, mouseCursorPosition: p, mouseButton: right ? .right : .left) else { continue }
                e.setIntegerValueField(.mouseEventClickState, value: Int64(c))
                e.setIntegerValueField(CGEventField(rawValue: 91)!, value: Int64(w.id))  // the window under the pointer
                e.setIntegerValueField(CGEventField(rawValue: 92)!, value: Int64(w.id))  // and the one that takes it
                post(e, pid: w.pid)
                usleep(20000)
            }
        }
        return "已向窗口发出后台点击。很多程序不接受后台点击，一定用 capture_window 核对；没反应就 request_full_control"
    }

    private func point(_ r: [String: Any], _ kx: String = "x", _ ky: String = "y") -> CGPoint {
        lock.lock(); let m = screenMap; lock.unlock()
        return CGPoint(x: m.origin.x + CGFloat(r[kx] as? Double ?? 0) / m.scale, y: m.origin.y + CGFloat(r[ky] as? Double ?? 0) / m.scale)
    }

    private func mouse(_ op: String, _ r: [String: Any]) throws -> String {
        let p = point(r)
        func send(_ t: CGEventType, _ at: CGPoint, _ b: CGMouseButton = .left, clicks: Int64 = 1) {
            guard let e = CGEvent(mouseEventSource: nil, mouseType: t, mouseCursorPosition: at, mouseButton: b) else { return }
            e.setIntegerValueField(.mouseEventClickState, value: clicks)
            post(e, pid: nil)
        }
        send(.mouseMoved, p)
        usleep(30000)
        switch op {
        case "click":
            let right = r["button"] as? String == "right"
            let count: Int64 = (r["double"] as? Bool) == true ? 2 : 1
            for c in 1...count {
                send(right ? .rightMouseDown : .leftMouseDown, p, right ? .right : .left, clicks: c)
                usleep(30000)
                send(right ? .rightMouseUp : .leftMouseUp, p, right ? .right : .left, clicks: c)
                usleep(30000)
            }
            return "已点击"
        case "drag":
            let to = point(r, "to_x", "to_y")
            send(.leftMouseDown, p)
            for i in 1...12 {
                let f = CGFloat(i) / 12
                send(.leftMouseDragged, CGPoint(x: p.x + (to.x - p.x) * f, y: p.y + (to.y - p.y) * f))
                usleep(16000)
            }
            send(.leftMouseUp, to)
            return "已拖动"
        case "scroll":
            let down = Int32(r["down"] as? Int ?? 0), right = Int32(r["right"] as? Int ?? 0)
            if let e = CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 2, wheel1: -down, wheel2: -right, wheel3: 0) {
                e.location = p
                post(e, pid: nil)
            }
            return "已滚动"
        default: return "已移动鼠标"
        }
    }

    private func activate(_ name: String) throws -> String {
        let a = try findApp(name)
        a.unhide()
        a.activate()
        return "已把 \(a.localizedName ?? "") 切到前台"
    }
}
