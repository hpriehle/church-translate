# Third-party components

Included in this repository:
- `models/silero_vad.onnx` — Silero VAD, MIT License (https://github.com/snakers4/silero-vad).
- `models/wolof/speaker-*.npy` — speaker x-vectors computed from the CMU ARCTIC corpus
  (https://huggingface.co/datasets/Matthijs/cmu-arctic-xvectors), free to use with attribution.

Downloaded at setup (not included):
- Whisper large-v3-turbo / faster-whisper models — MIT License (OpenAI).
- Piper voices `fr_FR-tom-medium` and `fr_FR-siwis-medium` — see each voice's MODEL_CARD on
  https://huggingface.co/rhasspy/piper-voices (siwis: CC BY 4.0; check tom's card before redistributing).
- Wolof voice `bilalfaye/speecht5_tts-wolof` — MIT License; vocoder `microsoft/speecht5_hifigan` — MIT.
- Ollama model `mistral-small3.2:24b` (optional fallback) — Apache 2.0.

The glossary uses publicly published terminology of The Church of Jesus Christ of Latter-day
Saints. This project is not affiliated with or endorsed by the Church.
