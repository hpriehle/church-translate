import CoreImage.CIFilterBuiltins
import SwiftUI

/// The operator page: Start meeting at the top left, the checks and controls on the left, and what is being
/// said (with its translations) filling the rest.
struct MainView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        VStack(spacing: 0) {
            if let warning = model.micWarning {
                Label(warning, systemImage: "mic.slash.fill")
                    .font(.headline).foregroundStyle(.white)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 16).padding(.vertical, 10)
                    .background(Color.red)
            }
            EngineFailedBanner()
            TopBar()
            Divider()
            DownloadBar()
            HStack(spacing: 0) {
                ScrollView { Sidebar().padding(16) }
                    .frame(width: 330)
                    .background(.background.secondary)
                Divider()
                if model.running { TranscriptView() } else { IdlePane() }
            }
        }
        // The microphone is only open while the checks can show its level (the orange dot; AirPods drop to call
        // quality). During a meeting the engine has it.
        .onAppear { model.startMicPreview() }
        .onDisappear { model.mic.stop() }
        .onChange(of: model.running) { _, running in running ? model.mic.stop() : model.startMicPreview() }
        .onChange(of: model.selectedInput) { model.startMicPreview() }
    }
}

// MARK: - top bar

private struct TopBar: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        HStack(spacing: 16) {
            if model.running { StartButton() }   // before a meeting it sits under the language selector
            MeetingState()
            HStack(spacing: 6) {
                Image(systemName: "mic.fill").foregroundStyle(.secondary)
                Group {
                    if model.running { LevelMeter(db: model.engine.status?.pipeline.level_db ?? -80) }
                    else { MicMeter(level: model.mic.level) }
                }
                .frame(width: 140)
            }
            Spacer(minLength: 12)
            Label("\(model.listeners) listening", systemImage: "person.2.fill")
                .foregroundStyle(model.running ? .primary : .secondary)
                .monospacedDigit()
            ConnectionBadge()
            Divider().frame(height: 22)
            VStack(alignment: .trailing, spacing: 0) {
                Text(model.profile.name).font(.headline).lineLimit(1)
                if !model.profile.stake.isEmpty { Text(model.profile.stake).font(.caption).foregroundStyle(.secondary) }
            }
            Button { model.openSettings?() } label: { Image(systemName: "gearshape") }
                .buttonStyle(.borderless)
                .help("Settings")
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
    }
}

/// Before a meeting: the languages, large, with Start meeting under them; the last meeting's transcript below.
private struct IdlePane: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(spacing: 18) {
                    LanguageSelector()
                    StartButton(large: true)
                }
                .padding(24)
                .frame(maxWidth: 760)
                .frame(maxWidth: .infinity)
            }
            .frame(maxHeight: model.engine.chunks.isEmpty ? .infinity : 520)
            if !model.engine.chunks.isEmpty {
                Divider()
                TranscriptView(title: "Last meeting")
            }
        }
    }
}

private struct StartButton: View {
    @EnvironmentObject var model: AppModel
    var large = false
    @State private var confirmStop = false
    @State private var busy = false

    var body: some View {
        Button {
            if model.running { confirmStop = true } else { Task { busy = true; await model.startMeeting(); busy = false } }
        } label: {
            Label(model.running ? "Stop meeting" : "Start meeting", systemImage: model.running ? "stop.fill" : "play.fill")
                .font(large ? .title2.weight(.semibold) : .title3.weight(.semibold))
                .frame(minWidth: large ? 260 : 170, minHeight: large ? 44 : 34)
        }
        .buttonStyle(.borderedProminent)
        .tint(model.running ? .red : .green)
        .controlSize(.large)
        .disabled(busy || (!model.running && !model.canStart))
        .help(model.canStart || model.running ? "" : "Fix the items marked in red first")
        .confirmationDialog("Stop translating for every listener?", isPresented: $confirmStop) {
            Button("Stop meeting", role: .destructive) { Task { await model.stopMeeting() } }
        }
    }
}

