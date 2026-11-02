import Darwin
import Foundation

/// Runs the Python engine (`python -m app --app`) as a child process, restarts it if it dies, and follows it:
/// status and downloads by polling, what was said (and its translations) over /ws/operator.
/// The bundled engine lives in Contents/Resources/engine with its own Python in Contents/Resources/python.
/// For development, point the app at a checkout instead:
///   defaults write com.hearchurch.HearChurch EngineDir ~/dev/hear-church     (runs it with `uv run`)
@MainActor
final class Engine: ObservableObject {
    enum State: Equatable { case stopped, starting, running, failed(String) }

    @Published private(set) var state: State = .stopped
    @Published private(set) var status: EngineStatus?
    @Published private(set) var packages: [PackageStatus] = []
    @Published private(set) var settings: EngineSettings?
    @Published private(set) var chunks: [Chunk] = []          // newest first
    @Published private(set) var log: [LogLine] = []           // newest first
    /// Which languages' downloads to follow (the operator's choice, also during setup).
    var packageLanguages: [String] = [] { didSet { if oldValue != packageLanguages { Task { await refreshPackages() } } } }
    var onRunning: (() -> Void)?
    private(set) var port = 0

    private var process: Process?
    private var wantRunning = false
    private var restarts = 0
    private var pollTask: Task<Void, Never>?
    private var feedTask: Task<Void, Never>?
    private var environment: [String: String] = [:]

    var baseURL: URL { URL(string: "http://127.0.0.1:\(port)")! }

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
        feedTask?.cancel()
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
        feedTask?.cancel()
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

    // MARK: status, downloads, settings

