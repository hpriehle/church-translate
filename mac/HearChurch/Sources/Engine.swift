import Darwin
import Foundation

/// Runs the Python engine (`python -m app --app`) as a child process, restarts it if it dies, and polls its
/// status. The bundled engine lives in Contents/Resources/engine with its own Python in
/// Contents/Resources/python. For development, point the app at a checkout instead:
///   defaults write com.hearchurch.HearChurch EngineDir ~/dev/hear-church     (runs it with `uv run`)
@MainActor
final class Engine: ObservableObject {
    enum State: Equatable { case stopped, starting, running, failed(String) }

    @Published private(set) var state: State = .stopped
    @Published private(set) var status: EngineStatus?
    @Published private(set) var packages: [PackageStatus] = []
    private(set) var port = 0

    private var process: Process?
    private var wantRunning = false
    private var restarts = 0
    private var pollTask: Task<Void, Never>?
    private var environment: [String: String] = [:]

    var baseURL: URL { URL(string: "http://127.0.0.1:\(port)")! }
    var operatorURL: URL { baseURL.appendingPathComponent("operator") }

    // MARK: lifecycle

    func start(environment env: [String: String]) {
        environment = env
        wantRunning = true
        restarts = 0
        launch()
    }

    /// New key or device token: the engine reads them at start, so start it again.
    func restart(environment env: [String: String]) {
        environment = env
        wantRunning = true
        if let p = process, p.isRunning {
            restarts = 0
            p.terminate()   // the termination handler launches it again
        } else {
            launch()
        }
    }

    func stop() {
        wantRunning = false
        pollTask?.cancel()
        guard let p = process, p.isRunning else { return }
        p.terminate()
        let pid = p.processIdentifier
        DispatchQueue.global().asyncAfter(deadline: .now() + 5) { if kill(pid, 0) == 0 { kill(pid, SIGKILL) } }
        p.waitUntilExit()
    }

    private func launch() {
        guard let (exe, args, cwd) = Engine.command() else {
            state = .failed("The translation engine is missing from this copy of the app. Download it again.")
            return
        }
        port = Engine.freePort()
        let p = Process()
        p.executableURL = exe
        p.arguments = args + ["-m", "app", "--app", "--no-browser", "--port", String(port)]
        p.currentDirectoryURL = cwd
        var env = ProcessInfo.processInfo.environment.filter { !$0.key.hasPrefix("HEARCHURCH_") && $0.key != "ANTHROPIC_API_KEY" }
        env["HEARCHURCH_HOME"] = AppPaths.home.path
        env["PYTHONDONTWRITEBYTECODE"] = "1"        // the bundled code is read-only (and signed)
        env["PYTHONUNBUFFERED"] = "1"
        env["PATH"] = (Bundle.main.resourceURL.map { $0.appendingPathComponent("bin").path + ":" } ?? "")
            + "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        env.merge(environment) { _, new in new }
        p.environment = env
        try? FileManager.default.createDirectory(at: AppPaths.logs, withIntermediateDirectories: true)
        let logURL = AppPaths.logs.appendingPathComponent("engine-output.log")
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        if let log = try? FileHandle(forWritingTo: logURL) {
            p.standardOutput = log
            p.standardError = log
        }
        p.terminationHandler = { [weak self] proc in
            Task { @MainActor in self?.exited(proc) }
        }
        do {
            try p.run()
        } catch {
            state = .failed("Could not start the translation engine: \(error.localizedDescription)")
            return
        }
        process = p
        state = .starting
        poll()
    }

    private func exited(_ p: Process) {
        guard p === process else { return }
        process = nil
        status = nil
        guard wantRunning else { state = .stopped; return }
        restarts += 1
        if restarts > 5 {
            state = .failed("The translation engine keeps stopping. See logs in ~/Library/Application Support/Hear Church/logs.")
            return
        }
        state = .starting
        let delay = Double(min(30, 2 << restarts))
        Task { @MainActor in
            try? await Task.sleep(for: .seconds(delay))
            if self.wantRunning && self.process == nil { self.launch() }
        }
    }

    // MARK: status

