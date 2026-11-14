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
        engine.$status.sink { [weak self] st in self?.followEngineMeeting(st) }.store(in: &bag)
        engine.onRunning = { [weak self] in self?.engineStarted() }
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

    // MARK: engine

    /// The engine runs from the first launch, so voices can download while the operator is still in setup.
    func launch() {
        engine.packageLanguages = OutputLanguage.all.map(\.id)
        engine.start(environment: engineEnvironment())
        Task { await checkKey() }
        Task { await checkHub() }
        Task { await checkDevice() }
    }

    /// A Mac revoked from another Mac (or the admin) goes back to first launch. Only a clear 403 counts: an
    /// unreachable or older hub changes nothing.
    func checkDevice() async {
        guard profile.setupDone, let token = Keychain.get(.deviceToken), !token.isEmpty else { return }
        do {
            _ = try await HubClient.account(token: token)
        } catch is HubClient.SignedOut {
            signOut()
            message = "This Mac was disconnected from the congregation. Set it up again to publish meetings."
        } catch {}
    }

    /// Forget the congregation on this Mac (Settings, or the hub revoked it). The details entered and the API key
    /// stay, so setting up again is quick.
    func signOut() {
        Keychain.delete(.deviceToken)
        Keychain.delete(.connectCode)
        OnboardingProgress.clear()
        var p = profile
        p.slug = ""
        p.claim = ""
        p.setupDone = false
        profile = p
        p.save()
        relaunchEngine()   // without the device token; onboarding needs the engine for downloads
    }

    func relaunchEngine() {
        engine.restart(environment: engineEnvironment())
    }

    private func engineEnvironment() -> [String: String] {
        var env: [String: String] = ["HEARCHURCH_DEVICE_NAME": deviceName]
        // The hub records it per Mac (the operator's install stats by app version).
        if let v = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String { env["HEARCHURCH_APP_VERSION"] = v }
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

    // MARK: the meeting's languages and downloads

    /// The languages spoken at the microphone, and the translation languages (= the listen pills), as chosen here.
    /// The engine follows them (POST /api/settings {"meeting"}), live during a meeting too.
    var speakers: [String] { profile.speakers }
    var targets: [String] { profile.targets }

    enum LanguageRole { case speaker, target }

    func isOn(_ code: String, _ role: LanguageRole) -> Bool { (role == .speaker ? speakers : targets).contains(code) }

    /// Downloaded and ready to use in a meeting.
    func isReady(_ code: String) -> Bool { package(code)?.state == "ready" }

    /// A toggle in a language grid. The last language of a set can't be switched off. A language that isn't
    /// downloaded starts downloading; during a meeting it is switched on only once it is ready (a download must not
    /// change what listeners get in the middle of a talk).
    /// Returns why nothing changed, if so.
    @discardableResult
    func toggle(_ code: String, _ role: LanguageRole) -> String? {
        var list = role == .speaker ? profile.speakers : profile.targets
        if list.contains(code) {
            guard list.count > 1 else {
                return role == .speaker ? "At least one language must be spoken." : "Keep at least one translation language."
            }
            list.removeAll { $0 == code }
        } else {
            if running && !isReady(code) {
                Task { await engine.download([code]) }
                return "\(OutputLanguage.name(code)) is downloading. Switch it on when it's ready."
            }
            list.append(code)
        }
        var speakers = profile.speakers, targets = profile.targets
        if role == .speaker { speakers = list } else { targets = list }
        setMeeting(speakers: speakers, targets: targets)
        return nil
    }

    func setMeeting(speakers: [String], targets: [String]) {
        profile.speakers = speakers
        profile.targets = targets
        profile.meetingPicked = true
        profile.languagesPicked = true
        profile.save()
        Task {
            if profile.setupDone { await pushMeeting() }
            await engine.download(neededDownloads)
        }
    }

    private func pushMeeting() async {
        if let e = await engine.set(["meeting": ["speakers": profile.speakers, "targets": profile.targets]]) { message = e }
    }

    /// Latter-day Saints get the Latter-day Saint glossary on top of the Christian one; other churches the Christian one.
    func pushChurch() async {
        guard !profile.church.isEmpty else { return }
        await engine.set(["church": profile.church])
    }

    /// The engine (re)started: give it this Mac's meeting and church, and resume downloads. A profile from before V3.3
    /// takes the engine's meeting instead (it was migrated from the old one-speaker settings).
    private func engineStarted() {
        Task {
            await pushChurch()
            if profile.setupDone {
                if !profile.meetingPicked {
                    await engine.loadSettings()
                    if let m = engine.settings?.settings.meeting, !m.speakers.isEmpty, !m.targets.isEmpty {
                        profile.speakers = m.speakers
                        profile.targets = m.targets
                    }
                    profile.meetingPicked = true
                    profile.save()
                }
                await pushMeeting()
            }
            if profile.setupDone || profile.languagesPicked { await engine.download(neededDownloads) }
        }
    }

    private var lastEngineMeeting: EngineStatus.Meeting?

    /// Someone changed the languages elsewhere (the speak page may): show what the engine does.
    private func followEngineMeeting(_ st: EngineStatus?) {
        guard profile.setupDone, let m = st?.meeting, m != lastEngineMeeting else { return }
        let first = lastEngineMeeting == nil
        lastEngineMeeting = m
        guard !first, !m.speakers.isEmpty, !m.targets.isEmpty,
              m.speakers != profile.speakers || m.targets != profile.targets else { return }
        profile.speakers = m.speakers
        profile.targets = m.targets
        profile.save()
    }

    func package(_ code: String) -> PackageStatus? { engine.packages.first { $0.id == code } }

    /// Download a language from Settings. Before a meeting it is ready to use in the language grids.
    func downloadLanguage(_ code: String) {
        Task { await engine.download([code]) }
    }

    func manageLanguages() {
        UserDefaults.standard.set(SettingsView.languagesTab, forKey: SettingsView.tabKey)
        openSettings?()
    }

    /// Delete a language's voice; nil if it worked, or why it couldn't.
    func removeLanguage(_ code: String) async -> String? {
        if profile.speakers == [code] || profile.targets == [code] {
            return "\(OutputLanguage.name(code)) is the meeting's only \(profile.speakers == [code] ? "speaker" : "translation") language: choose another one first."
        }
        let r = await engine.post("api/packages/remove", ["language": code])
        guard r.ok else { return r.message.isEmpty ? "Could not remove it." : r.message }
        if isOn(code, .speaker) || isOn(code, .target) {
            setMeeting(speakers: profile.speakers.filter { $0 != code }, targets: profile.targets.filter { $0 != code })
        }
        await engine.refreshPackages()
        return nil
    }

    /// Every language heard or spoken in the meeting is downloaded (speech recognition comes with any of them).
    var neededDownloads: [String] {
        var out = profile.speakers
        for t in profile.targets where !out.contains(t) { out.append(t) }
        return out
    }

    /// Downloads in progress (or failed), for the bar at the top of the window.
    var downloads: [PackageStatus] { engine.packages.filter { $0.state == "downloading" || $0.state == "failed" } }

    /// The engine's starting meeting is the one chosen in setup.
    private func writeMeeting() {
        let url = AppPaths.home.appendingPathComponent("data/overrides.darwin.json")
        var current = (try? JSONSerialization.jsonObject(with: Data(contentsOf: url))) as? [String: Any] ?? [:]
        current["meeting"] = ["speakers": profile.speakers, "targets": profile.targets, "auto_en": false]
        current.removeValue(forKey: "languages")   // legacy view: the engine derives it from the meeting
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        if let data = try? JSONSerialization.data(withJSONObject: current, options: [.prettyPrinted]) {
            try? data.write(to: url, options: .atomic)
        }
    }

    func finishSetup() {
        profile.setupDone = true
        profile.meetingPicked = true
        profile.approved = true   // asking is inferred from setting it up (V3.3)
        profile.save()
        writeMeeting()
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
        if profile.setupDone && (profile.speakers.isEmpty || profile.targets.isEmpty) {
            out.append(Check(id: "languages", title: "No language chosen", state: .bad,
                             detail: "Choose a speaker language and a translation language to start a meeting."))
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
