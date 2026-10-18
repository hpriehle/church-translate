"""Settings: config.toml defaults + data/overrides.json (changes made on the operator page)."""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"
# Per platform, because the project folder may be synced between a Mac and a Linux box
# (e.g. Syncthing) and each needs its own engines/voices: overrides.darwin.json, overrides.linux.json.
OVERRIDES_PATH = ROOT / "data" / f"overrides.{sys.platform}.json"
# Claude credentials for the translator (never committed): CLAUDE_CODE_OAUTH_TOKEN (from
# `claude setup-token`, uses a Claude subscription) or ANTHROPIC_API_KEY (pay per use).
SECRETS_PATH = ROOT / "data" / "secrets.env"
SECRET_KEYS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
GLOSSARY_PATH = ROOT / "glossary.json"
LOG_DIR = ROOT / "logs"
SAMPLES_DIR = ROOT / "samples"
MODELS_DIR = ROOT / "models"
WEB_DIR = ROOT / "web"


class ServerCfg(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    operator_pin: str = ""
    lan_ip: str = ""
    public_url: str = ""          # e.g. https://transcription.example.com/ when served through a tunnel
    use_public_url: bool = True   # listener link/QR: the public URL (if set) or the local-network address
    tunnel_metrics: str = "127.0.0.1:20241"  # cloudflared --metrics address, for the "Public link" status
    open_browser: bool = True


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
    language: str = "en"


class TranslateCfg(BaseModel):
    mode: str = "auto"
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


class VoiceCfg(BaseModel):
    engine: str
    voice: str


# Voices for languages other than French (French uses [tts]).
DEFAULT_VOICES: dict[str, VoiceCfg] = {"wo": VoiceCfg(engine="speecht5", voice="male")}


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


def tts_for(settings: "Settings", code: str) -> TtsCfg:
    """Voice settings for one output language (rate and volume are shared)."""
    if code == "fr":
        return settings.tts
    v = settings.voices.get(code) or DEFAULT_VOICES.get(code) or VoiceCfg(engine="piper", voice="")
    return TtsCfg(engine=v.engine, voice=v.voice, rate=settings.tts.rate, volume=settings.tts.volume)


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
    return Settings.model_validate(data)


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
