import AppKit
import Combine
import Foundation

/// Everything the windows show: the congregation, the engine, and the checks before Start.
@MainActor
final class AppModel: ObservableObject {
    static let shared = AppModel()

    @Published var profile = Profile.load()
    @Published private(set) var keyState: KeyState = .unknown
    @Published private(set) var hubReachable: Bool?
    @Published private(set) var onBattery = Power.onBattery
    @Published var message: String?
    var openSettings: (() -> Void)?   // set by the window (SwiftUI's openSettings)

    let engine = Engine()
    let mic = MicMonitor()
    let network = NetworkWatch()
    private let keepAwake = KeepAwake()
    private var bag: Set<AnyCancellable> = []
    private var lastHubCheck = Date.distantPast

    enum KeyState: Equatable { case unknown, checking, ok, missing, bad(String) }

    init() {
        // Re-publish the parts' changes so views only need to watch this model.
        engine.objectWillChange.sink { [weak self] in self?.objectWillChange.send() }.store(in: &bag)
        mic.objectWillChange.sink { [weak self] in self?.objectWillChange.send() }.store(in: &bag)
        network.objectWillChange.sink { [weak self] in self?.objectWillChange.send() }.store(in: &bag)
        Timer.publish(every: 3, on: .main, in: .common).autoconnect().sink { [weak self] _ in self?.tick() }.store(in: &bag)
    }

    var running: Bool { engine.status?.pipeline.running ?? false }
    var deviceToken: String? { Keychain.get(.deviceToken) }
    var deviceName: String { Host.current().localizedName ?? "Mac" }
    var listenerURL: URL? { profile.slug.isEmpty ? nil : AppPaths.hub.appendingPathComponent("c/\(profile.slug)") }

    // MARK: engine

    func launch() {
        guard profile.setupDone else { return }
        engine.start(environment: engineEnvironment())
        Task { await checkKey() }
        Task { await checkHub() }
    }

    func relaunchEngine() {
        engine.restart(environment: engineEnvironment())
    }

    private func engineEnvironment() -> [String: String] {
        var env: [String: String] = ["HEARCHURCH_DEVICE_NAME": deviceName]
        if let key = Keychain.get(.apiKey) { env["ANTHROPIC_API_KEY"] = key }
        if let token = deviceToken, var c = URLComponents(url: AppPaths.hub, resolvingAgainstBaseURL: false) {
            c.scheme = c.scheme == "http" ? "ws" : "wss"
            c.path = "/ws/engine"
            if let ws = c.url { env["HEARCHURCH_HUB_URL"] = ws.absoluteString }
            env["HEARCHURCH_DEVICE_TOKEN"] = token
        }
        if let url = listenerURL { env["HEARCHURCH_LISTENER_URL"] = url.absoluteString }
        return env
    }

    /// The languages chosen during setup become the engine's starting languages. The operator page can change
    /// them later; it saves to the same file.
    func writeLanguages() {
        let url = AppPaths.home.appendingPathComponent("data/overrides.darwin.json")
        var current = (try? JSONSerialization.jsonObject(with: Data(contentsOf: url))) as? [String: Any] ?? [:]
        current["languages"] = profile.languages
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        if let data = try? JSONSerialization.data(withJSONObject: current, options: [.prettyPrinted]) {
            try? data.write(to: url, options: .atomic)
        }
    }

    // MARK: meeting

    func startMeeting() async {
        message = nil
        mic.stop()   // the engine opens the microphone itself
        let r = await engine.post("api/start", ["source": "mic"])
        if !r.ok { message = r.message.isEmpty ? "Could not start." : r.message }
    }

    func stopMeeting() async {
        await engine.post("api/stop")
    }

    // MARK: checks

    func saveKey(_ key: String) async -> String? {
        let key = key.trimmingCharacters(in: .whitespacesAndNewlines)
        guard key.hasPrefix("sk-ant-") else { return "That doesn't look like an Anthropic API key (they start with sk-ant-)." }
        keyState = .checking
        switch await HubClient.checkAnthropicKey(key) {
        case .success:
            Keychain.set(.apiKey, key)
            keyState = .ok
            if profile.setupDone { relaunchEngine() }
            return nil
        case .failure(let f):
            keyState = .bad(f.message)
            return f.message
        }
    }

