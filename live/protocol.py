"""Live English wire protocol: small UDP packets between the office sender and listeners.

Every packet starts with a 14-byte header:

    magic   2s  b"HC"
    version B   1
    type    B   see PacketType
    channel B   0 = English (room audio)
    flags   B   FLAG_REDUNDANT on audio packets that carry a copy of the previous frame
    seq     I   audio frame number (wraps at 2**32); 0 on control packets
    ts      I   capture timestamp in samples at 48 kHz (wraps)

Listener -> sender: SUBSCRIBE every 1-2 s (the sender drops a listener after 5 s of silence),
UNSUBSCRIBE when it stops, PING with any payload (echoed back as PONG, for round-trip time).

Sender -> listener: HELLO in reply to each SUBSCRIBE, AUDIO every 5 ms, PONG.

AUDIO payload: one Opus frame, or with FLAG_REDUNDANT a u16 length, that frame, then the
previous frame again (so a single lost packet is recovered from the next one).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

MAGIC = b"HC"
VERSION = 1
DEFAULT_PORT = 47300
SAMPLE_RATE = 48000
FRAME_SAMPLES = 240  # 5 ms
FRAME_MS = FRAME_SAMPLES * 1000 / SAMPLE_RATE
MDNS_TYPE = "_hearchurch-live._udp.local."

SUBSCRIBE_EVERY_S = 1.0
SUBSCRIBER_TIMEOUT_S = 5.0

FLAG_REDUNDANT = 0x01
CODEC_OPUS = 1

_HEADER = struct.Struct("!2sBBBBII")
HEADER_SIZE = _HEADER.size  # 14
_HELLO = struct.Struct("!IHBHH")  # sample rate, frame samples, codec, listeners, sender capture latency (0.1 ms)


class PacketType(IntEnum):
    SUBSCRIBE = 1
    UNSUBSCRIBE = 2
    PING = 3
    PONG = 4
    HELLO = 5
    AUDIO = 16


@dataclass
class Packet:
    type: PacketType
    channel: int = 0
    flags: int = 0
    seq: int = 0
    ts: int = 0
    payload: bytes = b""


@dataclass
class Hello:
    sample_rate: int
    frame_samples: int
    codec: int
    listeners: int
    capture_latency_ms: float


def pack(p: Packet) -> bytes:
    return _HEADER.pack(MAGIC, VERSION, int(p.type), p.channel, p.flags,
                        p.seq & 0xFFFFFFFF, p.ts & 0xFFFFFFFF) + p.payload


def unpack(data: bytes) -> Packet | None:
    """Parse a packet; None for anything that isn't ours (wrong magic/version, unknown type, short)."""
    if len(data) < HEADER_SIZE:
        return None
    magic, ver, typ, channel, flags, seq, ts = _HEADER.unpack_from(data)
    if magic != MAGIC or ver != VERSION:
        return None
    try:
        t = PacketType(typ)
    except ValueError:
        return None
    return Packet(t, channel, flags, seq, ts, data[HEADER_SIZE:])


def audio_payload(frame: bytes, previous: bytes | None) -> tuple[int, bytes]:
    """(flags, payload) for an audio packet, carrying `previous` as redundancy when given."""
    if not previous:
        return 0, frame
    return FLAG_REDUNDANT, struct.pack("!H", len(frame)) + frame + previous


def split_audio(p: Packet) -> tuple[bytes, bytes | None]:
    """(this frame, copy of the previous frame or None)."""
    if not p.flags & FLAG_REDUNDANT:
        return p.payload, None
    if len(p.payload) < 2:
        return b"", None
    (n,) = struct.unpack_from("!H", p.payload)
    return p.payload[2:2 + n], p.payload[2 + n:] or None


def hello_payload(listeners: int, capture_latency_ms: float) -> bytes:
    return _HELLO.pack(SAMPLE_RATE, FRAME_SAMPLES, CODEC_OPUS, min(listeners, 0xFFFF),
                       max(0, min(int(capture_latency_ms * 10), 0xFFFF)))


def parse_hello(payload: bytes) -> Hello | None:
    if len(payload) < _HELLO.size:
        return None
    sr, fs, codec, listeners, lat = _HELLO.unpack_from(payload)
    return Hello(sr, fs, codec, listeners, lat / 10)


def seq_diff(a: int, b: int) -> int:
    """a - b for 32-bit wrapping sequence numbers."""
    d = (a - b) & 0xFFFFFFFF
    return d - 0x100000000 if d >= 0x80000000 else d
