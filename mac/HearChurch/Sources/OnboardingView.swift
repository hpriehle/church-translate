import AVFoundation
import CoreLocation
import MapKit
import SwiftUI

/// First launch: describe the congregation, pick languages (they start downloading), allow the microphone,
/// add a Claude key, sign up with hearchurch.com and get this Mac's device token.
struct OnboardingView: View {
    @EnvironmentObject var model: AppModel
    @State private var step: Step
    @State private var joined: Bool
    @State private var error: String?
    @State private var busy = false
    // "I already set up my congregation"
    @State private var joinSlug = ""
    @State private var joinTitle = ""
    @State private var joinCode = ""
    @State private var connectCode = Keychain.get(.connectCode) ?? ""

    enum Step: String { case welcome, church, name, place, times, languages, microphone, claude, approval, email, connect, join, finish }

    /// Setting up a new congregation. (No "check your e-mail" step: the hub hands the device token back at once.)
    private static let createOrder: [Step] = [.welcome, .church, .name, .place, .times, .languages, .microphone, .claude, .approval, .email, .connect, .finish]
    /// Connecting to one that already exists: the congregation's details come from the hub.
    private static let joinOrder: [Step] = [.welcome, .join, .languages, .microphone, .claude, .finish]
    private var order: [Step] { joined ? Self.joinOrder : Self.createOrder }

