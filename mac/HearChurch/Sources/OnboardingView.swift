import AVFoundation
import SwiftUI

/// First launch. The congregation is made, and its church, address, meeting times, e-mails and billing are managed, on
/// hearchurch.com (/start, /account): this Mac only signs in, with a code e-mailed to the congregation's addresses or
/// one from "Add device" on another Mac. Then it picks its languages (which start downloading) and the microphone,
/// and opens the main window. A congregation on its own API key (switched on by Hear Church) also enters the key.
struct OnboardingView: View {
    @EnvironmentObject var model: AppModel
    @State private var step: Step
    @State private var error: String?
    @State private var busy = false
    @State private var ownKey = OnboardingProgress.ownKey   // the congregation signed in to uses its own API key

    enum Step: String { case welcome, languages, microphone, claude }

    private var order: [Step] { ownKey ? [.welcome, .languages, .microphone, .claude] : [.welcome, .languages, .microphone] }
    private var isLast: Bool { step == order.last }

    init() {
        var s = OnboardingProgress.step ?? .welcome
        if let token = Keychain.get(.deviceToken), !token.isEmpty {
            if s == .welcome { s = .languages }   // already signed in: go past sign-in
        } else {
            s = .welcome
        }
        _step = State(initialValue: s)
    }

    var body: some View {
        VStack(spacing: 0) {
            EngineFailedBanner()
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    content
                    if let error { Text(error).foregroundStyle(.red).textSelection(.enabled) }
                }
                .padding(32)
                .frame(maxWidth: 620, alignment: .leading)
                .frame(maxWidth: .infinity)
            }
            Divider()
            if step == .welcome {
                OnboardingFooter().padding(16)   // signing in has its own buttons
            } else {
                HStack {
                    if step != order[1] { Button("Back") { move(-1) } }   // signed in: no way back to the sign-in
                    Spacer()
                    if busy { ProgressView().controlSize(.small) }
                    Button(buttonTitle) { Task { await next() } }
                        .keyboardShortcut(.defaultAction)
                        .disabled(!canContinue || busy)
                }
                .padding(16)
            }
        }
        .onChange(of: step) { _, s in OnboardingProgress.step = s }
        .onChange(of: ownKey) { _, o in OnboardingProgress.ownKey = o }
    }

    private var buttonTitle: String {
        if isLast { return "Open Hear Church" }
        if step == .microphone && model.mic.permission != .authorized { return "Skip for now" }
        return "Continue"
    }

    @ViewBuilder private var content: some View {
        switch step {
        case .welcome: SignInStep(onSignedIn: { own in ownKey = own; error = nil; step = .languages })
        case .languages: LanguagesStep()
        case .microphone: MicrophoneStep()
        case .claude: ClaudeStep()
        }
    }

    private var canContinue: Bool {
        let p = model.profile
        switch step {
        case .languages: return !p.speakers.isEmpty && !p.targets.isEmpty
        case .claude: return model.keyState == .ok
        default: return true
        }
    }

    static func isEmail(_ s: String) -> Bool {
        let t = s.trimmingCharacters(in: .whitespaces)
        guard let at = t.firstIndex(of: "@"), at > t.startIndex else { return false }
        return t[t.index(after: at)...].contains(".") && !t.contains(" ")
    }

    private func move(_ by: Int) {
        error = nil
        guard let i = order.firstIndex(of: step) else { return }
        step = order[max(1, min(order.count - 1, i + by))]
        model.profile.save()
    }

    private func next() async {
        error = nil
        guard isLast else { move(1); return }
        if !LoginItem.enabled { LoginItem.set(true) }   // as before; turned off in Settings → General
        OnboardingProgress.clear()
        model.finishSetup()
    }
}

// MARK: - steps

private struct StepTitle: View {
    let title: String
    let subtitle: String
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.largeTitle.bold())
            Text(subtitle).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
        .padding(.bottom, 8)
    }
}

