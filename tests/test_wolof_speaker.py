"""Wolof as a speaker language (docs/wolof-speaker.md): its own recogniser, chunks routed to it because Whisper's
detector can't hear Wolof, never added automatically, translated and metered like any other speaker language, and
its model downloaded from our host when Wolof is first picked."""
import asyncio
import hashlib
import io
import json
import tarfile
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np
import pytest

from app import packages as pk
from app import wolof_asr as wa
from app.config import Settings, SttCfg
from app.health import HealthCollector
from app.languages import SPEAKER_LANGUAGES
from app.pipeline import Pipeline
from app.stt import AutoLanguage, SttResult, Transcriber
from app.translate.prompts import build_system_prompt

JW = {"jw": 0.19, "fr": 0.15, "en": 0.1}         # what Whisper's detector made of a FLEURS Wolof utterance
FR_ISH = {"fr": 0.6, "jw": 0.1}                  # Wolof with French words: French, but not sure
FR = {"fr": 0.93, "en": 0.02}
EN = {"en": 0.95, "fr": 0.01}
RU = {"ru": 0.93, "uk": 0.04}


# ------------------------------------------------------------------ routing

def test_wolof_is_a_speaker_language():
    assert SPEAKER_LANGUAGES["wo"] == "Wolof"


def test_a_wolof_only_meeting_never_asks_the_detector():
    assert wa.route({}, ["wo"]) == ("wo", "")
    assert wa.route(EN, ["wo"]) == ("wo", "")       # even if it had an opinion


def test_with_wolof_and_english_the_detector_only_picks_out_sure_english():
    assert wa.route(EN, ["wo", "en"]) == ("en", "en")
    assert wa.route(JW, ["wo", "en"]) == ("wo", "jw")              # not "corrected" into Javanese...
    assert wa.route(FR_ISH, ["wo", "en"]) == ("wo", "fr")          # ... or French
    assert wa.route({"en": 0.45, "fr": 0.3}, ["wo", "en"]) == ("wo", "en")   # unsure English is Wolof
    assert wa.route({"en": 0.45, "fr": 0.3}, ["wo", "en"], prev="en") == ("en", "en")   # still speaking English


def test_french_needs_more_certainty_beside_wolof():
    assert wa.route(FR_ISH, ["wo", "fr"]) == ("wo", "fr")          # Wolof preaching full of French words
    assert wa.route(FR, ["wo", "fr"]) == ("fr", "fr")              # a French sentence


def test_a_language_outside_the_set_is_itself_only_when_very_sure_and_never_a_wolof_lookalike():
    assert wa.route(RU, ["wo", "en"]) == ("ru", "ru")
    assert wa.route({"jw": 0.9}, ["wo", "en"]) == ("wo", "jw")
    assert wa.route(FR, ["wo", "en"]) == ("wo", "fr")              # French isn't a speaker: Wolof's code-switching
    assert wa.route({"ru": 0.7}, ["wo", "en"]) == ("wo", "ru")


def test_wolof_is_never_added_automatically():
    a = AutoLanguage(set(SPEAKER_LANGUAGES) - wa.UNDETECTABLE)
    probs = {"wo": 0.99}   # Whisper has no Wolof; even if a detector said so, it is never "heard for sure"
    assert [a.decide(probs, ["en"], "wo").add for _ in range(4)] == ["", "", "", ""]


def test_main_refuses_to_auto_add_wolof(monkeypatch):
    from app import main

    settings = Settings(meeting={"speakers": ["en"], "targets": ["en", "fr"], "auto_en": False})
    monkeypatch.setattr(main.S, "settings", settings, raising=False)
    monkeypatch.setattr(main, "set_speakers", lambda codes: pytest.fail("Wolof must not be added"))
    main.auto_add_speaker("wo")
    assert settings.meeting.speakers == ["en"]


# ------------------------------------------------------------------ the pipeline: routed, translated, metered

class FakeStt:
    engine, stepped_down = "mlx", ""
    cfg = SttCfg()

    def __init__(self, said):
        self.said = iter(said)
        self.asked = 0
        self.told: list[str] = []

    def language_probs(self, audio):
        self.asked += 1
        return next(self.said)

    def transcribe(self, audio, prompt="", language=None, raw=None):
        self.told.append(language)
        if raw is not None:
            text = raw(audio)[0]
            return SttResult(text=text, ms=1.0, language=language, dropped_reason="" if text else "empty")
        return SttResult(text=f"words in {language}", ms=1.0, language=language)


