"""Per-chunk timing records, written as JSONL (one file per run) and summarised for reports.

A chunk is transcribed once (English) and then translated/voiced separately for every output
language; each language's progress lives in `ChunkRecord.out[code]`.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

TERMINAL = {"aired", "dropped", "skipped", "failed", "merged"}


@dataclass
class LangResult:
    text: str = ""
    engine: str = ""
    note: str = ""
    mt_ms: float = 0.0
    tts_ms: float = 0.0
    t_caption: float = 0.0
    t_audio_ready: float = 0.0
    t_on_air: float = 0.0
    clip_s: float = 0.0
    speed: float = 1.0
    condensed: bool = False
    backlog_at_enqueue: float = 0.0
    caption_no: int = 0
    merged: list[int] = field(default_factory=list)
    t_ref_end: float = 0.0        # end of the speech this result covers (later than the chunk's if merged)
    status: str = "pending"       # pending|captioned|aired|dropped|skipped|failed|merged

    def caption_lag(self, rec: "ChunkRecord") -> float | None:
        """Seconds from the end of the speech this line covers to its caption appearing: how far behind it is."""
        return self.t_caption - (self.t_ref_end or rec.t_speech_end) if self.t_caption else None

    def public(self, rec: "ChunkRecord") -> dict[str, Any]:
        d = asdict(self)
        end = self.t_ref_end or rec.t_speech_end
        d["caption_lag"] = _r(self.caption_lag(rec))
        d["air_lag"] = _r(self.t_on_air - end if self.t_on_air else None)
        d["air_lag_from_start"] = _r(self.t_on_air - rec.t_speech_start if self.t_on_air else None)
        return d


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
    en: str = ""                  # what was said, as recognised (in `src`, not only English)
    src: str = ""                 # its language: one of the meeting's speaker languages
    note: str = ""
    stt_ms: float = 0.0
    t_stt_done: float = 0.0
    status: str = "pending"       # pending|done|skipped|failed (speech-recognition level)
    out: dict[str, LangResult] = field(default_factory=dict)
    wall_time: float = field(default_factory=time.time)
    meeting: str = ""             # the meeting it was heard in (Pipeline.meeting_id)

    @property
    def complete(self) -> bool:
        return self.status in ("skipped", "failed") or (bool(self.out) and all(r.status in TERMINAL for r in self.out.values()))

    def public(self) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items() if k != "out"}
        d["langs"] = {code: r.public(self) for code, r in self.out.items()}
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


def languages_in(records: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for r in records:
        for code in r.get("langs", {}):
            if code not in seen:
                seen.append(code)
    return seen


def summarize(records: list[dict[str, Any]], lang: str) -> dict[str, Any]:
    """Latency/engine stats for one output language."""
    rs = [r["langs"][lang] for r in records if lang in r.get("langs", {})]
    aired = [x for x in rs if x["status"] == "aired"]
    captioned = [x for x in rs if x.get("t_caption")]
    engines: dict[str, int] = {}
    for x in captioned:
        engines[x["engine"]] = engines.get(x["engine"], 0) + 1
    return {
        # Lines heard while this language was on (it can be switched on/off mid-run).
        "chunks": len(rs) + sum(1 for r in records if not r.get("langs")),
        "captioned": len(captioned),
        "aired": len(aired),
        "dropped_audio": sum(1 for x in rs if x["status"] == "dropped"),
        "skipped": sum(1 for r in records if r["status"] == "skipped") + sum(1 for x in rs if x["status"] == "skipped"),
        "failed": sum(1 for x in rs if x["status"] == "failed"),
        "merged": sum(1 for x in rs if x["status"] == "merged"),
        "engines": engines,
        "chunk_s": percentiles([r["audio_dur_s"] for r in records]),
        "stt_ms": percentiles([r["stt_ms"] for r in records if r.get("stt_ms")]),
        # from the chunk being cut to its speech recognition starting: grows without bound when the CPU is too slow
        "stt_wait": percentiles([max(0.0, r["t_stt_done"] - r["t_cut"] - r["stt_ms"] / 1000)
                                 for r in records if r.get("t_stt_done") and r.get("t_cut")]),
        "mt_ms": percentiles([x["mt_ms"] for x in captioned]),
        "mt_ms_claude": percentiles([x["mt_ms"] for x in captioned if x["engine"] == "claude"]),
        "mt_ms_ollama": percentiles([x["mt_ms"] for x in captioned if x["engine"] == "ollama"]),
        "tts_ms": percentiles([x["tts_ms"] for x in aired]),
        "caption_lag": percentiles([x["caption_lag"] for x in captioned]),
        "air_lag": percentiles([x["air_lag"] for x in aired]),
        "air_lag_from_start": percentiles([x["air_lag_from_start"] for x in aired]),
        "backlog": percentiles([x["backlog_at_enqueue"] for x in aired]),
        "sped_up": sum(1 for x in aired if x.get("speed", 1) > 1.0),
        "condensed": sum(1 for x in rs if x.get("condensed")),
    }


def _p(values: list[float], q: float) -> float | None:
    vals = sorted(v for v in values if v is not None)
    return round(vals[min(len(vals) - 1, int(q * len(vals)))], 1) if vals else None


def live_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    """A running meeting's numbers for the operator (what the old web operator page showed): lines heard and
    skipped, seconds of speech, and per translation language how many lines were captioned / voiced / had their
    audio dropped / failed, which translator made them, and how far behind the speaker captions and voices were
    (median and 95th percentile). `records` are finished chunks (ChunkRecord.public()) of one meeting."""
    langs: dict[str, dict[str, Any]] = {}
    for code in languages_in(records):
        rs = [r["langs"][code] for r in records if code in r.get("langs", {})]
        engines: dict[str, int] = {}
        for x in rs:
            if x.get("t_caption") and x.get("engine"):
                engines[x["engine"]] = engines.get(x["engine"], 0) + 1
        cap = [x.get("caption_lag") for x in rs if x.get("t_caption")]
        air = [x.get("air_lag") for x in rs if x["status"] == "aired"]
        langs[code] = {
            "lines": len(rs),
            "captioned": len(cap),
            "aired": len(air),
            "dropped": sum(1 for x in rs if x["status"] == "dropped"),
            "skipped": sum(1 for x in rs if x["status"] == "skipped"),
            "failed": sum(1 for x in rs if x["status"] == "failed"),
            "merged": sum(1 for x in rs if x["status"] == "merged"),
            "sped_up": sum(1 for x in rs if (x.get("speed") or 1) > 1.0),
            "condensed": sum(1 for x in rs if x.get("condensed")),
            "engines": engines,
            "caption_p50": _p(cap, .5), "caption_p95": _p(cap, .95),
            "audio_p50": _p(air, .5), "audio_p95": _p(air, .95),
        }
    heard = [r for r in records if r["status"] not in ("skipped", "failed")]
    return {
        "lines": len(heard),
        "skipped": sum(1 for r in records if r["status"] == "skipped"),
        "failed": sum(1 for r in records if r["status"] == "failed"),
        "speech_s": round(sum(r.get("voiced_s") or 0 for r in heard), 1),
        "stt_ms_p50": _p([r["stt_ms"] for r in heard if r.get("stt_ms")], .5),
        "langs": langs,
    }
