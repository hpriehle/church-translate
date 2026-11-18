import Foundation

/// Reorders audio packets by sequence number and hands out one 5 ms frame per pull, `target`
/// frames behind the newest. Fixed storage, so pulling never allocates (it runs on the audio
/// thread). Not thread-safe: the caller holds a lock.
///
/// Drift between the sender's and the phone's clocks shows up as the buffer slowly filling or
/// draining; it is corrected by dropping a frame (too full for 1 s) or inserting a concealed one
/// (too empty for 1 s). Each is 5 ms and inaudible in speech.
final class JitterBuffer {
    enum Pull: Equatable {
        case wait                              // still filling, play silence
        case frame(len: Int, recovered: Bool)  // Opus bytes copied out
        case lost                              // missing: conceal, the stream moves on
        case fill                              // insert a concealed frame without moving on
    }

    static let capacity = 64
    static let maxPacket = 1500

    private(set) var target: Int
    let maxFrames: Int
    private(set) var next: UInt32?
    private(set) var newest: UInt32?
    private(set) var playing = false
    private(set) var late = 0
    private(set) var skipped = 0
    private(set) var inserted = 0
    private var misses = 0
    private var over = 0
    private var under = 0
    /// Pulls in a row the buffer may sit off target before correcting (200 = 1 s).
    var driftPulls = 200

    private var seqs = [UInt32](repeating: 0, count: capacity)
    private var valid = [Bool](repeating: false, count: capacity)
    private var recovered = [Bool](repeating: false, count: capacity)
    private var lens = [Int](repeating: 0, count: capacity)
    private var store = [UInt8](repeating: 0, count: capacity * maxPacket)

    init(target: Int, maxFrames: Int = 40) {
        self.maxFrames = min(maxFrames, Self.capacity - 4)
        self.target = max(1, min(target, self.maxFrames - 2))
    }

    func setTarget(_ t: Int) { target = max(1, min(t, maxFrames - 2)) }

    var depth: Int {
        guard let n = next, let w = newest else { return 0 }
        return max(0, LiveProtocol.seqDiff(w, n) + 1)
    }

    func reset() {
        next = nil; newest = nil; playing = false; misses = 0; over = 0; under = 0
        for i in 0..<Self.capacity { valid[i] = false }
    }

    func push<C: Collection>(seq: UInt32, primary: C, redundant: C?) where C.Element == UInt8 {
        if let n = next, LiveProtocol.seqDiff(seq, n) < 0 {
            late += 1
            return
        }
        if let w = newest {
            if LiveProtocol.seqDiff(seq, w) > 0 { newest = seq }
        } else {
            newest = seq
        }
        if next == nil { next = seq }
        put(seq: seq, bytes: primary, recovered: false)
        let prev = seq &- 1
        if let r = redundant, !r.isEmpty, let n = next, LiveProtocol.seqDiff(prev, n) >= 0 {
            put(seq: prev, bytes: r, recovered: true)
        }
        if depth > maxFrames, let w = newest {  // far behind after a stall: jump to the newest audio
            next = w &- UInt32(target) &+ 1
        }
    }

    private func put<C: Collection>(seq: UInt32, bytes: C, recovered rec: Bool) where C.Element == UInt8 {
        let i = Int(seq % UInt32(Self.capacity))
        if valid[i] && seqs[i] == seq { return }
        let n = min(bytes.count, Self.maxPacket)
        var o = i * Self.maxPacket
        for b in bytes.prefix(n) { store[o] = b; o += 1 }
        seqs[i] = seq; lens[i] = n; recovered[i] = rec; valid[i] = true
    }

    /// Copies the next frame's Opus bytes into `out` (room for at least maxPacket bytes).
    func pull(into out: UnsafeMutablePointer<UInt8>) -> Pull {
        guard let n = next else { return .wait }
        if !playing {
            if depth < target { return .wait }
            playing = true
        }
        let d = depth
        if d > target + 2 {
            over += 1
            if over > driftPulls {  // too full for a while: drop this frame
                over = 0
                skipped += 1
                valid[Int(n % UInt32(Self.capacity))] = false
                next = n &+ 1
                return pull(into: out)
            }
        } else { over = 0 }
        if d < target - 1 {
            under += 1
            if under > driftPulls {  // too empty for a while: stretch by one concealed frame
                under = 0
                inserted += 1
                return .fill
            }
        } else { under = 0 }

        let i = Int(n % UInt32(Self.capacity))
        next = n &+ 1
        if valid[i] && seqs[i] == n {
            valid[i] = false
            misses = 0
            let len = lens[i]
            store.withUnsafeBufferPointer { s in
                out.update(from: s.baseAddress! + i * Self.maxPacket, count: len)
            }
            return .frame(len: len, recovered: recovered[i])
        }
        misses += 1
        if misses >= 20 && depth == 0 {  // 100 ms of nothing: refill before playing again
            reset()
        }
        return .lost
    }
}