class FakeWolof:
    def __init__(self, missing=False):
        self.missing, self.heard = missing, 0

    def transcribe(self, audio):
        if self.missing:
            raise wa.ModelMissing("not downloaded")
        self.heard += 1
        return "Yàlla dafay jur ŋ", 0.0, -0.3, 1.0


async def _run(said, speakers, targets=("en", "fr"), wolof=None, n=None):
    settings = Settings(meeting={"speakers": list(speakers), "targets": list(targets), "auto_en": False})
    pipe = Pipeline.__new__(Pipeline)
    pipe.settings, pipe.records, pipe.hints, pipe._seq = settings, {}, [], 0
    pipe.source, pipe.prev_en, pipe.prev_lang, pipe.metrics = "", "", "", None
    pipe.meeting_id = "m1"
    pipe.loop = asyncio.get_running_loop()
    pipe.chunk_q, pipe.stt_ready = asyncio.Queue(), asyncio.Event()
    pipe.stt_ready.set()
    told: list = []
    pipe.operator = SimpleNamespace(publish=told.append)
    pipe.refs = SimpleNamespace(feed=lambda t: [])
    pipe.channels = {c: SimpleNamespace(on=True, mt_q=asyncio.Queue(), scripture=SimpleNamespace(enabled=False))
                     for c in targets}
    pipe.stt = FakeStt(said)
    pipe.stt_exec = ThreadPoolExecutor(1)
    pipe.autolang = AutoLanguage(set(SPEAKER_LANGUAGES) - wa.UNDETECTABLE)
    pipe.wolof = wolof or FakeWolof()
    pipe._wolof_missing_told = False
    pipe.health = HealthCollector()
    added: list[str] = []
    pipe.on_new_speaker = added.append
    taps: list = []
    pipe.speech_taps = [lambda seq, s, langs: taps.append(langs)]
    chunk = SimpleNamespace(start_s=0, duration=2, voiced_s=2.0, reason="x", t_speech_start=0, t_speech_end=0,
                            t_cut=0, audio=np.zeros(32000, dtype=np.float32), meeting="")
    task = asyncio.create_task(pipe._stt_stage())
    n = len(said) if n is None else n
    for _ in range(n):
        pipe.chunk_q.put_nowait(chunk)
    for _ in range(100):
        if len(pipe.records) == 0 and pipe._seq == n or len(taps) == n:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)
    task.cancel()
    return pipe, taps, added, told


async def test_wolof_only_meeting_goes_to_the_wolof_model_and_is_translated_and_metered():
    pipe, taps, added, _ = await _run([], ["wo"], n=3)
    assert pipe.stt.asked == 0                         # the detector can't hear Wolof: never asked
    assert pipe.stt.told == ["wo"] * 3 and pipe.wolof.heard == 3
    assert taps == [["en", "fr"]] * 3                  # billed: every chunk translated into both
    recs = [pipe.channels["en"].mt_q.get_nowait() for _ in range(3)]
    assert {r.src for r in recs} == {"wo"} and "ŋ" in recs[0].en
    assert added == []


async def test_wolof_and_english_route_by_the_detector_and_nothing_is_ever_added():
    # confident French twice would add French in a meeting without Wolof (AutoLanguage); here it is Wolof speech
    pipe, taps, added, _ = await _run([EN, JW, FR, FR, EN], ["wo", "en"])
    assert pipe.stt.told == ["en", "wo", "wo", "wo", "en"]
    assert added == [] and pipe.speakers == ["wo", "en"]
    assert taps == [["fr"], ["en", "fr"], ["en", "fr"], ["en", "fr"], ["fr"]]   # English passes through free


async def test_a_wolof_translation_target_passes_wolof_speech_through_unbilled():
    pipe, taps, _, _ = await _run([], ["wo"], targets=("wo", "en"), n=2)
    assert taps == [["en"], ["en"]]


async def test_wolof_speech_before_the_model_is_downloaded_is_skipped_and_the_operator_told_once():
    pipe, taps, _, told = await _run([], ["wo"], wolof=FakeWolof(missing=True), n=3)
    assert taps == []
    assert [m for m in told if m.get("type") == "log" and "Wolof" in m["text"]] == [
        {"type": "log", "level": "warning",
         "text": "Wolof speech recognition is still downloading: Wolof speech is skipped until it's ready."}]


