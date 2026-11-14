# Licence check: what ships inside the paid Mac app (before 0.3.0)

Checked 2026-11-14 against branch `chat/hc-v3-mac` (0.3.0 build 12). **Not legal advice**: an engineering read of
the licences, to decide what to fix and what to put in front of a lawyer if anything is doubtful.

## Short answer

- **Nothing blocks 0.3.0 as long as the Python engine stays MIT-licensed and we add the missing GPL/LGPL paperwork.**
  The GPL parts (Piper, eSpeak NG, and in the Intel app x264/x265) run inside the Python engine. The engine's code
  is MIT (`LICENSE`, and `download/src/terms.html` says "The Hear Church app's own code is open source under the MIT
  License"), its `.py` source ships in the bundle, and the closed Swift app talks to it as a separate process (it
  starts `python3` with `Process()` and connects over HTTP/WebSocket on 127.0.0.1, `mac/HearChurch/Sources/Engine.swift`).
  MIT code may be combined with GPL-3 code, so this setup is allowed.
- **What's missing today:** (1) the GPL/LGPL source is not offered anywhere; (2) the notices that ship with the app
  don't mention Piper, eSpeak NG, Sparkle, the LGPL/MPL libraries or FFmpeg/x264/x265 (fixed in this commit, see
  `THIRD_PARTY_NOTICES.md`); (3) attribution for the Hi-Fi TTS base model behind four voices was missing
  (also fixed here; the credits page still needs it).
- **If Harrison ever wants the engine to be closed source**, Piper in the same process stops being allowed. Then
  Piper would have to run as its own program, or be replaced. See option C.
- **Two voice questions for Harrison** (not GPL): the default French voice *Tom* is AGPLv3, and three voices
  (Persian *amir* and *ganji*, Vietnamese *vais1000*) are fine-tuned from the Lessac/Blizzard 2013 voice, whose
  recordings are licensed for research only. See "Voices".
- **The Wolof MMS voice (CC BY-NC) is never offered in the Mac app.** Checked below.

## How the app is put together (what "shipped" means)

| Part | Where | Both arches? |
|---|---|---|
| Swift app `Hear Church.app` + **Sparkle** 2.10.x (Swift package) | `Contents/MacOS`, `Contents/Frameworks` | yes |
| Standalone **CPython 3.12** (python-build-standalone, via uv) | `Contents/Resources/python` | yes (x86_64 copy for Intel) |
| Python packages from `uv.lock` minus the Wolof-only ones (`mac/split_requirements.py`) | `.../python/lib/python3.12/site-packages` | Apple Silicon |
| Python packages pinned in `mac/requirements-intel.txt` | same | Intel |
| Engine code `app/`, `web/`, `config.toml`, glossaries, `LICENSE`, `THIRD_PARTY_NOTICES.md` | `Contents/Resources/engine` | yes |
| `models/silero_vad.onnx`, `models/wolof/speaker-*.npy` | `.../engine/models` | yes |
| Voice samples (`.m4a`, audio made with the voices) | `Contents/Resources/VoiceSamples` | yes |
| **Downloaded at run time** (`app/packages.py`): speech model, Piper voices, g2pW + bert-base-chinese tokenizer (Chinese), SpeechT5 Wolof voice + vocoder | `~/Library/...` / Hugging Face cache | — |
| **Downloaded at run time** from hearchurch.com: `wolof-libs-*.tar.gz` (torch, transformers, sentencepiece, ...; built by `mac/wolof_libs.sh`) | `HOME/packages` | Apple Silicon only (Wolof and Chinese) |

Fonts: none bundled. Alegreya/Alegreya Sans (`download/src/fonts`, OFL 1.1) are used by the website only; the app
uses system fonts.

## Every component and its licence

Licences of Python packages come from PyPI metadata of the exact pinned versions (script run against
`uv export --frozen` + `split_requirements.py` and `mac/requirements-intel.txt`), and for the packages with native
code from the macOS wheels themselves (downloaded with `pip download --platform macosx_*` and inspected with
`unzip -l` / `strings`, not installed).

### Copyleft or otherwise needing action

