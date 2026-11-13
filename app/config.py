"""Settings: config.toml defaults + data/overrides.json (changes made on the operator page)."""
from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
# Where this computer's own files go (settings, logs, glossaries, downloaded voices). The project folder by
# default; the Hear Church Mac app sets HEARCHURCH_HOME to ~/Library/Application Support/Hear Church because
# the code itself sits read-only inside the signed app.
HOME = Path(os.environ["HEARCHURCH_HOME"]).expanduser() if os.environ.get("HEARCHURCH_HOME") else ROOT
if os.environ.get("HEARCHURCH_HOME"):
    # The Mac app: downloaded models (Whisper, Wolof) go to its data folder, never ~/.cache. Set before
    # huggingface_hub is imported anywhere (it reads this once at import).
    os.environ.setdefault("HF_HOME", str(HOME / "models" / "huggingface"))
CONFIG_PATH = ROOT / "config.toml"
# Per platform, because the project folder may be synced between a Mac and a Linux box
# (e.g. Syncthing) and each needs its own engines/voices: overrides.darwin.json, overrides.linux.json.
OVERRIDES_PATH = HOME / "data" / f"overrides.{sys.platform}.json"
# Claude credentials for the translator (never committed): CLAUDE_CODE_OAUTH_TOKEN (from
# `claude setup-token`, uses a Claude subscription) or ANTHROPIC_API_KEY (pay per use).
SECRETS_PATH = HOME / "data" / "secrets.env"
SECRET_KEYS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
LOG_DIR = HOME / "logs"
SAMPLES_DIR = HOME / "samples"
MODELS_DIR = ROOT / "models"          # shipped with the code (speech detector, Wolof speakers)
PIPER_DIR = HOME / "models" / "piper"  # downloaded voices
WEB_DIR = ROOT / "web"


def user_file(name: str) -> Path:
    """A file the operator page edits (e.g. a glossary): in HOME, first copied there from the code's default."""
    path = HOME / name
    if HOME != ROOT and not path.exists() and (ROOT / name).exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    return path


GLOSSARY_PATH = user_file("glossary.json")


