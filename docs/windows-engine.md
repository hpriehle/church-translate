# The engine on Windows (W1)

The Windows app runs the same Python engine as the Mac app (`app/`, `python -m app --app`), frozen into a
self-contained sidecar: `hearchurch-engine\hearchurch-engine.exe` plus its `_internal\` folder (PyInstaller onedir,
Python 3.12, no installer yet). The native WinUI 3 shell (W2) starts it and talks to it over the same local HTTP and
WebSocket interface the Mac app uses. Windows 10 and 11, x64.

CI: `.github/workflows/windows.yml` on `windows-latest`, on every push to `chat/hc-win` (and by hand:
Actions → windows → Run workflow). Two jobs run side by side:

- **tests**: `uv sync --frozen --extra linux`, then the whole pytest suite on Windows.
- **engine**: builds the exe, runs `--self-check`, then plays a recorded sermon through it end to end and prints the
  lag numbers; uploads the built folder (`hearchurch-engine-win-x64`) and the smoke-test logs as artifacts, kept 7 days.

## Building the sidecar

```
uv sync --frozen --extra linux
uv pip install "pyinstaller>=6.10,<7"
uv run --no-sync python windows/build.py        # → dist/hearchurch-engine/hearchurch-engine.exe
dist/hearchurch-engine/hearchurch-engine.exe --self-check
```

- `windows/build.py` stages the shipped `config.toml` with the Windows defaults, as the Intel Mac build does
  (`mac/build.sh`, `HC_ARCH=intel`): the multilingual CPU model `small` and French as the default translation.
- `windows/hearchurch-engine.spec` bundles the engine code, `web/`, `config.toml`, the glossaries, the speech
  detector and Wolof speaker embeddings (`models/silero_vad.onnx`, `models/wolof/`), the licences, and the native
  libraries of faster-whisper/CTranslate2, onnxruntime, Piper, PortAudio (sounddevice), soxr, lameenc and PyAV.
  torch, transformers and mlx are left out. 357 MB unpacked (CI build).
- **No models are bundled.** As on the Mac, speech models and voices are downloaded on first use through the engine's
  own `/api/packages/download` (Hugging Face for Whisper and Piper, hearchurch.com/download for Wolof speech).
- The exe runs with Python's UTF-8 mode on (built in), because the engine reads its JSON/TOML without naming an
  encoding; without it Windows would use cp1252.
- `--self-check` imports everything, checks the shipped files, and prints UTF-8 mode, the audio host APIs (MME,
  DirectSound, WASAPI, WDM-KS), input devices, CUDA devices and onnxruntime providers as JSON; exit 1 if anything is
  missing.

## What changed in the engine (the seams)

All small, and none changes what a Mac does:

| Where | Mac | Windows |
|---|---|---|
| `app/config.py` data folder | `HEARCHURCH_HOME` from the app (`~/Library/Application Support/Hear Church`) | `HEARCHURCH_HOME` from the app; the exe started by hand defaults to `%LOCALAPPDATA%\HearChurch` |
| shipped files (`ROOT`) | the app bundle | `_internal\` next to the exe (`sys._MEIPASS`) |
| speech recognition | mlx (Apple Silicon) / faster-whisper CPU int8 (Intel) | faster-whisper CPU int8, `small` stepping down to `base` when the PC can't keep up (the Intel Mac's ladder, `app/stt.py`); speech package = `Systran/faster-whisper-small` + `-base` (`app/main.py _stt_repo`) |
| NVIDIA GPU | never | optional: if CTranslate2 sees a CUDA device, the model loads on it (`int8_float16`); if the CUDA libraries are missing or fail, it falls back to the CPU and says so in the log. `HEARCHURCH_STT_DEVICE=cpu` turns it off. The CUDA DLLs (cuBLAS 12, cuDNN 9) are **not** bundled, so today this only works where they are installed |
| parent watchdog (`--app`) | the parent pid changes when the app dies | the parent pid no longer runs (or was reused: start time differs) |
| clean shutdown | `SIGINT` to itself | `signal.raise_signal(SIGINT)` (os.kill would terminate the process outright) |
| restart (`/api/restart`) | `python -m app …` | the exe itself (`hearchurch-engine.exe …`) |
| test recordings (`--file`) | ffmpeg from Homebrew | ffmpeg on PATH (CI installs it); stdin is passed explicitly, as a console-less engine has none |
| time zones | system | the `tzdata` package (Windows only, in `pyproject.toml`/`uv.lock`) |
| audio capture | PortAudio/CoreAudio | PortAudio via sounddevice (the wheel ships the DLL); device listing and the per-device stall/reconnect logic are the same code |
| secrets | the app passes them in the environment (Keychain) | the same: the shell keeps them in Windows Credential Manager and passes them in the environment; the engine never needs keyring. A `data\secrets.env` written by the operator page is protected by the per-user ACL of `%LOCALAPPDATA%` (no POSIX 0600 on Windows) |

## How the WinUI 3 shell starts it and talks to it (what W2 needs)

Exactly what `mac/HearChurch/Sources/Engine.swift` and `AppModel.swift` do:

1. Pick a free local port, then start
   `hearchurch-engine.exe --app --no-browser --port <port>` with working directory = the exe's folder,
   **stdin, stdout and stderr redirected** (stdout/stderr appended to `%LOCALAPPDATA%\HearChurch\logs\engine-output.log`, as on the Mac),
   no console window (`CREATE_NO_WINDOW`).
2. Environment (start from the shell's own, minus any `HEARCHURCH_*`, `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`):
   - `HEARCHURCH_HOME=%LOCALAPPDATA%\HearChurch` — settings, logs, glossaries and every downloaded model live here
     (Hugging Face cache under `models\huggingface`).
   - `PYTHONUNBUFFERED=1`
   - after device sign-in: `HEARCHURCH_HUB_URL` (`wss://hearchurch.com/ws/engine`), `HEARCHURCH_DEVICE_TOKEN`
     (kept in Credential Manager, never written to disk by the engine), `HEARCHURCH_DEVICE_NAME`,
     `HEARCHURCH_LISTENER_URL`, `HEARCHURCH_SHARE_LOGS` (`1`/`0`), `HEARCHURCH_APP_VERSION`, `HEARCHURCH_APP_BUILD`.
   - only for a church on its own key: `ANTHROPIC_API_KEY`. Without one, translation goes through the hub proxy on
     the congregation's credits (metered by the hub), as on the Mac.