    private func poll() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            var tick = 0
            while !Task.isCancelled {
                guard let self else { return }
                if let s: EngineStatus = await self.get("/api/status") {
                    if self.state != .running { self.state = .running; self.restarts = 0; tick = 0 }
                    self.status = s
                    if tick % 10 == 0 || self.packages.contains(where: { $0.state == "downloading" }) {
                        if let p: PackageList = await self.get("/api/packages") { self.packages = p.packages }
                    }
                    tick += 1
                } else if self.state == .running {
                    self.status = nil   // briefly unreachable (restarting in place)
                }
                try? await Task.sleep(for: .seconds(self.state == .running ? 1.5 : 0.5))
            }
        }
    }

    func get<T: Decodable>(_ path: String) async -> T? {
        guard port != 0, let (data, resp) = try? await URLSession.shared.data(from: baseURL.appendingPathComponent(path)),
              (resp as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return try? JSONDecoder().decode(T.self, from: data)
    }

    @discardableResult
    func post(_ path: String, _ body: [String: Any] = [:]) async -> (ok: Bool, message: String) {
        var req = URLRequest(url: baseURL.appendingPathComponent(path))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        guard let (data, resp) = try? await URLSession.shared.data(for: req) else { return (false, "The engine is not answering.") }
        let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        let ok = (resp as? HTTPURLResponse)?.statusCode == 200
        return (ok, json?["detail"] as? String ?? json?["message"] as? String ?? "")
    }

    func downloadPackages() async {
        guard let (data, _) = try? await URLSession.shared.data(for: {
            var r = URLRequest(url: baseURL.appendingPathComponent("api/packages/download")); r.httpMethod = "POST"; return r
        }()), let list = try? JSONDecoder().decode(PackageList.self, from: data) else { return }
        packages = list.packages
    }

    // MARK: helpers

    /// (executable, leading arguments, working directory)
    private static func command() -> (URL, [String], URL)? {
        if let dev = UserDefaults.standard.string(forKey: "EngineDir"), !dev.isEmpty {
            let dir = URL(fileURLWithPath: (dev as NSString).expandingTildeInPath)
            for uv in ["/opt/homebrew/bin/uv", "/usr/local/bin/uv", NSHomeDirectory() + "/.local/bin/uv"]
            where FileManager.default.isExecutableFile(atPath: uv) {
                return (URL(fileURLWithPath: uv), ["run", "--quiet", "--project", dir.path, "python"], dir)
            }
            return nil
        }
        guard let res = Bundle.main.resourceURL else { return nil }
        let python = res.appendingPathComponent("python/bin/python3")
        let code = res.appendingPathComponent("engine")
        guard FileManager.default.isExecutableFile(atPath: python.path) else { return nil }
        return (python, ["-s", "-B"], code)  // -s: no user site-packages, -B: no .pyc next to the signed code
    }

    private static func freePort() -> Int {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        addr.sin_port = 0
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        _ = withUnsafeMutablePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, len) }
        }
        _ = withUnsafeMutablePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(fd, $0, &len) }
        }
        return Int(UInt16(bigEndian: addr.sin_port))
    }
}

// MARK: - what /api/status and /api/packages return (only the parts the app shows)

struct EngineStatus: Decodable {
    struct Pipeline: Decodable {
        let running: Bool
        let level_db: Double?
        let stt_ready: Bool?
        let started_at: Double?
        let last_error: String?
    }
    struct Translate: Decodable {
        struct Claude: Decodable { let state: String }
        let claude: Claude
        let last_error: String?
    }
    struct Link: Decodable {
        let connected: Bool?
        let live: Bool?
        let error: String?
        let notice: String?
        let listeners: Listeners?
    }
    struct EngineInfo: Decodable { let client: Link? }
    struct Listeners: Decodable { let stream: Int; let captions: Int }

    let pipeline: Pipeline
    let translate: [String: Translate]
    let engine: EngineInfo
    let listeners: Listeners
    let listener_url: String?
}

struct PackageStatus: Decodable, Identifiable {
    let id: String
    let name: String
    let state: String
    let error: String
}

struct PackageList: Decodable { let packages: [PackageStatus] }