class ServerCfg(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    operator_pin: str = ""        # password for everything (operator page, settings, Claude access)
    speaker_pin: str = ""         # optional second password: the speak page only (start/stop, languages)
    lan_ip: str = ""
    public_url: str = ""          # e.g. https://transcription.example.com/ when served through a tunnel
    use_public_url: bool = True   # listener link/QR: the public URL (if set) or the local-network address
    tunnel_metrics: str = "127.0.0.1:20241"  # cloudflared --metrics address, for the "Public link" status
    update_from_git: bool = False  # "Restart server" first runs git pull (for a server that is a plain git clone)
    open_browser: bool = True
    # Hear Church app: "cloud" publishes meetings on hearchurch.com (anyone with the link, anywhere); "local"
    # keeps them on this Wi-Fi, for phones that scanned this meeting's QR code (/s/<key>).
    sharing: Literal["cloud", "local"] = "cloud"


class EngineCfg(BaseModel):
    """Let a faster computer (e.g. a Mac) run the meetings of a public server (e.g. a VPS)."""
    token: str = ""        # shared secret for the link; empty = off (set the same value on both)
    use: bool = True       # public server: hand meetings to the engine while it is connected
    hub_url: str = ""      # engine: the public server to serve, e.g. wss://live.example.org/ws/engine
    name: str = ""         # engine: how the public server's pages name it, e.g. "Harrison's Mac"
    device_token: str = "" # engine: a Hear Church device token. With hub_url set, this computer publishes its
                           # own locally started meetings as that congregation's session (instead of `token`)


class HearCfg(BaseModel):
    """Hear Church (many congregations) on a public server. E-mail goes to logs/outbox.log unless smtp_host
    (or HEARCHURCH_SMTP_HOST) is set; the SMTP password is only read from HEARCHURCH_SMTP_PASSWORD."""
    db_path: str = "data/hearchurch.db"
    smtp_host: str = ""
    smtp_port: int = 0               # 0 = 587 (or HEARCHURCH_SMTP_PORT)
    smtp_user: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    grace_s: float = 60.0            # a session whose engine dropped stays open this long for a reconnect
    verify_email: bool = False       # off until e-mail works: sign-up hands the app its device token at once
    site_host: str = "hearchurch.com"  # requests for this host get the finder at /, not the ward's listening page


class AudioCfg(BaseModel):
    input_device: str = ""
    input_channel: int = 0
    input_gain: float = 1.0


class VadCfg(BaseModel):
    threshold: float = 0.5
    min_chunk_s: float = 3.0
    target_max_s: float = 8.0
    hard_max_s: float = 10.0
    pause_ms: int = 300
    short_pause_ms: int = 120
    flush_pause_ms: int = 800
    min_voiced_s: float = 0.45


class SttCfg(BaseModel):
    engine: str = "auto"
    model: str = "mlx-community/whisper-large-v3-turbo"
    faster_whisper_model: str = "small.en"
    language: str = "en"   # the speaker's language (en, fr, es, ru): switched live next to Start meeting


class TranslateCfg(BaseModel):
    mode: str = "auto"
    backend: str = "cli"       # how Claude is reached: "cli" = the `claude` CLI (subscription or key),
                               # "api" = the Anthropic API directly with ANTHROPIC_API_KEY (the Mac app, own key),
                               # "hub" = through the Hear Church hub on the congregation's credits (no key here)
    claude_model: str = "opus"
    claude_effort: str = "low"
    claude_timeout_s: float = 6.0
    claude_session_turns: int = 150
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "mistral-small3.2:24b"
    ollama_timeout_s: float = 10.0
    audience: str = "Francophone members from West Africa, mostly Senegal"


class TtsCfg(BaseModel):
    engine: str = "piper"
    voice: str = "fr_FR-tom-medium"
    rate: int = 190
    volume: float = 1.0
    speed: float = 1.0   # other languages: 1.0 = the voice's natural pace (French uses rate instead)
    lang: str = "fr"     # which macOS voices `say` picks from


class VoiceCfg(BaseModel):
    engine: str
    voice: str
    speed: float = Field(1.0, ge=0.7, le=1.6)   # 1.2 = 20% faster


# Voices for languages other than French (French uses [tts]).
# Wolof: SpeechT5 (male/female) is fast on Apple Silicon; elsewhere the faster MMS voice keeps up better.
DEFAULT_VOICES: dict[str, VoiceCfg] = {
    "wo": VoiceCfg(engine="speecht5", voice="male") if sys.platform == "darwin" else VoiceCfg(engine="mms", voice="default"),
    "es": VoiceCfg(engine="piper", voice="es_MX-ald-medium"),
    "ru": VoiceCfg(engine="piper", voice="ru_RU-denis-medium"),
    "pt": VoiceCfg(engine="piper", voice="pt_BR-cadu-medium"),
    "uk": VoiceCfg(engine="piper", voice="uk_UA-tetiana-high"),
    "fa": VoiceCfg(engine="piper", voice="fa_IR-amir-medium"),
    "ne": VoiceCfg(engine="piper", voice="ne_NP-chitwan-medium"),
    "vi": VoiceCfg(engine="piper", voice="vi_VN-vais1000-medium"),
    "zh": VoiceCfg(engine="piper", voice="zh_CN-chaowen-medium"),
    "en": VoiceCfg(engine="piper", voice="en_US-joe-medium")}


class BacklogCfg(BaseModel):
    speedup_start_s: float = 6.0
    speedup_full_s: float = 15.0
    max_speed: float = 1.3
    condense_on_s: float = 15.0
    condense_off_s: float = 9.0
    drop_above_s: float = 25.0


class StreamCfg(BaseModel):
    bitrate_kbps: int = 64
    sample_rate: int = 44100
    burst_s: float = 1.0


class ScriptureCfg(BaseModel):
    """Published scripture wording for quoted verses (see scripture.py)."""
    enabled: bool = True
    # Output language → eBible.org Bible id, to change a language's Bible (scripture.SOURCES lists those
    # allowed) or "" to switch it off. Languages left out use scripture.DEFAULT_BIBLES.
    bibles: dict[str, str] = {}
    window_s: float = 120.0    # how long after a reference its verse is offered to the translator
    max_verses: int = 5        # longest range fetched ("John 3:16-40" → 16–20)


class Settings(BaseModel):
    # Output languages, in the order listeners see them; the first is the default.
    languages: list[str] = ["fr", "wo"]
    voices: dict[str, VoiceCfg] = {}
    server: ServerCfg = ServerCfg()
    audio: AudioCfg = AudioCfg()
    vad: VadCfg = VadCfg()
    stt: SttCfg = SttCfg()
    translate: TranslateCfg = TranslateCfg()
    tts: TtsCfg = TtsCfg()
    backlog: BacklogCfg = BacklogCfg()
    stream: StreamCfg = StreamCfg()
    engine: EngineCfg = EngineCfg()
    hearchurch: HearCfg = HearCfg()
    scripture: ScriptureCfg = ScriptureCfg()


def tts_for(settings: "Settings", code: str) -> TtsCfg:
    """Voice settings for one output language (rate and volume are shared)."""
    # "say": a choice saved by an older version. Apple's licence doesn't allow broadcasting macOS voices.
    if code == "fr":
        tts = settings.tts
        return tts.model_copy(update={"engine": "piper", "voice": "fr_FR-tom-medium"}) if tts.engine == "say" else tts
    v = settings.voices.get(code)
    if v is None or v.engine == "say":
        v = DEFAULT_VOICES.get(code) or VoiceCfg(engine="piper", voice="")
    elif v.engine == "mms" and sys.platform == "darwin":  # non-commercial licence: not in the paid Mac app
        v = DEFAULT_VOICES[code].model_copy(update={"speed": v.speed})
    return TtsCfg(engine=v.engine, voice=v.voice, rate=settings.tts.rate, volume=settings.tts.volume, speed=v.speed,
                  lang=code)


def load_secrets(path: Path = SECRETS_PATH) -> dict[str, str]:
    """KEY=value lines from data/secrets.env (only the Claude credential keys are used)."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k in SECRET_KEYS and v:
            out[k] = v
    return out


def save_secrets(values: dict[str, str], path: Path = SECRETS_PATH) -> None:
    """Replace the Claude credentials in data/secrets.env (other lines are kept). Only this user can read it."""
    keep = []
    if path.exists():
        keep = [line for line in path.read_text().splitlines() if line.split("=", 1)[0].strip() not in SECRET_KEYS]
    keep += [f"{k}={v}" for k, v in values.items() if k in SECRET_KEYS and v]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write("\n".join(keep) + "\n")
    os.replace(tmp, path)


def claude_access(path: Path = SECRETS_PATH) -> str:
    """Which credential the translator uses: "api_key", "token" (subscription) or "login" (this computer's)."""
    s = load_secrets(path)
    if os.environ.get("ANTHROPIC_API_KEY"):  # e.g. passed in by the Mac app from the Keychain
        return "api_key"
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):  # a subscription token, also from the Mac app
        return "token"
    return "api_key" if "ANTHROPIC_API_KEY" in s else "token" if "CLAUDE_CODE_OAUTH_TOKEN" in s else "login"


def _deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings(config_path: Path = CONFIG_PATH) -> Settings:
    data: dict[str, Any] = {}
    if config_path.exists():
        data = tomllib.loads(config_path.read_text())
    if OVERRIDES_PATH.exists():
        try:
            data = _deep_merge(data, json.loads(OVERRIDES_PATH.read_text()))
        except json.JSONDecodeError:
            pass
    return Settings.model_validate(_env_overrides(data))


# Set by the Hear Church Mac app for the engine it runs: they win over config.toml and the overrides file,
# and the device token never has to be written to disk.
ENV_OVERRIDES = {
    "HEARCHURCH_HUB_URL": ("engine", "hub_url"),
    "HEARCHURCH_DEVICE_TOKEN": ("engine", "device_token"),
    "HEARCHURCH_DEVICE_NAME": ("engine", "name"),
    "HEARCHURCH_LISTENER_URL": ("server", "public_url"),   # the congregation's hearchurch.com/<slug> page
}


def _env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    for var, (section, key) in ENV_OVERRIDES.items():
        if os.environ.get(var):
            data = _deep_merge(data, {section: {key: os.environ[var]}})
    return data


def save_override(section: str, values: Any) -> None:
    """Persist operator-page changes so they survive a restart."""
    OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    current: dict[str, Any] = {}
    if OVERRIDES_PATH.exists():
        try:
            current = json.loads(OVERRIDES_PATH.read_text())
        except json.JSONDecodeError:
            current = {}
    if isinstance(values, dict) and isinstance(current.get(section, {}), dict):
        current.setdefault(section, {}).update(values)
    else:
        current[section] = values
    OVERRIDES_PATH.write_text(json.dumps(current, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------- glossary

def load_glossary(path: Path = GLOSSARY_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"terms": [], "names": [], "books": [], "local_names": []}
    g = json.loads(path.read_text())
    for key in ("terms", "names", "books", "local_names"):
        g.setdefault(key, [])
    return g


def validate_glossary(g: Any) -> dict[str, Any]:
    if not isinstance(g, dict):
        raise ValueError("glossary must be a JSON object")
    for key in ("terms", "names", "books"):
        items = g.get(key, [])
        if not isinstance(items, list):
            raise ValueError(f"'{key}' must be a list")
        for i, item in enumerate(items):
            if not isinstance(item, dict) or not item.get("en") or not item.get("fr"):
                raise ValueError(f"'{key}' item {i + 1} needs both 'en' and 'fr'")
    if not isinstance(g.get("local_names", []), list):
        raise ValueError("'local_names' must be a list of strings")
    return g


def save_glossary(g: dict[str, Any], path: Path = GLOSSARY_PATH) -> None:
    validate_glossary(g)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(g, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)
