"""Simulate many phones against a running server.

Each simulated phone opens the caption WebSocket and the MP3 stream. A third of them drop
their connection halfway through and come back 5 s later (like a phone losing Wi-Fi), to
check they get the captions they missed and the audio resumes.

    uv run python scripts/load_test.py --url http://127.0.0.1:8000 --phones 30 --seconds 120
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time

import httpx
import websockets


async def caption_client(base: str, idx: int, seconds: float, drop: bool, out: dict) -> None:
    ws_url = base.replace("http", "ws", 1) + "/ws/listen"
    got: dict[int, float] = {}
    last_seq = 0
    boot = None
    reconnects = 0
    end = time.monotonic() + seconds
    drop_at = time.monotonic() + seconds / 2 if drop else None
    while time.monotonic() < end:
        try:
            async with websockets.connect(ws_url, open_timeout=5) as ws:
                await ws.send(json.dumps({"type": "hello", "last_seq": last_seq, "boot": boot}))
                while time.monotonic() < end:
                    if drop_at and time.monotonic() > drop_at:
                        drop_at = None
                        raise ConnectionResetError("simulated Wi-Fi drop")
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    m = json.loads(raw)
                    if m.get("type") == "init":
                        boot = m.get("boot")
                    items = m.get("history", []) if m.get("type") == "init" else [m] if m.get("type") == "caption" else []
                    for c in items:
                        got.setdefault(c["seq"], time.time())
                        last_seq = max(last_seq, c["seq"])
        except Exception as e:
            reconnects += 1
            await asyncio.sleep(5 if isinstance(e, ConnectionResetError) else 1)
    out[idx] = {"captions": got, "reconnects": reconnects, "dropped": drop}


async def stream_client(base: str, idx: int, seconds: float, drop: bool, out: dict) -> None:
    total = 0
    stalls = 0
    end = time.monotonic() + seconds
    drop_at = time.monotonic() + seconds / 2 if drop else None
    async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=10)) as c:
        while time.monotonic() < end:
            try:
                async with c.stream("GET", base + f"/stream.mp3?phone={idx}") as r:
                    async for chunk in r.aiter_bytes():
                        total += len(chunk)
                        if time.monotonic() > end:
                            break
                        if drop_at and time.monotonic() > drop_at:
                            drop_at = None
                            break
                await asyncio.sleep(5 if drop and drop_at is None and time.monotonic() < end else 0)
            except Exception:
                stalls += 1
                await asyncio.sleep(1)
    listened = seconds - (5 if drop else 0)
    out[idx] = {"bytes": total, "kbps": total * 8 / 1000 / listened, "stalls": stalls}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--phones", type=int, default=30)
    ap.add_argument("--seconds", type=float, default=120)
    a = ap.parse_args()
    base = a.url.rstrip("/")
    caps: dict = {}
    streams: dict = {}
    tasks = []
    for i in range(a.phones):
        drop = i % 3 == 0
        tasks.append(caption_client(base, i, a.seconds, drop, caps))
        tasks.append(stream_client(base, i, a.seconds, drop, streams))
    t0 = time.monotonic()
    await asyncio.gather(*tasks)
    print(f"\n{a.phones} simulated phones for {time.monotonic() - t0:.0f}s against {base}")

    kbps = [s["kbps"] for s in streams.values()]
    print(f"Audio stream: per-phone throughput min {min(kbps):.1f} / median {statistics.median(kbps):.1f} kbps "
          f"(expected ≈ bitrate), stream errors {sum(s['stalls'] for s in streams.values())}")

    all_seqs = set().union(*(set(c["captions"]) for c in caps.values()))
    if not all_seqs:
        print("Captions: none received (is a run in progress?)")
        return
    # Ignore captions that only appeared in the last few seconds (clients were already finishing).
    first_seen = {seq: min(c["captions"][seq] for c in caps.values() if seq in c["captions"]) for seq in all_seqs}
    cutoff = max(first_seen.values()) - 4
    settled = {seq for seq, t in first_seen.items() if t <= cutoff}
    missing = {i: sorted(settled - set(c["captions"])) for i, c in caps.items()}
    complete = sum(1 for m in missing.values() if not m)
    dropped_ok = sum(1 for i, c in caps.items() if c["dropped"] and not missing[i])
    n_drop = sum(1 for c in caps.values() if c["dropped"])
    spread = []
    steady = [c for c in caps.values() if not c["dropped"]]
    for seq in settled:
        ts = [c["captions"][seq] for c in steady if seq in c["captions"]]
        if len(ts) == len(steady):
            spread.append((max(ts) - min(ts)) * 1000)
    print(f"Captions: {len(all_seqs)} distinct; phones with every caption: {complete}/{a.phones}; "
          f"dropped-and-reconnected phones with no gaps: {dropped_ok}/{n_drop}")
    if spread:
        print(f"Caption fan-out spread across connected phones: median {statistics.median(spread):.0f} ms, max {max(spread):.0f} ms")
    bad = {i: m[:10] for i, m in missing.items() if m}
    if bad:
        print("Missing captions:", bad)


if __name__ == "__main__":
    asyncio.run(main())
