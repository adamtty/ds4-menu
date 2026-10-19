// ds4 menu bar item: runs the ds4proxy service as a child (so it inherits this app's
// ~/Documents access, where ds4 and its models live) and shows/controls it.
// Built by build.sh with swiftc (no Xcode project needed), which embeds ds4proxy.py as
// proxySourceBase64: the proxy runs from the signed binary, never from a file anyone can edit.
import AppKit

let control = "http://127.0.0.1:8011"
let stateDir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/share/ds4-menu")
let logURL = stateDir.appendingPathComponent("proxy.log")

/// The state dir must be a real directory we own, not a symlink planted to redirect our writes.
func stateDirIsSafe() -> Bool {
    try? FileManager.default.createDirectory(at: stateDir, withIntermediateDirectories: true,
                                             attributes: [.posixPermissions: 0o700])
    var st = stat()
    guard lstat(stateDir.path, &st) == 0, st.st_mode & S_IFMT == S_IFDIR, st.st_uid == getuid() else { return false }
    chmod(stateDir.path, 0o700)
    return true
}

/// Writes name in the state dir via a fresh O_EXCL | O_NOFOLLOW temp file and a rename, so a
/// planted symlink can't send the write anywhere else.
func writePrivate(_ name: String, _ data: Data, mode: mode_t) throws {
    let tmp = stateDir.appendingPathComponent(".\(name).\(UUID().uuidString)").path
    let fd = open(tmp, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, mode)
    guard fd >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
    let h = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
    h.write(data)
    try h.close()
    guard rename(tmp, stateDir.appendingPathComponent(name).path) == 0 else {
        unlink(tmp)
        throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
    }
}