| Component | Version | Licence | In which app | How it's used |
|---|---|---|---|---|
| **piper-tts** (OHF-Voice/piper1-gpl) | 1.8.0 | **GPL-3.0-or-later** (wheel METADATA, `COPYING`) | both | imported in-process by `app/tts/engines.py` |
| **eSpeak NG** (statically linked into `piper/espeakbridge.so`; `espeak-ng-data/` dictionaries shipped) | commit 724808c, pinned in piper1-gpl v1.8.0 `CMakeLists.txt` | **GPL-3.0-or-later** | both | phonemes for every Piper voice |
| **x264**, **x265** (`av/.dylibs/libx264.165.dylib`, `libx265.217.dylib`) | as in PyAV 19.0.2 wheel | **GPL-2.0-or-later** | Intel only | linked by FFmpeg's libavcodec inside PyAV; faster-whisper imports `av`; never used for audio we decode, but shipped |
| **FFmpeg** libs (libavcodec etc. in PyAV) | 8.x as in PyAV 19.0.2 | **LGPL-3.0-or-later** (`libavcodec license: LGPL version 3 or later` in the binary) | Intel only | as above |
| Other PyAV dylibs: libvpx, libdav1d, libopus, libwebp, SVT-AV1, libvmaf | | BSD-style | Intel only | as above |
| **lameenc** (bundles LAME) | 1.8.4 / 1.9.0 | **LGPL-3.0** (LAME itself LGPL-2.0+) | both | MP3 for listeners |
| **soxr** (python-soxr, bundles libsoxr + PFFFT) | 1.1.0 | **LGPL-2.1-or-later** | both | resampling |
| certifi | 2026.x | MPL-2.0 | both | unmodified: keep licence, nothing else |
| tqdm | 4.70.1 | MPL-2.0 AND MIT | both | unmodified: keep licence |
| scipy's bundled libgfortran, libquadmath, libgcc_s | | GPL-3 **with GCC Runtime Library Exception** | Apple Silicon | exception makes it permissive for us |

### Permissive (attribution: keep their licence files, which travel in each package's `dist-info`)