/// The opening screen's footer, pinned to the bottom of the window: the voices line (with the download's progress
/// while one runs), then Privacy, Terms, Voice credits, Open-source notices and Contact support.
private struct OnboardingFooter: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        let active = model.engine.packages.filter { $0.state == "downloading" }
        let done = active.reduce(0) { $0 + $1.done_mb }, total = active.reduce(0) { $0 + $1.size_mb }
        VStack(spacing: 8) {
            HStack(spacing: 8) {
                Label("Voices download while you finish setting up", systemImage: "arrow.down.circle")
                if !active.isEmpty {
                    ProgressView(value: total > 0 ? min(1, done / total) : 0).frame(width: 80)
                    Text("\(Format.size(done)) of \(Format.size(total))").monospacedDigit()
                }
            }
            .font(.callout).foregroundStyle(.secondary)
            HelpLinks().frame(maxWidth: .infinity)
        }
    }
}

/// Signing in: a code e-mailed to every address of the congregation, or one from Add device on a signed-in Mac
/// (POST /api/hc/email-code, then /api/hc/pair). A new church signs up on hearchurch.com/start first.
private struct SignInStep: View {
    @EnvironmentObject var model: AppModel
    let onSignedIn: (_ ownKey: Bool) -> Void
    enum Way { case email, otherMac }
    @State private var way: Way?
    @State private var email = ""
    @State private var sent: String?
    @State private var sending = false
    @State private var code = ""
    @State private var busy = false
    @State private var problem: String?

    var body: some View {
        StepTitle(title: "Welcome to Hear Church",
                  subtitle: "This Mac listens to your meeting and translates it live. Members hear the translation and read captions on their own phones, through hearchurch.com.")
        choice(.email, "Sign in with your email", "envelope.circle.fill",
               "Use an e-mail address of your congregation. We send a code to every address of the congregation.")
        choice(.otherMac, "Use a code from another Mac", "link.circle.fill",
               "On a Mac already signed in, open Hear Church → Settings → Congregation → Add device.")
        if way == .email {
            HStack {
                TextField("name@example.com", text: $email).textContentType(.emailAddress).onSubmit { Task { await send() } }
                Button(sent == nil ? "Send code" : "Send again") { Task { await send() } }
                    .disabled(!OnboardingView.isEmail(email) || sending || busy)
                if sending { ProgressView().controlSize(.small) }
            }
            if let sent { Label(sent, systemImage: "envelope").foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true) }
            if sent != nil { codeField }
        } else if way == .otherMac {
            codeField
        }
        if let problem { Text(problem).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true).textSelection(.enabled) }
        Link(destination: AppPaths.hub.appendingPathComponent("start")) {
            Label("New church? Get started on hearchurch.com", systemImage: "arrow.up.right.square")
        }
        .padding(.top, 4)
    }

    private var codeField: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Enter the code. Codes work once, for 15 minutes.").font(.callout).foregroundStyle(.secondary)
            HStack {
                TextField("XXXX-XXXX", text: $code)
                    .font(.system(.title, design: .monospaced))
                    .frame(maxWidth: 260)
                    .onChange(of: code) { _, v in
                        let shown = HubClient.pairingFormat(v)
                        if shown != v { code = shown }
                    }
                    .onSubmit { Task { await signIn() } }
                Button("Sign in") { Task { await signIn() } }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!codeComplete || busy)
                if busy { ProgressView().controlSize(.small) }
            }
        }
    }

    private var codeComplete: Bool { code.filter { $0.isLetter || $0.isNumber }.count == 8 }

    private func choice(_ w: Way, _ title: String, _ icon: String, _ detail: String) -> some View {
        Button { if way != w { way = w; code = ""; problem = nil } } label: {
            HStack(spacing: 14) {
                Image(systemName: icon).font(.system(size: 30)).foregroundStyle(Color.accentColor)
                VStack(alignment: .leading, spacing: 4) {
                    Text(title).font(.title3.weight(.semibold))
                    Text(detail).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                Image(systemName: way == w ? "checkmark.circle.fill" : "chevron.right")
                    .foregroundStyle(way == w ? AnyShapeStyle(Color.accentColor) : AnyShapeStyle(HierarchicalShapeStyle.tertiary))
            }
            .padding(16)
            .background(Color.secondary.opacity(way == w ? 0.18 : 0.1), in: RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(way == w ? Color.accentColor : .clear, lineWidth: 1.5))
            .contentShape(RoundedRectangle(cornerRadius: 10))
        }
        .buttonStyle(.plain)
    }

    private func send() async {
        let e = email.trimmingCharacters(in: .whitespaces)
        guard OnboardingView.isEmail(e) else { return }
        sending = true
        defer { sending = false }
        problem = nil
        do {
            sent = try await HubClient.emailCode(e)
        } catch {
            problem = error.localizedDescription
        }
    }

    private func signIn() async {
        guard codeComplete, !busy else { return }
        busy = true
        defer { busy = false }
        problem = nil
        do {
            let j = try await HubClient.pair(code: code, deviceName: model.deviceName)
            guard Keychain.set(.deviceToken, j.token) else {
                problem = "Could not save to this Mac's Keychain. Unlock the Keychain (or allow access) and try again."
                return
            }
            Keychain.delete(.connectCode)
            model.profile.slug = j.slug
            if !j.name.isEmpty { model.profile.name = j.name }
            if !j.church.isEmpty { model.profile.church = j.church }
            if !j.unitType.isEmpty { model.profile.unitType = j.unitType }
            model.profile.stake = j.stake
            if !j.address.isEmpty { model.profile.address = j.address }
            Task { await model.pushChurch() }   // the glossary follows the congregation's church
            model.profile.approved = true   // whoever set the congregation up already confirmed it
            model.profile.claim = ""
            model.profile.save()
            code = ""
            onSignedIn(j.billing == "own_key")
        } catch {
            problem = error.localizedDescription
        }
    }
}

