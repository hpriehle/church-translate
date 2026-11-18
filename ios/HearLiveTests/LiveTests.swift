import XCTest

final class ProtocolTests: XCTestCase {
    func testRoundTripMatchesPythonLayout() {
        // Bytes produced by live/protocol.py: pack(Packet(AUDIO, 0, FLAG_REDUNDANT, 0xFFFFFFFF, 123, audio_payload(b"abc", b"xy")))
        let python: [UInt8] = [0x48, 0x43, 1, 16, 0, 1, 0xFF, 0xFF, 0xFF, 0xFF, 0, 0, 0, 123,
                               0, 3, 0x61, 0x62, 0x63, 0x78, 0x79]
        let p = LiveProtocol.unpack(Data(python))!
        XCTAssertEqual(p.type, .audio)
        XCTAssertEqual(p.seq, 0xFFFF_FFFF)
        XCTAssertEqual(p.ts, 123)
        let (primary, redundant) = LiveProtocol.splitAudio(p)
        XCTAssertEqual(Array(primary), Array("abc".utf8))
        XCTAssertEqual(redundant.map(Array.init), Array("xy".utf8))
        XCTAssertEqual([UInt8](LiveProtocol.pack(p)), python)
    }

    func testRejectsForeignPackets() {
        XCTAssertNil(LiveProtocol.unpack(Data([0x48, 0x43, 1])))
        XCTAssertNil(LiveProtocol.unpack(Data([0x58, 0x43, 1, 16] + [UInt8](repeating: 0, count: 10))))
        XCTAssertNil(LiveProtocol.unpack(Data([0x48, 0x43, 1, 99] + [UInt8](repeating: 0, count: 10))))
    }

    func testHelloAndSeqWrap() {
        // python: hello_payload(3, 10.25)
        let h = LiveProtocol.parseHello([0, 0, 0xBB, 0x80, 0, 240, 1, 0, 3, 0, 102])!
        XCTAssertEqual(h.sampleRate, 48000)
        XCTAssertEqual(h.frameSamples, 240)
        XCTAssertEqual(h.listeners, 3)
        XCTAssertEqual(h.captureLatencyMs, 10.2, accuracy: 0.001)
        XCTAssertEqual(LiveProtocol.seqDiff(1, 0xFFFF_FFFF), 2)
        XCTAssertEqual(LiveProtocol.seqDiff(0xFFFF_FFFF, 1), -2)
    }
}

final class JitterBufferTests: XCTestCase {
    private let out = UnsafeMutablePointer<UInt8>.allocate(capacity: JitterBuffer.maxPacket)

    private func pull(_ jb: JitterBuffer) -> (JitterBuffer.Pull, [UInt8]) {
        let r = jb.pull(into: out)
        if case let .frame(len, _) = r { return (r, Array(UnsafeBufferPointer(start: out, count: len))) }
        return (r, [])
    }

    func testReordersWaitsAndRecovers() {
        let jb = JitterBuffer(target: 3)
        XCTAssertEqual(pull(jb).0, .wait)
        jb.push(seq: 10, primary: [1], redundant: nil)
        jb.push(seq: 12, primary: [3], redundant: [2])  // 11 lost, 12 carries a copy
        XCTAssertEqual(pull(jb).1, [1])
        let r = pull(jb)
        XCTAssertEqual(r.0, .frame(len: 1, recovered: true))
        XCTAssertEqual(r.1, [2])
        jb.push(seq: 11, primary: [9], redundant: nil)  // too late
        XCTAssertEqual(jb.late, 1)
        XCTAssertEqual(pull(jb).1, [3])
        XCTAssertEqual(pull(jb).0, .lost)
    }

    func testSeqWrapAndRefillAfterOutage() {
        let jb = JitterBuffer(target: 2)
        jb.push(seq: 0xFFFF_FFFF, primary: [1], redundant: nil)
        jb.push(seq: 0, primary: [2], redundant: [1])
        XCTAssertEqual(pull(jb).1, [1])
        XCTAssertEqual(pull(jb).1, [2])
        for _ in 0..<20 { _ = pull(jb) }
        XCTAssertFalse(jb.playing)
        jb.push(seq: 500, primary: [5], redundant: nil)
        XCTAssertEqual(pull(jb).0, .wait)
    }

    func testJumpsAheadAfterStall() {
        let jb = JitterBuffer(target: 2, maxFrames: 10)
        for s in 0..<30 { jb.push(seq: UInt32(s), primary: [UInt8(s)], redundant: nil) }
        XCTAssertLessThanOrEqual(jb.depth, 10)
        XCTAssertGreaterThanOrEqual(pull(jb).1.first ?? 0, 20)
    }

    func testDropsFrameWhenTooFullForAWhile() {
        let jb = JitterBuffer(target: 2)
        jb.driftPulls = 3
        var seq: UInt32 = 0
        func feed(_ n: Int) { for _ in 0..<n { jb.push(seq: seq, primary: [UInt8(seq & 0xFF)], redundant: nil); seq += 1 } }
        feed(8)  // depth 8 > target + 2
        for _ in 0..<4 { feed(1); _ = pull(jb) }
        XCTAssertEqual(jb.skipped, 1)
    }

    func testInsertsFrameWhenTooEmptyForAWhile() {
        let jb = JitterBuffer(target: 6)
        jb.driftPulls = 3
        var seq: UInt32 = 0
        for _ in 0..<6 { jb.push(seq: seq, primary: [1], redundant: nil); seq += 1 }
        var fills = 0
        for _ in 0..<8 {
            if pull(jb).0 == .fill { fills += 1 }
            jb.push(seq: seq, primary: [1], redundant: nil); seq += 1
            if jb.depth > 4 { _ = pull(jb) }  // keep it low: below target - 1
        }
        XCTAssertGreaterThanOrEqual(fills, 1)
    }
}
