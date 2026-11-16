# Third-party components

Included in this repository:
- `models/silero_vad.onnx` — Silero VAD, MIT License (https://github.com/snakers4/silero-vad).
- `models/wolof/speaker-*.npy` — speaker x-vectors computed from the CMU ARCTIC corpus
  (https://huggingface.co/datasets/Matthijs/cmu-arctic-xvectors), free to use with attribution.
- `download/src/fonts/` — Alegreya and Alegreya Sans, SIL Open Font License 1.1 (licenses alongside).
- `web/vendor/leaflet/` — Leaflet 1.9.4 (https://leafletjs.com), BSD 2-Clause License (licence alongside). The
  sign-up and account pages' map; its tiles are © OpenStreetMap contributors, and address suggestions come from
  Photon (photon.komoot.io).

Included in the Mac app:
- Piper (`piper-tts`, https://github.com/OHF-Voice/piper1-gpl) — GNU GPL v3 or later; it contains eSpeak NG
  (https://github.com/espeak-ng/espeak-ng) and its pronunciation data — GNU GPL v3 or later.
- Sparkle (https://github.com/sparkle-project/Sparkle) — MIT License, with bsdiff (BSD) and ed25519 (zlib-style).
- Python (python-build-standalone) — Python Software Foundation License, with the licences of its bundled libraries.
- Python packages, each with its licence in its `*.dist-info` folder inside the app's `python` folder. Most are MIT,
  BSD or Apache 2.0; these are not: `lameenc` (with LAME) — GNU LGPL v3; `soxr` (with libsoxr) — GNU LGPL v2.1 or
  later; `certifi` and `tqdm` — Mozilla Public License 2.0; SciPy's libgfortran/libgcc — GNU GPL v3 with the GCC
  Runtime Library Exception. `ctranslate2` (Intel Macs) — MIT License, Copyright (c) 2018- SYSTRAN.
- Intel Macs only: PyAV (`av`, BSD) contains FFmpeg — GNU LGPL v3 or later — built with x264 and x265 — GNU GPL v2
  or later — and lamer (a LAME fork) — GNU LGPL v2 or later.

The licence texts (GNU GPL v2 and v3, LGPL v2.1 and v3, MPL 2.0, AGPL v3, and the licences of Sparkle and
CTranslate2) are in the `licenses` folder next to this file, inside the app at
`Hear Church.app/Contents/Resources/engine/licenses`.

Source code of the GPL and LGPL parts. The complete source code of Piper, eSpeak NG, lameenc and LAME, soxr and
libsoxr, and (Intel Macs) PyAV, FFmpeg, x264, x265 and lamer, exactly as built into each version of the app, is at
https://hearchurch.com/source, as `hearchurch-oss-<version>.tar.gz` for the version you have (Hear Church menu → About
Hear Church). Written offer: for at least three years after we last distribute a version of the app, anyone may also
ask support@hearchurch.com for that source code on a physical medium, for no more than our cost of sending it.
The app's own engine (`app/`, `web/` inside `Contents/Resources/engine`) is MIT-licensed and ships as source.

Replacing an LGPL library. lameenc (`lameenc*.so`), soxr (`soxr_ext*.so`) and, on Intel Macs, PyAV's FFmpeg
(`av/.dylibs/*.dylib`) are separate files inside `Contents/Resources/python` that Python loads when the app runs,
so you may replace them with versions built from modified source. macOS only loads code signed by the same
developer into the app, so after replacing a file, re-sign the app on your Mac:
`codesign --force --deep -s - "/Applications/Hear Church.app"`. You may reverse-engineer the app as far as needed
to do this and to debug such changes.

Downloaded at setup (not included):
- Whisper large-v3-turbo / faster-whisper models — MIT License (OpenAI).
- Piper voices `fr_FR-tom-medium` and `fr_FR-siwis-medium` — see each voice's MODEL_CARD on
  https://huggingface.co/rhasspy/piper-voices (siwis: CC BY 4.0; tom: AGPLv3).
- Piper voices `es_MX-ald-medium` (Unlicense), `es_MX-claude-high` (Apache-2.0), `ru_RU-denis-medium` (CC0) and
  `ru_RU-dmitri-medium` (CC0), `pt_BR-cadu-medium` and `pt_BR-faber-medium` (CC0), `uk_UA-tetiana-high` and
  `uk_UA-oleksa-high` (Apache-2.0), `fa_IR-amir-medium` and `fa_IR-ganji-medium` (CC0), `ne_NP-chitwan-medium` (CC0),
  `zh_CN-chaowen-medium` (CC0), `en_US-mike-medium` (CC0), from the same repository.
- `fr_FR-siwis-medium`, `ru_RU-dmitri-medium`, `pt_BR-faber-medium` and `zh_CN-chaowen-medium` are fine-tuned from a
  model trained on the Hi-Fi TTS dataset (reader 92, Cori Samuel; Bakhturina, Lavrukhin, Ginsburg and Zhang, NVIDIA,
  https://www.openslr.org/109/), CC BY 4.0.
- Piper voice `en_US-kathleen-medium` — CC0 recordings (https://github.com/OHF-Voice/voice-datasets), fine-tuned from
  a model trained on the Hi-Fi TTS dataset (reader 92, Cori Samuel; Bakhturina, Lavrukhin, Ginsburg and Zhang, NVIDIA,
  https://www.openslr.org/109/), CC BY 4.0 — from the same repository.
- Piper voice `vi_VN-vais1000-medium` — CC BY 4.0, from the same repository.
- Piper voice `lv_LV-aivars-medium` — CC0 1.0, from the same repository (upstream:
  https://huggingface.co/RaivisDejus/Piper-lv_LV-Aivars-medium).
- Chinese pronunciation: g2pW model and lookup tables (`app/tts/g2pw_data`) — Apache License 2.0, see the LICENCE file
  there; `bert-base-chinese` tokenizer files — Apache License 2.0.
- Wolof voice `bilalfaye/speecht5_tts-wolof` — MIT License; vocoder `microsoft/speecht5_hifigan` — MIT.
- Wolof speech recognition, Apple Silicon: Kiriku-Wolof-ASR by AI Hub Senegal (`AIHubSN/Kiriku-Wolof-ASR`,
  https://huggingface.co/AIHubSN/Kiriku-Wolof-ASR) — Apache License 2.0 (`licenses/Apache-2.0.txt`), converted to
  mlx for Hear Church and downloaded from hearchurch.com. A fine-tune of Whisper large-v2 (OpenAI, MIT License),
  trained on `galsenai/wolof-audio-data` (Apache-2.0), which includes the Kallaama speech corpus
  (https://github.com/gauthelo/kallaama-speech-dataset, CC BY 4.0) and Google FLEURS (CC BY 4.0).
- Wolof speech recognition, Intel Macs: Meta Omnilingual ASR CTC 300M v2 (`omniASR_CTC_300M_v2`,
  https://github.com/facebookresearch/omnilingual-asr), Copyright (c) Meta Platforms, Inc. and affiliates — Apache
  License 2.0, exported to ONNX for Hear Church and downloaded from hearchurch.com.
- Wolof voice "MMS" `rnjema-unima/mms-tts-wol-baseline` (Meta's Massively Multilingual Speech) —
  CC BY-NC 4.0: non-commercial use only, so development on Linux only and never in the paid Mac app.
- Ollama model `mistral-small3.2:24b` (optional fallback) — Apache 2.0.

The glossary uses publicly published terminology of The Church of Jesus Christ of Latter-day
Saints. This project is not affiliated with or endorsed by the Church.