private struct MeetingState: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        if model.running {
            HStack(spacing: 8) {
                Text("LIVE").font(.caption.bold()).padding(.horizontal, 8).padding(.vertical, 3)
                    .background(live ? Color.red : Color.orange, in: Capsule()).foregroundStyle(.white)
                if let start = model.meetingStarted {
                    TimelineView(.periodic(from: .now, by: 1)) { ctx in
                        Text(elapsed(from: start, to: ctx.date)).monospacedDigit().foregroundStyle(.secondary)
                    }
                }
            }
        } else if model.engine.state != .running {
            Label("Starting…", systemImage: "hourglass").foregroundStyle(.secondary)
        } else if model.canStart {
            Label("Ready", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
        } else {
            Label(model.problems == 1 ? "1 thing to fix" : "\(model.problems) things to fix", systemImage: "exclamationmark.circle.fill")
                .foregroundStyle(.orange)
        }
    }

    private var live: Bool { model.engine.status?.engine.client?.live == true }

    private func elapsed(from: Date, to: Date) -> String {
        let s = max(0, Int(to.timeIntervalSince(from)))
        return s >= 3600 ? String(format: "%d:%02d:%02d", s / 3600, s / 60 % 60, s % 60) : String(format: "%d:%02d", s / 60, s % 60)
    }
}

private struct ConnectionBadge: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        let link = model.engine.status?.engine.client
        let host = AppPaths.hub.host ?? "hearchurch.com"
        let (icon, color, text): (String, Color, String) = {
            if !model.network.online { return ("wifi.slash", .red, "Offline") }
            if model.localSharing { return ("wifi", .green, "This Wi-Fi only") }
            if link?.connected == true { return ("antenna.radiowaves.left.and.right", .green, host) }
            if link == nil { return ("antenna.radiowaves.left.and.right.slash", .secondary, "Not linked") }
            return ("antenna.radiowaves.left.and.right.slash", .orange, "Connecting…")
        }()
        Label(text, systemImage: icon).foregroundStyle(color).help(link?.error ?? link?.notice ?? "")
    }
}

// MARK: - sidebar

private struct Sidebar: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            if let msg = model.message {
                Text(msg).foregroundStyle(.red).font(.callout)
            }
            if model.running { DuringMeeting() } else { BeforeMeeting() }
            if model.running { LanguagesSection() }   // before a meeting the selector is large, above Start
            SoundSection()
            ListenersSection()
        }
    }
}

private struct SectionCard<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title.uppercased()).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            content
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct BeforeMeeting: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        SectionCard(title: "Before the meeting") {
            ForEach(model.checks) { CheckRow(check: $0) }
            if model.onBattery {
                Text("Keep the lid open and the Mac plugged in until the meeting ends.").font(.callout).foregroundStyle(.secondary)
            }
        }
    }
}

private struct DuringMeeting: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        let st = model.engine.status
        SectionCard(title: "Translating") {
            ForEach(model.targets, id: \.self) { code in
                let t = st?.translate[code]
                HStack {
                    Text(OutputLanguage.name(code))
                    Spacer()
                    if st?.source == code {
                        Text("Original").foregroundStyle(.secondary).help("The speaker is speaking \(OutputLanguage.name(code)): it goes out as spoken")
                    } else {
                        Text(claude(t?.claude.state)).foregroundStyle(t?.claude.state == "ready" || t?.claude.state == "busy" ? .green : .orange)
                    }
                }
                if let e = t?.last_error, !e.isEmpty { Text(e).font(.caption).foregroundStyle(.orange) }
            }
            if let b = st?.pipeline.backlog_s, b > 6 {
                Text("Voices are \(Int(b)) s behind the speaker; they speed up to catch up.").font(.callout).foregroundStyle(.orange)
            }
            if let err = st?.pipeline.last_error, !err.isEmpty { Text(err).font(.callout).foregroundStyle(.red) }
            if let notice = st?.engine.client?.notice, !notice.isEmpty { Text(notice).font(.callout).foregroundStyle(.orange) }
            if let mic = model.micName {
                Label(mic, systemImage: "mic").font(.caption).foregroundStyle(.secondary).help("The microphone this meeting is using")
            }
            Text("Keep the lid open and the Mac plugged in until the meeting ends.").font(.callout).foregroundStyle(.secondary)
        }
    }

    private func claude(_ s: String?) -> String {
        switch s {
        case "ready", "busy": return "Translating"
        case "starting": return "Starting…"
        default: return "Paused, retrying"
        }
    }
}

