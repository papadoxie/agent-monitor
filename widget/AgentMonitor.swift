// Agent Monitor desktop widget: a translucent panel on the desktop plus a menu bar counter.
// Serves the dashboard itself by launching the bundled server.py if nothing is listening yet.
import Cocoa
import WebKit

let port = 8787
let base = "http://127.0.0.1:\(port)"

final class Panel: NSPanel {
    override var canBecomeKey: Bool { true }
}

/// Transparent strip along the top of the widget so it can be dragged.
final class DragStrip: NSView {
    override var mouseDownCanMoveWindow: Bool { true }
}

final class App: NSObject, NSApplicationDelegate {
    var panel: Panel!
    var web: WKWebView!
    var item: NSStatusItem!
    var server: Process?
    let floatKey = "floatAboveWindows"

    func applicationDidFinishLaunching(_ n: Notification) {
        NSApp.setActivationPolicy(.accessory)
        startServerIfNeeded()
        buildPanel()
        buildMenu()
        Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in self?.refreshCount() }
        refreshCount()
    }

    func applicationWillTerminate(_ n: Notification) { server?.terminate() }

    // MARK: server

    func serverUp() -> Bool {
        var ok = false
        let sem = DispatchSemaphore(value: 0)
        var req = URLRequest(url: URL(string: base + "/api/agents")!)
        req.timeoutInterval = 0.8
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            ok = (resp as? HTTPURLResponse)?.statusCode == 200
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 1.2)
        return ok
    }

    func startServerIfNeeded() {
        if serverUp() { return }
        guard let script = Bundle.main.path(forResource: "server", ofType: "py") else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        p.arguments = [script, "--port", String(port)]
        p.environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": NSHomeDirectory()]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        try? p.run()
        server = p
        for _ in 0..<20 { if serverUp() { break }; usleep(150_000) }
    }

    // MARK: panel

    func applyLevel() {
        let floating = UserDefaults.standard.bool(forKey: floatKey)
        panel.level = floating ? .floating
            : NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.desktopIconWindow)) + 1)
    }

    func buildPanel() {
        let size = NSSize(width: 360, height: 500)
        panel = Panel(contentRect: NSRect(origin: .zero, size: size),
                      styleMask: [.borderless, .nonactivatingPanel, .resizable],
                      backing: .buffered, defer: false)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.minSize = NSSize(width: 280, height: 220)
        panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle]
        panel.setFrameAutosaveName("AgentMonitorWidget")

        let fx = NSVisualEffectView(frame: NSRect(origin: .zero, size: size))
        fx.material = .hudWindow
        fx.blendingMode = .behindWindow
        fx.state = .active
        fx.wantsLayer = true
        fx.layer?.cornerRadius = 18
        fx.layer?.masksToBounds = true
        fx.autoresizingMask = [.width, .height]
        panel.contentView = fx

        let cfg = WKWebViewConfiguration()
        web = WKWebView(frame: fx.bounds, configuration: cfg)
        web.autoresizingMask = [.width, .height]
        web.setValue(false, forKey: "drawsBackground")
        web.underPageBackgroundColor = .clear
        fx.addSubview(web)
        web.load(URLRequest(url: URL(string: base + "/?view=widget")!))

        let strip = DragStrip(frame: NSRect(x: 0, y: size.height - 34, width: size.width, height: 34))
        strip.autoresizingMask = [.width, .minYMargin]
        fx.addSubview(strip)

        if !panel.setFrameUsingName("AgentMonitorWidget"), let s = NSScreen.main?.visibleFrame {
            panel.setFrameOrigin(NSPoint(x: s.maxX - size.width - 24, y: s.maxY - size.height - 24))
        }
        applyLevel()
        panel.orderFront(nil)
    }

    // MARK: menu bar

    func buildMenu() {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.image = NSImage(systemSymbolName: "cpu", accessibilityDescription: "Agent Monitor")
        item.button?.imagePosition = .imageLeft
        let m = NSMenu()
        m.addItem(withTitle: "Show / Hide Widget", action: #selector(toggle), keyEquivalent: "").target = self
        let f = m.addItem(withTitle: "Float Above Windows", action: #selector(toggleFloat(_:)), keyEquivalent: "")
        f.target = self
        f.state = UserDefaults.standard.bool(forKey: floatKey) ? .on : .off
        m.addItem(withTitle: "Open Full Dashboard", action: #selector(openFull), keyEquivalent: "").target = self
        m.addItem(withTitle: "Refresh", action: #selector(reload), keyEquivalent: "").target = self
        m.addItem(.separator())
        m.addItem(withTitle: "Quit Agent Monitor", action: #selector(quit), keyEquivalent: "q").target = self
        item.menu = m
    }

    @objc func toggle() { panel.isVisible ? panel.orderOut(nil) : panel.orderFront(nil) }
    @objc func toggleFloat(_ s: NSMenuItem) {
        let on = !UserDefaults.standard.bool(forKey: floatKey)
        UserDefaults.standard.set(on, forKey: floatKey)
        s.state = on ? .on : .off
        applyLevel()
    }
    @objc func openFull() { NSWorkspace.shared.open(URL(string: base)!) }
    @objc func reload() { web.reload() }
    @objc func quit() { NSApp.terminate(nil) }

    func refreshCount() {
        var req = URLRequest(url: URL(string: base + "/api/agents")!)
        req.timeoutInterval = 2
        URLSession.shared.dataTask(with: req) { [weak self] data, _, _ in
            guard let data = data,
                  let j = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let agents = j["agents"] as? [[String: Any]] else { return }
            let active = agents.filter { ($0["status"] as? String) == "active" }.count
            DispatchQueue.main.async { self?.item.button?.title = " \(active)/\(agents.count)" }
        }.resume()
    }
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.run()
