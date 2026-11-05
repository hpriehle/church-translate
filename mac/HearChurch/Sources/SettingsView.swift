import SwiftUI

/// Hear Church → Settings… (⌘,): the key, voices, the glossary Claude follows, and the congregation.
struct SettingsView: View {
    var body: some View {
        TabView {
            GeneralSettings().tabItem { Label("General", systemImage: "gearshape") }
            VoiceSettings().tabItem { Label("Voices", systemImage: "speaker.wave.2") }
            GlossarySettings().tabItem { Label("Glossary", systemImage: "character.book.closed") }
            CongregationSettings().tabItem { Label("Congregation", systemImage: "building.columns") }
        }
        .frame(width: 600, height: 520)
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
                Section("Voice for each language") {
                    ForEach(model.profile.languages, id: \.self) { code in
                        let options = s.voices[code] ?? []
                        // French uses the main voice (tts); the others have their own (voices)
                        let current = code == "fr" ? "\(s.settings.tts.engine):\(s.settings.tts.voice)"
                                                   : s.settings.voices[code].map { "\($0.engine):\($0.voice)" } ?? ""
                        Picker(OutputLanguage.name(code), selection: Binding(get: { current }, set: { id in
                            guard let o = options.first(where: { "\($0.engine):\($0.voice)" == id }) else { return }
                            let v = ["engine": o.engine, "voice": o.voice]
                            Task { await model.engine.set(code == "fr" ? ["tts": v] : ["voices": [code: v]]) }
                        })) {
                            if !options.contains(where: { "\($0.engine):\($0.voice)" == current }) { Text("Default").tag(current) }
                            ForEach(options, id: \.self) { Text($0.label).tag("\($0.engine):\($0.voice)") }
                        }
                    }
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
                    ForEach(OutputLanguage.all.filter(\.available)) { Text($0.name).tag($0.id) }
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

    var body: some View {
        Form {
            Section("Congregation") {
                LabeledContent("Name", value: model.profile.name)
                if !model.profile.stake.isEmpty { LabeledContent("Stake", value: model.profile.stake) }
                LabeledContent("Address", value: model.profile.address)
                LabeledContent("E-mail", value: model.profile.email)
                if let url = model.listenerURL { LabeledContent("Listener link") { Link(url.absoluteString, destination: url) } }
                LabeledContent("Server", value: AppPaths.hub.host ?? "")
            }
            Section("Connect code") { ConnectCodeRow() }
            Section {
                Button("Sign this Mac out…", role: .destructive) { confirmSignOut = true }
                    .disabled(model.running)
            }
        }
        .formStyle(.grouped)
        .confirmationDialog("Sign this Mac out of \(model.profile.name)?", isPresented: $confirmSignOut) {
            Button("Sign Out", role: .destructive) { signOut() }
        } message: {
            Text("It stops publishing meetings until it is set up again. The details you entered and the API key stay, so setting up again is quick.")
        }
    }

    private func signOut() {
        Keychain.delete(.deviceToken)
        Keychain.delete(.connectCode)
        OnboardingProgress.clear()
        var p = model.profile   // keep what was entered, so setting up again (e.g. on another server) is quick
        p.slug = ""
        p.claim = ""
        p.approved = false
        p.setupDone = false
        model.profile = p
        p.save()
        model.relaunchEngine()   // without the device token; onboarding needs the engine for downloads
    }
}

/// The code another computer needs to connect to this congregation: hidden until "Show", replaceable.
private struct ConnectCodeRow: View {
    @State private var code = Keychain.get(.connectCode) ?? ""
    @State private var shown = false
    @State private var confirmNew = false
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        if !code.isEmpty {
            LabeledContent("Code") {
                HStack {
                    Text(shown ? HubClient.spaced(code) : "••• •••")
                        .font(.system(.title3, design: .monospaced)).textSelection(.enabled)
                    Button(shown ? "Hide" : "Show") { shown.toggle() }.buttonStyle(.link)
                }
            }
            Text("Use this code to connect another computer to your congregation. Keep it private.")
                .font(.callout).foregroundStyle(.secondary)
        } else {
            Text("Another computer needs a connect code to join your congregation.")
                .font(.callout).foregroundStyle(.secondary)
        }
        HStack {
            Button(code.isEmpty ? "Make a connect code" : "Make a new code") {
                if code.isEmpty { Task { await make() } } else { confirmNew = true }
            }
            .disabled(busy)
            if busy { ProgressView().controlSize(.small) }
        }
        .confirmationDialog("Make a new connect code?", isPresented: $confirmNew) {
            Button("Make a new code", role: .destructive) { Task { await make() } }
        } message: {
            Text("The old code stops working. Computers already connected are not affected.")
        }
        if let error { Text(error).foregroundStyle(.red) }
    }

    private func make() async {
        error = nil
        guard let token = Keychain.get(.deviceToken), !token.isEmpty else { error = "This Mac isn't signed in to a congregation."; return }
        busy = true
        defer { busy = false }
        do {
            let new = try await HubClient.newConnectCode(token: token)
            guard Keychain.set(.connectCode, new) else {
                error = "Could not save the code in this Mac's Keychain. The new code is \(HubClient.spaced(new)): write it down."
                return
            }
            code = new
            shown = true
        } catch {
            self.error = error.localizedDescription
        }
    }
}
