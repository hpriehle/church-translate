import Foundation

/// Wire format shared with live/protocol.py (see that file for the full description).
enum LiveProtocol {
    static let magic: [UInt8] = [0x48, 0x43]  // "HC"
    static let version: UInt8 = 1
    static let defaultPort: UInt16 = 47300
    static let sampleRate = 48000
    static let frameSamples = 240  // 5 ms
    static let frameMs = 5.0
    static let headerSize = 14
    static let flagRedundant: UInt8 = 0x01
    static let serviceType = "_hearchurch-live._udp"
    static let subscribeEvery: TimeInterval = 1.0

    enum PacketType: UInt8 {
        case subscribe = 1, unsubscribe = 2, ping = 3, pong = 4, hello = 5, audio = 16
    }

    struct Packet: Equatable {
        var type: PacketType
        var channel: UInt8 = 0
        var flags: UInt8 = 0
        var seq: UInt32 = 0
        var ts: UInt32 = 0
        var payload: [UInt8] = []
    }

    struct Hello: Equatable {
        var sampleRate: Int
        var frameSamples: Int
        var codec: Int
        var listeners: Int
        var captureLatencyMs: Double
    }

    static func pack(_ p: Packet) -> Data {
        var b = magic + [version, p.type.rawValue, p.channel, p.flags]
        b += be32(p.seq) + be32(p.ts)
        b += p.payload
        return Data(b)
    }

    static func unpack(_ data: Data) -> Packet? {
        let b = [UInt8](data)
        guard b.count >= headerSize, b[0] == magic[0], b[1] == magic[1], b[2] == version,
              let type = PacketType(rawValue: b[3]) else { return nil }
        return Packet(type: type, channel: b[4], flags: b[5], seq: read32(b, 6), ts: read32(b, 10),
                      payload: Array(b[headerSize...]))
    }

    /// (this frame, copy of the previous frame or nil)
    static func splitAudio(_ p: Packet) -> (ArraySlice<UInt8>, ArraySlice<UInt8>?) {
        let pl = p.payload
        guard p.flags & flagRedundant != 0 else { return (pl[...], nil) }
        guard pl.count >= 2 else { return ([], nil) }
        let n = Int(pl[0]) << 8 | Int(pl[1])
        let end = min(2 + n, pl.count)
        let rest = pl[end...]
        return (pl[2..<end], rest.isEmpty ? nil : rest)
    }

    static func parseHello(_ pl: [UInt8]) -> Hello? {
        guard pl.count >= 11 else { return nil }
        return Hello(sampleRate: Int(read32(pl, 0)),
                     frameSamples: Int(pl[4]) << 8 | Int(pl[5]),
                     codec: Int(pl[6]),
                     listeners: Int(pl[7]) << 8 | Int(pl[8]),
                     captureLatencyMs: Double(Int(pl[9]) << 8 | Int(pl[10])) / 10)
    }

    /// a - b for 32-bit wrapping sequence numbers.
    @inline(__always) static func seqDiff(_ a: UInt32, _ b: UInt32) -> Int {
        Int(Int32(bitPattern: a &- b))
    }

    static func be32(_ v: UInt32) -> [UInt8] {
        [UInt8(v >> 24), UInt8(v >> 16 & 0xFF), UInt8(v >> 8 & 0xFF), UInt8(v & 0xFF)]
    }

    static func be64(_ v: UInt64) -> [UInt8] { be32(UInt32(v >> 32)) + be32(UInt32(v & 0xFFFF_FFFF)) }

    static func read32(_ b: [UInt8], _ i: Int) -> UInt32 {
        UInt32(b[i]) << 24 | UInt32(b[i + 1]) << 16 | UInt32(b[i + 2]) << 8 | UInt32(b[i + 3])
    }

    static func read64(_ b: [UInt8], _ i: Int) -> UInt64 {
        UInt64(read32(b, i)) << 32 | UInt64(read32(b, i + 4))
    }
}
