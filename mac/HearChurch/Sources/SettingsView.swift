import AVFoundation
import SwiftUI

/// Hear Church → Settings… (⌘,): the key, languages, voices, the glossary Claude follows, and the congregation.
struct SettingsView: View {
    static let tabKey = "settingsTab"
    static let languagesTab = "languages"
    @AppStorage(SettingsView.tabKey) private var tab = "general"

    var body: some View {
        TabView(selection: $tab) {
            GeneralSettings().tabItem { Label("General", systemImage: "gearshape") }.tag("general")
            LanguageSettings().tabItem { Label("Languages", systemImage: "globe") }.tag(SettingsView.languagesTab)
            VoiceSettings().tabItem { Label("Speaking", systemImage: "speaker.wave.2") }.tag("voices")
            GlossarySettings().tabItem { Label("Glossary", systemImage: "character.book.closed") }.tag("glossary")
            CongregationSettings().tabItem { Label("Congregation", systemImage: "building.columns") }.tag("congregation")
        }
        .frame(width: 600, height: 520)
    }
}

/// Every language: search for it, download it, delete it to get the disk space back, or open it to pick its voice.
/// What's downloaded shows as a checkbox next to Start meeting, and that checkbox decides what's translated.
private struct LanguageSettings: View {
    @EnvironmentObject var model: AppModel
    @State private var search = ""
    @State private var open: OutputLanguage?
    @State private var removing: OutputLanguage?
    @State private var problem: String?

    private var everyLanguage: [OutputLanguage] { OutputLanguage.all.filter(\.available) }