    func checkKey() async {
        guard let key = Keychain.get(.apiKey) else { keyState = .missing; return }
        keyState = .checking
        switch await HubClient.checkAnthropicKey(key) {
        case .success: keyState = .ok
        case .failure(let f): keyState = .bad(f.message)
        }
    }

    func checkHub() async {
        lastHubCheck = Date()
        hubReachable = await HubClient.reachable()
    }

    private func tick() {
        onBattery = Power.onBattery
        keepAwake.set(running)
        if Date().timeIntervalSince(lastHubCheck) > (hubReachable == true ? 60 : 10) {
            Task { await checkHub() }
        }
    }

    var checks: [Check] {
        var out: [Check] = []
        switch mic.permission {
        case .authorized:
            out.append(Check(id: "mic", title: "Microphone allowed", state: .ok))
        case .notDetermined:
            out.append(Check(id: "mic", title: "Microphone", state: .bad, detail: "Hear Church needs to hear the meeting.",
                             action: ("Allow", { Task { await self.mic.requestPermission(); self.mic.start() } })))
        default:
            out.append(Check(id: "mic", title: "Microphone blocked", state: .bad,
                             detail: "Allow Hear Church in System Settings → Privacy & Security → Microphone.",
                             action: ("Open Settings", {
                                 NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone")!)
                             })))
        }
        if mic.permission == .authorized && !running {
            out.append(mic.peakDb > -50
                       ? Check(id: "level", title: "Sound is reaching this Mac", state: .ok)
                       : Check(id: "level", title: "No sound yet", state: .warn,
                               detail: "Speak near the microphone, or check the sound system's cable."))
        }
        out.append(onBattery
                   ? Check(id: "power", title: "On battery", state: .warn, detail: "Plug the Mac in and keep the lid open during the meeting.")
                   : Check(id: "power", title: "Plugged in", state: .ok))
        if !network.online {
            out.append(Check(id: "net", title: "No internet", state: .bad, detail: "Connect to Wi-Fi or Ethernet."))
        } else if hubReachable == false {
            out.append(Check(id: "net", title: "Can't reach \(AppPaths.hub.host ?? "hearchurch.com")", state: .bad,
                             detail: "Listeners connect through it. Check the internet connection."))
        } else {
            out.append(Check(id: "net", title: "Internet connected", state: hubReachable == nil ? .pending : .ok))
        }
        switch keyState {
        case .ok: out.append(Check(id: "key", title: "Claude API key works", state: .ok))
        case .unknown, .checking: out.append(Check(id: "key", title: "Checking the Claude API key…", state: .pending))
        case .missing: out.append(Check(id: "key", title: "No Claude API key", state: .bad, detail: "Add one in Settings.",
                                        action: ("Settings…", { self.openSettings?() })))
        case .bad(let why): out.append(Check(id: "key", title: "Claude API key problem", state: .bad, detail: why,
                                             action: ("Settings…", { self.openSettings?() })))
        }
        switch engine.state {
        case .running:
            let pk = engine.packages
            let missing = pk.filter { $0.state == "missing" || $0.state == "failed" }
            if pk.contains(where: { $0.state == "downloading" }) {
                out.append(Check(id: "models", title: "Downloading languages…", state: .pending,
                                 detail: pk.filter { $0.state == "downloading" }.map(\.name).joined(separator: ", ")))
            } else if !missing.isEmpty {
                out.append(Check(id: "models", title: "Languages not downloaded", state: .bad,
                                 detail: missing.map { $0.name + ($0.error.isEmpty ? "" : " (\($0.error))") }.joined(separator: ", "),
                                 action: ("Download", { Task { await self.engine.downloadPackages() } })))
            } else if !pk.isEmpty {
                out.append(Check(id: "models", title: "Languages downloaded", state: .ok))
            }
        case .starting, .stopped:
            out.append(Check(id: "engine", title: "Starting the translation engine…", state: .pending))
        case .failed(let why):
            out.append(Check(id: "engine", title: "Translation engine stopped", state: .bad, detail: why,
                             action: ("Try again", { self.relaunchEngine() })))
        }
        return out
    }

    var canStart: Bool { engine.state == .running && !checks.contains { $0.state == .bad } }
}

struct Check: Identifiable {
    enum State { case ok, warn, bad, pending }
    let id: String
    let title: String
    let state: State
    var detail: String = ""
    var action: (String, () -> Void)?
}
