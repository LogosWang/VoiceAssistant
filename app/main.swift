// Carnot desktop app: a floating character with subtitles, on every desktop and over full-screen
// apps. It runs server/voice_server.py as a child process and restarts it if it dies; the server
// reports over stdout ("@event {json}" lines, see events.py) and everything else goes to the log.
//
//   ./build.sh    builds /Applications/Carnot.app and enables launch at login
//   Carnot --wav clip.wav --dry-run    other server arguments replace the default --mic (testing)
import AVFoundation
import AppKit
import ServiceManagement
import WebKit

let winSize = NSSize(width: 250, height: 266)
let charRect = NSRect(x: 45, y: 46, width: 160, height: 208)   // the parts that take the mouse: the character,
let barRect = NSRect(x: 0, y: 0, width: 250, height: 46)       // and the input box under it
let chipsHeight: CGFloat = 26                                  // attachments sit just above the box, if any
let defaultArgs = ["--mic"]
let logURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/Carnot/carnot.log")

final class Panel: NSPanel {
    var wantsKeyboard = false  // only for the input box; otherwise never takes focus
    override var canBecomeKey: Bool { wantsKeyboard }
    override var canBecomeMain: Bool { false }
}

/// Transparent layer over the character: click for the input box, drag to move, right-click for the menu.
final class Handle: NSView {
    var menuProvider: () -> NSMenu = { NSMenu() }
    var onMoved: () -> Void = {}
    var onClick: () -> Void = {}
    var passBelow: CGFloat = 0  // this much of the bottom edge lets clicks through, to the attachments
    private var grab = NSPoint.zero
    private var start = NSPoint.zero

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func hitTest(_ point: NSPoint) -> NSView? {
        point.y < frame.minY + passBelow ? nil : super.hitTest(point)
    }
    override func mouseDown(with event: NSEvent) {
        guard let w = window else { return }
        let m = NSEvent.mouseLocation
        start = m
        grab = NSPoint(x: m.x - w.frame.minX, y: m.y - w.frame.minY)
    }
    override func mouseDragged(with event: NSEvent) {
        let m = NSEvent.mouseLocation
        window?.setFrameOrigin(NSPoint(x: m.x - grab.x, y: m.y - grab.y))
    }
    override func mouseUp(with event: NSEvent) {
        let m = NSEvent.mouseLocation
        hypot(m.x - start.x, m.y - start.y) < 4 ? onClick() : onMoved()
    }
    override func rightMouseDown(with event: NSEvent) {
        NSMenu.popUpContextMenu(menuProvider(), with: event, for: self)
    }
}

/// Takes files and folders dropped on the input box. The page itself wouldn't see their paths.
final class DropWeb: WKWebView {
    var onDrop: ([String]) -> Void = { _ in }

    private func paths(_ info: NSDraggingInfo) -> [String] {
        let urls = info.draggingPasteboard.readObjects(forClasses: [NSURL.self],
                                                       options: [.urlReadingFileURLsOnly: true]) as? [URL]
        return urls?.map(\.path) ?? []
    }
    override func draggingEntered(_ info: NSDraggingInfo) -> NSDragOperation { paths(info).isEmpty ? [] : .copy }
    override func draggingUpdated(_ info: NSDraggingInfo) -> NSDragOperation { paths(info).isEmpty ? [] : .copy }
    override func performDragOperation(_ info: NSDraggingInfo) -> Bool {
        onDrop(paths(info))
        return true
    }
}

/// voice_server.py as a child process, restarted with a growing delay if it exits.
final class Server {
    let dir: String
    let args: [String]
    var onEvent: (String) -> Void = { _ in }
    var onExit: () -> Void = {}
    private var proc: Process?
    private var stdin: Pipe?
    private var buf = Data()
    private var stopping = false
    private var delay = 3.0
    private var startedAt = Date()
    private let log: FileHandle?

