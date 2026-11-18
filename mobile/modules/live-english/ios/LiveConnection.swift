import Foundation
import Network
import QuartzCore

/// UDP link to the office sender: subscribes every second, feeds audio into the stream,
/// measures round-trip time and packet timing wobble, and sizes the buffer from that wobble.
final class LiveConnection: @unchecked Sendable {
    struct Stats {
        var received = 0
        var rttMs: Double = 0
        var jitterP95Ms: Double = 0
        var jitterP99Ms: Double = 0
        var hello: LiveProtocol.Hello?
        var secondsSinceAudio: Double = .infinity
        var ready = false
        var failure: String?
    }

    /// Buffer limits in frames (5 ms each). The target follows the wobble measured on this network.
    static let minTarget = 2, maxTarget = 12, startTarget = 6

    private let queue = DispatchQueue(label: "hearchurch.live.net", qos: .userInteractive)
    private let conn: NWConnection
    private let stream: LiveStream
    private var timer: DispatchSourceTimer?

    // queue-confined state
    private var stats = Stats()
    private var lastAudio: CFTimeInterval = 0
    private var base: (arrival: CFTimeInterval, seq: UInt32)?
    private var transit = [Double](repeating: 0, count: 2000)  // last 10 s of arrival offsets
    private var transitCount = 0
    private var transitHead = 0
    private var rtts: [Double] = []
    private var lowerFor = 0
    private var cancelled = false
    private var started: CFTimeInterval = CACurrentMediaTime()

    init(endpoint: NWEndpoint, stream: LiveStream) {
        let params = NWParameters.udp
        params.serviceClass = .interactiveVoice  // Wi-Fi voice queue where the network honours it
        conn = NWConnection(to: endpoint, using: params)
        self.stream = stream
    }

    func start() {
        conn.stateUpdateHandler = { [weak self] state in
            guard let self else { return }
            switch state {
            case .ready:
                self.stats.ready = true
                self.stats.failure = nil
                self.sendKeepalive()
            case let .failed(err):
                self.stats.ready = false
                self.stats.failure = err.localizedDescription
            case let .waiting(err):
                self.stats.failure = err.localizedDescription
            default: break
            }
        }
        conn.start(queue: queue)
        receive()
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now() + LiveProtocol.subscribeEvery, repeating: LiveProtocol.subscribeEvery)
        t.setEventHandler { [weak self] in
            self?.sendKeepalive()
            self?.adaptBuffer()
        }
        t.resume()
        timer = t
    }

    func stop() {
        queue.sync {
            cancelled = true
            timer?.cancel()
            timer = nil
            send(.init(type: .unsubscribe))
        }
        conn.cancel()
    }

    func snapshot() -> Stats {
        queue.sync {
            var s = stats
            s.secondsSinceAudio = lastAudio == 0 ? .infinity : CACurrentMediaTime() - lastAudio
            return s
        }
    }

    // MARK: - queue

    private func send(_ p: LiveProtocol.Packet) {
        conn.send(content: LiveProtocol.pack(p), completion: .idempotent)
    }

    private func sendKeepalive() {
        send(.init(type: .subscribe))
        let now = CACurrentMediaTime()
        send(.init(type: .ping, payload: LiveProtocol.be64(now.bitPattern)))
    }

    private func receive() {
        conn.receiveMessage { [weak self] data, _, _, error in
            guard let self else { return }
            if let data { self.handle(data) }
            if self.cancelled { return }
            if error == nil {
                self.receive()
            } else {  // e.g. the sender isn't running yet (port unreachable): keep listening
                self.queue.asyncAfter(deadline: .now() + 0.5) { self.receive() }
            }
        }
    }

    private func handle(_ data: Data) {
        guard let p = LiveProtocol.unpack(data) else { return }
        let now = CACurrentMediaTime()
        switch p.type {
        case .audio:
            if base == nil || now - lastAudio > 2 {  // first packet, or the stream restarted
                base = (now, p.seq)
                transitCount = 0
                stream.reset()
            }
            lastAudio = now
            stats.received += 1
            let media = base!.arrival + Double(LiveProtocol.seqDiff(p.seq, base!.seq)) * LiveProtocol.frameMs / 1000
            transit[transitHead] = now - media
            transitHead = (transitHead + 1) % transit.count
            transitCount = min(transitCount + 1, transit.count)
            let (primary, redundant) = LiveProtocol.splitAudio(p)
            stream.push(seq: p.seq, primary: primary, redundant: redundant)
        case .hello:
            stats.hello = LiveProtocol.parseHello(p.payload)
        case .pong where p.payload.count >= 8:
            let sent = Double(bitPattern: LiveProtocol.read64(p.payload, 0))
            guard stats.hello != nil else { return }  // first ping went out before the stream was warm
            rtts.append((now - sent) * 1000)
            if rtts.count > 10 { rtts.removeFirst() }
            stats.rttMs = rtts.sorted()[rtts.count / 2]
        default: break
        }
    }

    /// Size the buffer to cover 99% of the timing wobble seen in the last 10 s, plus a small margin.
    /// Grows at once; shrinks one frame at a time after 5 s of calm.
    private func adaptBuffer() {
        guard transitCount >= 200 else { return }
        let window = Array(transit.prefix(transitCount))
        let lo = window.min() ?? 0
        let spread = window.map { ($0 - lo) * 1000 }.sorted()
        stats.jitterP95Ms = spread[Int(Double(spread.count - 1) * 0.95)]
        stats.jitterP99Ms = spread[Int(Double(spread.count - 1) * 0.99)]
        guard CACurrentMediaTime() - started > 3 else { return }
        let want = Self.clamp(Int(ceil((stats.jitterP99Ms + 3) / LiveProtocol.frameMs)))
        let current = stream.snapshot().target
        if want > current {
            stream.setTarget(frames: want)
            lowerFor = 0
        } else if want < current {
            lowerFor += 1
            if lowerFor >= 5 {
                stream.setTarget(frames: current - 1)
                lowerFor = 0
            }
        } else {
            lowerFor = 0
        }
    }

    static func clamp(_ frames: Int) -> Int { max(minTarget, min(maxTarget, frames)) }
}