    private var shown: [OutputLanguage] {
        let q = search.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return everyLanguage }
        return everyLanguage.filter { $0.name.localizedCaseInsensitiveContains(q) || $0.native.localizedCaseInsensitiveContains(q) }
    }

    var body: some View {
        Group {
            if model.engine.packages.isEmpty {
                Form { Text("The translation engine is starting…").foregroundStyle(.secondary) }.formStyle(.grouped)
            } else if let open {
                LanguageDetail(language: open, problem: problem, back: { problem = nil; self.open = nil }, remove: { askToRemove(open) })
            } else {
                list
            }
        }
        .alert("Remove \(removing?.name ?? "this language")?", isPresented: Binding(
            get: { removing != nil }, set: { if !$0 { removing = nil } }), presenting: removing) { language in
            Button("Remove", role: .destructive) { Task { problem = await model.removeLanguage(language.id) } }
            Button("Cancel", role: .cancel) {}
        } message: { language in
            Text("This deletes its voices and frees \(Format.size(model.package(language.id)?.size_mb ?? language.sizeMB)). Your glossary for it is kept, and you can download it again at any time.")
        }
    }

    private var list: some View {
        Form {
            Section {
                HStack(spacing: 6) {
                    Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                    TextField("Search languages", text: $search).textFieldStyle(.plain)
                    if !search.isEmpty {
                        Button { search = "" } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(.tertiary) }
                            .buttonStyle(.plain).help("Clear the search")
                    }
                }
            }
            Section {
                ForEach(shown) { row($0) }
                if shown.isEmpty { Text("No language called “\(search)”.").foregroundStyle(.secondary) }
            } footer: {
                Text("Choose the meeting's languages in the main window, above Start meeting. Any language you choose there downloads by itself.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let problem { Text(problem).font(.callout).foregroundStyle(.orange) }
            Section {
                HStack {
                    VStack(alignment: .leading) {
                        Text("Speech recognition")
                        Text("Needed for every language").font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    PackageBadge(package: model.package("speech"), sizeMB: speechDownloadMB)
                }
            }
        }
        .formStyle(.grouped)
    }

    private func row(_ language: OutputLanguage) -> some View {
        HStack {
            Button { problem = nil; open = language } label: {
                HStack {
                    Text(language.flag).font(.title3)
                    Text(language.name)
                    Spacer()
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            PackageBadge(package: model.package(language.id), sizeMB: language.sizeMB)
            LanguageAction(language: language, remove: { askToRemove(language) })
            Button { problem = nil; open = language } label: { Image(systemName: "chevron.right").foregroundStyle(.tertiary) }
                .buttonStyle(.plain).help("Choose its voice")
        }
    }

    /// The button always answers: with the question, or with the reason it can't be removed right now.
    private func askToRemove(_ language: OutputLanguage) {
        if model.running && (model.isOn(language.id, .speaker) || model.isOn(language.id, .target)) {
            problem = "\(language.name) is in this meeting: switch it off first, or stop the meeting."
        } else {
            problem = nil
            removing = language
        }
    }
}

/// Download, Try again or Remove, whichever fits the language's state.
private struct LanguageAction: View {
    @EnvironmentObject var model: AppModel
    let language: OutputLanguage
    let remove: () -> Void

    var body: some View {
        switch model.package(language.id)?.state {
        case "ready": Button("Remove", action: remove)
        case "downloading": EmptyView()
        case "failed": Button("Try again") { model.downloadLanguage(language.id) }
        default: Button("Download") { model.downloadLanguage(language.id) }
        }
    }
}

/// One language: its download and which voice reads it.
private struct LanguageDetail: View {
    @EnvironmentObject var model: AppModel
    let language: OutputLanguage
    let problem: String?
    let back: () -> Void
    let remove: () -> Void

    @State private var player: AVAudioPlayer?
    @State private var playing: String?
    @State private var listenProblem: String?

    private var state: String? { model.package(language.id)?.state }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button(action: back) { Label("Languages", systemImage: "chevron.left") }
                .buttonStyle(.link).padding([.top, .leading], 20)
            Form {
                Section {
                    HStack(spacing: 12) {
                        Text(language.flag).font(.system(size: 34))
                        VStack(alignment: .leading) {
                            Text(language.name).font(.title3.weight(.semibold))
                            Text(language.native).foregroundStyle(.secondary)
                        }
                        Spacer()
                        PackageBadge(package: model.package(language.id), sizeMB: language.sizeMB)
                        LanguageAction(language: language, remove: remove)
                    }
                    if let problem { Text(problem).font(.callout).foregroundStyle(.orange) }
                }
                Section("Voice") { voices }
            }
            .formStyle(.grouped)
        }
        .task(id: state) { await model.engine.loadSettings() }   // a finished download brings its voices
        .onDisappear { player?.stop(); player = nil; playing = nil }
    }

    @ViewBuilder private var voices: some View {
        let options = model.engine.settings?.voice_catalog[language.id] ?? []
        let usable = state == "ready" ? model.engine.settings?.voices[language.id] ?? [] : []
        let current = selected(usable)
        ForEach(options, id: \.self) { option in
            let id = "\(option.engine):\(option.voice)"
            HStack {
                if usable.contains(option) {
                    Button { choose(id, usable) } label: {
                        HStack {
                            Image(systemName: id == current ? "largecircle.fill.circle" : "circle")
                                .foregroundStyle(id == current ? Color.accentColor : Color.secondary)
                            Text(option.label)
                            Spacer()
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                } else {
                    Text(option.label)
                    Spacer()
                }
                listenButton(option, id)
            }
        }
        if usable.isEmpty {
            Text("Listen to each voice now. Download \(language.name) to choose one.").font(.callout).foregroundStyle(.secondary)
        }
        if let listenProblem { Text(listenProblem).font(.callout).foregroundStyle(.orange) }
    }

    @ViewBuilder private func listenButton(_ option: EngineSettings.VoiceOption, _ id: String) -> some View {
        Button { listen(option, id) } label: {
            Label(playing == id ? "Stop" : "Listen", systemImage: playing == id ? "stop.fill" : "play.fill")
        }
        .help("Hear this voice before choosing it")
    }

    /// A recording built into the app, so no download is needed. Plays on this Mac only.
    private func listen(_ option: EngineSettings.VoiceOption, _ id: String) {
        let wasPlaying = playing == id
        player?.stop()
        player = nil
        playing = nil
        listenProblem = nil
        guard !wasPlaying else { return }
        guard let url = Bundle.main.url(forResource: "sample-\(language.id)-\(option.voice)", withExtension: "m4a"),
              let sound = try? AVAudioPlayer(contentsOf: url), sound.play() else {
            listenProblem = "Couldn't play that voice."
            return
        }
        player = sound
        playing = id
        Task {
            try? await Task.sleep(for: .seconds(sound.duration + 0.1))
            if player === sound { player = nil; playing = nil }
        }
    }

    private func selected(_ options: [EngineSettings.VoiceOption]) -> String {
        let ids = options.map { "\($0.engine):\($0.voice)" }
        guard let s = model.engine.settings?.settings else { return ids.first ?? "" }
        let current = language.id == "fr" ? "\(s.tts.engine):\(s.tts.voice)" : s.voices[language.id].map { "\($0.engine):\($0.voice)" }
        return current.flatMap { ids.contains($0) ? $0 : nil } ?? ids.first ?? ""
    }

    private func choose(_ id: String, _ options: [EngineSettings.VoiceOption]) {
        guard let o = options.first(where: { "\($0.engine):\($0.voice)" == id }) else { return }
        let v = ["engine": o.engine, "voice": o.voice]
        Task {
            await model.engine.set(language.id == "fr" ? ["tts": v] : ["voices": [language.id: v]])
            await model.engine.loadSettings()
        }
    }
}

private struct GeneralSettings: View {
    @EnvironmentObject var model: AppModel
    @State private var key = ""
    @State private var keyError: String?
    @State private var atLogin = LoginItem.enabled

    var body: some View {
        Form {
            Section("Claude API key") {
                switch model.keyState {
                case .ok: Label("Saved in the Keychain and working.", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                case .missing: Text("No key yet.")
                case .bad(let why): Text(why).foregroundStyle(.red)
                default: ProgressView().controlSize(.small)
                }
                HStack {
                    SecureField("Paste a new key (sk-ant-…)", text: $key)
                    Button("Test and save") { Task { keyError = await model.saveKey(key); if keyError == nil { key = "" } } }
                        .disabled(key.isEmpty || model.running)
                }
                if let keyError { Text(keyError).foregroundStyle(.red) }
                DisclosureGroup("How to get a key") { ClaudeKeyGuide() }
            }
            if let s = model.engine.settings {
                Section("Translation") {
                    Picker("Claude model", selection: Binding(get: { s.settings.translate.claude_model }, set: { m in
                        Task { await model.engine.set(["translate": ["claude_model": m]]) }
                    })) {
                        Text("Claude Opus 5.5 (best)").tag("claude-opus-5-5")
                        Text("Claude Sonnet 5.5 (cheaper)").tag("claude-sonnet-5-5")
                        if !["claude-opus-5-5", "claude-sonnet-5-5"].contains(s.settings.translate.claude_model) {
                            Text(s.settings.translate.claude_model).tag(s.settings.translate.claude_model)
                        }
                    }
                    .disabled(model.running)
                }
            }
            Section("This Mac") {
                Toggle("Open Hear Church when this Mac starts", isOn: $atLogin)
                    .onChange(of: atLogin) { _, on in LoginItem.set(on) }
            }
            Section("Languages on this Mac") {
                ForEach(OutputLanguage.all) { language in
                    LabeledContent(language.name) {
                        if let reason = language.unavailableReason {
                            Label(reason, systemImage: "xmark.circle").foregroundStyle(.secondary)
                        } else {
                            Label("Works on this Mac", systemImage: "checkmark.circle").foregroundStyle(.secondary)
                        }
                    }
                }
            }
            Section("About and help") {
                HelpLinks()
            }
        }
        .formStyle(.grouped)
    }
}

private struct VoiceSettings: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        Form {
            if let s = model.engine.settings {
                Section("Test") {
                    Button("Play a test sentence") { Task { await model.engine.post("api/test-voice") } }
                        .disabled(!model.running)
                        .help("Listening phones hear a short sentence. Start the meeting first.")
                }
                Section("Speaking") {
                    SettingSlider(title: "French speed", value: Double(s.settings.tts.rate), range: 140...240, step: 5,
                                  format: { "\(Int($0)) wpm" }) { v in
                        Task { await model.engine.set(["tts": ["rate": Int(v)]]) }
                    }
                    SettingSlider(title: "Volume", value: s.settings.tts.volume, range: 0.1...1.0, step: 0.05,
                                  format: { String(format: "%.0f%%", $0 * 100) }) { v in
                        Task { await model.engine.set(["tts": ["volume": v]]) }
                    }
                }
            } else {
                Text("The translation engine is starting…").foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
    }
}

private struct SettingSlider: View {
    let title: String
    let value: Double
    let range: ClosedRange<Double>
    let step: Double
    let format: (Double) -> String
    let commit: (Double) -> Void
    @State private var v = 0.0

    var body: some View {
        LabeledContent(title) {
            HStack {
                Slider(value: $v, in: range, step: step) { editing in if !editing { commit(v) } }
                Text(format(v)).monospacedDigit().frame(width: 60, alignment: .trailing)
            }
        }
        .onAppear { v = value }
        .onChange(of: value) { _, n in v = n }
    }
}

/// The names and words Claude must translate a fixed way (app/config.py validate_glossary). Each entry is
/// {"en": …, "fr": …}; "fr" holds the translation whatever the language.
private struct GlossarySettings: View {
    @EnvironmentObject var model: AppModel
    @State private var lang = "fr"
    @State private var raw: [String: Any] = [:]
    @State private var localNames = ""
    @State private var terms: [Pair] = []
    @State private var status: String?
    @State private var dirty = false

    struct Pair: Identifiable { let id = UUID(); var en: String; var to: String; var list: String }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Picker("Language", selection: $lang) {
                    ForEach(OutputLanguage.all.filter { $0.available && $0.id != "en" }) { Text($0.name).tag($0.id) }
                }
                .frame(width: 220)
                Spacer()
                if let status { Text(status).font(.callout).foregroundStyle(.secondary) }
                Button("Save") { Task { await save() } }.disabled(!dirty).keyboardShortcut("s")
            }
            Text("Names of people in the congregation (one per line). Claude keeps them as they are.")
                .font(.callout).foregroundStyle(.secondary)
            TextEditor(text: $localNames).font(.body).frame(height: 90)
                .overlay(RoundedRectangle(cornerRadius: 4).stroke(.quaternary))
                .onChange(of: localNames) { dirty = true }
            HStack {
                Text("Words with a fixed translation").font(.callout).foregroundStyle(.secondary)
                Spacer()
                Button { terms.append(Pair(en: "", to: "", list: "terms")); dirty = true } label: { Image(systemName: "plus") }
            }
            List {
                ForEach($terms) { $t in
                    HStack {
                        TextField("English", text: $t.en)
                        Image(systemName: "arrow.right").foregroundStyle(.secondary)
                        TextField(OutputLanguage.name(lang), text: $t.to)
                        Button { terms.removeAll { $0.id == t.id }; dirty = true } label: { Image(systemName: "minus.circle") }
                            .buttonStyle(.borderless)
                    }
                    .onChange(of: t.en) { dirty = true }
                    .onChange(of: t.to) { dirty = true }
                }
            }
            .listStyle(.bordered)
        }
        .padding(20)
        .task(id: lang) { await load() }
        .task(id: model.engine.state) { if raw.isEmpty { await load() } }
    }

    private func load() async {
        guard let g = await model.engine.getJSON("api/glossary?lang=\(lang)") as? [String: Any] else {
            status = model.engine.state == .running ? "Could not load the glossary." : "Waiting for the engine…"
            return
        }
        raw = g
        localNames = ((g["local_names"] as? [String]) ?? []).joined(separator: "\n")
        terms = ["terms", "names", "books"].flatMap { key in
            ((g[key] as? [[String: Any]]) ?? []).map { Pair(en: $0["en"] as? String ?? "", to: $0["fr"] as? String ?? "", list: key) }
        }
        status = nil
        dirty = false
    }

    private func save() async {
        var g = raw   // keeps anything this editor doesn't show
        g["local_names"] = localNames.split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        let kept = terms.filter { !$0.en.trimmingCharacters(in: .whitespaces).isEmpty && !$0.to.trimmingCharacters(in: .whitespaces).isEmpty }
        for key in ["terms", "names", "books"] {
            let old = (raw[key] as? [[String: Any]]) ?? []
            g[key] = kept.filter { $0.list == key }.map { p -> [String: Any] in
                var item = old.first { ($0["en"] as? String) == p.en } ?? [:]   // keep notes etc.
                item["en"] = p.en
                item["fr"] = p.to
                return item
            }
        }
        let r = await model.engine.postJSON("api/glossary?lang=\(lang)", g)
        if r.ok { raw = g; dirty = false; status = "Saved" } else { status = r.message.isEmpty ? "Could not save." : r.message }
    }
}

private struct CongregationSettings: View {
    @EnvironmentObject var model: AppModel
    @State private var confirmSignOut = false
    @State private var account: HubClient.Account?
    @State private var loading = false
    @State private var problem: String?

    var body: some View {
        Form {
            Section("Congregation") {
                LabeledContent("Name", value: model.profile.name)
                if !model.profile.stake.isEmpty { LabeledContent("Stake", value: model.profile.stake) }
                LabeledContent("Address", value: model.profile.address)
                if let url = model.listenerURL { LabeledContent("Listener link") { Link(url.absoluteString, destination: url) } }
                LabeledContent("Server", value: AppPaths.hub.host ?? "")
            }
            if model.profile.setupDone {
                EmailsSection(account: $account, problem: $problem)
                DevicesSection(account: $account, problem: $problem)
                if let problem { Text(problem).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true) }
                if loading && account == nil { ProgressView().controlSize(.small) }
            }
            Section {
                Button("Sign this Mac out…", role: .destructive) { confirmSignOut = true }
                    .disabled(model.running)
            }
        }
        .formStyle(.grouped)
        .task { await load() }
        .confirmationDialog("Sign this Mac out of \(model.profile.name)?", isPresented: $confirmSignOut) {
            Button("Sign Out", role: .destructive) { model.signOut() }
        } message: {
            Text("It stops publishing meetings until it is connected again (with a code from Add device on another Mac, or e-mailed). The API key stays.")
        }
    }

    private func load() async {
        guard model.profile.setupDone, let token = Keychain.get(.deviceToken), !token.isEmpty else { return }
        loading = true
        defer { loading = false }
        do {
            account = try await HubClient.account(token: token)
            problem = nil
        } catch is HubClient.SignedOut {
            model.signOut()
        } catch {
            problem = error.localizedDescription
        }
    }
}

