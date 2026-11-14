import AVFoundation
import CoreLocation
import MapKit
import SwiftUI

/// First launch: create a congregation (describe it, pick its languages, which start downloading, allow the
/// microphone, credits or own key, its e-mails, then sign up with hearchurch.com for this Mac's device token), or
/// connect to one that exists with a code (from "Add device" on another Mac, or e-mailed to the congregation).
/// Billing: a congregation pays with credits, unless Harrison gave it a one-time own-key code. Until credits can be
/// bought, the Claude step needs that code (or a congregation that already uses its own key) before it takes a key.
struct OnboardingView: View {
    @EnvironmentObject var model: AppModel
    @State private var step: Step
    @State private var joined: Bool
    @State private var error: String?
    @State private var busy = false
    // "Connect to my congregation"
    @State private var joinCode = ""
    @State private var ownKey = OnboardingProgress.ownKey   // this congregation may use its own Anthropic key

    enum Step: String { case welcome, church, name, place, times, languages, microphone, claude, email, join, finish }

    /// Setting up a new congregation. (No "check your e-mail" step: the hub hands the device token back at once.)
    /// No permission step: whoever sets it up is taken to have it.
    private static let createOrder: [Step] = [.welcome, .church, .name, .place, .times, .languages, .microphone, .claude, .email, .finish]
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
                s = .finish
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
                if step != .welcome && step != .finish {
                    Button("Back") { move(-1) }
                }
                Spacer()
                if busy { ProgressView().controlSize(.small) }
                if step != .welcome {   // the welcome step's two choices are its buttons
                    Button(buttonTitle) { Task { await next() } }
                        .keyboardShortcut(.defaultAction)
                        .disabled(!canContinue || busy)
                }
            }
            .padding(16)
        }
        .onChange(of: step) { _, s in OnboardingProgress.step = s }
        .onChange(of: joined) { _, j in OnboardingProgress.joined = j }
        .onChange(of: ownKey) { _, o in OnboardingProgress.ownKey = o }
    }

    private var buttonTitle: String {
        switch step {
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
            WelcomeStep(onCreate: { error = nil; joined = false; step = .church },
                        onJoin: { error = nil; joined = true; step = .join })
        case .church: ChurchStep(profile: $model.profile)
        case .name: NameStep(profile: $model.profile)
        case .place: PlaceStep(profile: $model.profile)
        case .times: TimesStep(profile: $model.profile)
        case .languages: LanguagesStep()
        case .microphone: MicrophoneStep()
        case .claude: ClaudeStep(ownKey: $ownKey)
        case .email: EmailStep(profile: $model.profile)
        case .join: JoinStep(code: $joinCode)
        case .finish: FinishStep()
        }
    }

    private var canContinue: Bool {
        let p = model.profile
        switch step {
        case .church: return !p.church.trimmingCharacters(in: .whitespaces).isEmpty
        case .name: return p.name.trimmingCharacters(in: .whitespaces).count >= 2
        case .place: return p.lat != nil
        case .languages: return !p.speakers.isEmpty && !p.targets.isEmpty
        case .claude: return !ownKey || model.keyState == .ok   // credits need no key
        case .email: return Self.isEmail(p.email) && p.extraEmails.allSatisfy { $0.trimmingCharacters(in: .whitespaces).isEmpty || Self.isEmail($0) }
        case .join: return joinCode.filter { $0.isLetter || $0.isNumber }.count == 8
        default: return true
        }
    }

    static func isEmail(_ s: String) -> Bool {
        let t = s.trimmingCharacters(in: .whitespaces)
        guard let at = t.firstIndex(of: "@"), at > t.startIndex else { return false }
        return t[t.index(after: at)...].contains(".") && !t.contains(" ")
    }

    private static let keychainError = "Could not save to this Mac's Keychain. Unlock the Keychain (or allow access) and try again."

    private func move(_ by: Int) {
        error = nil
        guard let i = order.firstIndex(of: step) else { return }
        step = order[max(0, min(order.count - 1, i + by))]
        if step == .welcome {
            joined = false
            if (Keychain.get(.ownKeyCode) ?? "").isEmpty { ownKey = false }   // it came from the congregation joined
        }
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
                model.profile.approved = true   // inferred: whoever sets it up has the leaders' go-ahead
                model.profile.extraEmails = model.profile.extraEmails.map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
                model.profile.save()
                step = .finish
                if let e = await redeemOwnKeyCode() { error = e }   // tried again on the last step
            } catch {
                self.error = error.localizedDescription
            }
        case .join:
            busy = true
            defer { busy = false }
            do {
                let j = try await HubClient.pair(code: joinCode, deviceName: model.deviceName)
                guard Keychain.set(.deviceToken, j.token) else { error = Self.keychainError; return }
                Keychain.delete(.connectCode)
                model.profile.slug = j.slug
                if !j.name.isEmpty { model.profile.name = j.name }
                if !j.church.isEmpty { model.profile.church = j.church }
                if !j.unitType.isEmpty { model.profile.unitType = j.unitType }
                model.profile.stake = j.stake
                if !j.address.isEmpty { model.profile.address = j.address }
                Task { await model.pushChurch() }   // the glossary follows the congregation's church
                model.profile.approved = true   // whoever set the congregation up already confirmed it
                if j.billing == "own_key" { ownKey = true }
                model.profile.claim = ""
                model.profile.save()
                joinCode = ""
                move(1)
            } catch {
                self.error = error.localizedDescription
            }
        case .finish:
            busy = true
            defer { busy = false }
            if let e = await redeemOwnKeyCode() { error = e; return }
            OnboardingProgress.clear()
            model.finishSetup()
        default:
            move(1)
        }
    }

    /// Use up the own-key code checked on the Claude step, now that the congregation exists. nil when done (or
    /// there was none); otherwise the message to show.
    private func redeemOwnKeyCode() async -> String? {
        guard let code = Keychain.get(.ownKeyCode), !code.isEmpty,
              let token = Keychain.get(.deviceToken), !token.isEmpty else { return nil }
        do {
            try await HubClient.redeemOwnKeyCode(code, token: token)
            Keychain.delete(.ownKeyCode)
            return nil
        } catch {
            return "Your congregation is set up, but its own-key code wasn't accepted: \(error.localizedDescription)"
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
    let onCreate: () -> Void
    let onJoin: () -> Void

    var body: some View {
        StepTitle(title: "Welcome to Hear Church",
                  subtitle: "This Mac listens to your meeting and translates it live. Members hear the translation and read captions on their own phones, through hearchurch.com.")
        choice("Create a congregation", "plus.circle.fill",
               "First time on Hear Church: describe your congregation and choose its languages. About 5 minutes.", onCreate)
            .keyboardShortcut(.defaultAction)
        choice("Connect to my congregation", "link.circle.fill",
               "Your congregation already uses Hear Church on another Mac: connect this one with a code.", onJoin)
        Divider().padding(.vertical, 4)
        Label("Voices download while you finish setting up", systemImage: "arrow.down.circle").foregroundStyle(.secondary)
        HelpLinks()
    }

    private func choice(_ title: String, _ icon: String, _ detail: String, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 14) {
                Image(systemName: icon).font(.system(size: 30)).foregroundStyle(Color.accentColor)
                VStack(alignment: .leading, spacing: 4) {
                    Text(title).font(.title3.weight(.semibold))
                    Text(detail).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                Image(systemName: "chevron.right").foregroundStyle(.tertiary)
            }
            .padding(16)
            .background(Color.secondary.opacity(0.1), in: RoundedRectangle(cornerRadius: 10))
            .contentShape(RoundedRectangle(cornerRadius: 10))
        }
        .buttonStyle(.plain)
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

/// "Connect to my congregation": a code from Settings → Add device on a connected Mac, or one e-mailed to every
/// address of the congregation.
private struct JoinStep: View {
    @Binding var code: String
    @State private var byEmail = false
    @State private var email = ""
    @State private var sent: String?
    @State private var sending = false
    @State private var problem: String?

    var body: some View {
        StepTitle(title: "Connect to your congregation",
                  subtitle: "Enter a code to connect this Mac. Codes work once, for 15 minutes.")
        Picker("", selection: $byEmail) {
            Text("I have a code").tag(false)
            Text("Email me a code").tag(true)
        }
        .pickerStyle(.segmented).labelsHidden().frame(maxWidth: 320)
        if byEmail {
            Text("Enter one of your congregation's e-mail addresses. A code goes to every address of the congregation.")
                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            HStack {
                TextField("name@example.com", text: $email).textContentType(.emailAddress).onSubmit { Task { await send() } }
                Button(sent == nil ? "Send code" : "Send again") { Task { await send() } }
                    .disabled(!OnboardingView.isEmail(email) || sending)
                if sending { ProgressView().controlSize(.small) }
            }
            if let sent { Label(sent, systemImage: "envelope").foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true) }
            if let problem { Text(problem).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true) }
        } else {
            Text("On a Mac already connected to your congregation, open Hear Church → Settings → Congregation → Add device. Enter the code it shows.")
                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
        if !byEmail || sent != nil {
            TextField("XXXX-XXXX", text: $code)
                .font(.system(.title, design: .monospaced))
                .frame(maxWidth: 260)
                .onChange(of: code) { _, v in
                    let shown = HubClient.pairingFormat(v)
                    if shown != v { code = shown }
                }
        }
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
}

private struct ChurchStep: View {
    @Binding var profile: Profile
    @State private var choice = ""

    var body: some View {
        StepTitle(title: "Your church", subtitle: "Shown to listeners next to your congregation's name. It also chooses the glossary Claude follows: Christian terms for every church, and Latter-day Saint terms for The Church of Jesus Christ of Latter-day Saints.")
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
                  subtitle: "Choose the languages people speak at the microphone, and the languages listeners can choose. Every language you choose downloads now: keep going while it does. You can change them before each meeting.")
        LanguageSelector(showTitle: false)
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
    @Binding var ownKey: Bool
    @State private var key = ""
    @State private var code = ""
    @State private var checking = false
    @State private var error: String?

    var body: some View {
        if ownKey {
            StepTitle(title: "Claude translates",
                      subtitle: "Hear Church uses Claude, from Anthropic, to translate. Your congregation uses its own API key, and Anthropic bills you directly for what you use. The key is kept in this Mac's Keychain.")
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
        } else {
            StepTitle(title: "Claude translates",
                      subtitle: "Hear Church uses Claude, from Anthropic, to translate. You pay with prepaid credit: $5 per language for each hour of speech, and the first hour is free.")
            Text("Nothing to set up here: continue, and your first hour is free. You can buy credit later on the speaker page.")
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            Divider().padding(.vertical, 4)
            Text("Did we give you a setup code for your own Anthropic key? Enter it here. Each code works once.")
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                TextField("HC-XXXX-XXXX-XXXX", text: $code).onSubmit { Task { await useCode() } }
                Button("Use code") { Task { await useCode() } }
                    .disabled(code.trimmingCharacters(in: .whitespaces).isEmpty || checking)
                if checking { ProgressView().controlSize(.small) }
            }
        }
        if let error { Text(error).foregroundStyle(.red) }
    }

    private func save() async { error = await model.saveKey(key) }

    /// Check the code with hearchurch.com; it is used up after sign-up, once the congregation exists.
    private func useCode() async {
        let c = code.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !c.isEmpty else { return }
        checking = true
        defer { checking = false }
        error = nil
        do {
            try await HubClient.checkOwnKeyCode(c)
            guard Keychain.set(.ownKeyCode, c) else {
                error = "Could not save to this Mac's Keychain. Unlock the Keychain (or allow access) and try again."
                return
            }
            code = ""
            ownKey = true
        } catch {
            self.error = error.localizedDescription
        }
    }
}

private struct EmailStep: View {
    @Binding var profile: Profile

    var body: some View {
        StepTitle(title: "E-mail",
                  subtitle: "The main e-mail gets the receipts when you buy credit. Add the addresses of others who look after Hear Church: any of them can get a code to connect another Mac. We don't send newsletters.")
        LabeledContent("Main e-mail") {
            TextField("name@example.com", text: $profile.email).textContentType(.emailAddress)
        }
        ForEach(profile.extraEmails.indices, id: \.self) { i in
            LabeledContent(i == 0 ? "Also" : "") {
                HStack {
                    TextField("another@example.com", text: Binding(
                        get: { i < profile.extraEmails.count ? profile.extraEmails[i] : "" },
                        set: { if i < profile.extraEmails.count { profile.extraEmails[i] = $0 } }))
                    Button(role: .destructive) { profile.extraEmails.remove(at: i) } label: { Image(systemName: "minus.circle") }
                        .buttonStyle(.borderless)
                }
            }
        }
        if profile.extraEmails.count < 9 {
            Button("Add another e-mail") { profile.extraEmails.append("") }
        }
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
    private static let stepKey = "OnboardingStep", joinedKey = "OnboardingJoined", ownKeyKey = "OnboardingOwnKey"
    fileprivate static var step: OnboardingView.Step? {
        get { UserDefaults.standard.string(forKey: stepKey).flatMap(OnboardingView.Step.init(rawValue:)) }
        set { UserDefaults.standard.set(newValue?.rawValue, forKey: stepKey) }
    }
    static var joined: Bool {
        get { UserDefaults.standard.bool(forKey: joinedKey) }
        set { UserDefaults.standard.set(newValue, forKey: joinedKey) }
    }
    /// The congregation may use its own Anthropic key: a one-time code was accepted, or the congregation this Mac
    /// joined already uses its own key.
    static var ownKey: Bool {
        get { UserDefaults.standard.bool(forKey: ownKeyKey) }
        set { UserDefaults.standard.set(newValue, forKey: ownKeyKey) }
    }
    static func clear() {
        UserDefaults.standard.removeObject(forKey: stepKey)
        UserDefaults.standard.removeObject(forKey: joinedKey)
        UserDefaults.standard.removeObject(forKey: ownKeyKey)
        Keychain.delete(.ownKeyCode)
    }
}