final class App: NSObject, NSApplicationDelegate, NSMenuDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let menu = NSMenu()
    var proxy: Process?
    var quitting = false
    var restarts = 0
    var status: [String: Any]?

    func applicationDidFinishLaunching(_ n: Notification) {
        menu.delegate = self
        item.menu = menu
        item.button?.imagePosition = .imageLeading
        guard stateDirIsSafe() else {
            alert("ds4", "\(stateDir.path) isn't a directory owned by you (is it a symlink?), so ds4 won't start.")
            NSApp.terminate(nil)
            return
        }
        startProxy()
        redraw()
        Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.refresh() }
    }

    // MARK: the proxy child process

    func startProxy() {
        // the source is part of this signed binary and goes to python over a pipe, so there's no
        // file on disk that another process could swap for its own code
        guard let source = Data(base64Encoded: proxySourceBase64) else { return }
        let fd = open(logURL.path, O_WRONLY | O_APPEND | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0o600)
        let log = fd >= 0 ? FileHandle(fileDescriptor: fd, closeOnDealloc: true) : FileHandle.nullDevice
        let input = Pipe()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        p.arguments = ["-I", "-"]
        // a clean environment: nothing from launchd's (user-editable) LaunchAgent reaches the
        // proxy, and the ds4 path is the one compiled in
        p.environment = [
            "HOME": NSHomeDirectory(),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "DS4_DIR": String(decoding: Data(base64Encoded: ds4DirBase64) ?? Data(), as: UTF8.self),
        ]
        p.standardInput = input
        p.standardOutput = log
        p.standardError = log
        p.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                guard let self = self, !self.quitting else { return }
                // back off if it keeps dying (e.g. port 8010 taken)
                self.restarts += 1
                let delay = min(60.0, 2.0 * Double(self.restarts))
                DispatchQueue.main.asyncAfter(deadline: .now() + delay) { self.startProxy() }
            }
        }
        do {
            try p.run()
            proxy = p
            // python reads the whole script before running it; EOF tells it that's all
            DispatchQueue.global().async {
                input.fileHandleForWriting.write(source)
                try? input.fileHandleForWriting.close()
            }
        } catch { NSLog("ds4: can't start proxy: \(error)") }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let p = proxy, p.isRunning else { return .terminateNow }
        quitting = true
        setIcon("…")
        p.terminate()  // the proxy stops ds4-server (up to 30 s) before exiting
        DispatchQueue.global().async {
            p.waitUntilExit()
            DispatchQueue.main.async { sender.reply(toApplicationShouldTerminate: true) }
        }
        return .terminateLater
    }

    // MARK: talking to it

    func call(_ method: String, _ path: String, _ body: [String: Any]? = nil, done: (([String: Any]?) -> Void)? = nil) {
        var req = URLRequest(url: URL(string: control + path)!)
        req.httpMethod = method
        req.timeoutInterval = method == "GET" ? 5 : 600
        let token = (try? String(contentsOf: stateDir.appendingPathComponent("token"), encoding: .utf8)) ?? ""
        req.setValue(token.trimmingCharacters(in: .whitespacesAndNewlines), forHTTPHeaderField: "X-Token")
        if let body = body { req.httpBody = try? JSONSerialization.data(withJSONObject: body) }
        URLSession.shared.dataTask(with: req) { data, _, _ in
            let s = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            DispatchQueue.main.async {
                if let s = s, s["state"] != nil { self.status = s; self.restarts = 0 } else if method == "GET" { self.status = nil }
                self.redraw()
                done?(s)
            }
        }.resume()
    }

    func refresh() { call("GET", "/status") }

    func post(_ path: String, _ body: [String: Any]? = nil) {
        call("POST", path, body) { r in
            if let err = r?["error"] as? String, !err.isEmpty, r?["state"] == nil { self.alert("ds4", err) }
        }
        refresh()  // shows "loading" while a start is in progress
    }

    // MARK: drawing

    var state: String { status?["state"] as? String ?? "down" }
    var other: [String: Any]? { status?["other_ds4"] as? [String: Any] }
    var error: String { status?["error"] as? String ?? "" }

    // the DS4 wordmark (logo.png / logo@2x.png in Resources, made by make-icon.swift);
    // bright = model loaded, dimmed = not, with a short label for in-between states
    lazy var logo: NSImage? = {
        let img = Bundle.main.image(forResource: "logo")
        img?.isTemplate = true
        img?.accessibilityDescription = "ds4"
        return img
    }()

    func setIcon(_ title: String, dim: Bool = false) {
        item.button?.image = logo
        item.button?.appearsDisabled = dim
        item.button?.title = title.isEmpty ? "" : " " + title
    }

    func redraw() {
        switch state {
        case "ready": setIcon("")
        case "loading": setIcon("\(status?["loading_for"] as? Int ?? 0)s")
        case "stopping": setIcon("…")
        case "idle" where other != nil: setIcon("agent", dim: true)
        case "idle" where !error.isEmpty: setIcon("⚠︎", dim: true)
        case "idle": setIcon("", dim: true)
        default: setIcon("✕", dim: true)
        }
    }

    func menuNeedsUpdate(_ m: NSMenu) {
        refresh()
        m.removeAllItems()
        guard let s = status else {
            m.addItem(info("ds4 service isn't answering — restarting it…"))
            addFooter(m)
            return
        }
        let model = s["model"] as? String ?? ""
        let name = friendly(model)
        switch state {
        case "ready": m.addItem(info("● Ready · \(name)"))
        case "loading": m.addItem(info("◌ Loading \(name)… \(s["loading_for"] as? Int ?? 0)s"))
        case "stopping": m.addItem(info("◌ Unloading…"))
        default:
            if let o = other {
                m.addItem(info("○ Paused — another ds4 is running (pid \(o["pid"] ?? "?"))"))
                m.addItem(info("   " + shortCommand(o["command"] as? String ?? "")))
            } else {
                m.addItem(info("○ Idle · \(name) loads on first request"))
            }
        }
        if !error.isEmpty && other == nil { m.addItem(info("⚠︎ " + wrap(error))) }
        if state == "ready" {
            if s["busy"] as? Bool == true {
                m.addItem(info("   Working on a request"))
            } else if s["pinned"] as? Bool == true {
                m.addItem(info("   Kept loaded"))
            } else if let left = s["unload_in"] as? Int {
                m.addItem(info("   Unloads in \(max(1, (left + 59) / 60)) min if idle"))
            }
        }
        if let sp = s["speeds"] as? [String: Any], !sp.isEmpty {
            let pre = (sp["prefill"] as? Double).map { String(format: "prompt %.0f", $0) }
            let gen = (sp["gen"] as? Double).map { String(format: "reply %.1f", $0) }
            m.addItem(info("   Last request: " + [pre, gen].compactMap { $0 }.joined(separator: " · ") + " tok/s"))
        }
        let gb = { (k: String) in (s[k] as? Double ?? Double(s[k] as? Int ?? 0)) / 1e9 }
        m.addItem(info(String(format: "   Model %.0f GB%@ · Mac has %.0f GB free",
                              gb("model_bytes"), s["streaming"] as? Bool == true ? " (SSD streaming)" : "", gb("free_bytes"))))
        let endpoint = s["endpoint"] as? String ?? ""
        m.addItem(action("Copy Endpoint  \(endpoint)", #selector(copyEndpoint)))
        m.addItem(.separator())

        if state == "ready" || state == "loading" {
            m.addItem(action("Unload Now", #selector(unloadNow)))
        } else if other == nil {
            m.addItem(action("Load Now", #selector(loadNow)))
        }
        let pin = action("Keep Loaded", #selector(togglePin))
        pin.state = s["pinned"] as? Bool == true ? .on : .off
        m.addItem(pin)

        let idle = NSMenuItem(title: "Unload After", action: nil, keyEquivalent: "")
        idle.submenu = NSMenu()
        for mins in [5, 15, 30, 60, 120] {
            let it = action(mins < 60 ? "\(mins) min idle" : "\(mins / 60) h idle", #selector(setIdle(_:)))
            it.tag = mins
            it.state = s["idle_min"] as? Int == mins ? .on : .off
            idle.submenu?.addItem(it)
        }
        m.addItem(idle)

        let models = NSMenuItem(title: "Model", action: nil, keyEquivalent: "")
        models.submenu = NSMenu()
        for md in s["models"] as? [[String: Any]] ?? [] {
            let file = md["file"] as? String ?? ""
            let size = (md["bytes"] as? Double ?? Double(md["bytes"] as? Int ?? 0)) / 1e9
            let streaming = md["streaming"] as? Bool == true ? " · SSD streaming" : ""
            let it = action(String(format: "%@  —  %@, %.0f GB%@", friendly(file), quant(file), size, streaming), #selector(pickModel(_:)))
            it.representedObject = file
            it.toolTip = file
            it.state = file == model ? .on : .off
            models.submenu?.addItem(it)
        }
        m.addItem(models)
        m.addItem(.separator())
        m.addItem(action("Open ds4 Agent in Folder…", #selector(openAgent)))
        addFooter(m)
    }

    func addFooter(_ m: NSMenu) {
        m.addItem(action("Open Log", #selector(openLog)))
        m.addItem(.separator())
        m.addItem(action("Quit ds4", #selector(quit)))
    }

    func info(_ t: String) -> NSMenuItem {
        let it = NSMenuItem(title: t, action: nil, keyEquivalent: "")
        it.isEnabled = false
        return it
    }

    func action(_ t: String, _ sel: Selector) -> NSMenuItem {
        let it = NSMenuItem(title: t, action: sel, keyEquivalent: "")
        it.target = self
        return it
    }

    // "DeepSeek-V4-Flash-Layers37-42Q4K…gguf" → "DeepSeek V4 Flash"
    func friendly(_ file: String) -> String {
        let stop = try! NSRegularExpression(pattern: "^(I?Q\\d|Layers|MXFP|BF16|F16|FP8|UD|imatrix|chat|Other)", options: .caseInsensitive)
        var out: [String] = []
        for tok in file.replacingOccurrences(of: ".gguf", with: "").split(separator: "-").map(String.init) {
            if out.count == 4 || stop.firstMatch(in: tok, range: NSRange(tok.startIndex..., in: tok)) != nil { break }
            out.append(tok)
        }
        return out.isEmpty ? file : out.joined(separator: " ")
    }

    func quant(_ file: String) -> String {
        if file.contains("Q4K") && file.contains("IQ2") { return "Q2/Q4 mix" }
        let re = try! NSRegularExpression(pattern: "(IQ\\d_[A-Z]+|Q\\d_K(_[A-Z]+)?|Q\\d|MXFP4|FP8)")
        if let m = re.firstMatch(in: file, range: NSRange(file.startIndex..., in: file)), let r = Range(m.range, in: file) {
            return String(file[r])
        }
        return "gguf"
    }

    func shortCommand(_ c: String) -> String {
        c.replacingOccurrences(of: FileManager.default.homeDirectoryForCurrentUser.path, with: "~").prefix(70).description
    }

    func wrap(_ s: String) -> String { s.count > 90 ? String(s.prefix(90)) + "…" : s }

    // MARK: actions

    @objc func loadNow() { post("/start") }
    @objc func unloadNow() { post("/stop") }
    @objc func togglePin() { post("/settings", ["pinned": !(status?["pinned"] as? Bool ?? false)]) }
    @objc func setIdle(_ it: NSMenuItem) { post("/settings", ["idle_min": it.tag]) }
    @objc func pickModel(_ it: NSMenuItem) { post("/settings", ["model": it.representedObject as? String ?? ""]) }

    @objc func copyEndpoint() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString((status?["endpoint"] as? String ?? "http://127.0.0.1:8010") + "/v1", forType: .string)
    }

    /// ds4-agent and the server can't run at the same time, so unload first, then start the
    /// agent in Terminal via a .command file (no Automation permission needed).
    @objc func openAgent() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.prompt = "Open Agent Here"
        panel.message = "Pick the project folder for ds4-agent. The menu's server is unloaded while the agent runs."
        NSApp.activate(ignoringOtherApps: true)
        guard panel.runModal() == .OK, let dir = panel.url?.path else { return }
        let quote = { (s: String) in "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'" }
        let ds4 = status?["ds4_dir"] as? String ?? ""
        let extra = (status?["agent_args"] as? [String] ?? []).map(quote).joined(separator: " ")
        let cmd = stateDir.appendingPathComponent("agent.command")
        // the agent loads its model and Metal kernels relative to the ds4 checkout
        let script = "#!/bin/zsh\ncd \(quote(ds4)) && exec ./ds4-agent --chdir \(quote(dir)) \(extra)\n"
        do {
            try writePrivate("agent.command", Data(script.utf8), mode: 0o700)
        } catch { alert("ds4", "Couldn't write \(cmd.path): \(error.localizedDescription)"); return }
        call("POST", "/stop") { _ in NSWorkspace.shared.open(cmd) }
    }

    @objc func openLog() { NSWorkspace.shared.open(logURL) }
    @objc func quit() { NSApp.terminate(nil) }

    func alert(_ title: String, _ text: String) {
        let a = NSAlert()
        a.messageText = title
        a.informativeText = text
        NSApp.activate(ignoringOtherApps: true)
        a.runModal()
    }
}

// launchd (logout, install.sh) sends SIGTERM; go through the normal quit so the model unloads cleanly
signal(SIGTERM, SIG_IGN)
let sigterm = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
sigterm.setEventHandler { NSApp.terminate(nil) }
sigterm.resume()

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
