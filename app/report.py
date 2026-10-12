"""Markdown report for a run: per-language latency percentiles, engine split, usage, transcript.

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

from .metrics import languages_in, summarize

NAMES = {"fr": "French", "wo": "Wolof"}


def _row(label: str, p: dict | None, unit: str = "s") -> str:
    if not p:
        return f"| {label} | – | – | – | – | – |"
    return f"| {label} | {p['p50']}{unit} | {p['p90']}{unit} | {p['p95']}{unit} | {p['max']}{unit} | {p['n']} |"


def _lang_section(records: list[dict[str, Any]], code: str, info: dict[str, Any], audio_s: float) -> list[str]:
    s = summarize(records, code)
    li = info.get("languages", {}).get(code, {})
    lines = [f"## {NAMES.get(code, code)}", ""]
    if li.get("tts"):
        lines += [f"- Voice: {li['tts']} · fallback: {li.get('fallback', '?')}", ""]
    lines += ["| Measure | p50 | p90 | p95 | max | n |", "|---|---|---|---|---|---|",
              _row("Caption on phones, after speaker pauses", s["caption_lag"]),
              _row("Audio starts, after speaker pauses", s["air_lag"]),
              _row("Audio starts, after speaker *starts* the phrase", s["air_lag_from_start"]),
              _row("Translation (all)", s["mt_ms"], " ms"),
              _row("Translation — Claude", s["mt_ms_claude"], " ms"),
              _row("Translation — Ollama", s["mt_ms_ollama"], " ms"),
              _row("Voice synthesis", s["tts_ms"], " ms"),
              _row("Audio waiting when queued (backlog)", s["backlog"]), ""]
    total = max(1, s["captioned"])
    eng = ", ".join(f"{k} {v} ({100 * v / total:.0f}%)" for k, v in sorted(s["engines"].items(), key=lambda x: -x[1]))
    lines += [f"- Captioned: {s['captioned']} · audio aired: {s['aired']} · audio dropped (catch-up): {s['dropped_audio']} · "
              f"failed: {s['failed']} · merged when behind: {s['merged']}",
              f"- Engines: {eng or '–'}",
              f"- Voice sped up on {s['sped_up']} clips · translator asked to condense {s['condensed']} times"]
    notes = Counter(r["langs"][code]["note"] for r in records
                    if code in r.get("langs", {}) and r["langs"][code].get("note") and r["langs"][code]["engine"] != "claude")
    if notes:
        lines.append("- Notes: " + ", ".join(f"{k} ×{v}" for k, v in notes.most_common(6)))
    u = li.get("usage")
    if u and u.get("turns"):
        per_hour = u["cost_usd"] / (audio_s / 3600) if audio_s > 60 else 0
        lines.append(f"- Claude: {u['turns']} turns, {u['input'] + u['cache_read'] + u['cache_write']:,} input tokens "
                     f"({u['cache_read']:,} cached), {u['output']:,} output (≈ ${u['cost_usd']:.3f} API-equivalent, "
                     f"${per_hour:.2f}/hour of speech; on a subscription this is plan usage, not a charge)")
    return lines + [""]


def build_report(records: list[dict[str, Any]], info: dict[str, Any] | None = None) -> str:
    info = info or {}
    langs = languages_in(records) or list(info.get("languages", {}))
    audio_s = max((r["audio_start_s"] + r["audio_dur_s"] for r in records), default=0.0)
    first = summarize(records, langs[0]) if langs else {}
    lines = [f"# Live translation run — {info.get('source', '')}", ""]
    lines.append(f"- Date: {time.strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"- Machine: {platform.platform()} · Python {sys.version.split()[0]}")
    for k in ("stt", "translator"):
        if info.get(k):
            lines.append(f"- {'STT' if k == 'stt' else 'Translator'}: {info[k]}")
    lines.append(f"- Languages: {', '.join(NAMES.get(c, c) for c in langs)}")
    lines.append(f"- Audio processed: {audio_s / 60:.1f} min in {len(records)} chunks · "
                 f"skipped as non-speech: {sum(1 for r in records if r['status'] == 'skipped')}")
    if first:
        lines += ["", "| Shared | p50 | p90 | p95 | max | n |", "|---|---|---|---|---|---|",
                  _row("Chunk length", first["chunk_s"]), _row("Speech-to-text", first["stt_ms"], " ms")]
    lines += ["", "Latency is measured on the server. *After speaker pauses* = from the end of the phrase to the "
              "caption / the audio starting in that language's stream. Phones add their own playback buffer "
              "(typically 1–3 s).", ""]
    for code in langs:
        lines += _lang_section(records, code, info, audio_s)
    head = "| # | at | English | " + " | ".join(NAMES.get(c, c) for c in langs) + " |"
    lines += ["## Transcript", "", head, "|" + "---|" * (3 + len(langs))]
    for r in records:
        if r.get("status") == "skipped":
            continue
        at = f"{int(r['audio_start_s'] // 60)}:{int(r['audio_start_s'] % 60):02d}"
        cells = []
        for c in langs:
            x = r.get("langs", {}).get(c, {})
            lag = f" ({x['caption_lag']:.1f}s)" if x.get("caption_lag") is not None else ""
            text = (x.get("text") or x.get("status", "")).replace("|", "/")
            cells.append(f"{text}{lag}")
        lines.append(f"| {r['seq']} | {at} | {(r.get('en') or '').replace('|', '/')} | " + " | ".join(cells) + " |")
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
