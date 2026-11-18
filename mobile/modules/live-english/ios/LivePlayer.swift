import AVFoundation

/// Plays a LiveStream through AVAudioEngine with the smallest IO buffer iOS allows (about 5 ms).
final class LivePlayer {
    private let engine = AVAudioEngine()
    private let stream: LiveStream
    private var source: AVAudioSourceNode?
    private var observers: [NSObjectProtocol] = []
    var onRouteChange: (() -> Void)?

    init(stream: LiveStream) {
        self.stream = stream
    }

    func start() throws {
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playback, mode: .default, options: [])
        try? session.setPreferredSampleRate(Double(LiveProtocol.sampleRate))
        try? session.setPreferredIOBufferDuration(0.005)
        try session.setActive(true)

        let format = AVAudioFormat(standardFormatWithSampleRate: Double(LiveProtocol.sampleRate), channels: 1)!
        let stream = self.stream
        let node = AVAudioSourceNode(format: format) { _, _, frameCount, abl -> OSStatus in
            let buffers = UnsafeMutableAudioBufferListPointer(abl)
            guard let first = buffers.first, let data = first.mData else { return noErr }
            let out = data.assumingMemoryBound(to: Float.self)
            stream.render(into: out, frames: Int(frameCount), gain: 1)
            for b in buffers.dropFirst() {  // copy to any extra channel
                b.mData?.copyMemory(from: data, byteCount: Int(first.mDataByteSize))
            }
            return noErr
        }
        source = node
        engine.attach(node)
        engine.connect(node, to: engine.mainMixerNode, format: format)
        engine.prepare()
        try engine.start()
        observe()
    }

    func stop() {
        observers.forEach(NotificationCenter.default.removeObserver)
        observers = []
        engine.stop()
        if let source { engine.detach(source) }
        source = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    /// Delay added on the phone after the buffer: hardware output plus one IO buffer.
    var outputLatencyMs: Double {
        let s = AVAudioSession.sharedInstance()
        return (s.outputLatency + s.ioBufferDuration) * 1000
    }

    static var isWireless: Bool {
        AVAudioSession.sharedInstance().currentRoute.outputs.contains {
            [.bluetoothA2DP, .bluetoothLE, .bluetoothHFP, .airPlay].contains($0.portType)
        }
    }

    static var outputName: String {
        AVAudioSession.sharedInstance().currentRoute.outputs.first?.portName ?? "Speaker"
    }

    private func observe() {
        let nc = NotificationCenter.default
        observers.append(nc.addObserver(forName: AVAudioSession.interruptionNotification, object: nil,
                                        queue: .main) { [weak self] n in
            guard let raw = n.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                  AVAudioSession.InterruptionType(rawValue: raw) == .ended else { return }
            self?.restart()
        })
        observers.append(nc.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine,
                                        queue: .main) { [weak self] _ in
            self?.restart()
        })
        observers.append(nc.addObserver(forName: AVAudioSession.routeChangeNotification, object: nil,
                                        queue: .main) { [weak self] _ in
            self?.onRouteChange?()
        })
    }

    private func restart() {
        guard source != nil, !engine.isRunning else { return }
        try? AVAudioSession.sharedInstance().setActive(true)
        try? engine.start()
    }
}