    init(dir: String, args: [String]) {
        self.dir = dir
        self.args = args
        let fm = FileManager.default
        try? fm.createDirectory(at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        if let size = (try? fm.attributesOfItem(atPath: logURL.path))?[.size] as? Int, size > 5_000_000 {
            let old = logURL.appendingPathExtension("old")
            try? fm.removeItem(at: old)
            try? fm.moveItem(at: logURL, to: old)
        }
        if !fm.fileExists(atPath: logURL.path) { fm.createFile(atPath: logURL.path, contents: nil) }
        log = try? FileHandle(forWritingTo: logURL)
        _ = try? log?.seekToEnd()
    }

    func start() {
        stopping = false
        // A login shell, because apps launched from Finder don't get the PATH with `claude` on it.
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/zsh")
        p.arguments = ["-lc", "exec .venv/bin/python -u voice_server.py --ui \"$@\"", "carnot"] + args
        p.currentDirectoryURL = URL(fileURLWithPath: dir)
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        let input = Pipe()
        p.standardInput = input
        stdin = input
        pipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let d = h.availableData
            if d.isEmpty { h.readabilityHandler = nil } else { self?.received(d) }
        }
        p.terminationHandler = { [weak self] q in
            DispatchQueue.main.async { self?.exited(q.terminationStatus) }
        }
        write("--- starting server: \(args.joined(separator: " "))\n")
        startedAt = Date()
        proc = p
        do { try p.run() } catch {
            write("cannot start server: \(error)\n")
            exited(-1)
        }
    }

    /// A command from the app (typed text, settings). False if the service isn't running.
    /// These lines aren't logged: API keys pass through here.
    func send(_ command: [String: Any]) -> Bool {
        guard let p = proc, p.isRunning, let h = stdin?.fileHandleForWriting,
              var line = try? JSONSerialization.data(withJSONObject: command) else { return false }
        line.append(10)
        return (try? h.write(contentsOf: line)) != nil
    }

    func stop() {
        stopping = true
        guard let p = proc, p.isRunning else { return }
        p.terminate()
        let deadline = Date().addingTimeInterval(3)
        while p.isRunning && Date() < deadline { usleep(50_000) }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
    }

    func restart() {
        stop()
        delay = 3
        start()
    }

    private func exited(_ status: Int32) {
        write("--- server exited with status \(status)\n")
        onExit()
        guard !stopping, args == defaultArgs else { return }
        if Date().timeIntervalSince(startedAt) > 60 { delay = 3 }
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            if self?.stopping == false { self?.start() }
        }
        delay = min(delay * 2, 60)
    }

    private func received(_ d: Data) {
        buf.append(d)
        while let nl = buf.firstIndex(of: 10) {
            let line = String(decoding: buf[buf.startIndex..<nl], as: UTF8.self)
            buf.removeSubrange(buf.startIndex...nl)
            write(line + "\n")
            if let r = line.range(of: "@event ") {  // a log line from another thread may precede it
                let json = String(line[r.upperBound...])
                DispatchQueue.main.async { self.onEvent(json) }
            }
        }
    }

    private func write(_ s: String) {
        try? log?.write(contentsOf: Data(s.utf8))
    }
}

final class App: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKScriptMessageHandler, NSMenuDelegate, NSWindowDelegate {
    let serverArgs: [String]
    var panel: Panel!
    var web: DropWeb!
    var settings: NSWindow?
    var settingsWeb: WKWebView?
    var settingsReady = false
    var handle: Handle!
    var server: Server!
    var status: NSStatusItem!
    var pageReady = false
    var pending = [String]()
    var inputOpen = false
    var sessionItems = [[String: Any]]()
    var currentSession = ""
    var chipsShown = false
    var computer: ComputerUse!

    init(serverArgs: [String]) { self.serverArgs = serverArgs }

