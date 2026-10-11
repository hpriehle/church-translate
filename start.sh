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
cfg() { uv run --quiet python -c "from app.config import load_settings as l; s=l(); print($1)"; }
ollama_up() { curl -s --max-time 1 "$(cfg 's.translate.ollama_url')/api/tags" >/dev/null 2>&1; }

start_ollama() {
  if need ollama && ! ollama_up; then
    echo "Starting Ollama…"
    nohup ollama serve >logs/ollama.log 2>&1 &
    for _ in $(seq 1 20); do ollama_up && break; sleep 0.5; done
  fi
}

if [[ "${1:-}" == "--setup" ]]; then
  echo "== 1/6 Homebrew packages"
  if need brew; then
    for pkg in uv ffmpeg ollama; do need "$pkg" || brew install "$pkg"; done
  else
    echo "Homebrew not found — install it from https://brew.sh, then re-run ./start.sh --setup"; exit 1
  fi

  echo "== 2/6 Python packages"
  uv sync

  echo "== 3/6 Speech recognition model (Whisper large-v3-turbo, ~1.6 GB)"
  uv run python -c "
from huggingface_hub import snapshot_download
from app.config import load_settings
snapshot_download(load_settings().stt.model)
print('ok')"

  echo "== 4/6 Ollama fallback model"
  start_ollama
  MODEL="$(cfg 's.translate.ollama_model')"
  ollama pull "$MODEL"

  echo "== 5/6 French voices installed for 'say'"
  say -v '?' | grep -E 'fr_(FR|CA|BE|CH)' || true
  if ! say -v '?' | grep -E 'fr_' | grep -qiE 'premium|enhanced|améliorée'; then
    echo "  → No Premium/Enhanced French voice yet. Install one: System Settings → Accessibility →"
    echo "    Spoken Content → System voice → Manage Voices… → Français → Thomas or Audrey (Premium/Enhanced)."
  fi

  echo "== 6/6 Claude Code"
  if need claude; then
    claude --version
    echo "  Testing a one-line translation…"
    echo '{"type":"user","message":{"role":"user","content":"Good morning, brothers and sisters."}}' | \
      claude -p --input-format stream-json --output-format stream-json --verbose --tools "" \
        --system-prompt "Translate to French. Output only the translation." 2>/dev/null | \
      grep -o '"result":"[^"]*"' | head -1 || echo "  → claude did not answer. Run 'claude' once in Terminal to log in."
  else
    echo "  → Claude Code is not installed (https://claude.com/claude-code). The server will use Ollama only."
  fi
  echo
  echo "Setup done. Start with ./start.sh"
  exit 0
fi

need uv || { echo "uv is missing — run ./start.sh --setup"; exit 1; }
need ffmpeg || echo "warning: ffmpeg missing (needed for test recordings): brew install ffmpeg"
uv sync --quiet
start_ollama
exec uv run --quiet python -m app "$@"
