import AVFoundation
import Foundation
import Network
import SwiftUI

@MainActor
final class LiveModel: ObservableObject {
    enum Phase: Equatable {
        case idle, searching, connecting, playing, noAudio, failed(String)
    }

    struct Sender: Identifiable, Hashable {
        let name: String
        let endpoint: NWEndpoint
        var id: String { name }
    }

    struct Readout {
        var delayMs: Double = 0
        var bufferMs: Double = 0
        var jitterMs: Double = 0
        var lossPercent: Double = 0
        var rttMs: Double = 0
        var listeners = 0
        var outputMs: Double = 0
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var senders: [Sender] = []
    @Published private(set) var readout = Readout()
    @Published private(set) var wireless = LivePlayer.isWireless
    @Published private(set) var outputName = LivePlayer.outputName
    @Published var manualHost: String = UserDefaults.standard.string(forKey: "manualHost") ?? "" {
        didSet { UserDefaults.standard.set(manualHost, forKey: "manualHost") }
    }

    private var browser: NWBrowser?
    private var connection: LiveConnection?
    private var player: LivePlayer?
    private var stream: LiveStream?
    private var poll: Timer?
    private var wantsToPlay = false
    private var connectStarted = Date()

    var isOn: Bool { wantsToPlay }

    init() {
        browse()
        if ProcessInfo.processInfo.arguments.contains("-autoplay") { start() }  // testing
        NotificationCenter.default.addObserver(forName: AVAudioSession.routeChangeNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.refreshRoute() }
        }
    }

    func toggle() { wantsToPlay ? stop() : start() }

    func start() {
        wantsToPlay = true
        if let endpoint = chosenEndpoint() {
            connect(endpoint)
        } else {
            phase = .searching
        }
    }

    func stop() {
        wantsToPlay = false
        poll?.invalidate(); poll = nil
        connection?.stop(); connection = nil
        player?.stop(); player = nil
        stream = nil
        phase = .idle
        readout = Readout()
    }

    // MARK: - discovery

    private func chosenEndpoint() -> NWEndpoint? {
        let host = manualHost.trimmingCharacters(in: .whitespaces)
        if !host.isEmpty {
            let parts = host.split(separator: ":")
            let port = parts.count > 1 ? UInt16(parts[1]) ?? LiveProtocol.defaultPort : LiveProtocol.defaultPort
            return .hostPort(host: .init(String(parts[0])), port: .init(rawValue: port)!)
        }
        return senders.first?.endpoint
    }

    private func browse() {
        let b = NWBrowser(for: .bonjour(type: LiveProtocol.serviceType, domain: nil), using: .udp)
        b.browseResultsChangedHandler = { [weak self] results, _ in
            let found = results.compactMap { r -> Sender? in
                if case let .service(name, _, _, _) = r.endpoint { return Sender(name: name, endpoint: r.endpoint) }
                return nil
            }.sorted { $0.name < $1.name }
            Task { @MainActor in
                guard let self else { return }
                self.senders = found
                if self.phase == .searching, let e = self.chosenEndpoint() { self.connect(e) }
            }
        }
        b.start(queue: .main)
        browser = b
    }

    // MARK: - playback

    private func connect(_ endpoint: NWEndpoint) {
        connection?.stop()
        player?.stop()
        let s = LiveStream(targetFrames: LiveConnection.startTarget)
        let c = LiveConnection(endpoint: endpoint, stream: s)
        let p = LivePlayer(stream: s)
        p.onRouteChange = { [weak self] in self?.refreshRoute() }
        do {
            try p.start()
        } catch {
            phase = .failed("Couldn't start audio: \(error.localizedDescription)")
            return
        }
        c.start()
        stream = s; connection = c; player = p
        phase = .connecting
        connectStarted = Date()
        refreshRoute()
        poll?.invalidate()
        poll = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.update() }
        }
    }

    private func update() {
        guard let connection, let stream, let player else { return }
        let net = connection.snapshot()
        let c = stream.snapshot()
        if net.secondsSinceAudio < 2 && c.playing {
            phase = .playing
        } else if net.received == 0 {
            if let f = net.failure {
                phase = .failed(f)
            } else {
                phase = Date().timeIntervalSince(connectStarted) > 5 ? .noAudio : .connecting
            }
        } else if net.secondsSinceAudio >= 2 {
            phase = .noAudio
        }
        var r = Readout()
        r.bufferMs = Double(c.depth) * LiveProtocol.frameMs
        r.jitterMs = net.jitterP99Ms
        r.rttMs = net.rttMs
        r.listeners = net.hello?.listeners ?? 0
        r.outputMs = player.outputLatencyMs
        let played = c.ok + c.recovered + c.lost
        r.lossPercent = played > 0 ? Double(c.lost) / Double(played) * 100 : 0
        // capture + Opus frame and lookahead + one-way network + buffer + phone output
        r.delayMs = (net.hello?.captureLatencyMs ?? 10) + LiveProtocol.frameMs + 2.5 + net.rttMs / 2
            + r.bufferMs + r.outputMs
        readout = r
    }

    private func refreshRoute() {
        wireless = LivePlayer.isWireless
        outputName = LivePlayer.outputName
    }
}