3. Poll `GET http://127.0.0.1:<port>/api/status` until it answers (the engine binds 127.0.0.1 unless local Wi-Fi
   sharing is on, then 0.0.0.0 — Windows Firewall may then ask; W2 should add the rule at install).
4. Then the same routes the Mac app uses: `/api/state`, `/api/settings`, `/api/packages` and
   `/api/packages/download` (first-run downloads with progress), `/api/start` / `/api/stop` (source `mic` with a
   device name: the hearing-assist receiver's line-in/USB input), `/api/billing`, `/api/glossary`, `/api/report`,
   `/api/restart`, `/api/qr`, and the live feed over `ws://127.0.0.1:<port>/ws/operator`.
5. Restart it if it dies; on quit, terminate it (it also quits within ~1 s by itself when the shell's process is
   gone). Keep a pid file to reap an orphan after a crash, as `Engine.reapOrphan` does.

Device sign-in (email code / pairing code) is done by the shell against hearchurch.com, as the Mac app does
(`HubClient.swift`); the engine only receives the resulting token.

## The sermon smoke test

`scripts/windows_smoke.py` (runs on Linux too, against any engine command):

1. starts this repo's hub (`app/hear`, as `scripts/e2e_credits.py` does) on a free port with a throwaway database;
   Claude is replaced by an instant fake (`"En français, <the English>"`), so there is no Anthropic key and no network
   call, but the hub's own translation proxy, live-meeting check and metering are real;
2. signs up a test congregation (credits, the free hour) and takes its device token;
3. starts the exe with `--app` and the same environment the shell will pass, downloads the speech model and the
   French Piper voice through `/api/packages/download`, and stops it;
4. runs the exe on 5 minutes of a public-domain sermon recording (LibriVox: C. H. Spurgeon, *Talks to Farmers*,
   ch. 7, cached by the workflow and checked by SHA-1) in real time with `--exit-when-done`: faster-whisper on the
   CPU → translation through the hub proxy → the French Piper voice;
5. prints the lag numbers from the run's per-chunk records (`logs\session-*.jsonl`);
6. downloads Wolof speech recognition (the Omnilingual CTC 300M int8 ONNX model from hearchurch.com, what every
   non-Apple-Silicon computer uses) and runs 45 s with Wolof as the speaker language, to show onnxruntime runs it
   on Windows (the recording is English, so the Wolof text is meaningless; only that it runs is checked).

### Numbers from the runner

Run [43998113198](https://github.com/hpriehle/hear-church/actions/runs/43998113198) (commit 2b57e94,
2026-11-17), `windows-latest`, 4 vCPU, no GPU, the exe, 5 min of the sermon in real time, model `small` (CPU int8):

| | p50 | p95 | max |
|---|---|---|---|
| speech → caption (end of a sentence to its translated caption) | 12.6 s | 19.0 s | 19.7 s |
| speech → voice (end of a sentence to the French voice starting) | 19.5 s | 22.8 s | |
| speech recognition per chunk | 3.1 s | 4.4 s | |

- speech recognition real-time factor **0.57** (seconds of work per second of speech); 278 s of speech in 52
  chunks: 49 recognised, translated through the hub proxy and captioned, 47 voiced by Piper (1.3 s per clip),
  3 skipped by the catch-up rule; it stayed on `small` (the step-down starts above 0.8).
- Wolof speaker (Omnilingual ONNX int8, onnxruntime CPU): 45 s, 9 of 9 chunks recognised, RTF 0.97, caption delay
  p50 5.9 s. It runs on Windows; at RTF ~1 on 4 vCPU it is at its limit.
- First-run downloads on the runner: speech (`small` + `base`) 634 MB and the French voice 127 MB in 73 s;
  Wolof speech 244 MB in 12 s. The built sidecar folder is 357 MB.

**Finding:** the delay comes from queueing, not from recognition itself: each chunk takes ~3 s, but at an RTF of
0.57 with bursts of speech the queue builds and a chunk waits 6–15 s before its turn. This is what
`docs/intel-latency.md` measured on the Intel Mac ("`small` is right at the edge … lag reaches 16–32 s").
The step-down (RTF > 0.8) didn't trigger here. Since W2 it also steps down when lines wait more than 4 s for their
turn (median of the last 8), which this run would have triggered: docs/intel-latency.md, "1b".

Read them with care: the translation is the instant fake, so the caption delay here is speech recognition plus the
engine's own chunking and queueing; a real Claude turn through the hub adds its own time (about a second or two).
A GitHub runner (4 vCPU, shared) is also not the church office PC; the W4 test on that PC gives the real figure.

## Not done yet

- **W2**: the WinUI 3 shell is in `windows/HearChurch` (docs/windows-app.md). Still to come: an installer and updates (W3), signing.
- **Live microphone on Windows is untested**: the runner has no audio input (`input_devices: 0`). Capture uses the
  same PortAudio code as on the Mac, with the default host API (MME). Whether WASAPI should be preferred for the
  receiver's line-in, and how a USB receiver shows up, needs the office PC (W4).
- **NVIDIA GPU**: detection and CPU fallback are in, but the CUDA libraries are not bundled; untested on real
  hardware (runners have no GPU).
- **Wolof voice (speaking Wolof translations)**: not offered on Windows by default (like the Intel Mac). The
  non-Apple default would be the MMS voice, whose licence is non-commercial; the Mac's SpeechT5 voice needs torch.
  Before Wolof translation is offered on Windows (W5), decide: download torch with the voice as the Mac does, or
  leave it out. Wolof as a *speaker* language works (the ONNX model above).
- **Known test failure** `tests/test_languages.py::test_the_paid_mac_app_never_offers_the_non_commercial_wolof_voice`
  fails on every non-Mac machine (Linux too: the default voices are chosen when `app.config` is imported, before
  the test pretends to be a Mac). It is deselected in the Windows workflow; not a Windows problem.
- Test-file playback (`--file`) needs ffmpeg on PATH; the shipped engine doesn't need it for live meetings.
- First-run downloads come from Hugging Face without a token (the log warns about rate limits).
- The exe is unsigned: SmartScreen will warn on the office PC until it is signed.
