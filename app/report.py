"""Markdown report for a run: latency percentiles, engine split, usage, transcript sample.

Written next to the run's JSONL when a run finishes. Can also be rebuilt from a JSONL:
    uv run python -m app.report logs/session-YYYYmmdd-HHMMSS.jsonl
"""
from __future__ import annotations

import json
import platform
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .metrics import summarize


def _row(label: str, p: dict | None, unit: str = "s") -> str:
    if not p:
        return f"| {label} | – | – | – | – | – |"
    return f"| {label} | {p['p50']}{unit} | {p['p90']}{unit} | {p['p95']}{unit} | {p['max']}{unit} | {p['n']} |"


def build_report(records: list[dict[str, Any]], info: dict[str, Any] | None = None) -> str:
    info = info or {}
    s = summarize(records)
    audio_s = max((r["audio_start_s"] + r["audio_dur_s"] for r in records), default=0.0)
    lines = [f"# Live translation run — {info.get('source', '')}", ""]
    lines.append(f"- Date: {time.strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"- Machine: {platform.platform()} · Python {sys.version.split()[0]}")
    for k in ("stt", "translator", "tts"):
        if info.get(k):
            lines.append(f"- {k.upper() if k != 'translator' else 'Translator'}: {info[k]}")
    lines.append(f"- Audio processed: {audio_s / 60:.1f} min in {s['chunks']} chunks")
    lines += ["", "## Latency", "",
              "Measured on the server. *After speaker pauses* = from the end of the phrase (the moment the "
              "chunk could first be cut) to the French caption / the French audio starting in the stream. "
              "Phones add their own playback buffer on top of the audio figure (typically 1–3 s).", "",
              "| Measure | p50 | p90 | p95 | max | n |", "|---|---|---|---|---|---|",
              _row("Caption on phones, after speaker pauses", s["caption_lag"]),
              _row("French audio starts, after speaker pauses", s["air_lag"]),
              _row("French audio starts, after speaker *starts* the phrase", s["air_lag_from_start"]),
              _row("Chunk length", s["chunk_s"]),
              _row("Speech-to-text", s["stt_ms"], " ms"),
              _row("Translation (all)", s["mt_ms"], " ms"),
              _row("Translation — Claude", s["mt_ms_claude"], " ms"),
              _row("Translation — Ollama", s["mt_ms_ollama"], " ms"),
              _row("Voice synthesis", s["tts_ms"], " ms"),
              _row("Audio waiting when queued (backlog)", s["backlog"]),
              ""]
    total = max(1, s["captioned"])
    eng = ", ".join(f"{k} {v} ({100 * v / total:.0f}%)" for k, v in sorted(s["engines"].items(), key=lambda x: -x[1]))
    lines += ["## What happened", "",
              f"- Captioned: {s['captioned']} · audio aired: {s['aired']} · audio dropped (catch-up): {s['dropped_audio']}",
              f"- Engines: {eng or '–'}",
              f"- Skipped as non-speech: {s['skipped']} · failed: {s['failed']} · "
              f"merged when behind: {sum(1 for r in records if r.get('status') == 'merged')}",
              f"- Voice sped up on {s['sped_up']} clips · translator asked to condense {s['condensed']} times"]
    notes = Counter(r["note"] for r in records if r.get("note") and r.get("engine") == "ollama")
    if notes:
        lines.append("- Fallback reasons: " + ", ".join(f"{k} ×{v}" for k, v in notes.most_common()))
    usage = info.get("usage")
    if usage and usage.get("turns"):
        per_hour = usage["cost_usd"] / (audio_s / 3600) if audio_s > 60 else 0
        lines.append(f"- Claude usage: {usage['turns']} turns, {usage['input'] + usage['cache_read'] + usage['cache_write']:,} input tokens "
                     f"({usage['cache_read']:,} cached), {usage['output']:,} output tokens, "
                     f"${usage['cost_usd']:.3f} API-equivalent (≈ ${per_hour:.2f} per hour of speech)")
    lines += ["", "## Transcript", "", "| # | at | English | French | engine | caption | audio |", "|---|---|---|---|---|---|---|"]
    for r in records:
        if r.get("status") in ("skipped",):
            continue
        at = f"{int(r['audio_start_s'] // 60)}:{int(r['audio_start_s'] % 60):02d}"
        fr = (r.get("fr") or "").replace("|", "/")
        en = (r.get("en") or "").replace("|", "/")
        cap = f"{r['caption_lag']:.1f}s" if r.get("caption_lag") is not None else "–"
        air = "dropped" if r.get("status") == "dropped" else (f"{r['air_lag']:.1f}s" if r.get("air_lag") is not None else r.get("status", ""))
        lines.append(f"| {r['seq']} | {at} | {en} | {fr} | {r.get('engine', '')} | {cap} | {air} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    path = Path(sys.argv[1])
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    info_path = path.with_name(path.stem + "-info.json")
    info = json.loads(info_path.read_text()) if info_path.exists() else {}
    out = path.with_name(path.stem + "-report.md")
    out.write_text(build_report(records, info))
    print(out)


if __name__ == "__main__":
    main()
