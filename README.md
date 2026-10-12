# Live French translation for church meetings

English talks in → French captions and French audio out to phones on the church Wi-Fi.
Listeners scan a QR code, tap the sound button, and hear the French voice (with earphones)
while live captions appear. It's live only: the one control is mute/unmute, and there's no history to scroll back through. Everything runs on one Apple Silicon Mac. The phones don't need internet.

```
 mic / soundboard ─► VAD chunks (3–8 s, cut on pauses) ─► Whisper large-v3-turbo (mlx)
                                                              │ English text
                                                              ▼
                       Claude Code (one long-lived session, sermon context)
                       └─ falls back to Ollama (local) if Claude errors or takes > 6 s
                                                              │ French text ──► captions (WebSocket)
                                                              ▼
                       Piper French voice ─────► mixer ─► one continuous MP3 stream ──► phones
```

## One-time setup (on the Mac, with internet)

1. Install Homebrew from <https://brew.sh> if it isn't installed yet.
2. Voices: the default French voices are open-source Piper voices, **Tom** (male, default) and
   **Siwis** (female). `./start.sh` downloads them into `models/piper` (~63 MB each). The operator
   page also lists any macOS voices; Premium/Enhanced ones (System Settings → Accessibility →
   Spoken Content → Manage Voices) are optional.
3. Make sure Claude Code is installed and logged in: run `claude` once in Terminal and log in.
4. Run:

   ```bash
   cd ~/dev/church-translate
   ./start.sh --setup
   ```

   This installs `uv`, `ffmpeg` and `ollama` with Homebrew, then the Python packages. It also
   downloads the speech model (~1.6 GB) and the Ollama fallback model `mistral-small3.2:24b`
   (~15 GB on download; Ollama keeps a converted copy too, ~30 GB in all), the Piper voices, and
  sends Claude one test line.

To pull the fallback model by hand: `ollama pull mistral-small3.2:24b`. A lighter option is
`ollama pull gemma3:12b`; set it on the operator page or in `config.toml`.

## Keeping it running (Mac)

