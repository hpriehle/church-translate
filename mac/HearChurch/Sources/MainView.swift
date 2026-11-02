import SwiftUI
import WebKit

/// Every Sunday: the checks, one big Start button, and the operator page underneath.
struct MainView: View {
    @EnvironmentObject var model: AppModel
    @State private var showDetails = false

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            if showDetails, model.engine.state == .running {
                OperatorWebView(url: model.engine.operatorURL)
            } else {
                ScrollView { summary.padding(24).frame(maxWidth: 640) }
                    .frame(maxWidth: .infinity)
            }
        }
    }

    private var header: some View {
        HStack(spacing: 16) {
            VStack(alignment: .leading, spacing: 2) {
                Text(model.profile.name.isEmpty ? "Hear Church" : model.profile.name).font(.title2.bold())
                if let url = model.listenerURL {
                    Link(url.absoluteString.replacingOccurrences(of: "https://", with: ""), destination: url)
                        .font(.callout)
                }
            }
            Spacer()
            liveBadge
            Picker("", selection: $showDetails) {
                Text("Overview").tag(false)
                Text("Operator page").tag(true)
            }
            .pickerStyle(.segmented)
            .frame(width: 220)
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
    }

    @ViewBuilder private var liveBadge: some View {
        if model.running {
            let link = model.engine.status?.engine.client
            let label = link?.live == true ? "LIVE" : (link?.connected == true ? "STARTING" : "RECONNECTING…")
            Text(label)
                .font(.caption.bold())
                .padding(.horizontal, 10).padding(.vertical, 4)
                .background(link?.live == true ? Color.red : Color.orange, in: Capsule())
                .foregroundStyle(.white)
        }
    }

    private var summary: some View {
        VStack(alignment: .leading, spacing: 18) {
            if model.running {
                runningPanel
            } else {
                checksPanel
            }
            if let msg = model.message {
                Text(msg).foregroundStyle(.red)
            }
            bigButton
            if model.onBattery || model.running {
                Label("Keep the lid open and the Mac plugged in until the meeting ends.", systemImage: "powerplug")
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var checksPanel: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Before the meeting").font(.headline)
            ForEach(model.checks) { CheckRow(check: $0) }
            LevelMeter(db: Double(model.mic.levelDb))
        }
        // The microphone is only open while this list is on screen (the orange dot; AirPods drop to call quality).
        .onAppear { model.mic.start() }
        .onDisappear { model.mic.stop() }
    }

    private var runningPanel: some View {
        let st = model.engine.status
        let link = st?.engine.client
        return VStack(alignment: .leading, spacing: 10) {
            Text("Translating").font(.headline)
            LevelMeter(db: st?.pipeline.level_db ?? -80)
            HStack(spacing: 24) {
                stat("Listeners", "\(link?.listeners?.captions ?? 0)")
                stat("Connection", link?.connected == true ? "Connected" : "Reconnecting…")
                stat("Claude", claudeState(st))
            }
            if let notice = link?.notice, !notice.isEmpty { Text(notice).foregroundStyle(.orange) }
            if let err = st?.pipeline.last_error, !err.isEmpty { Text(err).foregroundStyle(.red) }
            if let err = st?.translate.values.compactMap(\.last_error).first(where: { !$0.isEmpty }) {
                Text("Translation paused: \(err)").foregroundStyle(.orange)
            }
        }
    }

    private func claudeState(_ st: EngineStatus?) -> String {
        let states = Set(st?.translate.values.map(\.claude.state) ?? [])
        if states.isSubset(of: ["ready", "busy"]) && !states.isEmpty { return "Working" }
        if states.contains("starting") { return "Starting…" }
        return "Paused, retrying"
    }

    private func stat(_ k: String, _ v: String) -> some View {
        VStack(alignment: .leading) {
            Text(k).font(.caption).foregroundStyle(.secondary)
            Text(v).font(.title3)
        }
    }

    private var bigButton: some View {
        Button {
            Task { model.running ? await model.stopMeeting() : await model.startMeeting() }
        } label: {
            Text(model.running ? "Stop" : "Start")
                .font(.title.bold())
                .frame(maxWidth: .infinity, minHeight: 56)
        }
        .buttonStyle(.borderedProminent)
        .tint(model.running ? .red : model.canStart ? .green : .gray)
        .disabled(!model.running && !model.canStart)
    }
}

struct CheckRow: View {
    let check: Check

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            icon.frame(width: 18)
            VStack(alignment: .leading, spacing: 2) {
                Text(check.title)
                if !check.detail.isEmpty { Text(check.detail).font(.callout).foregroundStyle(.secondary) }
            }
            Spacer()
            if let (title, run) = check.action { Button(title, action: run) }
        }
    }

    @ViewBuilder private var icon: some View {
        switch check.state {
        case .ok: Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
        case .warn: Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
        case .bad: Image(systemName: "xmark.circle.fill").foregroundStyle(.red)
        case .pending: ProgressView().controlSize(.small)
        }
    }
}

struct LevelMeter: View {
    let db: Double

    var body: some View {
        GeometryReader { g in
            let frac = max(0, min(1, (db + 60) / 60))
            ZStack(alignment: .leading) {
                Capsule().fill(.quaternary)
                Capsule().fill(frac > 0.9 ? Color.red : Color.green).frame(width: g.size.width * frac)
            }
        }
        .frame(height: 8)
        .animation(.linear(duration: 0.1), value: db)
        .accessibilityLabel("Input level")
    }
}

/// The engine's operator page (web/operator), which the app already knows how to show.
struct OperatorWebView: NSViewRepresentable {
    let url: URL

    func makeNSView(context: Context) -> WKWebView {
        let view = WKWebView()
        view.load(URLRequest(url: url))
        return view
    }

    func updateNSView(_ view: WKWebView, context: Context) {
        if view.url?.port != url.port { view.load(URLRequest(url: url)) }   // the engine restarted on a new port
    }
}