    func applicationDidFinishLaunching(_ n: Notification) {
        makeEditMenu()
        makePanel()
        computer = ComputerUse(notify: { [weak self] in self?.send($0) })
        computer.start()
        status = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        status.button?.image = NSImage(systemSymbolName: "waveform.circle", accessibilityDescription: "Carnot")
        status.menu = makeMenu()
        NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification,
                                               object: nil, queue: .main) { [weak self] _ in self?.placeOnScreen() }
        // The window lets clicks through to whatever is behind it, except over the character.
        Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] _ in
            guard let p = self?.panel else { return }
            let hit = charRect.offsetBy(dx: p.frame.minX, dy: p.frame.minY)
            let bar = barRect.offsetBy(dx: p.frame.minX, dy: p.frame.minY)
            let chips = NSRect(x: p.frame.minX, y: p.frame.minY + barRect.height, width: winSize.width, height: chipsHeight)
            let m = NSEvent.mouseLocation
            let open = self?.inputOpen == true
            p.wantsKeyboard = open || bar.contains(m)  // so the click on the box can give it the keyboard
            p.ignoresMouseEvents = !(hit.contains(m) || bar.contains(m) || (self?.chipsShown == true && chips.contains(m)))
        }

        let dir = ((Bundle.main.object(forInfoDictionaryKey: "CarnotServerDir") as? String ?? "") as NSString).expandingTildeInPath
        server = Server(dir: dir, args: serverArgs)
        server.onEvent = { [weak self] json in
            self?.send(json)
            if json.contains("\"open_settings\"") { self?.openSettings() }
            if json.contains("\"sessions\""), let d = json.data(using: .utf8),
               let ev = try? JSONSerialization.jsonObject(with: d) as? [String: Any], ev["type"] as? String == "sessions" {
                self?.sessionItems = ev["items"] as? [[String: Any]] ?? []
                self?.currentSession = ev["current"] as? String ?? ""
            }
        }
        server.onExit = { [weak self] in self?.send(#"{"type":"state","state":"offline"}"#) }
        guard serverArgs == defaultArgs else { return server.start() }

        // One microphone, one listener: a server left over in a terminal would answer as well.
        let pk = Process()
        pk.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        pk.arguments = ["-f", "python.*voice_server\\.py"]
        try? pk.run()
        pk.waitUntilExit()
        AVCaptureDevice.requestAccess(for: .audio) { granted in
            DispatchQueue.main.async {
                if !granted { self.send(#"{"type":"notice","text":"没有麦克风权限，请在系统设置 → 隐私与安全性里允许 Carnot"}"#) }
                if !CGPreflightScreenCaptureAccess() { _ = CGRequestScreenCaptureAccess() }
                self.server.start()
            }
        }
    }

    func applicationWillTerminate(_ n: Notification) { server.stop() }

    /// Prefers a copy outside the bundle: changing the look then doesn't need a rebuild, which would
    /// change the signature and make macOS ask for the permissions again. It isn't read from the
    /// source folder because that's on the Desktop, and a permission prompt there blocks this thread.
    private func pageURL(_ name: String) -> URL {
        let support = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Carnot/\(name).html")
        return FileManager.default.fileExists(atPath: support.path) ? support
            : Bundle.main.url(forResource: name, withExtension: "html")!
    }

    /// An app with no menu bar gets no copy and paste unless the menu exists.
    private func makeEditMenu() {
        let edit = NSMenu(title: "Edit")
        for (title, action, key) in [("Undo", "undo:", "z"), ("Redo", "redo:", "Z"), ("Cut", "cut:", "x"),
                                     ("Copy", "copy:", "c"), ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")] {
            edit.addItem(NSMenuItem(title: title, action: Selector(action), keyEquivalent: key))
        }
        let item = NSMenuItem()
        item.submenu = edit
        let main = NSMenu()
        main.addItem(item)
        NSApp.mainMenu = main
    }

    private func makePanel() {
        panel = Panel(contentRect: NSRect(origin: .zero, size: winSize),
                      styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.hidesOnDeactivate = false
        panel.level = .statusBar
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        panel.delegate = self

        let conf = WKWebViewConfiguration()
        conf.userContentController.add(self, name: "carnot")
        web = DropWeb(frame: NSRect(origin: .zero, size: winSize), configuration: conf)
        web.registerForDraggedTypes([.fileURL])
        web.onDrop = { [weak self] in self?.attach($0) }
        web.setValue(false, forKey: "drawsBackground")
        web.navigationDelegate = self
        web.autoresizingMask = [.width, .height]
        let page = pageURL("index")
        web.loadFileURL(page, allowingReadAccessTo: page.deletingLastPathComponent())

        handle = Handle(frame: charRect)
        handle.menuProvider = { [unowned self] in self.makeMenu() }
        handle.onClick = { [unowned self] in self.setInput(true) }
        handle.onMoved = { [unowned self] in
            UserDefaults.standard.set(NSStringFromPoint(self.panel.frame.origin), forKey: "originSmall")
        }
        let root = NSView(frame: NSRect(origin: .zero, size: winSize))
        root.addSubview(web)
        root.addSubview(handle)
        panel.contentView = root
        placeOnScreen()
        if !UserDefaults.standard.bool(forKey: "hideFloating") { panel.orderFrontRegardless() }
    }

    /// Saved position if any of the character is still on a screen, else the main screen's corner.
    private func placeOnScreen() {
        if let s = UserDefaults.standard.string(forKey: "originSmall") {
            let o = NSPointFromString(s)
            let hit = charRect.offsetBy(dx: o.x, dy: o.y)
            if NSScreen.screens.contains(where: { $0.visibleFrame.intersection(hit).width > 60 }) {
                panel.setFrameOrigin(o)
                return
            }
        }
        guard let v = (NSScreen.main ?? NSScreen.screens.first)?.visibleFrame else { return }
        panel.setFrameOrigin(NSPoint(x: v.maxX - winSize.width - 12, y: v.minY))
    }

    func webView(_ w: WKWebView, didFinish n: WKNavigation!) {
        if w === settingsWeb {
            settingsReady = true
            return
        }
        pageReady = true
        pending.forEach(send)
        pending = []
    }

    private func send(_ json: String) {
        guard pageReady else { return pending.append(json) }
        web.evaluateJavaScript("carnot(\(json))")
        if settingsReady { settingsWeb?.evaluateJavaScript("carnot(\(json))") }
    }

    // MARK: typed input

    func setInput(_ open: Bool) {
        inputOpen = open
        panel.wantsKeyboard = open
        if open {
            showFloating(true)
            panel.makeKey()  // the panel is non-activating: the app you're in stays frontmost
            panel.makeFirstResponder(web)
        } else if panel.isKeyWindow {
            panel.resignKey()
        }
        send(#"{"type":"input","open":\#(open)}"#)
    }

    func windowDidResignKey(_ n: Notification) {
        if n.object as? Panel != nil && inputOpen { setInput(false) }  // clicked elsewhere
    }

    private func attach(_ paths: [String]) {
        guard !paths.isEmpty, let data = try? JSONSerialization.data(withJSONObject: ["type": "attached", "paths": paths])
        else { return }
        send(String(decoding: data, as: UTF8.self))
    }

    private func pickFiles() {
        let open = NSOpenPanel()
        open.canChooseFiles = true
        open.canChooseDirectories = true
        open.allowsMultipleSelection = true
        open.prompt = "附加"
        open.level = NSWindow.Level(rawValue: panel.level.rawValue + 1)  // above the character
        NSApp.activate(ignoringOtherApps: true)
        if open.runModal() == .OK { attach(open.urls.map(\.path)) }
    }

    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        guard let body = m.body as? [String: Any], let type = body["type"] as? String else { return }
        switch type {
        case "closeInput": setInput(false)
        case "focused":
            inputOpen = true
            panel.wantsKeyboard = true
        case "attach": pickFiles()
        case "chips":
            chipsShown = (body["count"] as? Int ?? 0) > 0
            handle.passBelow = chipsShown ? chipsHeight : 0
        case "settings": openSettings()
        case "schedules": openSchedules()
        case "menu": showPickMenu(body["items"] as? [[String: Any]] ?? [])
        case "open":
            if let s = body["url"] as? String, let u = URL(string: s), u.scheme == "https" { NSWorkspace.shared.open(u) }
        case "server":
            if let payload = body["payload"] as? [String: Any], !server.send(payload) {
                send(#"{"type":"notice","text":"语音服务还没启动好，稍等一下再试","hold":5000}"#)
            }
        default: break
        }
    }

    @objc func openSettings() {
        if settings == nil {
            let conf = WKWebViewConfiguration()
            conf.userContentController.add(self, name: "carnot")
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: 680),
                             styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
            w.title = "Carnot 连接设置"
            w.isReleasedWhenClosed = false
            w.level = .floating
            w.center()
            let v = WKWebView(frame: w.contentView!.bounds, configuration: conf)
            v.autoresizingMask = [.width, .height]
            v.navigationDelegate = self
            let page = pageURL("settings")
            v.loadFileURL(page, allowingReadAccessTo: page.deletingLastPathComponent())
            w.contentView!.addSubview(v)
            settings = w
            settingsWeb = v
        } else {
            _ = server.send(["type": "providers"])
        }
        NSApp.activate(ignoringOtherApps: true)
        settings?.makeKeyAndOrderFront(nil)
    }

    // MARK: menu (status bar and right-click on the character)

    private func makeMenu() -> NSMenu {
        let m = NSMenu()
        m.delegate = self
        fillMenu(m)
        return m
    }

    /// Conversations, newest first, each with what can be done to it.
    private func sessionsMenu() -> NSMenu {
        let m = NSMenu()
        let new = NSMenuItem(title: "新对话", action: #selector(newSession), keyEquivalent: "")
        new.target = self
        m.addItem(new)
        if !sessionItems.isEmpty { m.addItem(.separator()) }
        let clients = ["telegram": "Telegram · ", "feishu": "飞书 · ", "vscode": "VS Code · ", "schedule": "定时 · "]
        for s in sessionItems.prefix(20) {
            guard let id = s["id"] as? String else { continue }
            let title = s["title"] as? String ?? "对话"
            let item = NSMenuItem(title: (clients[s["client"] as? String ?? ""] ?? "") + title, action: nil, keyEquivalent: "")
            item.state = id == currentSession ? .on : .off
            let sub = NSMenu()
            for (label, sel) in [("继续这个对话", #selector(useSession(_:))), ("重命名…", #selector(renameSession(_:))),
                                 ("删除…", #selector(deleteSession(_:)))] {
                let a = NSMenuItem(title: label, action: sel, keyEquivalent: "")
                a.target = self
                a.representedObject = ["id": id, "title": title]
                sub.addItem(a)
            }
            item.submenu = sub
            m.addItem(item)
        }
        return m
    }

    private func fillMenu(_ m: NSMenu) {
        @discardableResult func add(_ title: String, _ sel: Selector) -> NSMenuItem {
            let i = NSMenuItem(title: title, action: sel, keyEquivalent: "")
            i.target = self
            m.addItem(i)
            return i
        }
        add("显示悬浮窗", #selector(toggleVisible)).state = panel.isVisible ? .on : .off
        add("输入文字…", #selector(openInput))
        let conversations = NSMenuItem(title: "会话", action: nil, keyEquivalent: "")
        conversations.submenu = sessionsMenu()
        m.addItem(conversations)
        add("连接设置…", #selector(openSettings))
        add("定时任务…", #selector(openSchedules))
        add("远程机器人…", #selector(openBots))
        add("重启语音服务", #selector(restartServer))
        add("开机自启", #selector(toggleLogin)).state = SMAppService.mainApp.status == .enabled ? .on : .off
        m.addItem(.separator())
        add("打开设置文件", #selector(openConfig))
        add("打开日志", #selector(openLog))
        m.addItem(.separator())
        add("退出 Carnot", #selector(quit))
    }

    func menuNeedsUpdate(_ m: NSMenu) {
        guard m.items.contains(where: { $0.action == #selector(quit) }) else { return }
        m.removeAllItems()
        fillMenu(m)
    }

    @objc func newSession() { _ = server.send(["type": "session_new"]) }
    @objc func useSession(_ i: NSMenuItem) {
        guard let s = i.representedObject as? [String: String] else { return }
        _ = server.send(["type": "session_use", "id": s["id"] ?? ""])
    }
    @objc func renameSession(_ i: NSMenuItem) {
        guard let s = i.representedObject as? [String: String] else { return }
        let alert = NSAlert()
        alert.messageText = "重命名这个对话"
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
        field.stringValue = s["title"] ?? ""
        alert.accessoryView = field
        alert.addButton(withTitle: "确定")
        alert.addButton(withTitle: "取消")
        alert.window.level = NSWindow.Level(rawValue: panel.level.rawValue + 1)
        alert.window.initialFirstResponder = field
        NSApp.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertFirstButtonReturn {
            _ = server.send(["type": "session_rename", "id": s["id"] ?? "", "title": field.stringValue])
        }
    }
    @objc func deleteSession(_ i: NSMenuItem) {
        guard let s = i.representedObject as? [String: String] else { return }
        let alert = NSAlert()
        alert.messageText = "删除“\(s["title"] ?? "")”？"
        alert.informativeText = "只从列表里移除，之后不能再继续这个对话。"
        alert.addButton(withTitle: "删除")
        alert.addButton(withTitle: "取消")
        alert.window.level = NSWindow.Level(rawValue: panel.level.rawValue + 1)
        NSApp.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertFirstButtonReturn {
            _ = server.send(["type": "session_delete", "id": s["id"] ?? ""])
        }
    }
    /** A menu the page describes: it can't be cut off by the small floating window. */
    func showPickMenu(_ items: [[String: Any]]) {
        let m = NSMenu()
        m.autoenablesItems = false
        for it in items {
            if it["separator"] != nil { m.addItem(.separator()); continue }
            let item = NSMenuItem(title: it["title"] as? String ?? "", action: #selector(pickFromMenu(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = it["payload"] ?? it["action"]
            item.isEnabled = item.representedObject != nil
            item.state = it["on"] as? Bool == true ? .on : .off
            m.addItem(item)
        }
        m.popUp(positioning: nil, at: NSEvent.mouseLocation, in: nil)
    }

    @objc func pickFromMenu(_ item: NSMenuItem) {
        if let payload = item.representedObject as? [String: Any] {
            _ = server.send(payload)
            // turning computer use on is the moment to ask for the permission it needs
            if payload["type"] as? String == "computer_set", payload["mode"] as? String != "off", !AXIsProcessTrusted() {
                _ = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
            }
        }
        if item.representedObject as? String == "settings" { openSettings() }
        if item.representedObject as? String == "schedules" { openSchedules() }
    }

    @objc func openSchedules() {
        openSettings()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
            self.settingsWeb?.evaluateJavaScript("document.getElementById('schedules').scrollIntoView()")
        }
    }
    @objc func openBots() {
        openSettings()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
            self.settingsWeb?.evaluateJavaScript("document.getElementById('bots').scrollIntoView()")
        }
    }

    @objc func openInput() { setInput(true) }
    @objc func toggleVisible() { showFloating(!panel.isVisible) }
    /// Remembered, so a hidden window stays hidden after a restart. Carnot still listens while it's hidden.
    func showFloating(_ show: Bool) {
        UserDefaults.standard.set(!show, forKey: "hideFloating")
        if show { panel.orderFrontRegardless() } else { if inputOpen { setInput(false) }; panel.orderOut(nil) }
    }
    @objc func restartServer() { server.restart() }
    @objc func toggleLogin() {
        let s = SMAppService.mainApp
        try? s.status == .enabled ? s.unregister() : s.register()
    }
    @objc func openConfig() { NSWorkspace.shared.open(URL(fileURLWithPath: server.dir + "/config.toml")) }
    @objc func openLog() { NSWorkspace.shared.open(logURL) }
    @objc func quit() { NSApp.terminate(nil) }
}

let cli = Array(CommandLine.arguments.dropFirst().filter { !$0.hasPrefix("-psn_") })
if cli == ["--enable-login"] {  // build.sh: register without starting anything
    do { try SMAppService.mainApp.register() } catch { print("login item: \(error)") }
    print("login item status: \(SMAppService.mainApp.status.rawValue) (1 = enabled, 2 = needs approval in System Settings)")
    exit(0)
}
signal(SIGPIPE, SIG_IGN)  // writing to a server that just died is an error, not fatal
let app = NSApplication.shared
if cli == ["--computer-serve"] {  // testing: only the computer-use socket, no window and no service
    let cu = ComputerUse(notify: { print("notice \($0)") })
    cu.start()
    app.setActivationPolicy(.accessory)
    app.run()
}
let delegate = App(serverArgs: cli.isEmpty ? defaultArgs : cli)
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
