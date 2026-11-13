import AppKit
import Combine
import Foundation
import UserNotifications

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
    private var micWarningShown = false

    enum KeyState: Equatable { case unknown, checking, ok, missing, bad(String) }

    init() {
        // Re-publish the parts' changes so views only need to watch this model.
        engine.objectWillChange.sink { [weak self] in self?.objectWillChange.send() }.store(in: &bag)
        mic.objectWillChange.sink { [weak self] in self?.objectWillChange.send() }.store(in: &bag)
        network.objectWillChange.sink { [weak self] in self?.objectWillChange.send() }.store(in: &bag)
        Timer.publish(every: 3, on: .main, in: .common).autoconnect().sink { [weak self] _ in self?.tick() }.store(in: &bag)
        engine.$status.sink { [weak self] st in self?.micWarningChanged(st) }.store(in: &bag)
        engine.onRunning = { [weak self] in self?.resumeDownloads() }
        mic.onDevicesChanged = { [weak self] in Task { await self?.engine.loadSettings() } }
    }

    /// The engine's sentence when the microphone is silent or gone, during a meeting.
    var micWarning: String? {
        guard running, let w = engine.status?.mic_warning, !w.isEmpty else { return nil }
        return w
    }
    var micName: String? { running ? engine.status?.mic_name.flatMap { $0.isEmpty ? nil : $0 } : nil }

    /// One notification when a warning first appears; the next one only after it has cleared.
    private func micWarningChanged(_ st: EngineStatus?) {
        guard let st else { return }   // briefly unreachable: keep what we know
        guard st.pipeline.running, let w = st.mic_warning, !w.isEmpty else { micWarningShown = false; return }
        guard !micWarningShown else { return }
        micWarningShown = true
        let center = UNUserNotificationCenter.current()
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
            guard granted else { return }
            let c = UNMutableNotificationContent()
            c.title = "Hear Church: check the microphone"
            c.body = w
            c.sound = .default
            center.add(UNNotificationRequest(identifier: "mic-warning", content: c, trigger: nil))
        }
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
        // The engine picks its network address when it starts (this Mac only, or the Wi-Fi for local
        // sharing), so a change needs a restart. The picker is disabled during a meeting.
        if !running { relaunchEngine() }
    }

    /// The language the speaker talks in (en, fr, es, ru). Switchable during the meeting.
    var speaker: String { engine.status?.speaker ?? engine.settings?.settings.stt?.language ?? "en" }

    func setSpeaker(_ code: String) async {
        await engine.set(["stt": ["language": code]])
        if code != "en" { await engine.download(["en"]) }   // listeners get English: its voice comes with the first use
    }

    // MARK: engine

    /// The engine runs from the first launch, so voices can download while the operator is still in setup.
    func launch() {
        engine.packageLanguages = OutputLanguage.all.map(\.id) + ["en"]
        engine.start(environment: engineEnvironment())
        Task { await checkKey() }
        Task { await checkHub() }
    }

    func relaunchEngine() {
        engine.restart(environment: engineEnvironment())
    }

    private func engineEnvironment() -> [String: String] {
        var env: [String: String] = ["HEARCHURCH_DEVICE_NAME": deviceName]
        if let key = Keychain.get(.apiKey) {
            // Unadvertised: a Claude subscription token runs through Claude Code instead of the API.
            env[HubClient.isSubscriptionToken(key) ? "CLAUDE_CODE_OAUTH_TOKEN" : "ANTHROPIC_API_KEY"] = key
        }
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
        Task {
            if profile.setupDone && !profile.languages.isEmpty { await engine.set(["languages": profile.languages]) }
            await engine.download(profile.languages)
        }
    }

    func package(_ code: String) -> PackageStatus? { engine.packages.first { $0.id == code } }

    /// The checkboxes next to Start meeting: every language downloaded (or on its way), checked or not.
    var downloadedLanguages: [OutputLanguage] {
        OutputLanguage.all.filter { l in
            profile.languages.contains(l.id) || ["ready", "downloading", "failed"].contains(package(l.id)?.state ?? "")
        }
    }

    /// Download a language from Settings. It comes ready to use, except during a meeting: a download
    /// must not change what listeners get in the middle of a talk, so there it waits unchecked.
    func downloadLanguage(_ code: String) {
        if running || code == "en" { Task { await engine.download([code]) } } else { setLanguage(code, on: true) }
    }

    func manageLanguages() {
        UserDefaults.standard.set(SettingsView.languagesTab, forKey: SettingsView.tabKey)
        openSettings?()
    }

    /// Delete a language's voice; nil if it worked, or why it couldn't. The engine unchecks it too.
    func removeLanguage(_ code: String) async -> String? {
        let r = await engine.post("api/packages/remove", ["language": code])
        guard r.ok else { return r.message.isEmpty ? "Could not remove it." : r.message }
        profile.languages.removeAll { $0 == code }
        profile.save()
        await engine.refreshPackages()
        return nil
    }

    /// After a (re)start of the engine, pick up downloads where they stopped.
    private func resumeDownloads() {
        guard profile.setupDone || profile.languagesPicked else { return }
        Task { await engine.download(neededDownloads) }
    }

    /// What this Mac needs for the languages checked now, plus English while the speaker isn't speaking it.
    private var neededDownloads: [String] { profile.languages + (speaker == "en" ? [] : ["en"]) }

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
            guard Keychain.set(.apiKey, key) else {
                keyState = .missing
                return "Could not save the key in this Mac's Keychain. Unlock the Keychain (or allow access) and try again."
            }
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
        // Credits: no key on this Mac. Before the engine says, no key in the Keychain means credits too.
        let onCredits = engine.status.map { $0.claude_access == "credits" } ?? (keyState == .missing)
        if onCredits {
            // This Wi-Fi only works on credits too (the engine opens a private billing link), but the translation
            // still goes through hearchurch.com: without internet nothing could be translated.
            out.append(localSharing && hubReachable == false
                       ? Check(id: "key", title: "Translation on credit needs the internet", state: .bad,
                               detail: "Phones listen on this Wi-Fi, but the translation goes through \(AppPaths.hub.host ?? "hearchurch.com"), which this Mac can't reach. Check the internet connection.")
                       : Check(id: "key", title: "Translation on Hear Church credit", state: .ok))
        } else {
        switch keyState {
        case .ok: out.append(Check(id: "key", title: "Claude API key works", state: .ok))
        case .unknown, .checking: out.append(Check(id: "key", title: "Checking the Claude API key…", state: .pending))
        case .missing: out.append(Check(id: "key", title: "No Claude API key", state: .bad, detail: "Add one in Settings.",
                                        action: ("Settings…", { self.openSettings?() })))
        case .bad(let why): out.append(Check(id: "key", title: "Claude API key problem", state: .bad, detail: why,
                                             action: ("Settings…", { self.openSettings?() })))
        }
        }
        if profile.setupDone && profile.languages.isEmpty {
            out.append(Check(id: "languages", title: "No language chosen", state: .bad,
                             detail: "Download a language, or check one, to start a meeting.",
                             action: ("Languages…", { self.manageLanguages() })))
        }
        switch engine.state {
        case .running:
            let pk = engine.packages.filter { $0.id == "speech" || neededDownloads.contains($0.id) }
            let missing = pk.filter { $0.state == "missing" || $0.state == "failed" }
            if pk.contains(where: { $0.state == "downloading" }) {
                out.append(Check(id: "models", title: "Downloading languages…", state: .pending))
            } else if !missing.isEmpty {
                out.append(Check(id: "models", title: "Languages not downloaded", state: .bad,
                                 detail: missing.map(\.name).joined(separator: ", "),
                                 action: ("Download", { Task { await self.engine.download(self.neededDownloads) } })))
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