    init() {
        let joined = OnboardingProgress.joined
        var s = OnboardingProgress.step ?? .welcome
        if let token = Keychain.get(.deviceToken), !token.isEmpty {   // already signed up: go past sign-up
            if joined {
                if s == .welcome || s == .join { s = .languages }
            } else if let i = Self.createOrder.firstIndex(of: s), i <= Self.createOrder.firstIndex(of: .email)! {
                s = (Keychain.get(.connectCode) ?? "").isEmpty ? .finish : .connect
            }
        }
        if !joined && s == .join { s = .welcome }
        _step = State(initialValue: s)
        _joined = State(initialValue: joined)
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
            HStack {
                if step != .welcome && step != .connect && step != .finish {
                    Button("Back") { move(-1) }
                }
                Spacer()
                if busy { ProgressView().controlSize(.small) }
                Button(buttonTitle) { Task { await next() } }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!canContinue || busy)
            }
            .padding(16)
        }
        .onChange(of: step) { _, s in OnboardingProgress.step = s }
        .onChange(of: joined) { _, j in OnboardingProgress.joined = j }
    }

    private var buttonTitle: String {
        switch step {
        case .welcome: return "Get started"
        case .email: return "Sign up"
        case .join: return "Connect"
        case .finish: return "Open Hear Church"
        case .microphone where model.mic.permission != .authorized: return "Skip for now"
        default: return "Continue"
        }
    }

    @ViewBuilder private var content: some View {
        switch step {
        case .welcome:
            WelcomeStep(onJoin: { error = nil; joined = true; step = .join })
        case .church: ChurchStep(profile: $model.profile)
        case .name: NameStep(profile: $model.profile)
        case .place: PlaceStep(profile: $model.profile)
        case .times: TimesStep(profile: $model.profile)
        case .languages: LanguagesStep()
        case .microphone: MicrophoneStep()
        case .claude: ClaudeStep()
        case .approval: ApprovalStep(profile: $model.profile)
        case .email: EmailStep(profile: $model.profile)
        case .connect: ConnectCodeStep(code: connectCode, congregation: model.profile.name)
        case .join: JoinStep(slug: $joinSlug, title: $joinTitle, code: $joinCode)
        case .finish: FinishStep()
        }
    }

    private var canContinue: Bool {
        let p = model.profile
        switch step {
        case .church: return !p.church.trimmingCharacters(in: .whitespaces).isEmpty
        case .name: return p.name.trimmingCharacters(in: .whitespaces).count >= 2
        case .place: return p.lat != nil
        case .languages: return !p.languages.isEmpty
        case .claude: return model.keyState == .ok
        case .approval: return p.approved
        case .email: return p.email.contains("@") && p.email.contains(".")
        case .join: return !joinSlug.isEmpty && joinCode.filter(\.isNumber).count == 6
        default: return true
        }
    }

    private static let keychainError = "Could not save to this Mac's Keychain. Unlock the Keychain (or allow access) and try again."

    private func move(_ by: Int) {
        error = nil
        guard let i = order.firstIndex(of: step) else { return }
        step = order[max(0, min(order.count - 1, i + by))]
        if step == .welcome { joined = false }
        model.profile.save()
    }

    private func next() async {
        error = nil
        switch step {
        case .email:
            busy = true
            defer { busy = false }
            do {
                let r = try await HubClient.signUp(model.profile, deviceName: model.deviceName)
                guard let token = r.deviceToken, !token.isEmpty else {
                    // The hub asked for e-mail confirmation, which this version has no way to finish.
                    error = "hearchurch.com asked for e-mail confirmation, which this version of Hear Church can't complete yet. Try again later."
                    return
                }
                guard Keychain.set(.deviceToken, token) else { error = Self.keychainError; return }
                model.profile.slug = r.slug
                model.profile.claim = ""
                model.profile.save()
                if let code = r.connectCode, !code.isEmpty {
                    if !Keychain.set(.connectCode, code) { Keychain.delete(.connectCode) }
                    connectCode = code
                    step = .connect
                } else {
                    connectCode = ""
                    step = .finish
                }
            } catch {
                self.error = error.localizedDescription
            }
        case .join:
            busy = true
            defer { busy = false }
            do {
                let j = try await HubClient.connect(slug: joinSlug, code: joinCode.filter(\.isNumber), deviceName: model.deviceName)
                guard Keychain.set(.deviceToken, j.token) else { error = Self.keychainError; return }
                Keychain.delete(.connectCode)
                model.profile.slug = j.slug
                if !j.name.isEmpty { model.profile.name = j.name }
                if !j.church.isEmpty { model.profile.church = j.church }
                model.profile.approved = true   // whoever set the congregation up already confirmed it
                model.profile.claim = ""
                model.profile.save()
                joinCode = ""
                move(1)
            } catch {
                self.error = error.localizedDescription
            }
        case .connect:
            move(1)
        case .finish:
            OnboardingProgress.clear()
            model.finishSetup()
        default:
            move(1)
        }
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

private struct WelcomeStep: View {
    let onJoin: () -> Void

    var body: some View {
        StepTitle(title: "Welcome to Hear Church",
                  subtitle: "This Mac listens to your meeting and translates it live. Members hear the translation and read captions on their own phones, through hearchurch.com.")
        Label("About 5 minutes to set up", systemImage: "clock")
        Label("You'll need an Anthropic API key for Claude, which does the translating", systemImage: "key")
        Label("Voices download while you finish setting up", systemImage: "arrow.down.circle")
        Divider()
        Button("I already set up my congregation", action: onJoin)
        Text("Use this to connect another computer to a congregation that is already on hearchurch.com.")
            .font(.callout).foregroundStyle(.secondary)
        HelpLinks()
    }
}

/// Privacy, Terms and support, opened in the default browser / mail app.
struct HelpLinks: View {
    var body: some View {
        HStack(spacing: 16) {
            Link("Privacy", destination: URL(string: "https://hearchurch.com/privacy")!)
            Link("Terms", destination: URL(string: "https://hearchurch.com/terms")!)
            Link("Contact support", destination: URL(string: "mailto:support@hearchurch.com")!)
        }
        .font(.callout)
    }
}

/// Shown once after sign-up: the code another computer needs to connect to this congregation.
private struct ConnectCodeStep: View {
    let code: String
    let congregation: String

    var body: some View {
        StepTitle(title: "Your connect code",
                  subtitle: "You're signed up. Write this code down: you'll need it if you ever set up Hear Church on another computer.")
        Text(HubClient.spaced(code))
            .font(.system(size: 54, weight: .bold, design: .monospaced))
            .textSelection(.enabled)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 12)
            .background(.background.secondary, in: RoundedRectangle(cornerRadius: 10))
        Text("Use this code to connect another computer to \(congregation.isEmpty ? "your congregation" : congregation). Keep it private.")
            .foregroundStyle(.secondary)
        Text("It is saved in this Mac's Keychain, and you can see it again or make a new one in Settings.")
            .font(.callout).foregroundStyle(.secondary)
    }
}

/// "I already set up my congregation": find it by name or pasted link, then the code.
private struct JoinStep: View {
    @Binding var slug: String
    @Binding var title: String
    @Binding var code: String
    @State private var query = ""
    @State private var results: [HubClient.Match] = []
    @State private var searching = false
    @State private var note = ""

