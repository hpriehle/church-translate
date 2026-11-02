import SwiftUI

@main
struct HearChurchApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @ObservedObject private var model = AppModel.shared

    var body: some Scene {
        Window("Hear Church", id: "main") {
            RootView()
                .environmentObject(model)
                .frame(minWidth: 760, minHeight: 560)
        }
        .defaultSize(width: 1100, height: 780)

        MenuBarExtra {
            MenuBarContent().environmentObject(model)
        } label: {
            Image(systemName: model.running ? "waveform.circle.fill" : "waveform.circle")
        }

        Settings {
            SettingsView().environmentObject(model)
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        Task { @MainActor in AppModel.shared.launch() }
    }

    /// The menu bar icon keeps the app (and the engine) running when the window is closed.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        let model = MainActor.assumeIsolated { AppModel.shared }
        guard MainActor.assumeIsolated({ model.running }) else { return .terminateNow }
        let alert = NSAlert()
        alert.messageText = "A meeting is being translated"
        alert.informativeText = "Quitting stops the translation for every listener."
        alert.addButton(withTitle: "Keep Translating")
        alert.addButton(withTitle: "Stop and Quit")
        return alert.runModal() == .alertSecondButtonReturn ? .terminateNow : .terminateCancel
    }

    func applicationWillTerminate(_ notification: Notification) {
        MainActor.assumeIsolated { AppModel.shared.engine.stop() }
    }
}

struct RootView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.openSettings) private var openSettings

    var body: some View {
        Group {
            if model.profile.setupDone {
                MainView()
            } else {
                OnboardingView()
            }
        }
        .onAppear { model.openSettings = { openSettings() } }
    }
}

struct MenuBarContent: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        if model.profile.setupDone {
            Text(model.running ? "Translating · \(listenerText)" : "Not translating")
            if model.running {
                Button("Stop") { Task { await model.stopMeeting() } }
            } else {
                Button("Start") { Task { await model.startMeeting() } }.disabled(!model.canStart)
            }
            Divider()
        }
        Button("Open Hear Church") {
            openWindow(id: "main")
            NSApp.activate(ignoringOtherApps: true)
        }
        Button("Quit Hear Church") { NSApp.terminate(nil) }.keyboardShortcut("q")
    }

    private var listenerText: String {
        let n = model.engine.status?.engine.client?.listeners?.captions ?? 0
        return n == 1 ? "1 listener" : "\(n) listeners"
    }
}
