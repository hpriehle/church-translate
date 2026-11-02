import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var model: AppModel
    @State private var key = ""
    @State private var keyError: String?
    @State private var atLogin = LoginItem.enabled
    @State private var confirmSignOut = false

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
                        .disabled(key.isEmpty)
                }
                if let keyError { Text(keyError).foregroundStyle(.red) }
                DisclosureGroup("How to get a key") { ClaudeKeyGuide() }
            }
            Section("This Mac") {
                Toggle("Open Hear Church when this Mac starts", isOn: $atLogin)
                    .onChange(of: atLogin) { _, on in LoginItem.set(on) }
            }
            Section("Congregation") {
                LabeledContent("Name", value: model.profile.name)
                if let url = model.listenerURL { LabeledContent("Listener link") { Link(url.absoluteString, destination: url) } }
                LabeledContent("E-mail", value: model.profile.email)
                Button("Sign this Mac out…", role: .destructive) { confirmSignOut = true }
                    .disabled(model.running)
            }
        }
        .formStyle(.grouped)
        .frame(width: 520)
        .padding(.vertical, 8)
        .confirmationDialog("Sign this Mac out of \(model.profile.name)?", isPresented: $confirmSignOut) {
            Button("Sign Out", role: .destructive) { signOut() }
        } message: {
            Text("It stops publishing meetings until it is set up again. The API key stays in the Keychain.")
        }
    }

    private func signOut() {
        Keychain.delete(.deviceToken)
        model.engine.stop()
        var p = Profile()
        p.email = model.profile.email
        model.profile = p
        p.save()
    }
}
