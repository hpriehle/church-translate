import AppKit
import AVFoundation
import SwiftUI

/// Hear Church → Settings… (⌘,): billing (or the congregation's own key), languages, voices, the glossary the
/// translator follows, and the congregation.
struct SettingsView: View {
    static let tabKey = "settingsTab"
    static let languagesTab = "languages"
    static let billingTab = "billing"
    static let aiTab = "ai"
    @EnvironmentObject var model: AppModel
    @AppStorage(SettingsView.tabKey) private var tab = "general"

    var body: some View {
        // Credits: the Billing tab. Its own key (the hub says "own_key"): the AI tab instead, with the key.
        let ownKey = model.ownKeyBilling
        TabView(selection: Binding(get: { shown(ownKey) }, set: { tab = $0 })) {
            GeneralSettings().tabItem { Label("General", systemImage: "gearshape") }.tag("general")
            if ownKey {
                AISettings().tabItem { Label("AI", systemImage: "sparkles") }.tag(SettingsView.aiTab)
            } else {
                BillingSettings().tabItem { Label("Billing", systemImage: "creditcard") }.tag(SettingsView.billingTab)
            }
            LanguageSettings().tabItem { Label("Languages", systemImage: "globe") }.tag(SettingsView.languagesTab)
            VoiceSettings().tabItem { Label("Speaking", systemImage: "speaker.wave.2") }.tag("voices")
            GlossarySettings().tabItem { Label("Glossary", systemImage: "character.book.closed") }.tag("glossary")
            CongregationSettings().tabItem { Label("Congregation", systemImage: "building.columns") }.tag("congregation")
        }
        .frame(width: 600, height: 520)
        .task { await model.refreshBilling() }   // the operator may have switched it since
    }

    /// A tab that isn't there for this congregation opens its counterpart.
    private func shown(_ ownKey: Bool) -> String {
        if tab == SettingsView.aiTab && !ownKey { return SettingsView.billingTab }
        if tab == SettingsView.billingTab && ownKey { return SettingsView.aiTab }
        return tab
    }
}

/// Every language: search for it, download it, delete it to get the disk space back, or open it to pick its voice.
/// Only downloaded languages show in the main window's speaker and translation grids, so this is where they're added.
private struct LanguageSettings: View {
    @EnvironmentObject var model: AppModel
    @State private var search = ""
    @State private var open: OutputLanguage?
    @State private var removing: OutputLanguage?
    @State private var problem: String?

