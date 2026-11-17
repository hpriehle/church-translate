"""The Wolof voice on Windows: SpeechT5 on onnxruntime (app/wolof_voice.py, app/tts/speecht5_onnx.py)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.delenv("HEARCHURCH_WOLOF_VOICE", raising=False)


def test_the_paid_windows_app_never_offers_the_non_commercial_wolof_voice(windows, monkeypatch):
    from app.config import Settings, TtsCfg, VoiceCfg, tts_for
    from app.tts import engines

    assert all(o["engine"] != "mms" for o in engines.voice_catalog()["wo"])
    assert tts_for(Settings(), "wo").engine == "speecht5"
    cfg = tts_for(Settings(voices={"wo": VoiceCfg(engine="mms", voice="default", speed=1.25)}), "wo")
    assert (cfg.engine, cfg.speed) == ("speecht5", 1.25)
    built = []
    monkeypatch.setattr(engines, "MmsTts", lambda cfg, sr: built.append(cfg) or "mms")
    with pytest.raises(ValueError, match="non-commercial"):
        engines.make_tts(TtsCfg(engine="mms", voice="default", lang="wo"), 24000)
    assert built == []


def test_windows_speaks_wolof_with_the_onnx_voice_not_torch(windows, monkeypatch):
    from app.config import TtsCfg
    from app.tts import engines

    monkeypatch.setattr(engines, "SpeechT5OnnxTts", lambda cfg, sr: "onnx")
    monkeypatch.setattr(engines, "SpeechT5Tts", lambda cfg, sr: "torch")
    assert engines.make_tts(TtsCfg(engine="speecht5", voice="male", lang="wo"), 24000) == "onnx"
    monkeypatch.setattr("sys.platform", "darwin")      # the Apple Silicon Mac keeps its torch voice
    assert engines.make_tts(TtsCfg(engine="speecht5", voice="male", lang="wo"), 24000) == "torch"


def test_the_windows_wolof_voice_downloads_from_our_host_without_torch(windows, monkeypatch, tmp_path):
    from app import packages, wolof_voice

    entry = {"id": "wolof-voice-speecht5-onnx-int8-0123456789ab", "sha256": "ab" * 32, "size": 251_000_000,
             "url": "https://hearchurch.com/download/wolof-voice-speecht5-onnx-int8-0123456789ab.tar.gz",
             "unpacked": 1, "name": "Wolof voice"}
    manifest = tmp_path / "wolof_voice.json"
    manifest.write_text(json.dumps({"onnx": entry}))
    monkeypatch.setattr(wolof_voice, "MANIFEST", manifest)
    p = packages.package_for("wo", "")
    assert (p.name, p.size_mb, p.model, p.hf_repos, p.libs, p.unavailable) == ("Wolof voice", 251, entry, (), None, "")
    assert p.model["url"].startswith("https://hearchurch.com/download/")

    manifest.write_text(json.dumps({"onnx": {**entry, "sha256": ""}}))   # not published yet: never a 404
    p = packages.package_for("wo", "")
    assert p.unavailable and not packages.Packages._present(p)


def test_the_shipped_voice_manifest_is_a_published_onnx_voice():
    from app import wolof_voice

    entry = wolof_voice.manifest()
    assert entry, "app/wolof_voice.json names no ONNX voice"
    assert entry["id"].startswith("wolof-voice-speecht5-onnx-") and len(entry["sha256"]) == 64
    assert entry["url"] == f"https://hearchurch.com/download/{entry['id']}.tar.gz"


def test_the_tokenizer_matches_speecht5s_character_model():
    from app.tts.speecht5_onnx import Tokenizer

    pieces = ["<unk>", "<s>", "</s>", "▁", "a", "Y", "à", "l", ","]
    t = Tokenizer(pieces)
    # NFKC, words joined by "▁" (spaces collapse), unknown characters → <unk>, then </s>
    assert t("  Yàlla, al ") == [3, 5, 6, 7, 7, 4, 8, 3, 4, 7, 2]
    assert t("Yà") == [3, 5, 6, 2]
    assert t("q") == [3, 0, 2]


def test_the_dropout_masks_are_drawn_as_transformers_draws_them():
    from app.tts.speecht5_onnx import SpeechT5Onnx

    o = SpeechT5Onnx.__new__(SpeechT5Onnx)
    o.dropout, o.prenet_units = 0.5, 256
    m = o.masks(np.random.default_rng(0))
    assert m.shape == (2, 256) and set(np.unique(m)) == {0.0, 2.0}
    assert 0.35 < (m > 0).mean() < 0.65


@pytest.mark.skipif(not os.environ.get("HEARCHURCH_WOLOF_VOICE_DIR"), reason="needs the exported voice "
                    "(HEARCHURCH_WOLOF_VOICE_DIR, scripts/models/export_speecht5_onnx.py)")
def test_the_onnx_voice_speaks_a_wolof_sentence():
    from app.tts.speecht5_onnx import SpeechT5Onnx

    o = SpeechT5Onnx(Path(os.environ["HEARCHURCH_WOLOF_VOICE_DIR"]))
    spk = np.load(ROOT / "models" / "wolof" / "speaker-male.npy")
    wav = o.speak("Yàlla mooy sunu Boroom, ñu ngi koy sant.", spk, threshold=0.4, maxlenratio=6.0,
                  rng=np.random.default_rng(0))
    assert 1.0 < len(wav) / o.sr < 6.0 and np.abs(wav).max() > 0.05