private struct SoundSection: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        SectionCard(title: "Sound") {
            if let s = model.engine.settings {
                let audio = s.settings.audio
                Picker("Input", selection: Binding(get: { audio.input_device }, set: { name in
                    Task { await model.chooseInput(name) }
                })) {
                    Text("Mac default (\(s.devices.first { $0.default == true }?.name ?? "none"))").tag("")
                    if let missing = model.missingInput { Text("\(missing) (not connected)").tag(missing) }
                    Divider()
                    ForEach(s.devices, id: \.name) { Text($0.name).tag($0.name) }
                }
                .disabled(model.running)
                if let dev = s.devices.first(where: { $0.name == audio.input_device }), dev.channels > 1 {
                    Picker("Channel", selection: Binding(get: { audio.input_channel }, set: { ch in
                        Task { await model.engine.set(["audio": ["input_channel": ch]]) }
                    })) {
                        Text("Mix all").tag(0)
                        ForEach(1...dev.channels, id: \.self) { Text("\($0)").tag($0) }
                    }
                    .disabled(model.running)
                }
                GainSlider(gain: audio.input_gain)
            } else {
                ProgressView().controlSize(.small)
            }
        }
    }
}

private struct GainSlider: View {
    @EnvironmentObject var model: AppModel
    let gain: Double
    @State private var value = 1.0

    var body: some View {
        HStack {
            Text("Gain")
            Slider(value: $value, in: 0.5...4, step: 0.1) { editing in
                if !editing { Task { await model.engine.set(["audio": ["input_gain": value]]) } }
            }
            Text(String(format: "%.1f×", value)).monospacedDigit().frame(width: 38, alignment: .trailing)
        }
        .onAppear { value = gain }
        .onChange(of: gain) { _, g in value = g }
    }
}

/// During a meeting: the same speaker / translation toggles as before it, small; changes apply live.
private struct LanguagesSection: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        SectionCard(title: "Languages") {
            LanguageSelector(compact: true, showTitle: false)
            if let src = model.engine.status?.source, model.speakers.count > 1 {
                Text("Speaking now: \(OutputLanguage.name(src))").font(.caption).foregroundStyle(.secondary)
            }
            if let failed = model.engine.packages.first(where: { $0.state == "failed" }) {
                Text("\(failed.name): \(failed.error)").font(.caption).foregroundStyle(.orange)
            }
        }
    }
}

private struct ListenersSection: View {
    @EnvironmentObject var model: AppModel
    @State private var copied = false

    var body: some View {
        SectionCard(title: "Listeners") {
            Picker("Sharing", selection: Binding(get: { model.localSharing }, set: { local in
                Task { await model.setSharing(local: local) }
            })) {
                Text("hearchurch.com").tag(false)
                Text("This Wi-Fi only").tag(true)
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .disabled(model.running)
            .help(model.running ? "Stop the meeting to change how it is shared" : "")
            Text(model.localSharing
                 ? "Private: only phones on the same Wi-Fi as this Mac, with this meeting's QR code. A new code after every meeting."
                 : "Public: anyone with the link can listen, from anywhere.")
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if let url = model.listenerURL {
                HStack(alignment: .top, spacing: 12) {
                    if let qr = QRCode.image(url.absoluteString) {
                        Image(nsImage: qr).interpolation(.none).resizable().frame(width: 110, height: 110)
                            .padding(6).background(.white, in: RoundedRectangle(cornerRadius: 6))
                    }
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Members scan this, or open:").font(.callout).foregroundStyle(.secondary)
                        Link(url.absoluteString.replacingOccurrences(of: "https://", with: "").replacingOccurrences(of: "http://", with: ""),
                             destination: url)
                            .font(.callout).lineLimit(2)
                        Button(copied ? "Copied" : "Copy link") {
                            NSPasteboard.general.clearContents()
                            NSPasteboard.general.setString(url.absoluteString, forType: .string)
                            copied = true
                        }
                    }
                }
                HStack {
                    Button("Print a sign") { NSWorkspace.shared.open(model.engine.baseURL.appendingPathComponent("print")) }
                    Button("Play a test sentence") { Task { await model.engine.post("api/test-voice") } }
                        .help("Every listening phone hears a short sentence in its language")
                }
            } else {
                Text(model.localSharing ? "Waiting for the translation engine…" : "This Mac isn't linked to hearchurch.com yet.")
                    .font(.callout).foregroundStyle(.secondary)
            }
        }
        .onChange(of: model.listenerURL) { copied = false }
    }
}

// MARK: - transcript

