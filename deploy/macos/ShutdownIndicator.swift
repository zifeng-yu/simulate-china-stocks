import AppKit
import Darwin
import Foundation

private let interval: TimeInterval = 30
private let timeout: TimeInterval = 8
private let diagnosticLimit = 1024 * 1024

private func safeStack(_ data: Data) -> [String] {
    let text = String(decoding: data.suffix(16 * 1024), as: UTF8.self)
    return text.components(separatedBy: .newlines).filter {
        $0.range(of: #"^\s*File \"[^\"]+\", line [0-9]+, in [A-Za-z0-9_<>.]+\s*$"#, options: .regularExpression) != nil ||
        $0.range(of: #"^[A-Za-z_][A-Za-z0-9_.]*(Error|Exception)\s*$"#, options: .regularExpression) != nil ||
        $0 == "Traceback (most recent call last):"
    }.suffix(64).map { String($0.prefix(500)) }
}

private func safeText(_ value: String, limit: Int = 220) -> String {
    String(value.replacingOccurrences(of: "\n", with: " ").replacingOccurrences(of: "\r", with: " ").prefix(limit))
}

private func stamp(_ value: Date) -> String { ISO8601DateFormatter().string(from: value) }

private func clockText(_ value: String?) -> String {
    guard let value = value, let date = StatusModel.timestamp(value) else { return "尚无" }
    let format = DateFormatter()
    format.locale = Locale(identifier: "zh_CN")
    format.timeZone = TimeZone(identifier: "Asia/Shanghai")
    format.dateFormat = "yyyy-MM-dd HH:mm:ss"
    return format.string(from: date)
}

private func secureDirectory(_ url: URL) throws {
    let fm = FileManager.default
    var cursor = URL(fileURLWithPath: "/", isDirectory: true)
    // Foundation standardization can shorten /private/var to the /var symlink
    // after the directory exists. Preserve the installer-validated real path.
    for part in url.pathComponents.dropFirst() {
        guard part != "." && part != ".." else { throw ProbeFailure("UNSAFE_PATH") }
        cursor.appendPathComponent(part, isDirectory: true)
        if let attrs = try? fm.attributesOfItem(atPath: cursor.path) {
            guard attrs[.type] as? FileAttributeType == .typeDirectory else { throw ProbeFailure("UNSAFE_PATH") }
        } else {
            try fm.createDirectory(at: cursor, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        }
    }
}

private func rejectSymlink(_ url: URL) throws {
    if let attrs = try? FileManager.default.attributesOfItem(atPath: url.path),
       attrs[.type] as? FileAttributeType != .typeRegular { throw ProbeFailure("UNSAFE_PATH") }
}

private final class Diagnostics {
    let directory: URL
    private var fingerprint: String?
    init(root: URL) { directory = root.appendingPathComponent("logs/shutdown-indicator", isDirectory: true) }
    func record(_ value: [String: Any], key: String) {
        do {
            try secureDirectory(directory)
            let latest = directory.appendingPathComponent("latest.json")
            let current = directory.appendingPathComponent("events.jsonl")
            let previous = directory.appendingPathComponent("events.previous.jsonl")
            for file in [latest, current, previous] { try rejectSymlink(file) }
            let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
            guard data.count <= StatusModel.maximumBytes else { return }
            try data.write(to: latest, options: .atomic)
            guard key != fingerprint else { return }
            fingerprint = key
            if let attributes = try? FileManager.default.attributesOfItem(atPath: current.path),
               let size = attributes[.size] as? NSNumber,
               size.intValue + data.count + 1 > diagnosticLimit {
                if FileManager.default.fileExists(atPath: previous.path) { try FileManager.default.removeItem(at: previous) }
                try FileManager.default.moveItem(at: current, to: previous)
            }
            if !FileManager.default.fileExists(atPath: current.path) { FileManager.default.createFile(atPath: current.path, contents: nil, attributes: [.posixPermissions: 0o600]) }
            let file = try FileHandle(forWritingTo: current)
            defer { try? file.close() }
            try file.seekToEnd(); try file.write(contentsOf: data + Data([10]))
        } catch { /* Display must remain available if diagnostics are not writable. */ }
    }
}

private final class ProbeRunner {
    private let lock = NSLock()
    private var cancelled = false
    private var process: Process?
    func cancel() {
        lock.lock(); cancelled = true; let child = process; lock.unlock()
        if let child = child, child.isRunning { child.terminate() }
    }
    private var isCancelled: Bool { lock.lock(); defer { lock.unlock() }; return cancelled }

    func run(python: String, root: String) throws -> ReadinessPayload {
        let child = Process(), output = Pipe(), errors = Pipe()
        child.executableURL = URL(fileURLWithPath: python)
        child.arguments = ["-m", "china_stock_sim", "--root", root, "shutdown-status"]
        child.currentDirectoryURL = URL(fileURLWithPath: root)
        child.standardOutput = output; child.standardError = errors
        var environment = ProcessInfo.processInfo.environment
        environment["TZ"] = "Asia/Shanghai"; environment["PYTHONUNBUFFERED"] = "1"
        child.environment = environment
        lock.lock(); process = child; lock.unlock()
        guard !isCancelled else { throw ProbeFailure("CANCELLED") }
        do { try child.run() } catch { throw ProbeFailure("START_FAILED") }
        let fds = [output.fileHandleForReading.fileDescriptor, errors.fileHandleForReading.fileDescriptor]
        for fd in fds { _ = fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK) }
        defer {
            try? output.fileHandleForReading.close(); try? errors.fileHandleForReading.close()
            if child.isRunning { child.terminate(); usleep(50_000); if child.isRunning { _ = Darwin.kill(child.processIdentifier, SIGKILL) } }
        }
        var data = Data(), errorData = Data(), count = 0
        let started = ProcessInfo.processInfo.systemUptime
        while true {
            for (index, fd) in fds.enumerated() {
                var buffer = [UInt8](repeating: 0, count: 8192)
                while true {
                    let length = Darwin.read(fd, &buffer, buffer.count)
                    if length <= 0 { break }
                    count += length
                    guard count <= StatusModel.maximumBytes else { throw ProbeFailure("OUTPUT_LIMIT", diagnostic: safeStack(errorData)) }
                    if index == 0 { data.append(contentsOf: buffer.prefix(length)) }
                    else { errorData.append(contentsOf: buffer.prefix(length)); errorData = Data(errorData.suffix(16 * 1024)) }
                }
            }
            guard !isCancelled else { throw ProbeFailure("CANCELLED") }
            guard ProcessInfo.processInfo.systemUptime - started < timeout else { throw ProbeFailure("TIMEOUT", diagnostic: safeStack(errorData)) }
            if !child.isRunning { break }
            usleep(25_000)
        }
        // Drain once after observed exit: the final pipe bytes can arrive between iterations.
        for (index, fd) in fds.enumerated() {
            var buffer = [UInt8](repeating: 0, count: 8192)
            while true {
                let length = Darwin.read(fd, &buffer, buffer.count)
                if length <= 0 { break }
                count += length
                guard count <= StatusModel.maximumBytes else { throw ProbeFailure("OUTPUT_LIMIT", diagnostic: safeStack(errorData)) }
                if index == 0 { data.append(contentsOf: buffer.prefix(length)) }
                else { errorData.append(contentsOf: buffer.prefix(length)); errorData = Data(errorData.suffix(16 * 1024)) }
            }
        }
        guard child.terminationStatus == 0 else { throw ProbeFailure("PROBE_FAILED", diagnostic: safeStack(errorData)) }
        return try StatusModel.decode(data, now: Date())
    }
}

private final class Indicator: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var item: NSStatusItem!
    private let menu = NSMenu()
    private var timer: Timer?
    private var notifications: [NSObjectProtocol] = []
    private var runner: ProbeRunner?
    private var gate = ProbeGate()
    private var payload: ReadinessPayload?
    private var issue: String? = "STARTING"
    private var checked: Date?
    private var lastWall = Date()
    private var nextCheck = Date.distantPast
    private var lockFD: Int32 = -1
    private var root: URL!
    private var python = ""
    private var diagnostic: Diagnostics!
    private var testing = false
    private var lastDiagnostic: [String] = []
    #if INDICATOR_TEST
    private var fixture: URL!
    #endif

    func applicationDidFinishLaunching(_ notification: Notification) {
        do {
            guard CommandLine.arguments.count == 1,
                  let path = Bundle.main.object(forInfoDictionaryKey: "SCSProjectRoot") as? String,
                  path.hasPrefix("/"), let identifier = Bundle.main.bundleIdentifier else { throw ProbeFailure("INVALID_INSTALLATION") }
            root = URL(fileURLWithPath: path, isDirectory: true)
            #if INDICATOR_TEST
            testing = true
            guard identifier.hasSuffix(".test"),
                  FileManager.default.fileExists(atPath: root.appendingPathComponent(".shutdown-indicator-test-root").path),
                  let fixturePath = Bundle.main.object(forInfoDictionaryKey: "SCSFixturePath") as? String,
                  fixturePath.hasPrefix(root.path + "/") else { throw ProbeFailure("INVALID_TEST_INSTALLATION") }
            fixture = URL(fileURLWithPath: fixturePath)
            #else
            guard !identifier.hasSuffix(".test"), Bundle.main.object(forInfoDictionaryKey: "SCSFixturePath") == nil,
                  let interpreter = Bundle.main.object(forInfoDictionaryKey: "SCSPythonPath") as? String,
                  interpreter == root.appendingPathComponent(".venv/bin/python").path else { throw ProbeFailure("INVALID_INSTALLATION") }
            python = interpreter
            #endif
            let directory = root.appendingPathComponent("data/state/shutdown-indicator", isDirectory: true)
            try secureDirectory(directory)
            let file = directory.appendingPathComponent("instance.lock")
            lockFD = Darwin.open(file.path, O_CREAT | O_RDWR | O_NOFOLLOW, 0o600)
            guard lockFD >= 0 else { throw ProbeFailure("INSTANCE_LOCK_FAILED") }
            guard flock(lockFD, LOCK_EX | LOCK_NB) == 0 else { NSApp.terminate(nil); return }
            diagnostic = Diagnostics(root: root)
            item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
            item.autosaveName = identifier
            menu.autoenablesItems = false; menu.delegate = self; item.menu = menu
            render()
            let center = NSWorkspace.shared.notificationCenter
            for name in [NSWorkspace.willSleepNotification, NSWorkspace.didWakeNotification,
                         NSWorkspace.sessionDidBecomeActiveNotification] {
                notifications.append(center.addObserver(forName: name, object: nil, queue: .main) { [weak self] note in
                    if note.name == NSWorkspace.willSleepNotification { self?.gate.sleep() }
                    else { self?.gate.wake() }
                    self?.invalidate("WAKE_RECHECK")
                    if note.name != NSWorkspace.willSleepNotification { self?.requestCheck() }
                })
            }
            timer = Timer(timeInterval: 1, target: self, selector: #selector(tick), userInfo: nil, repeats: true)
            RunLoop.main.add(timer!, forMode: .common)
            requestCheck()
        } catch {
            // A broken installation has no green indicator and cannot start business work.
            fputs("shutdown-indicator: installation unavailable\n", stderr)
            Darwin.exit(2)
        }
    }

    private func invalidate(_ code: String) {
        gate.invalidate(); runner?.cancel(); issue = code; nextCheck = .distantPast
        render()
    }

    @objc private func tick() {
        let now = Date()
        if now < lastWall.addingTimeInterval(-1) || now.timeIntervalSince(lastWall) > 4 {
            invalidate("CLOCK_OR_RESUME_RECHECK")
        }
        lastWall = now
        if let value = payload, issue == nil, let expired = StatusModel.issue(value, now: now) {
            issue = expired; render()
        }
        if now >= nextCheck { requestCheck() }
    }

    private func requestCheck() {
        guard runner == nil, let token = gate.begin() else { return }
        let probe = ProbeRunner(); runner = probe
        nextCheck = Date().addingTimeInterval(interval)
        #if INDICATOR_TEST
        do {
            try rejectSymlink(fixture)
            guard let size = try fixture.resourceValues(forKeys: [.fileSizeKey]).fileSize,
                  size <= StatusModel.maximumBytes else { throw ProbeFailure("OUTPUT_LIMIT") }
            var data = try Data(contentsOf: fixture)
            if var json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
               json["fixture_live_clock"] as? Bool == true {
                json["sampled_at"] = stamp(Date().addingTimeInterval(-0.01))
                json["expires_at"] = stamp(Date().addingTimeInterval(60))
                data = try JSONSerialization.data(withJSONObject: json)
            }
            finish(try StatusModel.decode(data, now: Date()), error: nil, token: token)
        } catch { finish(nil, error: (error as? ProbeFailure)?.code ?? "FIXTURE_INVALID", token: token) }
        #else
        let executable = python, directory = root.path
        DispatchQueue.global(qos: .utility).async { [weak self] in
            do {
                let value = try probe.run(python: executable, root: directory)
                DispatchQueue.main.async { self?.finish(value, error: nil, token: token) }
            } catch {
                let failure = error as? ProbeFailure ?? ProbeFailure("PROBE_FAILED")
                DispatchQueue.main.async { self?.finish(nil, error: failure.code, token: token, diagnostic: failure.diagnostic) }
            }
        }
        #endif
    }

    private func finish(_ value: ReadinessPayload?, error: String?, token: Int, diagnostic: [String] = []) {
        runner = nil
        guard gate.complete(token) else { requestCheck(); return }
        checked = Date(); issue = error; lastDiagnostic = diagnostic
        if let value = value { payload = value; issue = StatusModel.issue(value, now: Date()) }
        render()
    }

    private func addLine(_ title: String) { let row = NSMenuItem(title: safeText(title), action: nil, keyEquivalent: ""); row.isEnabled = false; menu.addItem(row) }
    private func render() {
        guard item != nil else { return }
        let state = issue == nil ? payload?.state ?? "UNKNOWN" : "UNKNOWN"
        let label = StatusModel.label(state: state, payload: issue == nil ? payload : nil)
        let color: NSColor = state == "READY" ? .systemGreen : state == "BLOCKED" ? .systemRed :
            ["WORKING", "WAITING"].contains(state) ? .systemYellow : .systemGray
        let image = NSImage(size: NSSize(width: 12, height: 12), flipped: false) { rect in
            color.setFill(); NSBezierPath(ovalIn: rect.insetBy(dx: 1, dy: 1)).fill(); return true
        }
        item.button?.image = image
        item.button?.title = (testing ? "测试·" : "") + "A股 " + label
        item.button?.toolTip = "本机模拟关机就绪状态；点击立即复查"
        item.button?.setAccessibilityLabel((testing ? "隔离测试 " : "") + "A股模拟 " + label)
        menu.removeAllItems()
        addLine(testing ? "隔离测试 — 不代表正式账户" : "A股模拟 · 本机关机就绪")
        addLine("当前：" + label)
        addLine("D 应完成交易日：" + (payload?.business_session ?? "待核"))
        addLine("N 下一交易日：" + (payload?.target_session ?? "待核"))
        addLine("最近检查：" + clockText(checked.map(stamp)))
        addLine("证据采样：" + clockText(payload?.sampled_at))
        if let issue = issue { addLine("状态待核：" + issueMessage(issue)) }
        menu.addItem(.separator())
        if let value = payload {
            for account in value.accounts {
                addLine(account.account_id + "：" + account.summary)
                for reason in account.reasons.prefix(6) { addLine("  " + reason.message) }
            }
            for reason in value.reasons.prefix(10) { addLine(reason.message) }
            for work in value.optional_work.prefix(6) { addLine("非阻断：" + work) }
        }
        if issue != nil, payload != nil { addLine("上述历史结果仅供排查，已撤销绿色") }
        menu.addItem(.separator())
        addLine("仅表示本项目本次检查；不自动关机")
        let check = NSMenuItem(title: "立即检查", action: #selector(checkNow), keyEquivalent: "")
        check.target = self; check.isEnabled = true; menu.addItem(check)
        let quit = NSMenuItem(title: "退出指示器（业务继续运行）", action: #selector(quitApp), keyEquivalent: "q")
        quit.target = self; quit.isEnabled = true; menu.addItem(quit)
        var observation: [String: Any] = ["schema_version": 1, "display_state": state, "label": label,
            "checked_at": checked.map(stamp) ?? NSNull(), "observed_at": stamp(Date()),
            "sampled_at": payload?.sampled_at ?? NSNull(), "expires_at": payload?.expires_at ?? NSNull(),
            "business_session": payload?.business_session ?? NSNull(), "target_session": payload?.target_session ?? NSNull(),
            "issue": issue ?? NSNull(), "pid": ProcessInfo.processInfo.processIdentifier,
            "test_instance": testing, "financial_write": false, "diagnostic_stack": lastDiagnostic]
        observation["accounts"] = payload?.accounts.map { ["account_id": $0.account_id, "ready": $0.ready, "summary": safeText($0.summary)] as [String: Any] } ?? []
        let accountKeys: [String] = (payload?.accounts ?? []).map { account in
            let codes = account.reasons.map { $0.code }.joined(separator: ",")
            return [account.account_id, String(account.ready), account.summary, codes].joined(separator: ":")
        }
        var keyParts = [state, label, issue ?? "", payload?.business_session ?? "", payload?.target_session ?? ""]
        keyParts.append((payload?.reasons ?? []).map { $0.code }.joined(separator: ","))
        keyParts.append(accountKeys.joined(separator: "|"))
        keyParts.append((payload?.optional_work ?? []).joined(separator: "|"))
        keyParts.append(lastDiagnostic.joined(separator: "|"))
        let key = keyParts.joined(separator: "|")
        diagnostic?.record(observation, key: key)
    }

    private func issueMessage(_ code: String) -> String {
        switch code {
        case "TIMEOUT": return "检查超过8秒，稍后自动重试"
        case "OUTPUT_LIMIT": return "检查输出超过安全上限"
        case "EXPIRED": return "检查结果已过期，正在重新核查"
        case "WAKE_RECHECK", "CLOCK_OR_RESUME_RECHECK": return "启动、唤醒或时钟变化后正在重新核查"
        case "CHECKING", "STARTING": return "正在检查，请稍候"
        default: return "读取或校验失败，稍后自动重试（" + safeText(code, limit: 40) + "）"
        }
    }
    func menuWillOpen(_ menu: NSMenu) { checkNow() }
    @objc private func checkNow() { invalidate("CHECKING"); requestCheck() }
    @objc private func quitApp() { NSApp.terminate(nil) }
    func applicationWillTerminate(_ notification: Notification) {
        timer?.invalidate(); gate.invalidate(); runner?.cancel()
        for observer in notifications { NSWorkspace.shared.notificationCenter.removeObserver(observer) }
        if lockFD >= 0 { _ = flock(lockFD, LOCK_UN); _ = Darwin.close(lockFD) }
    }
}

@main
struct ShutdownIndicatorMain {
    static func main() {
        #if INDICATOR_SELFTEST
        do {
            let args = CommandLine.arguments
            let output: [String: Any]
            if args.count == 4 && args[1] == "--validate" {
                let payload = try StatusModel.decode(Data(contentsOf: URL(fileURLWithPath: args[2])), now: Date(timeIntervalSince1970: Double(args[3])!))
                output = ["state": payload.state, "label": StatusModel.label(state: payload.state, payload: payload)]
            } else if args.count == 4 && args[1] == "--probe" {
                let payload = try ProbeRunner().run(python: args[2], root: args[3])
                output = ["state": payload.state]
            } else if args.count == 2 && args[1] == "--gate" {
                var gate = ProbeGate()
                let first = gate.begin()!, overlapping = gate.begin()
                gate.sleep(); let asleep = gate.complete(first), asleepStart = gate.begin()
                gate.wake(); let after = gate.begin()!; gate.invalidate(); let stale = gate.complete(after)
                let final = gate.begin()!; let fresh = gate.complete(final)
                output = ["overlap_blocked": overlapping == nil, "sleep_result_rejected": !asleep,
                          "sleep_start_blocked": asleepStart == nil, "old_generation_rejected": !stale, "fresh_accepted": fresh]
            } else if args.count == 3 && args[1] == "--diagnostics" {
                let logger = Diagnostics(root: URL(fileURLWithPath: args[2]))
                for index in 0..<40 { logger.record(["state": "WORKING", "index": index, "detail": String(repeating: "x", count: 32000)], key: String(index)) }
                let file = logger.directory.appendingPathComponent("events.jsonl")
                let before = try Data(contentsOf: file)
                logger.record(["state": "WORKING", "latest_only": true], key: "39")
                output = ["duplicate_event_not_appended": before == (try Data(contentsOf: file))]
            } else { throw ProbeFailure("INVALID_TEST_COMMAND") }
            print(String(data: try JSONSerialization.data(withJSONObject: output, options: [.sortedKeys]), encoding: .utf8)!)
        } catch {
            let failure = error as? ProbeFailure ?? ProbeFailure("SELFTEST_FAILED")
            let output: [String: Any] = ["state": "UNKNOWN", "issue": failure.code, "diagnostic_stack": failure.diagnostic]
            if let data = try? JSONSerialization.data(withJSONObject: output, options: [.sortedKeys]) { print(String(decoding: data, as: UTF8.self)) }
            Darwin.exit(2)
        }
        #else
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let delegate = Indicator()
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
        #endif
    }
}