    private func poll() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            var tick = 0
            while !Task.isCancelled {
                guard let self else { return }
                if let s: EngineStatus = await self.get("api/status") {
                    if self.state != .running {
                        self.state = .running
                        self.restarts = 0
                        tick = 0
                        self.follow()
                        await self.loadSettings()
                        self.onRunning?()
                    }
                    if self.status != s { self.status = s }
                    if tick % 10 == 0 || self.packages.contains(where: { $0.state == "downloading" }) {
                        await self.refreshPackages()
                    }
                    tick += 1
                } else if self.state == .running {
                    self.status = nil   // briefly unreachable
                }
                try? await Task.sleep(for: .seconds(self.state == .running ? 1 : 0.5))
            }
        }
    }

    func refreshPackages() async {
        guard state == .running else { return }
        let q = packageLanguages.joined(separator: ",")
        if let p: PackageList = await get("api/packages?languages=\(q)"), p.packages != packages { packages = p.packages }
    }

    /// Start downloading whatever these languages still need (speech recognition included).
    func download(_ languages: [String]) async {
        let (data, ok) = await send("api/packages/download", ["languages": languages])
        if ok, let list = try? JSONDecoder().decode(PackageList.self, from: data) { packages = list.packages }
    }

    func loadSettings() async {
        if let s: EngineSettings = await get("api/state"), s != settings { settings = s }
    }

    /// Change engine settings (POST /api/settings), e.g. ["audio": ["input_gain": 1.5]].
    @discardableResult
    func set(_ body: [String: Any]) async -> String? {
        let (data, ok) = await send("api/settings", body)
        await loadSettings()
        return ok ? nil : Engine.detail(data)
    }

    func get<T: Decodable>(_ path: String) async -> T? {
        guard port != 0, let url = URL(string: path, relativeTo: baseURL),
              let (data, resp) = try? await URLSession.shared.data(from: url),
              (resp as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return try? JSONDecoder().decode(T.self, from: data)
    }

    @discardableResult
    func post(_ path: String, _ body: [String: Any] = [:]) async -> (ok: Bool, message: String) {
        let (data, ok) = await send(path, body)
        return (ok, Engine.detail(data) ?? "")
    }

    private func send(_ path: String, _ body: Any) async -> (Data, Bool) {
        guard port != 0, let url = URL(string: path, relativeTo: baseURL) else { return (Data(), false) }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        guard let (data, resp) = try? await URLSession.shared.data(for: req) else {
            return (Data(#"{"detail": "The translation engine is not answering."}"#.utf8), false)
        }
        return (data, (resp as? HTTPURLResponse)?.statusCode == 200)
    }

    /// POST any JSON value (e.g. a glossary).
    func postJSON(_ path: String, _ body: Any) async -> (ok: Bool, message: String) {
        let (data, ok) = await send(path, body)
        return (ok, Engine.detail(data) ?? "")
    }

    func getJSON(_ path: String) async -> Any? {
        guard port != 0, let url = URL(string: path, relativeTo: baseURL),
              let (data, _) = try? await URLSession.shared.data(from: url) else { return nil }
        return try? JSONSerialization.jsonObject(with: data)
    }

    private static func detail(_ data: Data) -> String? {
        let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        return json?["detail"] as? String ?? json?["message"] as? String
    }

    // MARK: live feed (/ws/operator)

    private func follow() {
        feedTask?.cancel()
        let url = URL(string: "ws://127.0.0.1:\(port)/ws/operator")!
        feedTask = Task { [weak self] in
            while !Task.isCancelled {
                let ws = URLSession.shared.webSocketTask(with: url)
                ws.resume()
                while !Task.isCancelled {
                    guard let msg = try? await ws.receive() else { break }
                    if case .string(let text) = msg { self?.handle(Data(text.utf8)) }
                }
                ws.cancel(with: .goingAway, reason: nil)
                try? await Task.sleep(for: .seconds(1))
            }
        }
    }

    private func handle(_ data: Data) {
        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        switch json["type"] as? String {
        case "chunk":
            guard let c = try? JSONDecoder().decode(Chunk.self, from: data) else { return }
            if let i = chunks.firstIndex(where: { $0.seq == c.seq }) {
                chunks[i] = c
            } else {
                if let first = chunks.first, c.seq < first.seq { chunks.removeAll() }   // a new meeting started
                chunks.insert(c, at: 0)
                if chunks.count > 300 { chunks.removeLast(chunks.count - 300) }
            }
        case "log":
            log.insert(LogLine(level: json["level"] as? String ?? "info", text: json["text"] as? String ?? ""), at: 0)
            if log.count > 200 { log.removeLast() }
        default:
            break
        }
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

// MARK: - what the engine sends (only the parts the app shows)

struct EngineStatus: Decodable, Equatable {
    struct Pipeline: Decodable, Equatable {
        let running: Bool
        let capturing: Bool?
        let source: String?
        let level_db: Double?
        let stt_ready: Bool?
        let started_at: Double?
        let backlog_s: Double?
        let last_error: String?
    }
    struct Translate: Decodable, Equatable {
        struct Claude: Decodable, Equatable { let state: String }
        let claude: Claude
        let last_error: String?
    }
    struct Link: Decodable, Equatable {
        let connected: Bool?
        let live: Bool?
        let error: String?
        let notice: String?
        let listeners: Listeners?
    }
    struct EngineInfo: Decodable, Equatable { let client: Link? }
    struct Listeners: Decodable, Equatable { let stream: Int; let captions: Int }
    struct Switch: Decodable, Equatable { let code: String; let name: String; let on: Bool }

    let pipeline: Pipeline
    let translate: [String: Translate]
    let engine: EngineInfo
    let listeners: Listeners
    let listener_url: String?
    let language_switches: [Switch]?
    let sharing: String?
    let speaker: String?
}

struct PackageStatus: Decodable, Equatable, Identifiable {
    let id: String
    let name: String
    let state: String
    let error: String
    let size_mb: Double
    let done_mb: Double
}

struct PackageList: Decodable, Equatable { let packages: [PackageStatus] }

/// GET /api/state: the settings the operator can change, and what there is to choose from.
struct EngineSettings: Decodable, Equatable {
    struct Settings: Decodable, Equatable {
        struct Audio: Decodable, Equatable { let input_device: String; let input_channel: Int; let input_gain: Double }
        struct Tts: Decodable, Equatable { let engine: String; let voice: String; let rate: Int; let volume: Double }
        struct Translate: Decodable, Equatable { let claude_model: String }
        struct Voice: Decodable, Equatable { let engine: String; let voice: String }
        struct Server: Decodable, Equatable { let sharing: String? }
        struct Stt: Decodable, Equatable { let language: String }
        let languages: [String]
        let audio: Audio
        let tts: Tts
        let translate: Translate
        let voices: [String: Voice]
        let server: Server
        let stt: Stt?
    }
    struct Device: Decodable, Hashable { let name: String; let channels: Int; let `default`: Bool? }
    struct VoiceOption: Decodable, Hashable { let id: String; let engine: String; let voice: String; let label: String }
    struct Language: Decodable, Hashable { let code: String; let name: String }

    let settings: Settings
    let devices: [Device]
    let voices: [String: [VoiceOption]]
    let all_languages: [Language]
}

/// One stretch of speech: what was heard and what each language made of it.
struct Chunk: Decodable, Equatable, Identifiable {
    struct Out: Decodable, Equatable {
        let text: String
        let status: String     // pending|captioned|aired|dropped|skipped|failed|merged
        let note: String?
        let caption_lag: Double?
    }
    let seq: Int
    let en: String
    let status: String
    let note: String?
    let wall_time: Double?
    let langs: [String: Out]
    var id: Int { seq }
}

struct LogLine: Identifiable {
    let id = UUID()
    let level: String
    let text: String
}
