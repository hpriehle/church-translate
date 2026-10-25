# Live translation for church meetings (English → French and Wolof)

English talks in → live French and Wolof captions and voice out to listeners' phones (or the
chapel's headset transmitter). Listeners scan a QR code, pick a language, tap the sound button and
listen with earphones. It's live only: the one control is mute/unmute, with no history to scroll
back through.

It runs on an Apple Silicon Mac (best: GPU speech recognition) or on a Linux server, where the
speaker uses a phone's browser as the microphone. Translation uses Claude, through your own Claude
subscription or an Anthropic API key (see [Claude access](#claude-access)).

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

## Claude access

Each installation needs its own Claude access. Pick one:

- **A Claude subscription (Pro/Max).**
  - **On a Mac you use:** sign in once with `claude` in Terminal. Nothing else is needed.
  - **On a server, or a Mac running the background service:** run `claude setup-token` on any
    computer with a browser, then put the token it prints in `data/secrets.env`:
    `CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-…`. It counts against that subscription's plan limits.
- **An Anthropic API key** (pay per use, roughly $1.50–2 per language per hour of speech with
  caching). Put it in `data/secrets.env` as `ANTHROPIC_API_KEY=sk-ant-…`. This is the better choice
  for an organisation, or when several buildings run their own copy.

The easiest way to add either one is the operator page: **Voice & translation → Claude access**.
Paste the token or key and press Save. It is written to `data/secrets.env` on the server, Claude
restarts with it right away, and the page never shows it again. You can also edit the file yourself
(see `secrets.env.example`); it is never committed. If both lines are present, Claude Code uses the
API key. The operator page shows which sign-in the translator is using.

## Running on a Linux server (no laptop at church)

On a server, the speaker opens **/speak** on a phone (its own microphone, or one plugged into it)
and keeps that page open with the screen on. Listeners use the public link as usual.

```bash
sudo apt install -y ffmpeg git
curl -LsSf https://astral.sh/uv/install.sh | sh                # Python package manager
curl -fsSL https://claude.ai/install.sh | bash                 # Claude Code
git clone https://github.com/hpriehle/church-translate && cd church-translate
./start.sh --setup                                             # packages, Whisper, voices (about a minute)
```

Then create `data/overrides.linux.json` (pick your own password and address):

```json
{"server": {"host": "127.0.0.1", "port": 8420, "operator_pin": "choose-a-password",
            "public_url": "https://your.domain/", "open_browser": false, "update_from_git": true},
 "translate": {"mode": "claude"}}
```

- Run it with systemd (`scripts/church-translate.service.example`) and publish it with a Cloudflare
  tunnel pointing at `http://127.0.0.1:8420` (`scripts/church-translate-tunnel.service.example`). If another cloudflared already uses metrics port
  20241, give this tunnel another one (`--metrics 127.0.0.1:20242`) and set
  `"tunnel_metrics": "127.0.0.1:20242"` so the operator page can show the Public link status.
- The **password** (`operator_pin`) protects the speaker and operator pages: people go to `/speak` or
  `/operator`, type it once, and the phone stays signed in for 30 days.
- To let other people run meetings, set a **speaker password** on the operator page (Server →
  Speaker password). It opens only the speak page: Start/Stop, the language switches and the voices.
  Settings, Claude access, Restart and the passwords stay with the operator password. Changing it
  signs out everyone who used the old one. Send them `/guide` (how to run a meeting from a phone).
- Add Claude access on the operator page (**Claude access**, see above).
- With `update_from_git`, **Restart server** on the operator page first pulls the latest code from
  GitHub, so updates need no SSH.

Notes for servers without a GPU:
- Speech recognition uses Whisper `small.en` on the processor (about 1.7 s per 6 s of speech on 8
  cores). It makes a few more mistakes with names than the Mac's large model; add names to the
  glossary.
- The local Ollama fallback is too slow without a GPU, so `"mode": "claude"` turns it off: French then
  relies on Claude like Wolof does.
- At church, the speaker's phone (or a mic plugged into it) must keep the speak page open **with the
  screen on**: phones stop the microphone when they lock. Plug it in to charge.

## A faster computer as the engine (Mac + server)

A server without a GPU is a few seconds slower than an Apple Silicon Mac. You can have both: keep
the public server for the pages, passwords and phones, and let a Mac do the work whenever it is on.

- On the **server**, set a shared secret: `"engine": {"token": "<long random text>"}` in
  `data/overrides.linux.json`.
- On the **Mac**, set the same secret and the server's address in `data/overrides.darwin.json`:
  `"engine": {"hub_url": "wss://your.domain/ws/engine", "token": "<same text>", "name": "Church Mac"}`,
  then restart its server.

The Mac connects out to the server (no tunnel or open port needed, any internet connection works,
it can stay at home). While it is linked and free, every meeting started on the server's speak page
runs on the Mac, and its captions and voice reach phones through the server as usual. If the Mac is
asleep, busy with its own meeting, or drops out mid-meeting, the server runs the meeting itself
(phones see the captions restart once). The operator page shows the link under Server → Engine,
with a switch to stop using it. Keep the Mac plugged in with the lid open: it keeps itself awake
while linked. Voice and speed choices for meetings on the Mac are the Mac's own.

## Keeping it running (Mac)

Run `scripts/autostart.sh install` once. The server then runs in the background of your Mac
login: it starts when you log in, restarts itself if it crashes, and keeps the keychain access
that Claude Code needs (which it doesn't have when started over SSH). After that:

- **Restart server** on the operator page restarts it in place: it picks up code and dependency
  updates and new settings, and phones reconnect by themselves. Or run `scripts/autostart.sh restart`.
- `scripts/autostart.sh status` / `remove`. The log is `logs/launchd.log`.
- **Public link:** if `~/.cloudflared/church-translate.yml` exists, `install` also runs the Cloudflare
  tunnel on the Mac (`scripts/autostart.sh tunnel` installs just that). The public link then needs
  nothing but this Mac and an internet connection: no VPN, no other computer. The operator page
  shows **Public link: connected / offline**. If a building's network blocks it, set **Listener
  link → Local network** (phones on the same Wi-Fi) or use a hotspot / travel router.
- The Mac is only kept awake, with its screen on, while a run is active (a screen that turns off
  and locks can hold back the browser tab recording the microphone). Keep the lid open, and the Ollama fallback model is loaded when
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

## License

MIT (see `LICENSE`). Models and voices downloaded at setup have their own licenses: see
`THIRD_PARTY_NOTICES.md`.