Run `scripts/autostart.sh install` once. The server then runs in the background of your Mac
login: it starts when you log in, restarts itself if it crashes, and keeps the keychain access
that Claude Code needs (which it doesn't have when started over SSH). After that:

- **Restart server** on the operator page restarts it in place: it picks up code and dependency
  updates and new settings, and phones reconnect by themselves. Or run `scripts/autostart.sh restart`.
- `scripts/autostart.sh status` / `remove`. The log is `logs/launchd.log`.
- The Mac is only kept awake while a run is active, and the Ollama fallback model is loaded when
  a run starts, so an idle server uses little memory.
- The first time the server uses the Mac's microphone from the background service, macOS asks
  whether "python3" may use the microphone. Click Allow on the Mac (once).

Without the background service, `./start.sh` in Terminal works as before.

## Sunday checklist

1. Plug the Mac into power and join the **church Wi-Fi** (the same network the phones will use).
2. If the background service isn't installed, run `./start.sh`. Open <http://localhost:8000/operator>.
3. The first time, macOS asks whether Python may **accept incoming connections** (allow it) and
   whether Terminal may use the **microphone** (allow it).
4. Wait until the top bar shows *Speech-to-text: ready*, *Claude: ready* and *Ollama: ready*.
5. Pick the input device. Use the built-in mic for now; later, the USB interface from the
   soundboard, with the right channel. Watch the input level meter while someone speaks.
6. Press **Start**, then **Play a test sentence to all phones** to check that phones hear it.
7. Put out the printed QR signs (**Printable sign** on the operator page). Type the Wi-Fi name
   there to add a "join Wi-Fi" code too.
8. Press **Stop** at the end. A report and a log for the meeting are saved in `logs/`.

Listeners: scan → tap **Son coupé / Touchez pour écouter** → earphones. That button is the only control: it mutes and unmutes, and unmuting is always at the live point. Only the latest lines show; starting a new run clears every phone.

## Testing with a recording

```bash
./start.sh --file samples/talk.mp3                    # plays it in real time as if live; watch the operator page
./start.sh --file samples/talk.mp3 --exit-when-done --record --no-browser
                                                      # unattended run: writes logs/session-…-report.md
                                                      # and the French audio logs/session-…-fr.mp3
uv run python scripts/load_test.py --phones 30 --seconds 120   # 30 simulated phones (while a run is going)
```

You can also upload a recording on the operator page (**Test recording**). The report shows
latency percentiles, which engine handled each chunk, Claude token use, and the whole
English/French transcript.

## Speaking from a browser (remote testing)

`/speak?key=<operator key>` turns any browser's microphone into the input. It works on a laptop or
phone, but needs HTTPS (or localhost), e.g. through a tunnel. The page shows what was heard, the
French, and the latency. Open the listener page on a *different* device with headphones; if the
French voice reaches this microphone, it gets translated again.

The key is `[server] operator_pin` (in `data/overrides.<platform>.json`). When the server sits
behind a tunnel or reverse proxy, requests carrying `Cf-Connecting-Ip`/`X-Forwarded-For` never count
as "local", so the operator, speak and API pages always require the key. Set `[server] public_url`
so the QR code shows the public address.

## How it keeps up (backlog control)

French usually takes a bit longer to say than English. The mixer always knows how many seconds of
French audio are waiting, and reacts in steps:

| Waiting audio | What happens |
|---|---|
| < 6 s | normal voice rate |
| 6–15 s | the voice speeds up gradually, to 1.3× |
| > 15 s | Claude/Ollama are also asked to condense (`[ABRÉGER]`) until it falls below 9 s |
| > 25 s | the oldest waiting audio is dropped (its caption still shows), so the delay never grows past this |

If translation itself falls behind, waiting chunks are merged into one request. All thresholds
are in `config.toml` under `[backlog]`.

## Languages (French + Wolof)

`languages = ["fr", "wo"]` in `config.toml` (or the checkboxes on the operator page, then
**Restart server**) chooses which languages are offered. The English is transcribed once; each
language then runs on its own: its own Claude session, voice, live stream (`/stream/fr.mp3`,
`/stream/wo.mp3`) and captions. Listeners pick **Français / Wolof** at the top of the listen page
(remembered on the phone; `/?lang=wo` links straight to Wolof).

- **Wolof translation:** Claude only. The local fallback model's Wolof is poor, so if Claude
  doesn't answer within 15 s that sentence has no Wolof rather than a wrong one. Numbers are
  written and spoken as Wolof words. `glossary.wo.json` is a small starter list; a Wolof-speaking
  member should review and extend it (operator page → Glossary → Wolof).
- **Wolof voice:** SpeechT5 fine-tuned on recordings of native Wolof speakers
  (`bilalfaye/speecht5_tts-wolof`, MIT), male or female (`models/wolof/speaker-*.npy`). Needs PyTorch
  (installed automatically on the Mac; on Linux use `--extra wolof`). About 0.5–1 s per sentence on
  the M4 Pro. Samples are on `/static/voices/index.html`.
- **Load with both languages** (measured on the M4 Pro, 3 minutes of a talk): captions 3.0 s (French)
  and 3.4 s (Wolof) after each pause, audio 4.4 s / 5.0 s, nothing dropped. The server process uses
  under half a CPU core on average, with short bursts of 1–3 cores, and about 6 GB of memory
  (Whisper + Wolof voice); each Claude session is ~120 MB. Claude usage roughly doubles (one session
  per language).

## Claude and the fallback

- One `claude -p --input-format stream-json --output-format stream-json` process stays alive, so
  Claude sees the whole talk as one conversation. It runs with its own system prompt, no tools,
  and `--safe-mode`, so your CLAUDE.md, hooks and MCP servers aren't loaded. Model and effort are
  set in `[translate]`; the default is Opus at low effort.
- Every 150 chunks (~15 min), a fresh session is prepared in the background, primed with the last
  12 chunks, and swapped in between two requests. This keeps context and cost bounded.
- If Claude errors, is still busy with a late reply, or hasn't answered within **6 s**, Ollama
  translates that chunk. A late Claude reply still wins if it arrives before Ollama's. The engine
  used for each chunk is shown on the operator page and logged in `logs/session-*.jsonl`.
- With no internet at the church, choose *Ollama only* on the operator page. Otherwise it switches
  to Ollama automatically, chunk by chunk.

**Usage.** Measured on test runs: about **$1.80–2 per hour of speech** API-equivalent with Opus,
about 600 Claude turns per hour, mostly cached input. On a Claude subscription this counts against
your plan limits instead of costing money. The operator page shows live turns, tokens and $.

## The glossary

`glossary.json` holds official Church French terminology (Sainte-Cène, pieu, Société de Secours,
Doctrine et Alliances…), scripture book names, and names with French spellings (Néphi, Ésaïe…).
Add the people in your ward under **Local names** on the operator page. This helps speech
recognition spell them and keeps them unchanged in French. A few terms are marked `"review": true`
for a French-speaking member to check.

Saving the glossary takes effect right away for Ollama and speech recognition. Claude switches to
a session with the new glossary in the background.

## Phones, iPhone lock screen, and the network

- **Audio is one continuous stream** (`/stream.mp3`, like internet radio; silence between
  sentences), not clip-by-clip. On iPhone this should keep playing with the screen locked, because
  Safari treats it like a podcast. Muting keeps the stream running silently, so unmuting is instantly
  live. The fallback `/?mode=clips` plays each new clip as it arrives (never old ones) and keeps the
  screen on.
- **Reconnects:** if a phone loses Wi-Fi, the page reconnects by itself (captions resume from
  where they stopped, audio restarts at *live*). Tested by cutting all connections for 10 s.
- **Many listeners:** 30 simulated phones ran with steady 64 kbps audio and captions arriving
  within milliseconds of each other. 20–30 phones use about 2 Mbps of Wi-Fi.
- **Guest Wi-Fi "client isolation":** many church guest networks block phones from reaching other
  devices. If phones can't open the page, ask for a network without isolation, or use a small
  travel router as the Mac's own network.

### iPhone lock-screen test (please do this once)

1. Open the link; tap the sound button; earphones in.
2. On the Mac, run a test recording (or press *Play a test sentence* every few seconds).
3. Lock the iPhone. You should keep hearing French for several minutes, and the lock screen
   should show *Traduction en direct* with a play/pause control.
4. Turn Wi-Fi off for 10 s and back on while unlocked: the page should say *Reconnexion…* and
   then resume.
5. If the audio stops when locked, use `/?mode=clips` (it keeps the screen on while unmuted), and
   tell people to keep their screens on.

## Configuration

`config.toml` holds the defaults (ports, VAD thresholds, models, voice, rate, backlog thresholds).
Changes made on the operator page are saved to `data/overrides.<platform>.json`
(`overrides.darwin.json` on the Mac); delete it to go back to the defaults. Useful settings:

- `[server] port = 80` gives a shorter link without `:8000`.
- `[server] operator_pin = "1234"` opens the operator page from a tablet:
  `http://<mac-ip>:8000/operator?pin=1234`. Without a PIN, it only opens on the Mac itself.
- `[tts]` picks the voice: `engine = "piper"` with `voice = "fr_FR-tom-medium"` or
  `"fr_FR-siwis-medium"`, or `engine = "say"` with a macOS voice name. The **French voice** menu on
  the operator page switches voices live (the next sentence uses the new one). Samples of the
  candidates we compared are on `/static/voices/index.html`.

## Files

```
start.sh               one start command (+ --setup)
config.toml            defaults          glossary.json   names & terms
app/main.py            FastAPI routes, WebSockets, operator API
app/pipeline.py        capture → STT (once) → one Channel per language, per-chunk timing
app/channel.py         per language: translator → voice → mixer → MP3 stream + captions
app/languages.py       the output languages (French, Wolof)
app/audio_in.py        input devices, test-file playback, capture thread
app/chunker.py         Silero VAD (ONNX) + pause-based chunking
app/stt.py             mlx-whisper (faster-whisper for non-Mac development)
app/translate/         Claude session, Ollama client, router (timeout/fallback), prompts
app/tts/               Piper (in-process) / macOS say, French speech normalisation (scripture references)
app/broadcast/         mixer (real-time clock), MP3 stream fan-out, caption hub
app/backlog.py         speed-up / condense / drop policy
app/report.py          markdown run reports
web/listener/          phone page (French)        web/operator/   operator page
web/print.html         printable QR sign
scripts/               load_test.py, browser_check.mjs, flaky_proxy.py
tests/                 uv run pytest
```
