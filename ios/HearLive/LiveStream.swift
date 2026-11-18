import Copus
import Foundation
import os

/// The part shared by the network queue (push) and the audio render thread (render):
/// jitter buffer, Opus decoder and a small PCM FIFO, all behind one short lock.
final class LiveStream: @unchecked Sendable {
    struct Counters {
        var ok = 0, recovered = 0, lost = 0, filled = 0
        var late = 0, skipped = 0, inserted = 0
        var depth = 0, target = 0
        var playing = false
    }

    private var lock = os_unfair_lock()
    let jb: JitterBuffer
    private var decoder: OpaquePointer
    private let packet = UnsafeMutablePointer<UInt8>.allocate(capacity: JitterBuffer.maxPacket)
    private let pcm = UnsafeMutablePointer<Float>.allocate(capacity: 960)  // max 20 ms per decode
    private let fifoCap = 4096
    private let fifo: UnsafeMutablePointer<Float>
    private var fifoRead = 0
    private var fifoCount = 0
    private var counters = Counters()

    init(targetFrames: Int) {
        jb = JitterBuffer(target: targetFrames)
        decoder = Self.makeDecoder()
        fifo = .allocate(capacity: fifoCap)
    }

    private static func makeDecoder() -> OpaquePointer {
        var err: Int32 = 0
        let d = opus_decoder_create(Int32(LiveProtocol.sampleRate), 1, &err)
        precondition(err == OPUS_OK && d != nil, "opus_decoder_create failed: \(err)")
        return d!
    }

    deinit {
        opus_decoder_destroy(decoder)
        packet.deallocate(); pcm.deallocate(); fifo.deallocate()
    }

    func push<C: Collection>(seq: UInt32, primary: C, redundant: C?) where C.Element == UInt8 {
        os_unfair_lock_lock(&lock)
        jb.push(seq: seq, primary: primary, redundant: redundant)
        os_unfair_lock_unlock(&lock)
    }

    func setTarget(frames: Int) {
        os_unfair_lock_lock(&lock)
        jb.setTarget(frames)
        os_unfair_lock_unlock(&lock)
    }

    func reset() {
        os_unfair_lock_lock(&lock)
        jb.reset()
        fifoCount = 0
        opus_decoder_destroy(decoder)
        decoder = Self.makeDecoder()
        os_unfair_lock_unlock(&lock)
    }

    func snapshot() -> Counters {
        os_unfair_lock_lock(&lock)
        var c = counters
        c.late = jb.late; c.skipped = jb.skipped; c.inserted = jb.inserted
        c.depth = jb.depth; c.target = jb.target; c.playing = jb.playing
        os_unfair_lock_unlock(&lock)
        return c
    }

    /// Audio thread: fill `out` with `frames` samples.
    func render(into out: UnsafeMutablePointer<Float>, frames: Int, gain: Float) {
        os_unfair_lock_lock(&lock)
        var waiting = false
        while fifoCount < frames && !waiting {
            var n: Int32 = 0
            switch jb.pull(into: packet) {
            case .wait:
                waiting = true
            case let .frame(len, rec):
                n = opus_decode_float(decoder, packet, Int32(len), pcm, 960, 0)
                if rec { counters.recovered += 1 } else { counters.ok += 1 }
            case .lost:
                n = opus_decode_float(decoder, nil, 0, pcm, Int32(LiveProtocol.frameSamples), 0)
                counters.lost += 1
            case .fill:
                n = opus_decode_float(decoder, nil, 0, pcm, Int32(LiveProtocol.frameSamples), 0)
                counters.filled += 1
            }
            if n > 0 { appendFifo(Int(n)) }
        }
        let take = min(frames, fifoCount)
        for k in 0..<take {
            out[k] = fifo[(fifoRead + k) % fifoCap] * gain
        }
        fifoRead = (fifoRead + take) % fifoCap
        fifoCount -= take
        if take < frames { (out + take).update(repeating: 0, count: frames - take) }
        os_unfair_lock_unlock(&lock)
    }

    private func appendFifo(_ n: Int) {
        let room = fifoCap - fifoCount
        let m = min(n, room)
        var w = (fifoRead + fifoCount) % fifoCap
        for k in 0..<m {
            fifo[w] = pcm[k]
            w += 1
            if w == fifoCap { w = 0 }
        }
        fifoCount += m
    }
}