def test_wolof_output_gets_whispers_checks():
    t = Transcriber(SttCfg(engine="faster-whisper"))
    rep = " ".join(["yàlla"] * 20)
    assert t.transcribe(np.zeros(16000, np.float32), "", "wo", raw=lambda a: (rep, 0.0, -0.2, 3.0)).dropped_reason
    assert t.transcribe(np.zeros(16000, np.float32), "", "wo", raw=lambda a: ("", 1.0, -2.0, 0.0)).dropped_reason == "empty"
    ok = t.transcribe(np.zeros(16000, np.float32), "", "wo", raw=lambda a: (" ndey  ji ", 0.0, -0.2, 1.0))
    assert (ok.text, ok.language, ok.dropped_reason) == ("ndey ji", "wo", "")
    assert t.rtf == 0.0                                 # another model's time never steps Whisper down


def test_the_interpreter_is_told_the_wolof_is_a_rough_transcript():
    assert "WOLOF INPUT" in build_system_prompt({}, "", "en", ["wo", "fr"])
    assert "WOLOF INPUT" in build_system_prompt({}, "", "fr", ["wo"])
    assert "WOLOF INPUT" not in build_system_prompt({}, "", "en", ["fr"])


# ------------------------------------------------------------------ decoding

def test_kiriku_added_letters_decode_as_letters():
    vocab = {100: "ñ", 101: "do", 102: " ", 103: "ga", 104: "n"}
    extra = {51865: "ŋ", 51866: "ɗ", 51867: "ɓ", 51868: "ƴ"}
    eot = 50257

    def decode(ids):
        return "".join(vocab[i] for i in ids)
    ids = [101, 51865, 102, 51867, 103, 50364, 51864, 50257, 104]   # timestamps and end-of-text dropped
    assert wa.decode_tokens(ids, decode, extra, eot) == "doŋ ɓgan"
    assert wa.decode_tokens([51866, 51868], decode, extra, eot) == "ɗƴ"


def test_ctc_greedy_decoding():
    pieces = ["<s>", "<pad>", "</s>", "<unk>", "▁ya", "lla", "▁xale"]
    assert wa.ctc_decode([4, 4, 1, 5, 5, 1, 1, 6, 0], pieces, {0, 1, 2, 3}) == "yalla xale"
    assert wa.ctc_decode([4, 1, 4], pieces, {0, 1, 2, 3}) == "ya ya"   # a blank between: a real repeat