    private func matching(_ list: [OutputLanguage]) -> [OutputLanguage] {
        let q = search.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return list }
        return list.filter { $0.name.localizedCaseInsensitiveContains(q) || $0.native.localizedCaseInsensitiveContains(q) }
    }

    /// Downloaded (the engine says "ready"), as the main window's grids show them.
    private var downloaded: [OutputLanguage] { matching(OutputLanguage.all.filter { $0.available && model.isReady($0.id) }) }
    /// The rest this Mac can use: downloading first, then alphabetical.
    private var others: [OutputLanguage] {
        matching(OutputLanguage.all.filter { $0.available && !model.isReady($0.id) })
            .sorted { a, b in
                let da = model.package(a.id)?.state == "downloading", db = model.package(b.id)?.state == "downloading"
                return da != db ? da : a.name < b.name
            }
    }
    private var unusable: [OutputLanguage] { matching(OutputLanguage.all.filter { !$0.available }) }

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
            if !downloaded.isEmpty {
                Section("Downloaded") { ForEach(downloaded) { row($0) } }
            }
            Section {
                ForEach(others) { row($0) }
                if downloaded.isEmpty && others.isEmpty && unusable.isEmpty {
                    Text("No language called “\(search)”.").foregroundStyle(.secondary)
                } else if others.isEmpty {
                    Text("Every language is downloaded.").foregroundStyle(.secondary)
                }
            } header: {
                Text("More languages")
            } footer: {
                Text("Downloaded languages appear in the main window's speaker and translation grids, above Start meeting. Download a language here to add it there; remove it to free the space.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if !unusable.isEmpty {
                Section("Not on this Mac") {
                    ForEach(unusable) { language in
                        LabeledContent {
                            Text(language.unavailableReason ?? "").foregroundStyle(.secondary)
                        } label: {
                            HStack { Text(language.flag).font(.title3); Text(language.name) }.foregroundStyle(.secondary)
                        }
                    }
                }
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

/// This Mac and help. The key is in the AI tab (own-key congregations only); languages are in Languages.
private struct GeneralSettings: View {
    @State private var atLogin = LoginItem.enabled

    var body: some View {
        Form {
            Section("This Mac") {
                Toggle("Open Hear Church when this Mac starts", isOn: $atLogin)
                    .onChange(of: atLogin) { _, on in LoginItem.set(on) }
            }
            Section("About and help") {
                HelpLinks()
                if Updater.devUpdates {
                    Label("Dev updates on", systemImage: "hammer").foregroundStyle(.secondary)
                        .help("This Mac also gets test builds. Turn off: defaults delete com.hearchurch.HearChurch HCDevUpdates")
                }
            }
        }
        .formStyle(.grouped)
    }
}

/// How each of the meeting's translation languages is read aloud. The engine has a speed per language
/// (app/config.py: French in [tts].speed, the others in [voices.<code>].speed; 1 = the voice's own pace), and one
/// volume for all.
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
                Section {
                    ForEach(model.targets, id: \.self) { code in
                        SettingSlider(title: "\(OutputLanguage.all.first { $0.id == code }?.flag ?? "") \(OutputLanguage.name(code))",
                                      value: speed(code, s.settings), range: 0.7...1.6, step: 0.05,
                                      format: { "\(Int(($0 * 100).rounded()))%" }) { v in
                            let v = (v * 100).rounded() / 100
                            Task { await model.engine.set(code == "fr" ? ["tts": ["speed": v]] : ["voices": [code: ["speed": v]]]) }
                        }
                    }
                } header: {
                    Text("Speed")
                } footer: {
                    Text("How fast each translation language of the meeting is read aloud. 100% is the voice's own pace. Voices also speed up by themselves when they fall behind the speaker.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Section("Volume") {
                    SettingSlider(title: "Every language", value: s.settings.tts.volume, range: 0.1...1.0, step: 0.05,
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

    private func speed(_ code: String, _ s: EngineSettings.Settings) -> Double {
        (code == "fr" ? s.tts.speed : s.voices[code]?.speed) ?? 1.0
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

/// The names and words the translator must translate a fixed way (app/config.py validate_glossary). Each entry is
/// {"en": …, "fr": …}; "fr" holds the translation whatever the language. English comes first: it has no list of its
/// own (translations into English read each spoken language's list the other way round), so its page only holds the
/// names kept as they are.
private struct GlossarySettings: View {
    @EnvironmentObject var model: AppModel
    @State private var lang = "en"
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
                    Text(OutputLanguage.english.name).tag(OutputLanguage.english.id)
                    Divider()
                    ForEach(OutputLanguage.all.filter { $0.available && $0.id != "en" }) { Text($0.name).tag($0.id) }
                }
                .frame(width: 220)
                Spacer()
                if let status { Text(status).font(.callout).foregroundStyle(.secondary) }
                Button("Save") { Task { await save() } }.disabled(!dirty).keyboardShortcut("s")
            }
            Text("Names of people in the congregation (one per line). Hear Church keeps them as they are.")
                .font(.callout).foregroundStyle(.secondary)
            TextEditor(text: $localNames).font(.body).frame(height: lang == "en" ? 200 : 90)
                .overlay(RoundedRectangle(cornerRadius: 4).stroke(.quaternary))
                .onChange(of: localNames) { dirty = true }
            if lang == "en" {
                Text("Fixed translations into English come from each spoken language's list: choose that language above.")
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                Spacer()
            } else {
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
        terms = lang == "en" ? [] : ["terms", "names", "books"].flatMap { key in
            ((g[key] as? [[String: Any]]) ?? []).map { Pair(en: $0["en"] as? String ?? "", to: $0["fr"] as? String ?? "", list: key) }
        }
        status = nil
        dirty = false
    }

    private func save() async {
        var g = raw   // keeps anything this editor doesn't show
        g["local_names"] = localNames.split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        let kept = terms.filter { !$0.en.trimmingCharacters(in: .whitespaces).isEmpty && !$0.to.trimmingCharacters(in: .whitespaces).isEmpty }
        for key in ["terms", "names", "books"] where lang != "en" {   // English shows (and changes) the names only
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
                LabeledContent("Server", value: AppPaths.hub.host ?? "")
            }
            if model.profile.setupDone && !model.profile.slug.isEmpty { ListenerLinkSection() }
            if model.profile.setupDone {
                EmailsSection(account: $account, problem: $problem)
                DevicesSection(account: $account, problem: $problem)
                if let problem {
                    // An older hearchurch.com without these calls yet is not a problem: say so quietly.
                    Text(problem).foregroundStyle(problem == HubClient.NotAvailable().localizedDescription ? .secondary : .red)
                        .fixedSize(horizontal: false, vertical: true)
                }
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
            } else if problem == nil {
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

/// The congregation's page, hearchurch.com/<slug>: open it, copy it, or change it. The old address (and every QR
/// code printed with it) keeps working: the hub redirects it to the new one for good.
private struct ListenerLinkSection: View {
    @EnvironmentObject var model: AppModel
    @State private var editing = false
    @State private var slug = ""
    @State private var problem: String?
    @State private var saving = false
    @State private var saved = false

    private var host: String { AppPaths.hub.host ?? "hearchurch.com" }
    private var url: URL { AppPaths.hub.appendingPathComponent(model.profile.slug) }
    /// As typed → what the hub accepts: lowercase letters, digits, single dashes.
    private var cleaned: String {
        let t = slug.lowercased().map { $0.isASCII && ($0.isLetter || $0.isNumber) ? String($0) : "-" }.joined()
        return t.split(separator: "-", omittingEmptySubsequences: true).joined(separator: "-")
    }

    var body: some View {
        Section {
            if editing {
                HStack(spacing: 2) {
                    Text("\(host)/").foregroundStyle(.secondary)
                    TextField("your-congregation", text: $slug).textFieldStyle(.roundedBorder).onSubmit { Task { await save() } }
                }
                if !slug.isEmpty && cleaned != slug {
                    Text("Becomes \(host)/\(cleaned)").font(.caption).foregroundStyle(.secondary)
                }
                HStack {
                    Button("Cancel") { editing = false; problem = nil }
                    Button("Save") { Task { await save() } }
                        .keyboardShortcut(.defaultAction)
                        .disabled(cleaned.count < 3 || cleaned == model.profile.slug || saving)
                    if saving { ProgressView().controlSize(.small) }
                }
                if let problem {
                    Text(problem).foregroundStyle(problem == HubClient.NotAvailable().localizedDescription ? .secondary : .red)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } else {
                LabeledContent("Listener link") {
                    HStack {
                        Link(url.absoluteString, destination: url).textSelection(.enabled)
                        Button("Change…") { slug = model.profile.slug; problem = nil; saved = false; editing = true }
                            .disabled(model.running)
                            .help(model.running ? "Change it after the meeting." : "Choose another address for your congregation's page")
                    }
                }
                if saved { Label("Saved. The old link still works.", systemImage: "checkmark.circle").foregroundStyle(.secondary) }
            }
        } header: {
            Text("Listener link")
        } footer: {
            Text("Listeners open this page or scan its QR code. After a change, the old link and printed QR codes keep working: they lead to the new one.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private func save() async {
        let new = cleaned
        guard new.count >= 3, new != model.profile.slug else { return }
        saving = true
        defer { saving = false }
        problem = await model.changeSlug(new)
        if problem == nil { editing = false; saved = true }
    }
}

/// Settings → Billing (congregations on credit): what's left, buying more, and what the meetings used.
private struct BillingSettings: View {
    @EnvironmentObject var model: AppModel
    @State private var meetings: [HubClient.Meeting] = []
    @State private var charges: [HubClient.Charge] = []
    @State private var historyNote: String?
    @State private var problem: String?
    @State private var buying: Int?
    @State private var opened = false

    var body: some View {
        Form {
            if !model.profile.setupDone {
                Text("Connect this Mac to a congregation first.").foregroundStyle(.secondary)
            } else {
                balance
                buy
                history
            }
        }
        .formStyle(.grouped)
        .task { await load() }
    }

    @ViewBuilder private var balance: some View {
        Section("Credit") {
            if let b = model.billing {
                HStack(alignment: .firstTextBaseline) {
                    Text(String(format: "%.1f", max(0, b.hoursLeft ?? 0))).font(.system(size: 34, weight: .semibold)).monospacedDigit()
                    Text("hours left").foregroundStyle(.secondary)
                    Spacer()
                    Text(Self.usd(b.balanceUSD)).font(.title3).monospacedDigit().foregroundStyle(b.balanceUSD < 0 ? .red : .primary)
                }
                Text("An hour is one language for one hour of speech: a meeting translated into two languages uses two hours an hour. \(Self.usd(b.usdPerHour)) per hour.")
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                if b.freeLeftHours > 0 {
                    Label(String(format: "%.1f free hours left", b.freeLeftHours), systemImage: "gift").foregroundStyle(.secondary)
                }
                if b.outOfCredit { Text(b.message).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true) }
            } else {
                Text(problem ?? "Loading…").foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder private var buy: some View {
        if let b = model.billing {
            Section {
                if b.canBuy {
                    HStack {
                        ForEach(b.buyUSD, id: \.self) { usd in
                            Button { Task { await buy(usd) } } label: {
                                VStack(spacing: 1) {
                                    Text("$\(usd)").bold()
                                    Text(String(format: "%g hours", Double(usd) / max(b.usdPerHour, 0.01))).font(.caption)
                                }
                                .frame(maxWidth: .infinity)
                            }
                            .disabled(buying != nil)
                        }
                    }
                    if buying != nil { ProgressView("Opening the payment page…").controlSize(.small) }
                    if opened { Text("Pay in your browser. The credit shows here within a minute; reopen Settings to see it.").font(.callout).foregroundStyle(.secondary) }
                } else {
                    Text("Buying credit isn't available yet. Write to support@hearchurch.com.").foregroundStyle(.secondary)
                }
                if let problem, model.billing != nil { Text(problem).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true) }
            } header: {
                Text("Buy credit")
            } footer: {
                Text("A secure payment page opens in your browser. The receipt goes to the congregation's main e-mail.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder private var history: some View {
        Section("Meetings") {
            if let historyNote {
                Text(historyNote).foregroundStyle(.secondary)
            } else if meetings.isEmpty {
                Text("No meetings yet.").foregroundStyle(.secondary)
            }
            ForEach(meetings) { m in
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(m.startedAt.formatted(date: .abbreviated, time: .shortened))
                        Text(m.languages.map(OutputLanguage.name).joined(separator: ", ")).font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    VStack(alignment: .trailing, spacing: 2) {
                        Text(String(format: "%.2f hours", m.languageHours)).monospacedDigit()
                        Text(cost(m)).font(.caption).foregroundStyle(.secondary).monospacedDigit()
                    }
                }
            }
        }
        if !charges.isEmpty {
            Section("Payments") {
                ForEach(charges) { c in
                    HStack {
                        Text(Self.kind(c.kind))
                        Text(c.at.formatted(date: .abbreviated, time: .omitted)).foregroundStyle(.secondary)
                        Spacer()
                        Text((c.amountUSD > 0 ? "+" : "") + Self.usd(c.amountUSD)).monospacedDigit()
                    }
                }
            }
        }
    }

    private func cost(_ m: HubClient.Meeting) -> String {
        if m.ownKey { return "own key" }
        if m.costUSD > 0 { return Self.usd(m.costUSD) + (m.freeMinutes > 0 ? " (part free)" : "") }
        return m.languageHours > 0 ? "free" : "no speech"
    }

    private static func kind(_ k: String) -> String {
        switch k {
        case "top_up": return "Credit bought"
        case "manual_credit": return "Credit added"
        case "refund": return "Refund"
        default: return k
        }
    }

    static func usd(_ v: Double) -> String { v < 0 ? String(format: "-$%.2f", -v) : String(format: "$%.2f", v) }

    private func load() async {
        guard model.profile.setupDone, let token = Keychain.get(.deviceToken), !token.isEmpty else { return }
        await model.refreshBilling()
        if model.billing == nil { problem = "Can't reach \(AppPaths.hub.host ?? "hearchurch.com") right now." }
        do {
            (meetings, charges) = try await HubClient.usage(token: token)
            historyNote = nil
        } catch is HubClient.NotAvailable {
            historyNote = "The history is available after the next update."
        } catch is HubClient.SignedOut {
            model.signOut()
        } catch {
            historyNote = error.localizedDescription
        }
    }

    private func buy(_ usd: Int) async {
        guard let token = Keychain.get(.deviceToken), !token.isEmpty else { return }
        buying = usd
        defer { buying = nil }
        do {
            NSWorkspace.shared.open(try await HubClient.checkout(usd: usd, token: token))
            opened = true
            problem = nil
        } catch is HubClient.SignedOut {
            model.signOut()
        } catch {
            problem = error.localizedDescription
        }
    }
}