/// Runs one account change against the hub; a revoked Mac is signed out.
@MainActor
private func accountChange(_ model: AppModel, _ account: Binding<HubClient.Account?>, _ problem: Binding<String?>,
                           _ call: (String) async throws -> HubClient.Account) async -> Bool {
    guard let token = Keychain.get(.deviceToken), !token.isEmpty else { problem.wrappedValue = "This Mac isn't connected to a congregation."; return false }
    do {
        account.wrappedValue = try await call(token)
        problem.wrappedValue = nil
        return true
    } catch is HubClient.SignedOut {
        model.signOut()
        return false
    } catch {
        problem.wrappedValue = error.localizedDescription
        return false
    }
}

/// The congregation's e-mails: the main one (billing receipts) first. Add, remove, make main.
private struct EmailsSection: View {
    @EnvironmentObject var model: AppModel
    @Binding var account: HubClient.Account?
    @Binding var problem: String?
    @State private var newEmail = ""
    @State private var busy = false

    var body: some View {
        Section {
            if let account {
                ForEach(account.emails) { e in
                    HStack {
                        Text(e.email).textSelection(.enabled)
                        if e.main { Text("Main").font(.caption.bold()).padding(.horizontal, 6).padding(.vertical, 2)
                            .background(Color.accentColor.opacity(0.2), in: Capsule()) }
                        Spacer()
                        if !e.main {
                            Button("Make main") { run { try await HubClient.setMainEmail(e.email, token: $0) } }
                            Button(role: .destructive) { run { try await HubClient.removeEmail(e.email, token: $0) } } label: {
                                Image(systemName: "minus.circle")
                            }
                            .buttonStyle(.borderless).help("Remove this e-mail")
                        }
                    }
                    .disabled(busy)
                }
                if account.emails.count < 10 {
                    HStack {
                        TextField("Add an e-mail", text: $newEmail).onSubmit(add)
                        Button("Add", action: add).disabled(!OnboardingView.isEmail(newEmail) || busy)
                    }
                }
            } else {
                Text("Loading…").foregroundStyle(.secondary)
            }
        } header: {
            Text("E-mails")
        } footer: {
            Text("The main e-mail gets the receipts for credit. Any of these addresses can get a code to connect another Mac.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private func add() {
        let e = newEmail.trimmingCharacters(in: .whitespaces)
        guard OnboardingView.isEmail(e) else { return }
        run({ try await HubClient.addEmail(e, token: $0) }, then: { newEmail = "" })
    }

    private func run(_ call: @escaping (String) async throws -> HubClient.Account, then: @escaping () -> Void = {}) {
        Task {
            busy = true
            defer { busy = false }
            if await accountChange(model, $account, $problem, call) {
                then()
                if let main = account?.mainEmail, !main.isEmpty { model.profile.email = main; model.profile.save() }
            }
        }
    }
}

/// The congregation's Macs: this one, the others (revoke), and "Add device" for a new one.
private struct DevicesSection: View {
    @EnvironmentObject var model: AppModel
    @Binding var account: HubClient.Account?
    @Binding var problem: String?
    @State private var revoking: HubClient.Device?
    @State private var code: String?
    @State private var expires = Date()
    @State private var busy = false

    var body: some View {
        Section {
            if let account {
                ForEach(account.devices) { d in
                    HStack {
                        Image(systemName: "laptopcomputer").foregroundStyle(.secondary)
                        VStack(alignment: .leading, spacing: 2) {
                            HStack {
                                Text(d.name)
                                if d.this { Text("This Mac").font(.caption).foregroundStyle(.secondary) }
                            }
                            Text(detail(d)).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if !d.this { Button("Revoke…") { revoking = d }.disabled(busy) }
                    }
                }
            }
            if let code {
                TimelineView(.periodic(from: .now, by: 1)) { ctx in
                    let left = Int(expires.timeIntervalSince(ctx.date))
                    VStack(alignment: .leading, spacing: 6) {
                        if left > 0 {
                            Text(code).font(.system(size: 40, weight: .bold, design: .monospaced)).textSelection(.enabled)
                                .frame(maxWidth: .infinity)
                            Text("On the new Mac: Connect to my congregation → I have a code. Works once, for \(left / 60):\(String(format: "%02d", left % 60)) more.")
                                .font(.callout).foregroundStyle(.secondary).monospacedDigit()
                        } else {
                            Text("That code has expired.").foregroundStyle(.secondary)
                        }
                    }
                }
            }
            HStack {
                Button(code == nil ? "Add device" : "New code") { Task { await addDevice() } }.disabled(busy)
                if busy { ProgressView().controlSize(.small) }
            }
        } header: {
            Text("Devices")
        } footer: {
            Text("Add device shows a code to connect another Mac to this congregation. A revoked Mac stops publishing meetings at once.")
                .font(.caption).foregroundStyle(.secondary)
        }
        .confirmationDialog("Revoke \(revoking?.name ?? "this Mac")?", isPresented: Binding(
            get: { revoking != nil }, set: { if !$0 { revoking = nil } }), presenting: revoking) { d in
            Button("Revoke", role: .destructive) {
                Task {
                    busy = true
                    defer { busy = false }
                    _ = await accountChange(model, $account, $problem) { try await HubClient.revokeDevice(d.id, token: $0) }
                }
            }
        } message: { _ in
            Text("It stops publishing meetings at once (a meeting it is running ends). It can connect again with a new code.")
        }
    }

    private func detail(_ d: HubClient.Device) -> String {
        var parts: [String] = []
        if !d.appVersion.isEmpty { parts.append("Hear Church \(d.appVersion)") }
        if let seen = d.lastSeen { parts.append("last seen \(seen.formatted(.relative(presentation: .named)))") }
        else if let added = d.createdAt { parts.append("added \(added.formatted(date: .abbreviated, time: .omitted))") }
        return parts.joined(separator: " · ")
    }

    private func addDevice() async {
        guard let token = Keychain.get(.deviceToken), !token.isEmpty else { problem = "This Mac isn't connected to a congregation."; return }
        busy = true
        defer { busy = false }
        do {
            let c = try await HubClient.pairingCode(token: token)
            code = c.code
            expires = c.expires
            problem = nil
        } catch is HubClient.SignedOut {
            model.signOut()
        } catch {
            problem = error.localizedDescription
        }
    }
}
