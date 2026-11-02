import CoreLocation
import MapKit
import SwiftUI

/// First launch: describe the congregation, add a Claude key, verify the e-mail, get a device token.
struct OnboardingView: View {
    @EnvironmentObject var model: AppModel
    @State private var step: Step = .welcome
    @State private var newCongregation = true
    @State private var error: String?
    @State private var busy = false

    enum Step: Int { case welcome, church, name, place, times, languages, claude, approval, email, waiting, finish }

    private var order: [Step] {
        newCongregation
            ? [.welcome, .church, .name, .place, .times, .languages, .claude, .approval, .email, .waiting, .finish]
            : [.welcome, .email, .waiting, .languages, .claude, .finish]
    }

    var body: some View {
        VStack(spacing: 0) {
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
                if step != .welcome && step != .waiting && step != .finish {
                    Button("Back") { move(-1) }
                }
                Spacer()
                if busy { ProgressView().controlSize(.small) }
                if step != .welcome && step != .waiting {
                    Button(step == .finish ? "Start using Hear Church" : step == .email ? "Send the link" : "Continue") {
                        Task { await next() }
                    }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!canContinue || busy)
                }
            }
            .padding(16)
        }
    }

    @ViewBuilder private var content: some View {
        switch step {
        case .welcome: WelcomeStep { isNew in newCongregation = isNew; move(1) }
        case .church: ChurchStep(profile: $model.profile)
        case .name: NameStep(profile: $model.profile)
        case .place: PlaceStep(profile: $model.profile)
        case .times: TimesStep(profile: $model.profile)
        case .languages: LanguagesStep(profile: $model.profile)
        case .claude: ClaudeStep()
        case .approval: ApprovalStep(profile: $model.profile)
        case .email: EmailStep(profile: $model.profile, newCongregation: newCongregation)
        case .waiting: WaitingStep(newCongregation: newCongregation, onToken: { move(1) }, onBack: { move(-1) })
        case .finish: FinishStep()
        }
    }

    private var canContinue: Bool {
        let p = model.profile
        switch step {
        case .church: return !p.church.trimmingCharacters(in: .whitespaces).isEmpty
        case .name: return p.name.trimmingCharacters(in: .whitespaces).count >= 2
        case .place: return p.lat != nil
        case .times: return true
        case .languages: return !p.languages.isEmpty
        case .claude: return model.keyState == .ok
        case .approval: return p.approved
        case .email: return p.email.contains("@") && p.email.contains(".")
        default: return true
        }
    }

    private func move(_ by: Int) {
        error = nil
        guard let i = order.firstIndex(of: step) else { return }
        step = order[max(0, min(order.count - 1, i + by))]
        model.profile.save()
    }

    private func next() async {
        error = nil
        switch step {
        case .email:
            busy = true
            defer { busy = false }
            do {
                if newCongregation {
                    let r = try await HubClient.signUp(model.profile, deviceName: model.deviceName)
                    model.profile.slug = r.slug
                    model.profile.claim = r.claim
                } else {
                    model.profile.claim = try await HubClient.deviceLink(email: model.profile.email, slug: "",
                                                                         deviceName: model.deviceName)
                }
                move(1)
            } catch {
                self.error = error.localizedDescription
            }
        case .finish:
            model.profile.claim = ""
            model.profile.setupDone = true
            model.profile.save()
            model.writeLanguages()
            model.launch()
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
    let choose: (Bool) -> Void
    var body: some View {
        StepTitle(title: "Welcome to Hear Church",
                  subtitle: "This Mac listens to your meeting and translates it live. Members hear the translation and read captions on their own phones.")
        Button { choose(true) } label: {
            Label("Set up our congregation", systemImage: "plus.circle").frame(maxWidth: .infinity, minHeight: 36)
        }
        .buttonStyle(.borderedProminent)
        Button { choose(false) } label: {
            Label("Our congregation already uses Hear Church: add this Mac", systemImage: "laptopcomputer")
                .frame(maxWidth: .infinity, minHeight: 36)
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

private struct LanguagesStep: View {
    @Binding var profile: Profile

    var body: some View {
        StepTitle(title: "Languages", subtitle: "The meeting is in English; listeners pick one of these. Each language uses its own share of the Claude API.")
        ForEach(OutputLanguage.all) { lang in
            Toggle(isOn: Binding(get: { profile.languages.contains(lang.id) }, set: { on in
                profile.languages.removeAll { $0 == lang.id }
                if on { profile.languages.append(lang.id) }
            })) {
                Text(lang.name) + Text(lang.available ? "" : "  coming soon").foregroundStyle(.secondary)
            }
            .disabled(!lang.available)
        }
        Text("Voices download in the background the first time (about 2 GB with speech recognition).")
            .font(.callout).foregroundStyle(.secondary)
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
    let newCongregation: Bool

    var body: some View {
        if newCongregation {
            StepTitle(title: "Your e-mail",
                      subtitle: "We send a link to confirm it. The same address lets you add another Mac later.")
        } else {
            StepTitle(title: "Add this Mac",
                      subtitle: "Enter the e-mail your congregation signed up with. We send a link to it.")
        }
        TextField("name@example.com", text: $profile.email)
            .textContentType(.emailAddress)
    }
}

private struct WaitingStep: View {
    @EnvironmentObject var model: AppModel
    let newCongregation: Bool
    let onToken: () -> Void
    let onBack: () -> Void
    @State private var error: String?
    @State private var resent = false

    var body: some View {
        StepTitle(title: "Check your e-mail",
                  subtitle: "We sent a link to \(model.profile.email). Open it on any device; this screen continues by itself.")
        ProgressView().controlSize(.small)
        if let error { Text(error).foregroundStyle(.red) }
        HStack {
            Button("Change the e-mail") { onBack() }
            Button(resent ? "Sent again" : "Send the link again") { Task { await resend() } }.disabled(resent)
        }
        .task { await poll() }
    }

    private func poll() async {
        while !Task.isCancelled {
            do {
                switch try await HubClient.claim(model.profile.claim) {
                case .pending: break
                case .token(let token, let slug, let name):
                    Keychain.set(.deviceToken, token)
                    model.profile.slug = slug
                    if !newCongregation && !name.isEmpty { model.profile.name = name }
                    model.profile.claim = ""
                    model.profile.save()
                    onToken()
                    return
                }
                error = nil
            } catch {
                self.error = error.localizedDescription
            }
            try? await Task.sleep(for: .seconds(3))
        }
    }

    private func resend() async {
        do {
            model.profile.claim = try await HubClient.deviceLink(email: model.profile.email, slug: model.profile.slug,
                                                                 deviceName: model.deviceName)
            model.profile.save()
            resent = true
        } catch {
            self.error = error.localizedDescription
        }
    }
}

private struct FinishStep: View {
    @State private var atLogin = true

    var body: some View {
        StepTitle(title: "All set",
                  subtitle: "On Sunday, open the lid, check the list, and press Start. Keep the Mac plugged in with the lid open during the meeting.")
        Toggle("Open Hear Church when this Mac starts", isOn: $atLogin)
            .onChange(of: atLogin) { _, on in LoginItem.set(on) }
            .onAppear { if !LoginItem.enabled { LoginItem.set(true) } }
    }
}
