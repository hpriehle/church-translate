import MediaPlayer
import SwiftUI

struct ContentView: View {
    @StateObject private var model = LiveModel()
    @State private var showDetails = ProcessInfo.processInfo.arguments.contains("-details")
    @FocusState private var addressFocused: Bool

    var body: some View {
        ScrollView {
            VStack(spacing: 28) {
                VStack(spacing: 6) {
                    Text("Live English")
                        .font(.largeTitle.bold())
                    Text("Hear the chapel microphone in your earbuds")
                        .font(.body)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }
                .padding(.top, 24)

                Button(action: model.toggle) {
                    ZStack {
                        Circle()
                            .fill(model.isOn ? Color.red : Color.accentColor)
                            .frame(width: 180, height: 180)
                            .shadow(radius: 8, y: 4)
                        Image(systemName: model.isOn ? "stop.fill" : "play.fill")
                            .font(.system(size: 64, weight: .bold))
                            .foregroundStyle(.white)
                    }
                }
                .buttonStyle(.plain)
                .accessibilityLabel(model.isOn ? "Stop listening" : "Start listening")

                Text(statusText)
                    .font(.title3)
                    .multilineTextAlignment(.center)
                    .foregroundStyle(statusColor)
                    .accessibilityAddTraits(.updatesFrequently)

                if model.isOn && model.wireless {
                    Label {
                        Text("\(model.outputName) adds about a quarter-second of delay, which you'll hear as an echo of the room. Wired earbuds remove it. If your earbuds have Noise Cancellation, turn it on, and turn off Transparency or Hearing Aid mode.")
                    } icon: {
                        Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                    }
                    .font(.callout)
                    .padding()
                    .background(.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 12))
                }

                VStack(alignment: .leading, spacing: 8) {
                    Text("Volume").font(.headline)
                    VolumeSlider().frame(height: 36)
                }

                if model.senders.count > 1 {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Found on this network").font(.headline)
                        ForEach(model.senders) { Text($0.name).foregroundStyle(.secondary) }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }

                DisclosureGroup("Details", isExpanded: $showDetails) {
                    VStack(alignment: .leading, spacing: 12) {
                        if model.isOn {
                            details
                        }
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Connect by address").font(.subheadline.bold())
                            TextField("Found automatically (or e.g. 10.0.0.20)", text: $model.manualHost)
                                .textFieldStyle(.roundedBorder)
                                .keyboardType(.numbersAndPunctuation)
                                .autocorrectionDisabled()
                                .textInputAutocapitalization(.never)
                                .focused($addressFocused)
                                .onSubmit { if model.isOn { model.stop(); model.start() } }
                        }
                    }
                    .padding(.top, 8)
                }
            }
            .padding(.horizontal, 20)
            .padding(.bottom, 32)
        }
        .scrollDismissesKeyboard(.interactively)
    }

    private var details: some View {
        let r = model.readout
        return Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 6) {
            row("Delay (estimate)", "\(Int(r.delayMs.rounded())) ms" + (model.wireless ? " + Bluetooth" : ""))
            row("Buffer", "\(Int(r.bufferMs)) ms")
            row("Wi-Fi wobble (99%)", String(format: "%.1f ms", r.jitterMs))
            row("Lost audio", String(format: "%.1f%%", r.lossPercent))
            row("Round trip", String(format: "%.1f ms", r.rttMs))
            row("Phone output", String(format: "%.1f ms", r.outputMs))
            row("Listening now", "\(r.listeners)")
        }
        .font(.callout.monospacedDigit())
    }

    private func row(_ k: String, _ v: String) -> some View {
        GridRow {
            Text(k).foregroundStyle(.secondary)
            Text(v)
        }
    }

    private var statusText: String {
        switch model.phase {
        case .idle: return "Tap to listen"
        case .searching: return "Looking for the chapel audio on this Wi-Fi…"
        case .connecting: return "Connecting…"
        case .playing: return "Listening"
        case .noAudio:
            return "Connected, but no audio is coming through. Make sure you're on the church Wi-Fi and the meeting has started."
        case let .failed(msg): return "Can't connect: \(msg)"
        }
    }

    private var statusColor: Color {
        switch model.phase {
        case .playing: return .green
        case .noAudio, .failed: return .orange
        default: return .primary
        }
    }
}

/// The system volume slider (it also shows the output picker).
struct VolumeSlider: UIViewRepresentable {
    func makeUIView(context: Context) -> MPVolumeView { MPVolumeView(frame: .zero) }
    func updateUIView(_ uiView: MPVolumeView, context: Context) {}
}