def test_the_converter_writes_the_added_letters_and_large_v2s_language_count(tmp_path):
    """mlx-whisper derives the language count from the vocabulary: 103 for Kiriku's 51869 tokens, which would shift
    every special token by one. The converter records 99 and the letters Kiriku added; the loader uses both."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "convert_kiriku_mlx", Path(__file__).resolve().parent.parent / "scripts" / "models" / "convert_kiriku_mlx.py")
    conv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conv)
    (tmp_path / "added_tokens.json").write_text(json.dumps(
        {"<|30.00|>": 51864, "ŋ": 51865, "ɗ": 51866, "ɓ": 51867, "ƴ": 51868, "<|en|>": 50259}))
    assert conv.added_letters(tmp_path, 51865) == {"51865": "ŋ", "51866": "ɗ", "51867": "ɓ",
                                                   "51868": "ƴ"}
    assert conv.NUM_LANGUAGES == 99 and conv.LANGUAGE == "fr"
    assert conv.remap("model.decoder.layers.3.encoder_attn_layer_norm.weight") == "decoder.blocks.3.cross_attn_ln.weight"
    assert conv.remap("model.encoder.layers.0.self_attn.k_proj.weight") == "encoder.blocks.0.attn.key.weight"
    assert conv.remap("model.encoder.layer_norm.bias") == "encoder.ln_post.bias"
    assert conv.remap("model.decoder.embed_tokens.weight") == "decoder.token_embedding.weight"


def test_route_for_the_runtime():
    assert wa.runtime("mlx") == "mlx"
    assert wa.runtime("faster-whisper") == "onnx"


# ------------------------------------------------------------------ the download

def _model_archive(tmp_path, ident="wolof-asr-test-onnx-abc123"):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("config.json", b'{"kind": "omnilingual-ctc"}'), ("tokens.json", b"{}")):
            info = tarfile.TarInfo(f"{ident}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    raw = buf.getvalue()
    entry = {"id": ident, "url": f"https://hearchurch.com/download/{ident}.tar.gz",
             "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw), "name": "test"}
    return raw, entry


@pytest.fixture
def models(tmp_path, monkeypatch):
    monkeypatch.setattr(wa, "MODELS_DIR", tmp_path / "models")
    manifest = tmp_path / "wolof_asr.json"
    monkeypatch.setattr(wa, "MANIFEST", manifest)
    return tmp_path, manifest


def test_unpublished_model_is_offered_but_says_why_it_cant_download(models):
    _, manifest = models
    manifest.write_text(json.dumps({"mlx": None, "onnx": None}))
    p = pk.package_for("speech-wo", "", "onnx")
    assert p.name == "Wolof speech recognition" and p.size_mb == pk.WOLOF_ASR_MB["onnx"] and p.unavailable

    async def go():
        ps = pk.Packages()
        ps.asr_runtime = "onnx"
        await ps.status(["speech-wo"])
        ps.download_missing(["speech-wo"])
        return ps.items["speech-wo"].public()
    st = asyncio.run(go())
    assert st["state"] == "failed" and "can't be downloaded yet" in st["error"]


def test_the_model_downloads_from_our_host_checks_its_hash_and_unpacks(models, monkeypatch):
    tmp_path, manifest = models
    raw, entry = _model_archive(tmp_path)
    manifest.write_text(json.dumps({"onnx": entry}))
    old = wa.MODELS_DIR / "wolof-asr-test-onnx-old999"
    old.mkdir(parents=True)
    fetched = []

    async def fake_fetch(url, dest):
        fetched.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
    monkeypatch.setattr(pk, "_fetch", fake_fetch)

    async def go():
        ps = pk.Packages()
        ps.asr_runtime = "onnx"
        await ps.status(["speech-wo"])
        ps.download_missing(["speech-wo"])
        await ps.items["speech-wo"].task
        return await ps.status(["speech-wo"])
    st = [p for p in asyncio.run(go()) if p["id"] == "speech-wo"][0]
    assert fetched == [entry["url"]] and entry["url"].startswith("https://hearchurch.com/download/")
    assert st["state"] == "ready"
    assert (wa.MODELS_DIR / entry["id"] / "config.json").exists()
    assert not old.exists()                                  # an older version of the same model goes
    assert not (wa.MODELS_DIR / f'{entry["id"]}.tar.gz').exists()
    rec = wa.WolofRecogniser("faster-whisper")
    assert rec.available() and not rec.ready

    ps = pk.Packages()
    ps.asr_runtime = "onnx"
    asyncio.run(ps.remove("speech-wo"))
    assert not (wa.MODELS_DIR / entry["id"]).exists() and not rec.available()


def test_a_damaged_model_download_is_refused(models):
    tmp_path, _ = models
    raw, entry = _model_archive(tmp_path)
    archive = wa.MODELS_DIR / f'{entry["id"]}.tar.gz'
    archive.parent.mkdir(parents=True)
    archive.write_bytes(raw + b"x")
    with pytest.raises(RuntimeError, match="damaged"):
        wa.install(archive, entry)
    assert not archive.exists() and not wa.installed(entry)


def test_a_wolof_speaker_needs_wolof_speech_recognition_not_the_voice(monkeypatch):
    from app import main

    settings = Settings(meeting={"speakers": ["wo", "en"], "targets": ["en", "fr"], "auto_en": False})
    monkeypatch.setattr(main.S, "settings", settings, raising=False)
    assert main._package_languages("") == ["en", "fr", "speech-wo"]
    assert main._package_languages("fr,speech-wo,xx") == ["fr", "speech-wo"]
    settings.meeting.targets = ["wo", "en"]
    assert main._package_languages(None) == ["wo", "en", "speech-wo"]   # a Wolof translation: the voice too


def test_the_recogniser_is_missing_until_downloaded(models):
    _, manifest = models
    manifest.write_text(json.dumps({"mlx": None}))
    rec = wa.WolofRecogniser("mlx")
    assert not rec.available() and not rec.load()
    with pytest.raises(wa.ModelMissing):
        rec.transcribe(np.zeros(16000, np.float32))