    var body: some View {
        StepTitle(title: "Connect to your congregation",
                  subtitle: "Find it by name, or paste its listener link (hearchurch.com/c/…). Then enter the connect code that came with the first setup.")
        if slug.isEmpty {
            HStack {
                TextField("Congregation name, town, or listener link", text: $query).onSubmit { Task { await find() } }
                Button("Find") { Task { await find() } }.disabled(query.trimmingCharacters(in: .whitespaces).count < 2 || searching)
            }
            if searching { ProgressView().controlSize(.small) }
            ForEach(results) { m in
                Button { slug = m.slug; title = m.title } label: {
                    HStack {
                        Text(m.title)
                        if !m.city.isEmpty { Text(m.city).foregroundStyle(.secondary) }
                        Spacer()
                        Image(systemName: "chevron.right").foregroundStyle(.tertiary)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                Divider()
            }
            if !note.isEmpty { Text(note).font(.callout).foregroundStyle(.secondary) }
        } else {
            LabeledContent("Congregation") {
                HStack {
                    Text(title.isEmpty ? slug : title).bold()
                    Button("Change") { slug = ""; title = ""; code = "" }.buttonStyle(.link)
                }
            }
            TextField("Connect code (6 digits)", text: $code)
                .font(.system(.title2, design: .monospaced))
                .onChange(of: code) { _, v in
                    let digits = String(v.filter(\.isNumber).prefix(6))
                    let shown = HubClient.spaced(digits)
                    if shown != v { code = shown }
                }
            Text("Ask whoever set up Hear Church for the current code.").font(.callout).foregroundStyle(.secondary)
        }
    }

    private func find() async {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        note = ""
        if let s = HubClient.slug(fromLink: q) {
            slug = s
            title = ""
            if let m = (try? await HubClient.search(s))?.first(where: { $0.slug == s }) { title = m.title }
            return
        }
        searching = true
        defer { searching = false }
        do {
            results = try await HubClient.search(q)
            if results.isEmpty { note = "No congregation found. Try the town, or paste its listener link." }
        } catch {
            note = error.localizedDescription
        }
    }
}

private struct ChurchStep: View {
    @Binding var profile: Profile
    @State private var choice = ""

    var body: some View {
        StepTitle(title: "Your church", subtitle: "Shown to listeners next to your congregation's name.")
        Picker("Church", selection: $choice) {
            Text("Choose…").tag("")
            ForEach(Churches.all, id: \.self) { Text($0).tag($0) }
            Text("Other").tag("other")
        }
        .onAppear { choice = Churches.all.contains(profile.church) ? profile.church : (profile.church.isEmpty ? "" : "other") }
        .onChange(of: choice) { _, v in
            if v != "other" { profile.church = v } else if Churches.all.contains(profile.church) { profile.church = "" }
            if v == Churches.latterDaySaints {
                if !["ward", "branch"].contains(profile.unitType) { profile.unitType = "ward" }
            } else {
                profile.unitType = "congregation"
            }
        }
        if choice == "other" { TextField("Name of your church", text: $profile.church) }
    }
}

private struct NameStep: View {
    @Binding var profile: Profile

    var body: some View {
        if profile.isLatterDaySaint {
            StepTitle(title: "Ward or branch", subtitle: "As members know it, e.g. Maple Grove Ward.")
            Picker("", selection: $profile.unitType) {
                Text("Ward").tag("ward")
                Text("Branch").tag("branch")
            }
            .pickerStyle(.segmented).labelsHidden().frame(width: 200)
            TextField("\(profile.unitLabel) name", text: $profile.name)
            TextField("Stake (optional)", text: $profile.stake)
        } else {
            StepTitle(title: "Congregation", subtitle: "Its name as members know it.")
            TextField("Congregation name", text: $profile.name)
        }
    }
}

private struct PlaceStep: View {
    @Binding var profile: Profile
    @State private var camera: MapCameraPosition = .automatic
    @State private var looking = false
    @State private var note = ""

    private var pin: CLLocationCoordinate2D? {
        guard let lat = profile.lat, let lng = profile.lng else { return nil }
        return CLLocationCoordinate2D(latitude: lat, longitude: lng)
    }

    var body: some View {
        StepTitle(title: "Where you meet",
                  subtitle: "Members find your meeting by distance, so place the pin on the building where you meet. Only the building's location is saved.")
        HStack {
            TextField("Building address", text: $profile.address).onSubmit { Task { await find() } }
            Button("Find") { Task { await find() } }.disabled(profile.address.isEmpty || looking)
        }
        Button("Use this Mac's location") { Task { await here() } }.disabled(looking)
        MapReader { proxy in
            Map(position: $camera) {
                if let pin { Marker(profile.name.isEmpty ? "Building" : profile.name, coordinate: pin) }
            }
            .onTapGesture { point in
                if let c = proxy.convert(point, from: .local) { setPin(c, recenter: false) }
            }
        }
        .frame(height: 320)
        .clipShape(RoundedRectangle(cornerRadius: 8))
        Text(pin == nil ? "Find the address, then click the map to put the pin exactly on the building."
                        : "Not quite right? Click the map on the building to move the pin.")
            .font(.callout).foregroundStyle(.secondary)
        if !note.isEmpty { Text(note).font(.callout).foregroundStyle(.orange) }
    }

    private func setPin(_ c: CLLocationCoordinate2D, recenter: Bool = true) {
        profile.lat = (c.latitude * 1e6).rounded() / 1e6
        profile.lng = (c.longitude * 1e6).rounded() / 1e6
        if recenter {
            camera = .region(MKCoordinateRegion(center: c, latitudinalMeters: 600, longitudinalMeters: 600))
        }
    }

    private func find() async {
        looking = true
        defer { looking = false }
        note = ""
        let found = try? await CLGeocoder().geocodeAddressString(profile.address)
        if let c = found?.first?.location?.coordinate {
            setPin(c)
        } else {
            note = "We couldn't find that address. Click the map on the building instead."
        }
    }

    private func here() async {
        looking = true
        defer { looking = false }
        note = ""
        guard let c = await OneShotLocation().locate() else {
            note = "This Mac couldn't tell where it is. Type the address instead."
            return
        }
        setPin(c)
        note = "A Mac's location comes from Wi-Fi and can be off by a few blocks: check the pin is on the building."
        if let place = try? await CLGeocoder().reverseGeocodeLocation(CLLocation(latitude: c.latitude, longitude: c.longitude)).first,
           profile.address.isEmpty {
            profile.address = [place.subThoroughfare, place.thoroughfare, place.locality, place.administrativeArea]
                .compactMap { $0 }.joined(separator: " ")
        }
    }
}

private struct TimesStep: View {
    @Binding var profile: Profile
    static let days = [("sun", "Sunday"), ("mon", "Monday"), ("tue", "Tuesday"), ("wed", "Wednesday"),
                       ("thu", "Thursday"), ("fri", "Friday"), ("sat", "Saturday")]

    var body: some View {
        StepTitle(title: "Meeting times", subtitle: "Listeners see when your next meeting starts (\(TimeZone.current.identifier) time).")
        ForEach($profile.meetingTimes) { $t in
            HStack {
                Picker("", selection: $t.day) { ForEach(Self.days, id: \.0) { Text($0.1).tag($0.0) } }
                    .labelsHidden().frame(width: 140)
                DatePicker("", selection: timeBinding($t), displayedComponents: .hourAndMinute).labelsHidden()
                Button(role: .destructive) { profile.meetingTimes.removeAll { $0.id == t.id } } label: { Image(systemName: "minus.circle") }
                    .buttonStyle(.borderless)
            }
        }
        Button("Add a meeting time") { profile.meetingTimes.append(MeetingTime()) }
    }

    private func timeBinding(_ t: Binding<MeetingTime>) -> Binding<Date> {
        Binding(get: {
            let parts = t.wrappedValue.time.split(separator: ":").compactMap { Int($0) }
            return Calendar.current.date(bySettingHour: parts.first ?? 10, minute: parts.last ?? 0, second: 0, of: Date()) ?? Date()
        }, set: { d in
            let c = Calendar.current.dateComponents([.hour, .minute], from: d)
            t.wrappedValue.time = String(format: "%02d:%02d", c.hour ?? 10, c.minute ?? 0)
        })
    }
}

/// A language row with its download size and state; used in setup and on the operator page.
struct LanguageToggle: View {
    @EnvironmentObject var model: AppModel
    let language: OutputLanguage

    var body: some View {
        let on = model.profile.languages.contains(language.id)
        let pkg = model.engine.packages.first { $0.id == language.id }
        Toggle(isOn: Binding(get: { on }, set: { model.setLanguage(language.id, on: $0) })) {
            HStack {
                Text(language.flag)
                Text(language.name)
                Spacer()
                if !language.available {
                    Text(language.unavailableReason ?? "Not available").foregroundStyle(.secondary)
                } else {
                    PackageBadge(package: pkg, sizeMB: language.sizeMB)
                }
            }
        }
        .toggleStyle(.checkbox)
        .disabled(!language.available || (on && model.profile.languages.count == 1 && model.profile.setupDone))
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
    @EnvironmentObject var model: AppModel

    var body: some View {
        StepTitle(title: "Languages",
                  subtitle: "The meeting is in English; listeners pick one of these. A language starts downloading as soon as you choose it. Keep going while it does.")
        ForEach(OutputLanguage.all) { LanguageToggle(language: $0) }
        Divider()
        HStack {
            Text("Speech recognition").foregroundStyle(.secondary)
            Text("needed for every language").font(.callout).foregroundStyle(.tertiary)
            Spacer()
            PackageBadge(package: model.engine.packages.first { $0.id == "speech" }, sizeMB: speechDownloadMB)
        }
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
        }
        .onAppear { model.startMicPreview() }
        .onDisappear { model.mic.stop() }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            model.startMicPreview()   // back from System Settings
        }
    }
}

struct ClaudeKeyGuide: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("1. Sign in at console.anthropic.com (or create an account).")
            Text("2. Billing: add a payment method and buy some credit. $10–25 is plenty to start.")
            Text("3. Limits: set a monthly spend limit. $25 a month covers a few Sundays for most congregations.")
            Text("4. API keys: Create Key, name it \"Hear Church\", and copy it.")
            Text("5. Paste it below.")
            Link("Open the Anthropic console", destination: URL(string: "https://console.anthropic.com/settings/keys")!)
        }
        .font(.callout)
    }
}

