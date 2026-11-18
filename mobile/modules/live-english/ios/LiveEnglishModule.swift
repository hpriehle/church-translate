import AVFoundation
import ExpoModulesCore
import MediaPlayer
import Network

/// Live English for React Native: finds the office sender on the Wi-Fi (Bonjour) and plays its
/// low-delay stream. JS polls `getStats()` for the readout and listens to `onSenders`.
public class LiveEnglishModule: Module {
  private var browser: NWBrowser?
  private var senders: [String: NWEndpoint] = [:]
  private var connection: LiveConnection?
  private var player: LivePlayer?
  private var stream: LiveStream?
  private var startedAt = Date()

  public func definition() -> ModuleDefinition {
    Name("LiveEnglish")

    Events("onSenders")

    Function("isSupported") { true }

    Function("startBrowsing") {
      DispatchQueue.main.async { self.browse() }
    }

    Function("stopBrowsing") {
      DispatchQueue.main.async {
        self.browser?.cancel()
        self.browser = nil
        self.senders = [:]
      }
    }

    /// Start playing. `host` (with optional `port`) connects by address; otherwise the sender
    /// called `name`, or the first one found.
    AsyncFunction("play") { (name: String?, host: String?, port: Int?, title: String?) in
      let endpoint: NWEndpoint
      if let host, !host.isEmpty {
        let p = UInt16(port ?? Int(LiveProtocol.defaultPort))
        endpoint = .hostPort(host: .init(host), port: .init(rawValue: p)!)
      } else if let name, let e = self.senders[name] {
        endpoint = e
      } else if let e = self.senders.values.first {
        endpoint = e
      } else {
        throw Exception(name: "NoSender", description: "The Live English sender wasn't found on this Wi-Fi.")
      }
      try self.start(endpoint, title: title ?? "Live English")
    }.runOnQueue(.main)

    Function("stop") {
      DispatchQueue.main.async { self.stopPlaying() }
    }

    Function("getStats") { () -> [String: Any] in
      guard let connection = self.connection, let stream = self.stream, let player = self.player else {
        return ["active": false, "wireless": LivePlayer.isWireless, "outputName": LivePlayer.outputName]
      }
      let net = connection.snapshot()
      let c = stream.snapshot()
      let played = c.ok + c.recovered + c.lost
      let bufferMs = Double(c.depth) * LiveProtocol.frameMs
      let outputMs = player.outputLatencyMs
      let delay = (net.hello?.captureLatencyMs ?? 10) + LiveProtocol.frameMs + 2.5 + net.rttMs / 2 + bufferMs + outputMs
      var phase = "connecting"
      if net.secondsSinceAudio < 2 && c.playing {
        phase = "playing"
      } else if net.received > 0 || Date().timeIntervalSince(self.startedAt) > 5 {
        phase = "noAudio"
      }
      if net.received == 0, let f = net.failure { phase = "failed: \(f)" }
      return [
        "active": true,
        "phase": phase,
        "delayMs": delay,
        "bufferMs": bufferMs,
        "jitterMs": net.jitterP99Ms,
        "lossPercent": played > 0 ? Double(c.lost) / Double(played) * 100 : 0,
        "rttMs": net.rttMs,
        "listeners": net.hello?.listeners ?? 0,
        "outputMs": outputMs,
        "wireless": LivePlayer.isWireless,
        "outputName": LivePlayer.outputName,
      ]
    }

    OnDestroy {
      DispatchQueue.main.async {
        self.stopPlaying()
        self.browser?.cancel()
      }
    }
  }

  private func browse() {
    guard browser == nil else {
      emitSenders()
      return
    }
    let b = NWBrowser(for: .bonjour(type: LiveProtocol.serviceType, domain: nil), using: .udp)
    b.browseResultsChangedHandler = { [weak self] results, _ in
      guard let self else { return }
      var found: [String: NWEndpoint] = [:]
      for r in results {
        if case let .service(name, _, _, _) = r.endpoint { found[name] = r.endpoint }
      }
      self.senders = found
      self.emitSenders()
    }
    b.start(queue: .main)
    browser = b
  }

  private func emitSenders() {
    sendEvent("onSenders", ["senders": senders.keys.sorted().map { ["name": $0] }])
  }

  private func start(_ endpoint: NWEndpoint, title: String) throws {
    stopPlaying()
    let s = LiveStream(targetFrames: LiveConnection.startTarget)
    let c = LiveConnection(endpoint: endpoint, stream: s)
    let p = LivePlayer(stream: s)
    try p.start()
    c.start()
    stream = s; connection = c; player = p
    startedAt = Date()
    MPNowPlayingInfoCenter.default().nowPlayingInfo = [
      MPMediaItemPropertyTitle: title,
      MPNowPlayingInfoPropertyIsLiveStream: true,
      MPNowPlayingInfoPropertyPlaybackRate: 1.0,
    ]
  }

  private func stopPlaying() {
    connection?.stop()
    player?.stop()
    connection = nil; player = nil; stream = nil
    MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
  }
}