/// Privacy, Terms and support, opened in the default browser / mail app (the opening screen's footer, and Settings).
struct HelpLinks: View {
    var body: some View {
        HStack(spacing: 16) {
            Link("Privacy", destination: URL(string: "https://hearchurch.com/privacy")!)
            Link("Terms", destination: URL(string: "https://hearchurch.com/terms")!)
            Link("Voice credits", destination: URL(string: "https://hearchurch.com/credits")!)
            Button("Open-source notices", action: Self.showNotices)
                .buttonStyle(.link)
                .help("The open-source parts of the app, their licences, and where to get their source code")
            Link("Contact support", destination: URL(string: "mailto:support@hearchurch.com")!)
        }
        .font(.callout)
    }

    /// Shows THIRD_PARTY_NOTICES.md in Finder, next to the licences/ folder (both in Resources/engine, from
    /// mac/build.sh). A development build without the bundled engine opens the source page instead.
    static func showNotices() {
        let notices = Bundle.main.resourceURL?.appendingPathComponent("engine/THIRD_PARTY_NOTICES.md")
        if let notices, FileManager.default.fileExists(atPath: notices.path) {
            NSWorkspace.shared.activateFileViewerSelecting([notices])
        } else {
            NSWorkspace.shared.open(URL(string: "https://hearchurch.com/source")!)
        }
    }
}

struct PackageBadge: View {
    let package: PackageStatus?
    let sizeMB: Double

    var body: some View {
        switch package?.state {
        case "ready": Label("Ready", systemImage: "checkmark.circle.fill").foregroundStyle(.green).font(.callout)
        case "downloading":
            HStack(spacing: 6) {
                ProgressView(value: min(1, (package?.done_mb ?? 0) / max(1, package?.size_mb ?? 1))).frame(width: 70)
                Text(Format.size(package?.size_mb ?? sizeMB)).foregroundStyle(.secondary).font(.callout)
            }
        case "failed": Label("Download failed", systemImage: "exclamationmark.triangle.fill").foregroundStyle(.orange).font(.callout)
        default: Text(Format.size(package?.size_mb ?? sizeMB)).foregroundStyle(.secondary).font(.callout)
        }
    }
}

private struct LanguagesStep: View {
    var body: some View {
        StepTitle(title: "Languages",
                  subtitle: "Choose the languages people speak at the microphone, and the languages listeners can choose. Every language you choose downloads now: keep going while it does. You can add or remove languages later in Settings.")
        LanguageSelector(showTitle: false, everyLanguage: true)
        Divider()
        SpeechPackageRow()
    }
}