private struct ClaudeStep: View {
    @EnvironmentObject var model: AppModel
    @State private var key = ""
    @State private var error: String?

    var body: some View {
        StepTitle(title: "Claude translates",
                  subtitle: "Hear Church uses Claude, from Anthropic, to translate. You bring your own API key, and Anthropic bills you directly for what you use. The key is kept in this Mac's Keychain.")
        ClaudeKeyGuide()
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
        Button("Start with a free first Sunday instead") {}
            .disabled(true)
            .help("Coming soon")
    }

    private func save() async { error = await model.saveKey(key) }
}

private struct ApprovalStep: View {
    @Binding var profile: Profile

    var body: some View {
        StepTitle(title: "Permission",
                  subtitle: "While a meeting is live, anyone nearby can find it on hearchurch.com and listen; there is no PIN. Anyone with your link or QR code can listen from anywhere.")
        Toggle(profile.isLatterDaySaint ? "My bishop or branch president has approved translating and streaming this meeting."
                                        : "My pastor or leader has approved translating and streaming this meeting.",
               isOn: $profile.approved)
        Text("Hear Church is an independent project, not affiliated with or endorsed by any church.")
            .font(.callout).foregroundStyle(.secondary)
    }
}

private struct EmailStep: View {
    @Binding var profile: Profile

