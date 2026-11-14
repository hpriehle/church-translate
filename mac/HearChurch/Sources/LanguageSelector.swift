import SwiftUI

/// The meeting's languages (docs/v3-languages.md): "Select speaker language" and "Select translation language",
/// two grids of toggles with the direction between them. Whatever is on defines the meeting. Used large above
/// Start meeting, compact in the sidebar during a meeting, and in setup.
/// The grids show only downloaded languages (plus any the meeting already has, while they finish downloading);
/// languages are added and removed in Settings → Languages. Setup's first pick (`everyLanguage`) shows all of them,
/// and what's picked there downloads and fills the grids.
struct LanguageSelector: View {
    @EnvironmentObject var model: AppModel
    var compact = false
    var showTitle = true
    var everyLanguage = false
    @State private var note: String?

    var body: some View {
        VStack(alignment: compact ? .leading : .center, spacing: compact ? 8 : 14) {
            if showTitle {
                Text("Translation").font(compact ? .headline : .largeTitle.bold())
            }
            section("Select speaker language:", shown(OutputLanguage.speakers, .speaker), .speaker)
            Image(systemName: "arrow.down")
                .font(compact ? .body.weight(.semibold) : .title.weight(.semibold))
                .foregroundStyle(Color.accentColor)
                .frame(maxWidth: .infinity)
                .accessibilityLabel("translated into")
            section("Select translation language:", shown(OutputLanguage.all, .target), .target)
            if let note { Text(note).font(.callout).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true) }
            if !compact, let summary { Text(summary).font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center) }
            if !everyLanguage {
                Button(compact ? "Add or remove languages…" : "Add or remove languages in Settings…") { model.manageLanguages() }
                    .buttonStyle(.link).font(.callout)
                    .frame(maxWidth: .infinity, alignment: compact ? .leading : .center)
            }
        }
        .onChange(of: model.speakers) { note = nil }
        .onChange(of: model.targets) { note = nil }
    }

    /// Downloaded languages, and the ones this meeting already has (still downloading, or failed: so they can be seen
    /// and switched off). In setup, every language.
    private func shown(_ languages: [OutputLanguage], _ role: AppModel.LanguageRole) -> [OutputLanguage] {
        everyLanguage ? languages : languages.filter { model.isReady($0.id) || model.isOn($0.id, role) }
    }

    private func section(_ title: String, _ languages: [OutputLanguage], _ role: AppModel.LanguageRole) -> some View {
        VStack(alignment: .leading, spacing: compact ? 6 : 8) {
            Text(title).font(compact ? .callout.weight(.semibold) : .title3.weight(.semibold))
            if languages.isEmpty {
                Text(model.engine.packages.isEmpty ? "The translation engine is starting…" : "No language downloaded yet: add one in Settings → Languages.")
                    .font(.callout).foregroundStyle(.secondary)
            }
            LazyVGrid(columns: [GridItem(.adaptive(minimum: compact ? 92 : 128), spacing: compact ? 6 : 8)], spacing: compact ? 6 : 8) {
                ForEach(languages) { l in
                    LanguageCell(language: l, on: model.isOn(l.id, role), compact: compact) { note = model.toggle(l.id, role) }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// "Russian and Latvian → English and Latvian" in words, with what pass-through means.
    private var summary: String? {
        let from = model.speakers.map(OutputLanguage.name), to = model.targets.map(OutputLanguage.name)
        guard !from.isEmpty, !to.isEmpty else { return nil }
        let both = model.targets.filter(model.speakers.contains).map(OutputLanguage.name)
        var s = "\(Self.list(from)) → \(Self.list(to))."
        if !both.isEmpty { s += " \(Self.list(both)) listeners hear \(both.count == 1 ? "that" : "those") speakers in the original." }
        return s
    }

    static func list(_ names: [String]) -> String {
        names.count <= 1 ? names.joined() : names.dropLast().joined(separator: ", ") + " and " + names.last!
    }
}

/// One language toggle: on (filled), off, or not downloaded yet (with its size, or the download's progress).
private struct LanguageCell: View {
    @EnvironmentObject var model: AppModel
    let language: OutputLanguage
    let on: Bool
    let compact: Bool
    let action: () -> Void

    var body: some View {
        let pkg = model.package(language.id)
        let state = pkg?.state ?? (model.engine.packages.isEmpty ? "unknown" : "missing")
        Button(action: action) {
            VStack(spacing: 2) {
                HStack(spacing: 6) {
                    Text(language.flag)
                    Text(compact ? language.name : language.native).lineLimit(1).minimumScaleFactor(0.8)
                    Spacer(minLength: 0)
                    if on { Image(systemName: "checkmark").font(.caption.weight(.bold)) }
                }
                if !compact || state != "ready" {
                    HStack(spacing: 4) {
                        switch state {
                        case "ready": Text(language.name).foregroundStyle(on ? Color.white.opacity(0.85) : .secondary)
                        case "downloading":
                            ProgressView(value: min(1, (pkg?.done_mb ?? 0) / max(1, pkg?.size_mb ?? 1))).controlSize(.mini)
                        case "failed": Label("Download failed", systemImage: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                        case "unknown": Text(language.name).foregroundStyle(.secondary)
                        default: Label("Download \(Format.size(pkg?.size_mb ?? language.sizeMB))", systemImage: "arrow.down.circle")
                            .foregroundStyle(on ? Color.white.opacity(0.85) : .secondary)
                        }
                        Spacer(minLength: 0)
                    }
                    .font(.caption)
                    .labelStyle(.titleAndIcon)
                }
            }
            .padding(.horizontal, compact ? 8 : 10)
            .padding(.vertical, compact ? 5 : 8)
            .frame(maxWidth: .infinity)
            .foregroundStyle(on ? Color.white : Color.primary)
            .background(on ? Color.accentColor : Color.secondary.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
            .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(state == "ready" || on ? Color.clear : Color.secondary.opacity(0.35),
                                                                    style: StrokeStyle(lineWidth: 1, dash: [4, 3])))
            .contentShape(RoundedRectangle(cornerRadius: 8))
        }
        .buttonStyle(.plain)
        .disabled(!language.available)
        .opacity(language.available ? 1 : 0.45)
        .help(help(state))
        .accessibilityAddTraits(on ? .isSelected : [])
    }

    private func help(_ state: String) -> String {
        if let why = language.unavailableReason { return why }
        switch state {
        case "ready": return on ? "Click to switch \(language.name) off" : "Click to switch \(language.name) on"
        case "downloading": return "\(language.name) is downloading"
        default: return "\(language.name) needs to be downloaded: click to download it"
        }
    }
}

/// Speech recognition, needed whatever the languages: its download state, for setup and Settings.
struct SpeechPackageRow: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        HStack {
            Text("Speech recognition").foregroundStyle(.secondary)
            Text("needed for every language").font(.callout).foregroundStyle(.tertiary)
            Spacer()
            PackageBadge(package: model.package("speech"), sizeMB: speechDownloadMB)
        }
    }
}
