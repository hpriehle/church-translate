"""V3 languages, end to end on this computer (docs/v3-languages.md): real Piper clips → the real pipeline (VAD,
speech recognition with language detection among the meeting's speaker languages, voices, streams), with the
translation mocked (no Claude key needed).

    HF_HOME=~/.cache/huggingface uv run --extra linux python scripts/v3_mixed_check.py [/tmp/hcv3home]

Runs three meetings and prints, per chunk, the detected language, what each target did with it (translated /
passed through as the original) and which languages the meter would bill:
  1. speakers ru + lv → targets en + lv   (Riga)
  2. speaker en        → targets fr + lv
  3. speakers en + fr  → targets fr + lv   (French passes through on the French channel)
Exits 1 if a check fails.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import wave
from pathlib import Path

HOME = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/hcv3home").resolve()
os.environ["HEARCHURCH_HOME"] = str(HOME)
os.environ.setdefault("HF_HOME", str(Path.home() / ".cache" / "huggingface"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

VOICES = {"ru": "ru_RU-denis-medium", "lv": "lv_LV-aivars-medium", "en": "en_US-joe-medium", "fr": "fr_FR-tom-medium"}
SAY = {
    "ru": ["Братья и сёстры, сегодня я хочу поговорить о вере в Иисуса Христа.",
           "Общество милосердия встретится после причастного собрания в нашем здании."],
    "lv": ["Brāļi un māsas, šodien es vēlos runāt par ticību Jēzum Kristum.",
           "Palīdzības biedrība tiksies pēc Svētā Vakarēdiena sanāksmes mūsu ēkā."],
    "en": ["Brothers and sisters, today I want to talk about faith in Jesus Christ.",
           "The Relief Society will meet after sacrament meeting in the chapel."],
    "fr": ["Frères et sœurs, aujourd'hui je veux vous parler de la foi en Jésus-Christ.",
           "La Société de Secours se réunira après la réunion de Sainte-Cène."],
}


def fetch_voices() -> None:
    import httpx

    from app.config import PIPER_DIR
    from app.packages import PIPER_URL

    PIPER_DIR.mkdir(parents=True, exist_ok=True)
    for v in VOICES.values():
        lang, name, quality = v.split("-")
        for ext in ("onnx", "onnx.json"):
            out = PIPER_DIR / f"{v}.{ext}"
            if out.exists():
                continue
            url = PIPER_URL.format(family=lang.split("_")[0], region=lang, name=name, quality=quality, voice=v, ext=ext)
            print("downloading", url)
            with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
                r.raise_for_status()
                out.write_bytes(b"".join(r.iter_bytes()))


def clip(order: list[tuple[str, int]], path: Path) -> None:
    """Sentences (language, index) one after another, 1.5 s of silence between them, 16 kHz mono WAV."""
    from piper import PiperVoice

    from app.config import PIPER_DIR

    pcm: list[np.ndarray] = []
    voices: dict[str, PiperVoice] = {}
    for lang, i in order:
        voice = voices.setdefault(lang, PiperVoice.load(str(PIPER_DIR / f"{VOICES[lang]}.onnx")))
        audio = np.concatenate([c.audio_int16_array for c in voice.synthesize(SAY[lang][i])])
        sr = voice.config.sample_rate
        audio = np.interp(np.arange(0, len(audio), sr / 16000), np.arange(len(audio)), audio).astype(np.int16)
        pcm += [np.zeros(16000, np.int16), audio, np.zeros(24000, np.int16)]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes(np.concatenate(pcm).tobytes())


async def run(main, speakers: list[str], targets: list[str], wav: Path) -> list[dict]:
    from app.config import Settings

    st = main.S.settings.model_copy(deep=True)
    st.meeting.speakers, st.meeting.targets, st.meeting.auto_en = speakers, targets, False
    main.S.settings = Settings.model_validate(st.model_dump())
    main.S.pipeline.apply_settings(main.S.settings, main.S.glossaries)
    main.set_targets(targets, persist=False)
    taps: list[tuple[int, float, list[str]]] = []
    main.S.pipeline.speech_taps[:] = [lambda seq, s, langs: taps.append((seq, s, langs))]
    await main.S.pipeline.stt_ready.wait()
    for ch in main.S.pipeline.on_channels.values():   # voices loaded before the clip starts
        while ch.tts.engine is None and not ch.tts.error:
            await asyncio.sleep(0.2)
    main.S.pipeline.start("file", file=str(wav))
    await asyncio.sleep(1)
    while main.S.pipeline.running:
        await asyncio.sleep(0.5)
    recs = main.S.pipeline.metrics.records
    billed = {seq: langs for seq, _, langs in taps}
    for r in recs:
        r["billed"] = billed.get(r["seq"])
    return recs


def show(title: str, recs: list[dict]) -> None:
    print(f"\n=== {title}")
    for r in recs:
        if r.get("status") == "skipped":
            continue
        outs = ", ".join(f"{c}: {o.get('engine') or '-'}/{o.get('status')}" for c, o in r["langs"].items())
        print(f"#{r['seq']} [{r.get('src')}] {r['en'][:70]!r}\n      → {outs}   billed: {r['billed']}")


def check(recs: list[dict], expect_srcs: list[str], targets: list[str], problems: list[str], title: str) -> None:
    heard = [r for r in recs if r.get("status") != "skipped" and r.get("en")]
    srcs = [r["src"] for r in heard]
    if [s for i, s in enumerate(srcs) if i == 0 or s != srcs[i - 1]] != expect_srcs:
        problems.append(f"{title}: languages {srcs}, expected the sequence {expect_srcs}")
    for r in heard:
        for c in targets:
            o = r["langs"].get(c) or {}
            want = "original" if c == r["src"] else "claude"
            if o.get("engine") != want:
                problems.append(f"{title} #{r['seq']}: {c} engine {o.get('engine')}, expected {want}")
            if o.get("status") not in ("aired", "dropped") or not o.get("tts_ms"):
                problems.append(f"{title} #{r['seq']}: {c} not voiced ({o.get('status')} {o.get('note')})")
        if sorted(r["billed"] or []) != sorted(c for c in targets if c != r["src"]):
            problems.append(f"{title} #{r['seq']}: billed {r['billed']}")


async def main_async() -> int:
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "data").mkdir(exist_ok=True)
    (HOME / "data" / f"overrides.{sys.platform}.json").write_text(json.dumps({
        "stt": {"engine": "faster-whisper", "faster_whisper_model": "small"},
        "translate": {"mode": "claude"},
        # This box's CPU recognises speech slower than real time: let the audio lag instead of dropping it.
        "backlog": {"drop_above_s": 900},
        "meeting": {"speakers": ["ru", "lv"], "targets": ["en", "lv"], "auto_en": False},
    }))
    fetch_voices()
    clips = HOME / "clips"
    clips.mkdir(exist_ok=True)
    clip([("ru", 0), ("lv", 0), ("ru", 1), ("lv", 1)], clips / "ru_lv.wav")
    clip([("en", 0), ("en", 1)], clips / "en.wav")
    clip([("en", 0), ("fr", 0), ("fr", 1), ("en", 1)], clips / "en_fr.wav")

    from app import main
    from app.translate import router

    async def fake_translate(self, english, condense=False, scripture=""):
        await asyncio.sleep(0.05)
        return router.TranslationResult(text=f"<{self.lang.code} from {'/'.join(self.src)}> {english}",
                                        engine="claude", ms=50.0)

    async def no_start(self):
        return None

    # Nothing may reach Claude (or start the `claude` CLI): no sessions, standbys or restarts.
    router.Translator.translate = fake_translate
    router.Translator.start = no_start
    router.Translator.prepare_fallback = lambda self: None
    router.Translator._maintain = lambda self: None
    router.Translator.restart_claude = lambda self: None
    from app.translate import claude_session

    async def refuse(*a, **k):
        raise RuntimeError("v3_mixed_check: no Claude sessions")

    claude_session.ClaudeSession.start = refuse

    problems: list[str] = []
    async with main.lifespan(main.app):
        for title, speakers, targets, wav, srcs in [
            ("ru + lv → en + lv", ["ru", "lv"], ["en", "lv"], "ru_lv.wav", ["ru", "lv", "ru", "lv"]),
            ("en → fr + lv", ["en"], ["fr", "lv"], "en.wav", ["en"]),
            ("en + fr → fr + lv", ["en", "fr"], ["fr", "lv"], "en_fr.wav", ["en", "fr", "en"]),
        ]:
            recs = await run(main, speakers, targets, clips / wav)
            show(title, recs)
            check(recs, srcs, targets, problems, title)
            prompt = main.S.pipeline.channels[targets[0]].translator.system_prompt
            print("   interpreter for", targets[0], "is told:", prompt.split("\n")[0][:160])
    print("\nPROBLEMS:\n- " + "\n- ".join(problems) if problems else "\nall checks passed")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))
