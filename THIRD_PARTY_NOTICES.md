# Third-party components

Included in this repository:
- `models/silero_vad.onnx` — Silero VAD, MIT License (https://github.com/snakers4/silero-vad).
- `models/wolof/speaker-*.npy` — speaker x-vectors computed from the CMU ARCTIC corpus
  (https://huggingface.co/datasets/Matthijs/cmu-arctic-xvectors), free to use with attribution.
- `download/src/fonts/` — Alegreya and Alegreya Sans, SIL Open Font License 1.1 (licenses alongside).

Downloaded at setup (not included):
- Whisper large-v3-turbo / faster-whisper models — MIT License (OpenAI).
- Piper voices `fr_FR-tom-medium` and `fr_FR-siwis-medium` — see each voice's MODEL_CARD on
  https://huggingface.co/rhasspy/piper-voices (siwis: CC BY 4.0; tom: AGPLv3).
- Piper voices `es_MX-ald-medium` (Unlicense), `es_MX-claude-high` (Apache-2.0), `ru_RU-denis-medium` (CC0) and
  `ru_RU-dmitri-medium` (CC0), `pt_BR-cadu-medium` and `pt_BR-faber-medium` (CC0), `uk_UA-tetiana-high` and
  `uk_UA-oleksa-high` (Apache-2.0), `fa_IR-amir-medium` and `fa_IR-ganji-medium` (CC0), `ne_NP-chitwan-medium` (CC0),
  `zh_CN-chaowen-medium` (CC0), `en_US-joe-medium` (CC0), `en_US-kristin-medium` (public domain), from the same repository.
- Piper voice `vi_VN-vais1000-medium` — CC BY 4.0, from the same repository.
- Wolof voice `bilalfaye/speecht5_tts-wolof` — MIT License; vocoder `microsoft/speecht5_hifigan` — MIT.
- Wolof voice "MMS" `rnjema-unima/mms-tts-wol-baseline` (Meta's Massively Multilingual Speech) —
  CC BY-NC 4.0: non-commercial use only, so development on Linux only and never in the paid Mac app.
- Ollama model `mistral-small3.2:24b` (optional fallback) — Apache 2.0.

The glossary uses publicly published terminology of The Church of Jesus Christ of Latter-day
Saints. This project is not affiliated with or endorsed by the Church.
