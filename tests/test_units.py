import asyncio
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from app.backlog import BacklogPolicy
from app.broadcast.mixer import Clip, Mixer
from app.broadcast.stream import Mp3Broadcaster, find_frame_sync
from app.chunker import FRAME, SR, Chunker
from app.config import BacklogCfg, TranslateCfg, VadCfg, load_glossary, validate_glossary
from app.translate.claude_session import Usage
from app.translate.prompts import CONDENSE_TAG, build_system_prompt, clean_output, seed_message, user_message
from app.translate.router import Translator
from app.tts.speech_fr import speechify


# ------------------------------------------------------------------ speech text

@pytest.mark.parametrize("text,expected", [
    ("Lisons Jean 3:16 ensemble.", "Lisons Jean, chapitre 3, verset 16 ensemble."),
    ("Voir Doctrine et Alliances 88:118.", "Voir Doctrine et Alliances, chapitre 88, verset 118."),
    ("D&A 4:2", "Doctrine et Alliances, chapitre 4, verset 2"),
    ("1 Néphi 3:7", "Premier Néphi, chapitre 3, verset 7"),
    ("Alma 32:21-23", "Alma, chapitre 32, versets 21 à 23"),
    ("Moroni 10:4–5 nous invite", "Moroni, chapitre 10, versets 4 à 5 nous invite"),
    ("« Viens et suis-moi »", "Viens et suis-moi"),
    ("Nous avons 3 enfants.", "Nous avons 3 enfants."),
])
def test_speechify(text, expected):
    assert speechify(text) == expected


# ------------------------------------------------------------------ prompts

def test_clean_output():
    assert clean_output("<skip>") is None
    assert clean_output("  ") is None
    assert clean_output("Traduction : Bonjour.") == "Bonjour."
    assert clean_output("« Bonjour à tous. »") == "Bonjour à tous."
    assert clean_output("Bonjour.\n\n(Note: greeting)") == "Bonjour."
    assert clean_output(f"{CONDENSE_TAG} Bref.") == "Bref."


def test_system_prompt_has_glossary():
    g = load_glossary()
    p = build_system_prompt(g, "West Africa")
    assert "Sainte-Cène" in p and "Doctrine et Alliances" in p and "1 Néphi" in p
    assert "West Africa" in p
    assert user_message("hi", True).startswith(CONDENSE_TAG)
    assert "OK" in seed_message([("Hello", "Bonjour")])


def test_glossary_validation():
    validate_glossary(load_glossary())
    with pytest.raises(ValueError):
        validate_glossary({"terms": [{"en": "x"}]})


# ------------------------------------------------------------------ backlog

def test_backlog_policy():
    b = BacklogPolicy(BacklogCfg())
    assert b.speed_for(2) == 1.0
    assert 1.0 < b.speed_for(10) < 1.3
    assert b.speed_for(40) == 1.3
    assert b.update_condense(16) is True
    assert b.update_condense(12) is True      # hysteresis: stays on
    assert b.update_condense(8) is False


# ------------------------------------------------------------------ mixer + stream

def test_mixer_queue_drop_and_on_air():
    sr = 44100
    aired = []
    m = Mixer(sr, on_air=lambda c, t: aired.append(c.seq))
    tone = (np.sin(np.arange(sr * 5) / 10) * 8000).astype(np.int16)  # 5 s clips
    for i in range(1, 7):
        dropped = m.enqueue(Clip(i, tone.copy()), drop_above_s=25, drop_to_s=15)
    assert m.backlog_s <= 25.5
    assert dropped or len(m.queue) < 6
    frames = [m._next_frame() for _ in range(5)]
    assert all(len(f) == int(sr * 0.1) for f in frames)
    assert aired and aired[0] == m.queue[0].seq - 1 if m.queue else True


def test_mp3_broadcast_frames():
    b = Mp3Broadcaster(44100, 64, 1.0)
    sub, burst = b.subscribe()
    for _ in range(30):
        b.feed(np.zeros(4410, dtype=np.int16))
    got = b"".join(sub.queue.get_nowait() for _ in range(sub.queue.qsize()))
    assert len(got) > 20000  # ~3 s at 64 kbps
    assert find_frame_sync(got) == 0
    sub2, burst2 = b.subscribe()
    assert burst2[:1] == b"\xff"


# ------------------------------------------------------------------ chunker on real speech