/// What is said, in columns: the original (as spoken) and one per translation language, a row per sentence,
/// newest at the top.
private struct TranscriptView: View {
    @EnvironmentObject var model: AppModel
    var title = "Transcript"
    @State private var showLog = false

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(title).font(.headline)
                Text(summary).font(.callout).foregroundStyle(.secondary)
                Spacer()
                Toggle("Activity", isOn: $showLog).toggleStyle(.button).controlSize(.small).focusEffectDisabled()
            }
            .padding(.horizontal, 16).padding(.vertical, 10)
            Divider()
            if model.engine.chunks.isEmpty {
                VStack(spacing: 8) {
                    Image(systemName: "text.bubble").font(.system(size: 36)).foregroundStyle(.tertiary)
                    Text(model.running ? "Listening… what's said appears here with its translations."
                                       : "Press Start meeting. What's said appears here with its translations.")
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                TranscriptColumns(chunks: model.engine.chunks, targets: columns)
            }
            if showLog {
                Divider()
                List(model.engine.log) { line in
                    Text(line.text).font(.caption.monospaced())
                        .foregroundStyle(line.level == "error" ? .red : line.level == "warn" ? .orange : .secondary)
                }
                .listStyle(.plain)
                .frame(height: 140)
            }
        }
    }

    /// The meeting's translation languages, then any other language still in the shown sentences (switched off since).
    private var columns: [String] {
        var out = model.targets
        for c in model.engine.chunks.prefix(50) {
            for code in c.langs.keys.sorted() where !out.contains(code) { out.append(code) }
        }
        return out
    }

    private var summary: String {
        let c = model.engine.chunks
        guard !c.isEmpty else { return "" }
        let lags = c.flatMap { $0.langs.values.compactMap(\.caption_lag) }.sorted()
        let median = lags.isEmpty ? "" : String(format: " · captions %.1f s after the speaker pauses", lags[lags.count / 2])
        return "\(c.count) sentences\(median)"
    }
}

private struct TranscriptColumns: View {
    let chunks: [Chunk]
    let targets: [String]
    private static let minWidth: CGFloat = 200

    var body: some View {
        GeometryReader { g in
            let count = CGFloat(targets.count + 1)
            let width = max(Self.minWidth, (g.size.width - 1) / count)
            ScrollView(.horizontal) {
                VStack(spacing: 0) {
                    HStack(spacing: 0) {
                        header("Original", detail: "as spoken", width: width)
                        ForEach(targets, id: \.self) { code in
                            header("\(OutputLanguage.all.first { $0.id == code }?.flag ?? "") \(OutputLanguage.name(code))", detail: OutputLanguage.native(code), width: width)
                        }
                    }
                    .background(.bar)
                    Divider()
                    ScrollView(.vertical) {
                        LazyVStack(spacing: 0) {
                            ForEach(chunks) { chunk in
                                ChunkRow(chunk: chunk, targets: targets, width: width)
                                Divider()
                            }
                        }
                    }
                }
                .frame(width: max(g.size.width, width * count), height: g.size.height, alignment: .topLeading)
            }
        }
    }

    private func header(_ title: String, detail: String, width: CGFloat) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(title).font(.subheadline.weight(.semibold))
            Text(detail).font(.caption).foregroundStyle(.secondary)
            Spacer(minLength: 0)
        }
        .lineLimit(1)
        .padding(.horizontal, 12).padding(.vertical, 6)
        .frame(width: width, alignment: .leading)
    }
}

/// One sentence across the columns.
private struct ChunkRow: View {
    let chunk: Chunk
    let targets: [String]
    let width: CGFloat

