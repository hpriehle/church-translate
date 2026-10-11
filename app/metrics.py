"""Per-chunk timing records, written as JSONL (one file per run) and summarised for reports."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np


@dataclass
class ChunkRecord:
    seq: int
    audio_start_s: float          # position in the input
    audio_dur_s: float
    voiced_s: float
    cut_reason: str
    t_speech_start: float         # monotonic
    t_speech_end: float
    t_cut: float
    en: str = ""
    fr: str = ""
    engine: str = ""
    note: str = ""
    stt_ms: float = 0.0
    mt_ms: float = 0.0
    tts_ms: float = 0.0
    t_stt_done: float = 0.0
    t_caption: float = 0.0
    t_audio_ready: float = 0.0
    t_on_air: float = 0.0
    clip_s: float = 0.0
    speed: float = 1.0
    condensed: bool = False
    backlog_at_enqueue: float = 0.0
    merged: list[int] = field(default_factory=list)
    caption_no: int = 0           # gap-free number the phones see (chunks can be skipped or merged)
    status: str = "pending"       # pending|skipped|captioned|aired|dropped|failed
    wall_time: float = field(default_factory=time.time)

    # Lags measured from the moment the speaker *finished* the phrase.
    @property
    def caption_lag(self) -> float | None:
        return self.t_caption - self.t_speech_end if self.t_caption else None

    @property
    def air_lag(self) -> float | None:
        return self.t_on_air - self.t_speech_end if self.t_on_air else None

    # ...and from the moment the speaker *started* it.
    @property
    def air_lag_from_start(self) -> float | None:
        return self.t_on_air - self.t_speech_start if self.t_on_air else None

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["caption_lag"] = _r(self.caption_lag)
        d["air_lag"] = _r(self.air_lag)
        d["air_lag_from_start"] = _r(self.air_lag_from_start)
        d["cut_to_caption"] = _r(self.t_caption - self.t_cut if self.t_caption else None)
        return d


def _r(x: float | None, n: int = 3) -> float | None:
    return None if x is None else round(x, n)


class MetricsLog:
    def __init__(self, directory: Path, tag: str = "session"):
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = directory / f"{tag}-{stamp}.jsonl"
        self.records: list[dict[str, Any]] = []
        self._fh = open(self.path, "a", encoding="utf-8")

    def write(self, rec: ChunkRecord) -> None:
        d = rec.public()
        self.records.append(d)
        self._fh.write(json.dumps(d, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def percentiles(values: list[float]) -> dict[str, float] | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    a = np.array(vals)
    return {"n": len(vals), "p50": round(float(np.percentile(a, 50)), 2), "p90": round(float(np.percentile(a, 90)), 2),
            "p95": round(float(np.percentile(a, 95)), 2), "max": round(float(a.max()), 2),
            "mean": round(float(a.mean()), 2)}


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    aired = [r for r in records if r.get("status") == "aired"]
    captioned = [r for r in records if r.get("t_caption")]
    engines: dict[str, int] = {}
    for r in captioned:
        engines[r["engine"]] = engines.get(r["engine"], 0) + 1
    return {
        "chunks": len(records),
        "captioned": len(captioned),
        "aired": len(aired),
        "dropped_audio": sum(1 for r in records if r.get("status") == "dropped"),
        "skipped": sum(1 for r in records if r.get("status") == "skipped"),
        "failed": sum(1 for r in records if r.get("status") == "failed"),
        "engines": engines,
        "chunk_s": percentiles([r["audio_dur_s"] for r in records]),
        "stt_ms": percentiles([r["stt_ms"] for r in records if r.get("stt_ms")]),
        "mt_ms": percentiles([r["mt_ms"] for r in captioned]),
        "mt_ms_claude": percentiles([r["mt_ms"] for r in captioned if r["engine"] == "claude"]),
        "mt_ms_ollama": percentiles([r["mt_ms"] for r in captioned if r["engine"] == "ollama"]),
        "tts_ms": percentiles([r["tts_ms"] for r in aired]),
        "caption_lag": percentiles([r["caption_lag"] for r in captioned]),
        "air_lag": percentiles([r["air_lag"] for r in aired]),
        "air_lag_from_start": percentiles([r["air_lag_from_start"] for r in aired]),
        "backlog": percentiles([r["backlog_at_enqueue"] for r in aired]),
        "sped_up": sum(1 for r in aired if r.get("speed", 1) > 1.0),
        "condensed": sum(1 for r in records if r.get("condensed")),
    }