def _speech_fixture(tmp: Path) -> np.ndarray | None:
    wav = tmp / "speech.wav"
    if sys.platform == "darwin" and shutil.which("say"):
        subprocess.run(["say", "-o", str(wav), "--data-format=LEI16@16000", "--file-format=WAVE",
                        "Brothers and sisters, I am grateful to be with you this morning. "
                        "The Lord has prepared a way for each of us to return to him."], check=True)
    elif shutil.which("piper") and Path("~/.local/share/piper-voices/en_US-lessac-medium.onnx").expanduser().exists():
        subprocess.run(["piper", "-m", str(Path("~/.local/share/piper-voices/en_US-lessac-medium.onnx").expanduser()),
                        "-f", str(wav)], input=b"Brothers and sisters, I am grateful to be with you this morning. "
                        b"The Lord has prepared a way for each of us to return to him.", check=True, capture_output=True)
    else:
        return None
    raw = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(wav), "-f", "f32le", "-ac", "1",
                          "-ar", str(SR), "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def test_chunker_cuts_on_pauses(tmp_path):
    speech = _speech_fixture(tmp_path)
    if speech is None:
        pytest.skip("no TTS available to make a speech fixture")
    silence = np.zeros(int(1.2 * SR), dtype=np.float32)
    audio = np.concatenate([silence, speech, silence, speech, silence, speech, silence])
    ch = Chunker(VadCfg())
    chunks = []
    for i in range(len(audio) // FRAME):
        chunks += ch.feed(audio[i * FRAME:(i + 1) * FRAME], i * FRAME / SR)
    chunks += ch.flush()
    assert len(chunks) >= 3
    assert all(c.duration <= VadCfg().hard_max_s + 0.1 for c in chunks)
    assert sum(c.voiced_s for c in chunks) > 0.7 * 3 * len(speech) / SR


def test_chunker_hard_cap_on_continuous_speech(tmp_path):
    speech = _speech_fixture(tmp_path)
    if speech is None:
        pytest.skip("no TTS available")
    # Remove pauses: tile the voiced parts tightly so there is no natural break for 30 s.
    voiced = speech[np.abs(speech) > 0.01]
    audio = np.tile(voiced, int(30 * SR / len(voiced)) + 1)[: 30 * SR]
    ch = Chunker(VadCfg())
    chunks = []
    for i in range(len(audio) // FRAME):
        chunks += ch.feed(audio[i * FRAME:(i + 1) * FRAME], i * FRAME / SR)
    assert chunks, "continuous speech must still be cut"
    assert max(c.duration for c in chunks) <= VadCfg().hard_max_s + 0.05


# ------------------------------------------------------------------ router fallback

class FakeSession:
    id = 99

    def __init__(self, delay=0.1, fail=False):
        self.delay, self.fail = delay, fail
        self.alive = self.ready = True
        self.pending_since = None
        self.turns = 0
        self.usage = Usage()
        self.rate_limit = None
        self.last_error = ""

    @property
    def busy(self):
        return self.pending_since is not None

    @property
    def busy_for(self):
        return time.monotonic() - self.pending_since if self.pending_since else 0

    async def ask(self, text):
        self.pending_since = time.monotonic()
        try:
            await asyncio.sleep(self.delay)
            if self.fail:
                raise RuntimeError("boom")
            return "claude: " + text
        finally:
            self.pending_since = None

    async def close(self):
        self.alive = False


def make_translator(claude_delay=0.1, ollama_delay=0.2, claude_fail=False, timeout=0.5):
    cfg = TranslateCfg(claude_timeout_s=timeout, ollama_timeout_s=2.0)
    t = Translator(cfg, load_glossary(), Path("/tmp/ct-test"))
    t.has_claude = True
    t.active = FakeSession(claude_delay, claude_fail)

    async def fake_ollama(system, history, english, condense=False, timeout_s=None):
        await asyncio.sleep(ollama_delay)
        return "ollama: " + english
    t.ollama.translate = fake_ollama
    return t


async def test_router_uses_claude_when_fast():
    t = make_translator(claude_delay=0.05)
    r = await t.translate("hello")
    assert r.engine == "claude" and r.text == "claude: hello"


async def test_router_falls_back_on_timeout():
    t = make_translator(claude_delay=3.0, ollama_delay=0.1, timeout=0.3)
    t0 = time.monotonic()
    r = await t.translate("hello")
    assert r.engine == "ollama" and "0.3s" in r.note
    assert time.monotonic() - t0 < 0.8
    # Claude still busy with the late request → next chunk goes straight to Ollama.
    r2 = await t.translate("again")
    assert r2.engine == "ollama" and "busy" in r2.note


async def test_router_falls_back_on_error():
    t = make_translator(claude_fail=True)
    r = await t.translate("hello")
    assert r.engine == "ollama" and "error" in r.note


async def test_router_late_claude_beats_slow_ollama():
    t = make_translator(claude_delay=0.6, ollama_delay=2.0, timeout=0.3)
    r = await t.translate("hello")
    assert r.engine == "claude" and "late" in r.note


async def test_router_ollama_only_mode():
    t = make_translator()
    t.cfg = TranslateCfg(mode="ollama")
    r = await t.translate("hello")
    assert r.engine == "ollama"


# ------------------------------------------------------------------ Piper voices

def test_piper_voices_and_synthesis():
    from app.config import TtsCfg
    from app.tts.engines import PIPER_DIR, PiperTts, list_voices

    if not (PIPER_DIR / "fr_FR-tom-medium.onnx").exists():
        pytest.skip("Piper voices not downloaded (./start.sh --setup)")
    voices = list_voices()
    assert voices[0]["id"] == "piper:fr_FR-tom-medium"
    assert any(v["voice"] == "fr_FR-siwis-medium" for v in voices)
    tts = PiperTts(TtsCfg(engine="piper", voice="fr_FR-siwis-medium"), 44100)
    assert tts.voice == "fr_FR-siwis-medium"
    t0 = time.monotonic()
    pcm = tts.synth("Frères et sœurs, je suis reconnaissant d'être avec vous. Bonne journée.")
    assert time.monotonic() - t0 < 3
    assert 2.0 < len(pcm) / 44100 < 8.0
    faster = tts.synth("Frères et sœurs, je suis reconnaissant d'être avec vous. Bonne journée.", speed=1.3)
    assert len(faster) < len(pcm)
    # Unknown voice falls back to an installed one instead of failing.
    assert PiperTts(TtsCfg(engine="piper", voice="nope"), 44100).voice.startswith("fr_FR-")


# ------------------------------------------------------------------ multi-language (French + Wolof)

def test_wolof_numbers_and_speech_text():
    from app.tts.normalize import speechify, wolof_number

    assert wolof_number(3) == "ñett"
    assert wolof_number(16) == "fukk ak juróom-benn"
    assert wolof_number(30) == "fanweer"
    assert wolof_number(118) == "téeméer ak fukk ak juróom-ñett"
    out = speechify("wo", "Yowaana 3:16 « Yàlla »")
    assert not any(ch.isdigit() for ch in out) and "«" not in out
    assert speechify("fr", "Jean 3:16") == "Jean, chapitre 3, verset 16"


def test_wolof_prompt():
    from app.languages import LANGUAGES

    g = load_glossary(LANGUAGES["wo"].glossary_file)
    p = build_system_prompt(g, "x", "wo")
    assert "Wolof" in p and "Yeesu Krist" in p and "never digits" in p
    assert "Sainte-Cène" not in p


def test_chunk_record_completes_only_when_every_language_is_done():
    from app.metrics import ChunkRecord, LangResult

    r = ChunkRecord(seq=1, audio_start_s=0, audio_dur_s=4, voiced_s=3, cut_reason="pause",
                    t_speech_start=0, t_speech_end=4, t_cut=4.1, en="Hello")
    r.out = {"fr": LangResult(), "wo": LangResult()}
    r.out["fr"].status = "aired"
    assert not r.complete
    r.out["wo"].status = "dropped"
    assert r.complete
    d = r.public()
    assert set(d["langs"]) == {"fr", "wo"} and d["langs"]["fr"]["status"] == "aired"


async def test_wolof_never_falls_back_to_ollama():
    from app.languages import LANGUAGES

    cfg = TranslateCfg(claude_timeout_s=0.2)
    t = Translator(cfg, load_glossary(LANGUAGES["wo"].glossary_file), Path("/tmp/ct-test"), LANGUAGES["wo"])
    t.has_claude = True
    t.active = FakeSession(delay=0.05, fail=True)
    called = []

    async def fake_ollama(*a, **k):
        called.append(1)
        return "nope"
    t.ollama.translate = fake_ollama
    r = await t.translate("hello")
    assert r.engine == "none" and r.text is None and not called
    t.active = FakeSession(delay=0.3)     # slower than the French deadline: Wolof still waits for Claude
    r = await t.translate("hello")
    assert r.engine == "claude"


def test_mixer_never_plays_stale_audio():
    sr = 44100
    skipped, aired = [], []
    m = Mixer(sr, on_air=lambda c, t: aired.append(c.seq), )
    m.on_skip = lambda c, age: skipped.append(c.seq)
    m.max_age_s = 30
    tone = (np.sin(np.arange(sr) / 10) * 8000).astype(np.int16)
    m.enqueue(Clip(1, tone.copy(), {"t_ref": time.monotonic() - 40}))   # too old
    m.enqueue(Clip(2, tone.copy(), {"t_ref": time.monotonic() - 5}))
    for _ in range(3):
        m._next_frame()
    assert skipped == [1] and aired == [2]


# ------------------------------------------------------------------ sign-in

def test_operator_sign_in_rules():
    from app import main
    from app.config import Settings

    main.S.settings = Settings.model_validate({"server": {"operator_pin": "s3cret"}})
    main.S.local_addrs = {"127.0.0.1"}
    tunnel = {"cf-connecting-ip": "203.0.113.9"}
    ok = lambda host, headers=None, query=None, cookies=None: main._is_operator(host, headers or {}, query or {}, cookies or {})
    assert ok("127.0.0.1")                                   # on the machine itself
    assert not ok("127.0.0.1", tunnel)                       # via the tunnel: never "local"
    assert ok("127.0.0.1", tunnel, {"key": "s3cret"})       # password in the link still works
    assert not ok("127.0.0.1", tunnel, {"key": "nope"})
    token = main._cookie_token("s3cret")
    assert token != "s3cret" and ok("203.0.113.9", tunnel, cookies={"op_pin": token})
    assert not ok("203.0.113.9", tunnel, cookies={"op_pin": "s3cret"})   # old plain cookies are refused
    main.S.settings = Settings.model_validate({"server": {"operator_pin": ""}})
    assert not ok("203.0.113.9", tunnel, {"key": ""})                   # no password set: nobody remote


def test_speaker_password_is_limited():
    from app import main
    from app.config import Settings

    main.S.settings = Settings.model_validate({"server": {"operator_pin": "boss", "speaker_pin": "talk"}})
    main.S.local_addrs = {"127.0.0.1"}
    tunnel = {"cf-connecting-ip": "203.0.113.9"}
    role = lambda query=None, cookies=None: main._role("127.0.0.1", tunnel, query or {}, cookies or {})
    assert role({"key": "boss"}) == "operator" and role({"key": "talk"}) == "speaker"
    assert role(cookies={"op_pin": main._cookie_token("talk", "speaker")}) == "speaker"
    assert role(cookies={"op_pin": main._cookie_token("talk")}) is None     # a speaker cookie can't pose as operator
    assert role({"key": "nope"}) is None
    d = main._public_settings()
    assert d["server"]["operator_pin"] == "set" and d["server"]["speaker_pin"] == "set"   # never sent
    assert "boss" not in str(d) and "talk" not in str(d)


def test_secrets_file(tmp_path):
    from app.config import load_secrets

    f = tmp_path / "secrets.env"
    f.write_text("# comment\nCLAUDE_CODE_OAUTH_TOKEN='sk-ant-oat01-abc'\nOTHER=ignored\nANTHROPIC_API_KEY=\n")
    assert load_secrets(f) == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-abc"}


def test_save_secrets_keeps_other_lines_and_is_private(tmp_path):
    from app.config import claude_access, load_secrets, save_secrets

    f = tmp_path / "secrets.env"
    f.write_text("# comment\nCLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-old\nOTHER=1\n")
    save_secrets({"ANTHROPIC_API_KEY": "sk-ant-api03-new"}, f)
    assert load_secrets(f) == {"ANTHROPIC_API_KEY": "sk-ant-api03-new"}
    assert "# comment" in f.read_text() and "OTHER=1" in f.read_text()
    assert f.stat().st_mode & 0o777 == 0o600
    assert claude_access(f) == "api_key"
    save_secrets({}, f)
    assert claude_access(f) == "login"


def test_mms_wolof_spelling():
    from app.tts.engines import mms_wolof_text

    assert mms_wolof_text("Xam naa ñu dëkk ci Yàlla, Mbóotaay") == "kham naa nu dekk ci yalla, mbootaay"
    assert mms_wolof_text("naqari") == "nakari"


async def test_no_fallback_waits_for_busy_claude_instead_of_dropping():
    from app.languages import LANGUAGES

    t = Translator(TranslateCfg(), load_glossary(LANGUAGES["wo"].glossary_file), Path("/tmp/ct-test"), LANGUAGES["wo"])
    t.has_claude = True
    t.active = FakeSession(delay=0.3)
    first = asyncio.create_task(t.translate("one"))
    await asyncio.sleep(0.05)              # the first reply is still on its way
    r = await t.translate("two")
    assert r.engine == "claude" and r.text and "busy" not in r.note
    assert (await first).engine == "claude"


def test_voice_speed_setting():
    from app.config import Settings, VoiceCfg, tts_for

    st = Settings(voices={"wo": VoiceCfg(engine="mms", voice="default", speed=1.25)})
    assert tts_for(st, "wo").speed == 1.25
    import pytest
    with pytest.raises(Exception):
        VoiceCfg(engine="mms", voice="default", speed=3)