    var body: some View {
        HStack(alignment: .top, spacing: 0) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    if let src = chunk.src, !src.isEmpty {
                        Text(src.uppercased()).font(.caption2.bold()).foregroundStyle(.secondary)
                            .help("Spoken in \(OutputLanguage.name(src))")
                    }
                    if let t = chunk.wall_time {
                        Text(Date(timeIntervalSince1970: t), style: .time).font(.caption).foregroundStyle(.tertiary)
                    }
                }
                Text(chunk.en.isEmpty ? "…" : chunk.en).textSelection(.enabled)
                if chunk.status == "skipped" || chunk.status == "failed" {
                    Text(chunk.note ?? chunk.status).font(.caption).foregroundStyle(.secondary)
                }
            }
            .cell(width)
            ForEach(targets, id: \.self) { code in
                Group {
                    if let out = chunk.langs[code] {
                        VStack(alignment: .leading, spacing: 4) {
                            if out.original {
                                Text("Original").font(.caption2.bold()).foregroundStyle(.secondary)
                                    .help("The speaker spoke \(OutputLanguage.name(code)): it goes out as spoken, not translated")
                            }
                            Text(out.text.isEmpty ? (out.status == "pending" ? "…" : "—") : out.text)
                                .foregroundStyle(color(out.status)).textSelection(.enabled)
                            if let note = note(out) { Text(note).font(.caption).foregroundStyle(.orange) }
                        }
                    } else {
                        Text("—").foregroundStyle(.tertiary)
                    }
                }
                .cell(width)
            }
        }
    }

    private func color(_ status: String) -> Color {
        switch status {
        case "aired", "captioned": return .primary
        default: return .secondary
        }
    }

    private func note(_ out: Chunk.Out) -> String? {
        switch out.status {
        case "dropped": return "Caption shown; audio skipped to catch up"
        case "failed", "skipped": return out.note?.isEmpty == false ? out.note : out.status
        case "merged": return out.note
        default: return nil
        }
    }
}

private extension View {
    func cell(_ width: CGFloat) -> some View {
        padding(.horizontal, 12).padding(.vertical, 8).frame(width: width, alignment: .topLeading)
    }
}

// MARK: - shared pieces

/// Shown (main window and setup) when the translation engine is stopped for good: restart it or read why.
struct EngineFailedBanner: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        if case .failed(let why) = model.engine.state {
            HStack(spacing: 12) {
                Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.red)
                Text(why).fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 8)
                Button("Restart engine") { model.relaunchEngine() }
                Button("Show log") { Engine.revealLog() }
            }
            .padding(.horizontal, 16).padding(.vertical, 10)
            .background(Color.red.opacity(0.12))
        }
    }
}

struct CheckRow: View {
    let check: Check

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            icon.frame(width: 16)
            VStack(alignment: .leading, spacing: 2) {
                Text(check.title)
                if !check.detail.isEmpty { Text(check.detail).font(.callout).foregroundStyle(.secondary) }
            }
            Spacer()
            if let (title, run) = check.action { Button(title, action: run).controlSize(.small) }
        }
    }

    @ViewBuilder private var icon: some View {
        switch check.state {
        case .ok: Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
        case .warn: Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
        case .bad: Image(systemName: "xmark.circle.fill").foregroundStyle(.red)
        case .pending: ProgressView().controlSize(.mini)
        }
    }
}

/// The preflight meter: watches only the level, so nothing else redraws with it.
struct MicMeter: View {
    @ObservedObject var level: MicLevel
    var body: some View { LevelMeter(db: Double(level.db)) }
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

/// The thin bar at the top of the window while voices and speech recognition download.
struct DownloadBar: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        let items = model.downloads
        if !items.isEmpty {
            let active = items.filter { $0.state == "downloading" }
            let done = active.reduce(0) { $0 + $1.done_mb }
            let total = active.reduce(0) { $0 + $1.size_mb }
            VStack(spacing: 4) {
                HStack {
                    if active.isEmpty {
                        Label("Download failed: \(items.map(\.name).joined(separator: ", "))", systemImage: "exclamationmark.triangle.fill")
                            .foregroundStyle(.orange)
                        Spacer()
                        Button("Try again") { Task { await model.engine.download(model.neededDownloads) } }.controlSize(.small)
                    } else {
                        Text("Downloading \(active.map { $0.name.lowercased() }.joined(separator: " and "))")
                        Spacer()
                        Text("\(Format.size(done)) of \(Format.size(total))").monospacedDigit().foregroundStyle(.secondary)
                    }
                }
                .font(.callout)
                if !active.isEmpty { ProgressView(value: total > 0 ? done / total : 0).progressViewStyle(.linear) }
            }
            .padding(.horizontal, 16).padding(.vertical, 8)
            .background(.bar)
            Divider()
        }
    }
}

enum QRCode {
    static func image(_ text: String) -> NSImage? {
        let f = CIFilter.qrCodeGenerator()
        f.message = Data(text.utf8)
        f.correctionLevel = "M"
        guard let out = f.outputImage else { return nil }
        let rep = NSCIImageRep(ciImage: out)
        let img = NSImage(size: rep.size)
        img.addRepresentation(rep)
        return img
    }
}
