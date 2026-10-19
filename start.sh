#!/usr/bin/env bash
# Live English → French translation server.
#
#   ./start.sh                          normal use (opens the operator page)
#   ./start.sh --setup                  first-time install/download (needs internet, ~20 GB)
#   ./start.sh --file samples/talk.mp3  test mode: play a recording as if it were live
#   ./start.sh --help                   all options
set -euo pipefail
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
mkdir -p logs samples data

need() { command -v "$1" >/dev/null 2>&1; }
# On Linux the CPU speech recognition and the Wolof voice come from optional package groups.
if [[ "$(uname)" == "Darwin" ]]; then EXTRAS=(); else EXTRAS=(--extra linux --extra wolof); fi
cfg() { uv run --quiet "${EXTRAS[@]}" python -c "from app.config import load_settings as l; s=l(); print($1)"; }
ollama_up() { curl -s --max-time 1 "$(cfg 's.translate.ollama_url')/api/tags" >/dev/null 2>&1; }

start_ollama() {
  # Not needed when the translator is set to Claude only (translate.mode = "claude").
  [[ "$(cfg 's.translate.mode' 2>/dev/null)" == "claude" ]] && return 0
  if need ollama && ! ollama_up; then
    echo "Starting Ollama…"
    # One request slot: the long glossary prompt is cached once and every fallback reuses it.
    # (With several slots each one re-reads the prompt the first time, ~10 s each on a 24B model.)
    OLLAMA_NUM_PARALLEL=1 OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 OLLAMA_KEEP_ALIVE=3h \
      nohup ollama serve >logs/ollama.log 2>&1 &
    for _ in $(seq 1 20); do ollama_up && break; sleep 0.5; done
  fi
}

# Open-source Piper voices (Tom = male, Siwis = female), ~63 MB each, kept in models/piper.
ensure_piper_voices() {
  mkdir -p models/piper
  for v in tom siwis; do
    f="fr_FR-$v-medium"
    for ext in onnx onnx.json; do
      [ -s "models/piper/$f.$ext" ] && continue
      echo "Downloading Piper voice $f.$ext…"
      curl -fsSL -o "models/piper/$f.$ext.part" "https://huggingface.co/rhasspy/piper-voices/resolve/main/fr/fr_FR/$v/medium/$f.$ext" \
        && mv "models/piper/$f.$ext.part" "models/piper/$f.$ext" || { rm -f "models/piper/$f.$ext.part"; echo "  (offline? skipped)"; }
    done
  done
}

if [[ "${1:-}" == "--setup" ]]; then
  echo "== 1/6 System packages"
  if [[ "$(uname)" == "Darwin" ]]; then
    if need brew; then
      for pkg in uv ffmpeg ollama; do need "$pkg" || brew install "$pkg"; done
    else
      echo "Homebrew not found — install it from https://brew.sh, then re-run ./start.sh --setup"; exit 1
    fi
  else
    need ffmpeg || { echo "Install ffmpeg first (e.g. sudo apt install -y ffmpeg)"; exit 1; }
    need uv || { echo "Install uv first: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
  fi

  echo "== 2/6 Python packages"
  uv sync "${EXTRAS[@]}"

  echo "== 3/6 Speech recognition and Wolof voice models"
  uv run "${EXTRAS[@]}" python -c "
import sys
from huggingface_hub import snapshot_download
from app.config import load_settings
s = load_settings()
if sys.platform == 'darwin':
    snapshot_download(s.stt.model)
else:
    from faster_whisper import WhisperModel
    WhisperModel(s.stt.faster_whisper_model, device='cpu', compute_type='int8')
try:
    import torch  # noqa: F401  (only if the Wolof voice is installed)
    for m in ('bilalfaye/speecht5_tts-wolof', 'microsoft/speecht5_hifigan', 'rnjema-unima/mms-tts-wol-baseline'):
        snapshot_download(m)
except ImportError:
    pass
print('ok')"

  echo "== 4/6 Ollama fallback model (optional)"
  if [[ "$(cfg 's.translate.mode')" == "claude" ]]; then
    echo "  Translator set to Claude only — no Ollama model needed."
  elif need ollama; then
    start_ollama
    MODEL="$(cfg 's.translate.ollama_model')"
    ollama pull "$MODEL"
  else
    echo "  Ollama not installed — skipping (French will rely on Claude only)."
  fi

  echo "== 5/6 French voices"
  ensure_piper_voices
  if need say; then say -v '?' | grep -E 'fr_(FR|CA|BE|CH)' || true; fi

  echo "== 6/6 Claude Code"
  if need claude; then
    claude --version
    if [[ -s data/secrets.env ]]; then echo "  Using credentials from data/secrets.env"; else
      echo "  No data/secrets.env — the translator uses this machine's Claude login (run 'claude' once)."
      echo "  On a server: cp secrets.env.example data/secrets.env and add a token or API key."
    fi
  else
    echo "  → Claude Code is not installed: curl -fsSL https://claude.ai/install.sh | bash"
  fi
  echo
  echo "Setup done. Start with ./start.sh"
  exit 0
fi

need uv || { echo "uv is missing — run ./start.sh --setup"; exit 1; }
need ffmpeg || echo "warning: ffmpeg missing (needed for test recordings): brew install ffmpeg"
uv sync --quiet "${EXTRAS[@]}"
ensure_piper_voices
start_ollama
exec uv run --quiet "${EXTRAS[@]}" python -m app "$@"
