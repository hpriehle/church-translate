import AppKit
import Combine
import Foundation

/// Everything the windows show: the congregation, the engine, downloads and the checks before Start.
@MainActor
final class AppModel: ObservableObject {
    static let shared = AppModel()

    @Published var profile = Profile.load()
    @Published private(set) var keyState: KeyState = .unknown
    @Published private(set) var hubReachable: Bool?
    @Published private(set) var onBattery = Power.onBattery
    @Published var message: String?
    @Published private(set) var meetingStarted: Date?
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
        engine.onRunning = { [weak self] in self?.resumeDownloads() }
        mic.onDevicesChanged = { [weak self] in Task { await self?.engine.loadSettings() } }
    }

    var running: Bool { engine.status?.pipeline.running ?? false }
    var deviceName: String { Host.current().localizedName ?? "Mac" }
    /// Cloud: anyone with the link listens through hearchurch.com. Local: phones on this Wi-Fi only, with
    /// this meeting's QR code (a new one after every meeting).
    var localSharing: Bool { (engine.settings?.settings.server.sharing ?? engine.status?.sharing) == "local" }
    var listenerURL: URL? {
        if localSharing { return engine.status?.listener_url.flatMap(URL.init(string:)) }
        return profile.slug.isEmpty ? nil : AppPaths.hub.appendingPathComponent(profile.slug)   // hearchurch.com/<slug>
    }
    var listeners: Int {
        localSharing ? (engine.status?.listeners.captions ?? 0) : (engine.status?.engine.client?.listeners?.captions ?? 0)
    }

    func setSharing(local: Bool) async {
        await engine.set(["server": ["sharing": local ? "local" : "cloud"]])
    }

    // MARK: engine

    /// The engine runs from the first launch, so voices can download while the operator is still in setup.
    func launch() {
        engine.packageLanguages = profile.languages
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
        if profile.setupDone, let token = Keychain.get(.deviceToken),
           var c = URLComponents(url: AppPaths.hub, resolvingAgainstBaseURL: false) {
            c.scheme = c.scheme == "http" ? "ws" : "wss"
            c.path = "/ws/engine"
            if let ws = c.url { env["HEARCHURCH_HUB_URL"] = ws.absoluteString }
            env["HEARCHURCH_DEVICE_TOKEN"] = token
        }
        if let url = listenerURL { env["HEARCHURCH_LISTENER_URL"] = url.absoluteString }
        return env
    }

    // MARK: languages and downloads

    /// Switch a language on or off. During setup this only starts its download; afterwards it also switches the
    /// translation on or off right away.
    func setLanguage(_ code: String, on: Bool) {
        profile.languages.removeAll { $0 == code }
        if on { profile.languages.append(code) }
        profile.languagesPicked = true
        profile.save()
        engine.packageLanguages = profile.languages
        Task {
            if profile.setupDone && !profile.languages.isEmpty { await engine.set(["languages": profile.languages]) }
            await engine.download(profile.languages)
        }
    }

    /// After a (re)start of the engine, pick up downloads where they stopped.
    private func resumeDownloads() {
        guard profile.setupDone || profile.languagesPicked else { return }
        Task { await engine.download(profile.languages) }
    }

    /// Downloads in progress (or failed), for the bar at the top of the window.
    var downloads: [PackageStatus] { engine.packages.filter { $0.state == "downloading" || $0.state == "failed" } }

    /// The engine's starting languages are the ones chosen in setup (the operator page changes them later).
    private func writeLanguages() {
        let url = AppPaths.home.appendingPathComponent("data/overrides.darwin.json")
        var current = (try? JSONSerialization.jsonObject(with: Data(contentsOf: url))) as? [String: Any] ?? [:]
        current["languages"] = profile.languages
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        if let data = try? JSONSerialization.data(withJSONObject: current, options: [.prettyPrinted]) {
            try? data.write(to: url, options: .atomic)
        }
    }

    func finishSetup() {
        profile.setupDone = true
        profile.save()
        writeLanguages()
        relaunchEngine()   // now with the device token, so meetings go to hearchurch.com
    }

    // MARK: sound input

    /// The input chosen under Sound ("" = the Mac's default).
    var selectedInput: String { engine.settings?.settings.audio.input_device ?? "" }

    /// The chosen input, if it has been unplugged.
    var missingInput: String? {
        guard let s = engine.settings, !selectedInput.isEmpty,
              !s.devices.contains(where: { $0.name == selectedInput }) else { return nil }
        return selectedInput
    }

    /// The level meter before a meeting, from the chosen input.
    func startMicPreview() {
        guard !running else { return }
        mic.start(device: missingInput == nil ? selectedInput : "")
    }

    func chooseInput(_ name: String) async {
        await engine.set(["audio": ["input_device": name, "input_channel": 0]])
        startMicPreview()
    }

    // MARK: meeting

    func startMeeting() async {
        message = nil
        mic.stop()   // the engine opens the microphone itself
        var body: [String: Any] = ["source": "mic"]
        if let dev = engine.settings?.settings.audio.input_device, !dev.isEmpty { body["device"] = dev }
        let r = await engine.post("api/start", body)
        if r.ok { meetingStarted = Date() } else { message = r.message.isEmpty ? "Could not start." : r.message }
    }

    func stopMeeting() async {
        await engine.post("api/stop")
        meetingStarted = nil
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
            relaunchEngine()
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
        if running, meetingStarted == nil, let t = engine.status?.pipeline.started_at {
            meetingStarted = Date(timeIntervalSince1970: t)
        } else if !running {
            meetingStarted = nil
        }
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
                             action: ("Open Settings", { MicMonitor.openPrivacySettings() })))
        }
        if mic.permission == .authorized && !running {
            if let missing = missingInput {
                out.append(Check(id: "input", title: "\(missing) is not connected", state: .bad,
                                 detail: "Connect it, or choose another input under Sound."))
            }
            out.append(mic.heardSound
                       ? Check(id: "level", title: "Sound is reaching this Mac", state: .ok)
                       : Check(id: "level", title: "No sound yet", state: .warn,
                               detail: "Speak near the microphone, or check the sound system's cable."))
        }
        out.append(onBattery
                   ? Check(id: "power", title: "On battery", state: .warn, detail: "Plug the Mac in and keep the lid open during the meeting.")
                   : Check(id: "power", title: "Plugged in", state: .ok))
        if !network.online {
            out.append(Check(id: "net", title: "No internet", state: .bad, detail: "Connect to Wi-Fi or Ethernet."))
        } else if localSharing {
            let host = listenerURL?.host ?? ""
            out.append(host.isEmpty || host == "127.0.0.1"
                       ? Check(id: "net", title: "Not on a local network", state: .bad,
                               detail: "Local sharing needs this Mac on the same Wi-Fi as the phones.")
                       : Check(id: "net", title: "Sharing on this Wi-Fi (\(host))", state: .ok))
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
                out.append(Check(id: "models", title: "Downloading languages…", state: .pending))
            } else if !missing.isEmpty {
                out.append(Check(id: "models", title: "Languages not downloaded", state: .bad,
                                 detail: missing.map(\.name).joined(separator: ", "),
                                 action: ("Download", { Task { await self.engine.download(self.profile.languages) } })))
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
    var problems: Int { checks.filter { $0.state == .bad }.count }
}

struct Check: Identifiable {
    enum State { case ok, warn, bad, pending }
    let id: String
    let title: String
    let state: State
    var detail: String = ""
    var action: (String, () -> Void)?
}
