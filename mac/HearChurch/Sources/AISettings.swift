import SwiftUI

// The only place in the app that names the AI provider: a congregation on its own key pastes an Anthropic API key,
// so the AI tab and its how-to say so. Everywhere else it is "the translator" (tests/test_no_brand_names.py).

/// Settings → AI, shown only when hearchurch.com says the congregation pays with its own key ("own_key").
struct AISettings: View {
    @EnvironmentObject var model: AppModel
    @State private var key = ""
    @State private var keyError: String?

    var body: some View {
        Form {
            Section {
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
                DisclosureGroup("How to get a key") { APIKeyGuide() }
            } header: {
                Text("Anthropic API key")
            } footer: {
                Text("Your congregation translates with its own key, and Anthropic bills you directly for what you use.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let s = model.engine.settings {
                Section("Translation") {
                    Picker("Quality", selection: Binding(get: { s.settings.translate.claude_model }, set: { m in
                        Task { await model.engine.set(["translate": ["claude_model": m]]) }
                    })) {
                        ForEach(TranslationQuality.choices, id: \.id) { Text($0.label).tag($0.id) }
                        if !TranslationQuality.choices.contains(where: { $0.id == s.settings.translate.claude_model }) {
                            Text(TranslationQuality.name(s.settings.translate.claude_model)).tag(s.settings.translate.claude_model)
                        }
                    }
                    .disabled(model.running)
                }
            }
        }
        .formStyle(.grouped)
    }
}

/// How to make the key, in the AI tab and in setup for a congregation with an own-key code.
struct APIKeyGuide: View {
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

/// The translation model, by what it means to a church rather than by name.
enum TranslationQuality {
    static let choices: [(id: String, label: String)] = [
        ("claude-opus-5-5", "Best"),
        ("claude-sonnet-5-5", "Good, cheaper"),
    ]

    static func name(_ id: String) -> String {
        if let c = choices.first(where: { $0.id == id }) { return c.label }
        if id.contains("opus") { return "Best" }
        if id.contains("sonnet") { return "Good" }
        if id.contains("haiku") { return "Fast" }
        return "Custom"
    }
}