    var body: some View {
        StepTitle(title: "Your e-mail",
                  subtitle: "In case we need to reach you about your congregation's page on hearchurch.com. We don't send newsletters.")
        TextField("name@example.com", text: $profile.email)
            .textContentType(.emailAddress)
    }
}

private struct FinishStep: View {
    @EnvironmentObject var model: AppModel
    @State private var atLogin = true

    var body: some View {
        StepTitle(title: "All set",
                  subtitle: "On Sunday, open the lid, check the list, and press Start meeting. Keep the Mac plugged in with the lid open during the meeting.")
        if let url = model.listenerURL {
            LabeledContent("Your listeners' page") { Link(url.absoluteString, destination: url) }
        }
        Toggle("Open Hear Church when this Mac starts", isOn: $atLogin)
            .onChange(of: atLogin) { _, on in LoginItem.set(on) }
            .onAppear { if !LoginItem.enabled { LoginItem.set(true) } }
    }
}

enum Format {
    static func size(_ mb: Double) -> String {
        mb >= 1000 ? String(format: "%.1f GB", mb / 1000) : "\(Int(mb.rounded())) MB"
    }
}

/// Where setup stopped, so quitting in the middle resumes there.
enum OnboardingProgress {
    private static let stepKey = "OnboardingStep", joinedKey = "OnboardingJoined"
    fileprivate static var step: OnboardingView.Step? {
        get { UserDefaults.standard.string(forKey: stepKey).flatMap(OnboardingView.Step.init(rawValue:)) }
        set { UserDefaults.standard.set(newValue?.rawValue, forKey: stepKey) }
    }
    static var joined: Bool {
        get { UserDefaults.standard.bool(forKey: joinedKey) }
        set { UserDefaults.standard.set(newValue, forKey: joinedKey) }
    }
    static func clear() {
        UserDefaults.standard.removeObject(forKey: stepKey)
        UserDefaults.standard.removeObject(forKey: joinedKey)
    }
}