- **Python runtime**: CPython (PSF-2.0, `lib/python3.12/LICENSE.txt` kept). python-build-standalone builds against
  libedit instead of GNU readline and disable `_gdbm` precisely to avoid GPL
  ([docs](https://gregoryszorc.com/docs/python-build-standalone/main/running.html#licensing)). Bundled OpenSSL
  (Apache-2.0), SQLite (public domain), libffi, expat, zlib, bzip2, xz, mpdecimal, ncurses, Tcl/Tk: permissive.
- **Sparkle** 2.x: MIT (plus bsdiff BSD-2, ed25519 zlib-style in its `LICENSE`). Its notice must ship: it wasn't
  listed; added.
- **Speech recognition code**: mlx 0.32.3, mlx-metal, mlx-whisper 0.4.3 (MIT); faster-whisper 1.2.1 (MIT);
  ctranslate2 4.8.5 (MIT — its macOS wheel has no licence file, so our notice has to carry it: added); its
  `libiomp5.dylib` is Intel's OpenMP runtime, redistributable. numba (BSD), llvmlite (BSD-2 + Apache-2.0 with LLVM
  exception), tiktoken (MIT), more-itertools (MIT), scipy (BSD-3), numpy (BSD-3 + others, Accelerate on Apple Silicon).
- **ONNX Runtime** 1.31.0 / 1.23.2: MIT (its `ThirdPartyNotices.txt` ships inside the package).
- **sounddevice** 0.5.6: MIT, bundles PortAudio (MIT).
- **Web/server**: fastapi, starlette, uvicorn, uvloop, httptools, h11, websockets, watchfiles, pydantic(-core),
  python-multipart, httpx/httpcore(2), anyio, sniffio, idna, urllib3, requests, charset-normalizer, truststore,
  python-dotenv, pyyaml, segno, psutil, cffi, pycparser, protobuf, flatbuffers, packaging, filelock, fsspec,
  huggingface-hub, hf-xet, regex, opentelemetry-api, pathvalidate, annotated-*, typing-*, docstring-parser, jiter,
  click, coloredlogs, humanfriendly, mpmath, sympy, tokenizers: MIT / BSD / Apache-2.0 / ISC / PSF.
- **anthropic** (Apache-2.0), **stripe** (MIT), **unicode-rbnf** (MIT), **sentence-stream** (Apache-2.0).
- **Wolof/Chinese download** (`wolof-libs`): torch 2.14.1 (BSD-3 + Apache-2.0 etc.), transformers 5.19.0
  (Apache-2.0), sentencepiece (Apache-2.0), safetensors (Apache-2.0), networkx, jinja2, markupsafe, rich, typer,
  pygments, setuptools, shellingham: all permissive. We host this archive, so we redistribute it: the licence files
  are inside it (`dist-info`).
- **Files in the repo**: Silero VAD (MIT); CMU ARCTIC x-vectors (free with attribution); g2pW tables in
  `app/tts/g2pw_data` (Apache-2.0, LICENCE alongside).

### Models and voices (all downloaded by the user's app from Hugging Face, not redistributed by us)

| Model | Licence (checked upstream 2026-11-14) | OK for the paid app? |
|---|---|---|
| `mlx-community/whisper-large-v3-turbo` | no card licence; conversion of `openai/whisper-large-v3-turbo`, MIT | yes |
| `Systran/faster-whisper-{small,base}{,.en}` (Intel) | MIT (HF API) | yes |
| `bilalfaye/speecht5_tts-wolof` (base `microsoft/speecht5_tts`) + `microsoft/speecht5_hifigan` | MIT / MIT | yes |
| `google-bert/bert-base-chinese` tokenizer files | Apache-2.0 | yes |
| g2pW (`rhasspy/piper-checkpoints` zh `_resources/g2pw.tar.gz`) | Apache-2.0 | yes |
| `rnjema-unima/mms-tts-wol-baseline` (Wolof MMS) | **CC BY-NC 4.0** | **no** — and it is not offered, see below |
| Piper voices: see next table | | |

Piper voices, from each voice's `MODEL_CARD` in `rhasspy/piper-voices` (fetched with curl):

| Voice | Card licence | Fine-tuned from | Verdict |
|---|---|---|---|
| fr_FR-tom-medium (default French) | **AGPLv3** | — | allowed, but see "Voices" |
| fr_FR-siwis-medium | CC BY 4.0 | hifi_tts_92 (Hi-Fi TTS, CC BY 4.0) | yes, with attribution of both |
| es_MX-ald-medium | Unlicense | davefx (CC0) | yes |
| es_MX-claude-high | Apache-2.0 | — | yes |
| ru_RU-denis-medium | CC0 | rdh (CC0) | yes |
| ru_RU-dmitri-medium | CC0 | hifi_tts_92 (CC BY 4.0) | yes, credit Hi-Fi TTS |
| pt_BR-cadu-medium | CC0 | rdh (CC0) | yes |
| pt_BR-faber-medium | CC0 | hifi_tts_92 (CC BY 4.0) | yes, credit Hi-Fi TTS |
| uk_UA-tetiana-high, uk_UA-oleksa-high | Apache-2.0 | — | yes |
| fa_IR-amir-medium | CC0 | **lessac** (Blizzard 2013, research-only) | risk, see "Voices" |
| fa_IR-ganji-medium | CC0 | fa_IR amir → **lessac** | risk |
| ne_NP-chitwan-medium | CC0 | rdh (CC0) | yes |
| vi_VN-vais1000-medium | CC BY 4.0 | **lessac** | risk |
| zh_CN-chaowen-medium | CC0 | hifi_tts_92 (CC BY 4.0) | yes, credit Hi-Fi TTS |
| en_US-mike-medium | CC0 | rdh (CC0) | yes |
| en_US-kathleen-medium | CC0 | hifi_tts_92 (CC BY 4.0) | yes (already credited) |
| lv_LV-aivars-medium | CC0 1.0 | not stated | yes |

`app/packages.py` already leaves out ru_RU-irina (unknown) and ru_RU-ruslan (non-commercial).

### Wolof: the non-commercial MMS voice never reaches the paid app

- `app/config.py:151`: the default Wolof voice on macOS is `speecht5`; MMS is the default only off macOS.
- `app/config.py:253`: a saved `mms` choice is replaced by the default on macOS.
- `app/tts/engines.py` `list_wolof_voices()`: offers only `speecht5:*` voices.
- `app/packages.py`: the Wolof package downloads only `WOLOF_REPOS` (SpeechT5 + HiFi-GAN), never the MMS repo.
- Nothing in `web/` or the Swift app names MMS (grep for `mms`, `MmsWolof`, `rnjema`).

The only gap is defence in depth: `make_tts()` itself would still build the MMS engine if handed `engine="mms"`
on a Mac; today nothing can hand it that. Also note MMS is the Linux default: if a Linux server ever serves paying
churches, it must not use it.

## The GPL/LGPL items: obligations, our usage, ways to comply

### Piper + eSpeak NG (GPL-3.0-or-later), both arches

**Obligation when we distribute the app** (GPL-3 §4–6):
1. Give the GPL-3 text and keep the copyright notices (the wheel's `COPYING` lands in `Resources/python/COPYING`,
   which nobody would find; the notices didn't mention Piper or eSpeak NG at all).
2. Make the **Corresponding Source** available for the binaries we ship: piper1-gpl v1.8.0 and eSpeak NG at commit
   724808c, plus the build scripts. For a download, §6(d) allows "equivalent access to the Corresponding Source in
   the same way through the same place" — a source tarball next to the DMG on hearchurch.com. A link to upstream
   GitHub is allowed too, but then we stay responsible for it staying available; mirroring is safer.
3. Anything combined with it **in the same program** must be offered under GPL-3-compatible terms. Our Python engine
   imports `piper` in-process, so the engine is part of that combined work. It is MIT and its source is in the
   bundle, so this is met. The Swift app is a separate executable talking over a socket: it's "aggregation", not
   part of the combined work, and can stay closed.
4. No extra restrictions on recipients: the terms must not forbid reverse engineering or modifying these parts.
   `terms.html` doesn't (it says the app's own code is MIT and points to the notices).
5. GPL-3's "Installation Information" rule applies only when the code is conveyed with a consumer *device*; we sell
   software, not Macs, so it doesn't apply.

**Does our usage comply today?** Partly: the structure is fine (point 3), points 1 and 2 are missing.

**Options:**
- **A. Keep Piper as is, add source + notices** (recommended). Licence texts in the app, notices list everything,
  a source tarball (piper-tts 1.8.0 sdist, eSpeak NG 724808c, and the LGPL/FFmpeg sources below) hosted on
  hearchurch.com next to each release, and one line in the notices saying where. No change to how the app works.
- **B. Run Piper/eSpeak NG as a separate executable** (`piper` CLI or a small helper process). This does *not* remove
  the duty to ship notices and source; it only matters if the engine itself becomes closed source. Costs a process
  boundary and latency on every sentence.
- **C. Replace eSpeak NG** with a permissive phonemizer (e.g. gruut, MIT; DeepPhonemizer, MIT; a dictionary +
  rules of our own). Every Piper voice was trained on eSpeak NG's exact IPA output, so another phonemizer gives
  mispronunciations across 12 languages; it would mean re-checking or retraining voices. The older MIT `piper`
  (rhasspy/piper 1.2) / `piper-phonemize` do not help: they link eSpeak NG too. Weeks of work and a quality risk;
  only worth it if the engine must become closed source.
- **D. Different TTS** (e.g. Kokoro: Apache-2.0 weights, but its multilingual G2P also falls back to eSpeak NG).
  Not a real escape for 12+ languages.

### x264, x265 (GPL-2.0-or-later) and FFmpeg (LGPL-3), Intel app only

Shipped because faster-whisper depends on PyAV and PyAV's macOS wheel bundles FFmpeg built with x264/x265. The
app never decodes media with it (audio goes in as numpy arrays), but the dylibs are in the bundle, so we distribute
them. Same structure as Piper: loaded into the MIT engine process, GPL-2.0-*or-later* is compatible with that.
**Obligation:** licence texts, notices, and the Corresponding Source of the FFmpeg/x264/x265 build PyAV 19.0.2 used
(PyAV's `pyav-ffmpeg` build scripts and the library tarballs they pin). **Options:** include them in the source
tarball of option A (cheapest); or stop shipping `av` (faster-whisper only needs it in `decode_audio`, which the
app doesn't call, but `faster_whisper/__init__` imports it, so this needs a stub module or a patched import —
fragile, not recommended now).

### lameenc (LAME) and soxr (libsoxr): LGPL, both arches

Both are Python extension modules with the library statically linked in. **LGPL obligation:** licence text +
notices; source of the library (or a written offer); let the user replace the library with a modified version;
don't forbid reverse engineering for that purpose. Replacing `lameenc*.so` / `soxr_ext*.so` with a rebuilt one is
possible (each is its own file that Python loads dynamically), but the app's hardened runtime only loads modules
signed by us, so the user would also have to re-sign the app locally (`codesign --force --deep -s - "Hear
Church.app"`); say so in the notices. **Complies after** adding the texts, the source (sdists of lameenc and soxr
include LAME/libsoxr sources) and that one sentence.

### certifi, tqdm (MPL-2.0)

File-level copyleft only for modified files; we ship them unmodified and their licence files are in `dist-info`.
Complies.

## Voices (not GPL, but for Harrison)

- **fr_FR-tom (AGPLv3), the default French voice.** We don't redistribute it (each Mac downloads it from Hugging
  Face) and we don't modify it, so AGPL's network clause (§13, which covers *modified* versions) isn't triggered;
  attribution and a link to its source (git.bksp.space/Tjiho/French-tts-model-piper) are already in the notices
  and on the credits page. Allowed. The doubt is that nobody knows what "Corresponding Source" of a voice model is;
  if Harrison wants zero AGPL in the product, make *siwis* (CC BY 4.0) the default French voice.
- **Lessac-derived voices: fa_IR-amir, fa_IR-ganji, vi_VN-vais1000.** Their cards say CC0 / CC BY 4.0, but they were
  fine-tuned from Piper's `en_US-lessac` voice, trained on the Blizzard 2013 recordings, licensed by Lessac
  Technologies "for Research Purposes only" (https://www.cstr.ed.ac.uk/projects/blizzard/2013/lessac_blizzard2013/license.html).
  Whether a fine-tuned voice inherits that restriction is legally unsettled, and that licence binds whoever trained
  the base, not us directly; but it's the one voice-provenance risk in the catalogue. These are the only Persian
  and Vietnamese voices; dropping them means losing those languages' audio (captions would stay).
- **Hi-Fi TTS attribution (CC BY 4.0)** for siwis, dmitri, faber, chaowen: was only given for kathleen. Added to
  `THIRD_PARTY_NOTICES.md`; `download/src/credits.html` should get the same line on those four rows.
- **Voice samples in the app** (`VoiceSamples/*.m4a`) are audio produced by these voices; the voices' credits
  cover them.

## Recommendation: option A, about one day

1. **Licence texts in the bundle** (~2 h): `build.sh` copies a `licenses/` folder into `Resources/engine`: GPL-3,
   GPL-2, LGPL-2.1, LGPL-3, MPL-2.0, AGPL-3 texts, plus Sparkle's and ctranslate2's licence. (The rest already ship
   in each package's `dist-info`.)
2. **Source tarball per release** (~3–4 h): a script that gathers the piper-tts 1.8.0 sdist, eSpeak NG at 724808c,
   the lameenc and soxr sdists, and for Intel the PyAV 19.0.2 sdist plus the FFmpeg/x264/x265 tarballs its build
   pins; `release.sh` uploads it to R2 as `hear-church-<version>-gpl-sources.tar.gz` (only rebuilt when those pins
   change, like `wolof-libs`).
3. **Notices** (~1 h): `THIRD_PARTY_NOTICES.md` names the source tarball's URL, and the re-signing sentence for LGPL
   relinking. Optionally an "Acknowledgements" item in the Swift app's menu that opens the notices file.
4. **Credits page** (~15 min): Hi-Fi TTS on the siwis, dmitri, faber, chaowen rows.
5. **Optional hardening** (~15 min): `make_tts()` refuses `mms` on macOS.

**Done 2026-11-14 (option A, Harrison's go-ahead):** `licenses/` ships in `Resources/engine/licenses` (build.sh);
Settings → About and help → "Open-source notices" shows the notices in Finder; `mac/oss_sources.py` builds
`hearchurch-oss-<version>.tar.gz` (one archive for both apps, ~64 MB, reproducible) from the pins in
`mac/oss-sources.json` (piper1-gpl v1.8.0 tag source — the PyPI sdist lacks the CMake/C files —, eSpeak NG 724808c,
lameenc 1.8.4/1.9.0 + LAME 3.100/4.0, soxr 1.1.0, and for Intel PyAV 19.0.2 + pyav-ffmpeg 9.0.2-1's FFmpeg 9.0.2,
x264, x265, lamer); `release.sh` uploads it to R2 `source/` on every public `--publish`; the Worker serves
`/source` and `/source/<file>`; notices and credits carry the written offer, the re-signing note and the Hi-Fi TTS
credits; `make_tts()` refuses `mms` on macOS.

Total about one working day, no change to how the app sounds or behaves. Must be in place before 0.3.0 is sold
(the GPL duty starts with the first paid copy; it arguably already started with test copies on the dev channel).

## Harrison must decide

1. Option A (engine stays MIT, add source + notices) — or does he want the engine closed source someday? Then
   Piper must move out of process (B) or be replaced (C), and the x264/x265 question comes back for Intel.
2. Keep *Tom* (AGPLv3) as the default French voice, or switch the default to *siwis*?
3. Keep the three Lessac-derived voices (Persian ×2, Vietnamese), or drop/replace them?