private struct MicrophoneStep: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        // A real container (not EmptyView) carries onAppear, so the meter starts as soon as the step shows.
        VStack(alignment: .leading, spacing: 16) {
            StepTitle(title: "Microphone",
                      subtitle: "Hear Church listens through this Mac's microphone, or a cable from the sound system. The sound itself never leaves this Mac: only the words go out, as text, to be translated.")
            switch model.mic.permission {
            case .authorized:
                Label("Microphone allowed", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                Text("Say something to check the level:").foregroundStyle(.secondary)
                MicMeter(level: model.mic.level).frame(maxWidth: 400)
            case .notDetermined:
                Button { Task { await model.mic.requestPermission(); model.startMicPreview() } } label: {
                    Label("Allow the microphone", systemImage: "mic").frame(minWidth: 220, minHeight: 32)
                }
                .buttonStyle(.borderedProminent)
            default:
                Label("The microphone is blocked for Hear Church.", systemImage: "xmark.circle.fill").foregroundStyle(.red)
                Button("Open System Settings → Microphone") { MicMonitor.openPrivacySettings() }
                Text("Turn Hear Church on there, then come back.").font(.callout).foregroundStyle(.secondary)
            }
            Divider().padding(.vertical, 4)
            Toggle(isOn: Binding(get: { model.profile.shareLogs }, set: { model.setShareLogs($0) })) {
                Text("Share diagnostic logs with Hear Church support")
                Text(DiagnosticLogText.detail + " You can turn this off in Settings.")
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            .toggleStyle(.checkbox)
        }
        .onAppear { model.meterAppeared() }
        .onDisappear { model.meterDisappeared() }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            model.startMicPreview()   // back from System Settings
        }
    }
}

/// Only for a congregation on its own API key (Hear Church switches that on; there is no code for it in the app).
private struct ClaudeStep: View {
    @EnvironmentObject var model: AppModel
    @State private var key = ""
    @State private var error: String?

    var body: some View {
        StepTitle(title: "Your translation key",
                  subtitle: "Your congregation translates with its own API key, and is billed directly for what it uses. The key is kept in this Mac's Keychain.")
        APIKeyGuide()
        HStack {
            SecureField("sk-ant-…", text: $key).onSubmit { Task { await save() } }
            Button("Test and save") { Task { await save() } }.disabled(key.isEmpty || model.keyState == .checking)
        }
        switch model.keyState {
        case .ok: Label("The key works.", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
        case .checking: ProgressView().controlSize(.small)
        default: EmptyView()
        }
        if let error { Text(error).foregroundStyle(.red) }
    }

    private func save() async { error = await model.saveKey(key) }
}

enum Format {
    static func size(_ mb: Double) -> String {
        mb >= 1000 ? String(format: "%.1f GB", mb / 1000) : "\(Int(mb.rounded())) MB"
    }

    /// 754 s → "12 min"; under a minute → "40 s".
    static func minutes(_ s: Double) -> String {
        s < 60 ? "\(Int(s)) s" : "\(Int((s / 60).rounded())) min"
    }

    /// A delay: "2.4 s", or "–" when there is none yet.
    static func seconds(_ s: Double?) -> String {
        s.map { String(format: "%.1f s", $0) } ?? "–"
    }
}

/// Where setup stopped, so quitting in the middle resumes there.
enum OnboardingProgress {
    private static let stepKey = "OnboardingStep", joinedKey = "OnboardingJoined", ownKeyKey = "OnboardingOwnKey"
    fileprivate static var step: OnboardingView.Step? {
        get { UserDefaults.standard.string(forKey: stepKey).flatMap(OnboardingView.Step.init(rawValue:)) }
        set { UserDefaults.standard.set(newValue?.rawValue, forKey: stepKey) }
    }
    /// The congregation signed in to uses its own API key.
    static var ownKey: Bool {
        get { UserDefaults.standard.bool(forKey: ownKeyKey) }
        set { UserDefaults.standard.set(newValue, forKey: ownKeyKey) }
    }
    static func clear() {
        UserDefaults.standard.removeObject(forKey: stepKey)
        UserDefaults.standard.removeObject(forKey: joinedKey)   // from versions that also made congregations
        UserDefaults.standard.removeObject(forKey: ownKeyKey)
        Keychain.delete(.ownKeyCode)
    }
}
