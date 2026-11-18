# Live English

Low-delay room audio for hard-of-hearing members: the office computer captures the chapel audio
(from the old translation receiver) and streams it over the church Wi-Fi to phones.

- `sender.py`: runs on the office Windows computer.
- `listen.py`: a test listener for a laptop, which plays the stream and measures loss, jitter and delay.
  The iPhone app will speak the same protocol.
- `protocol.py`: the packet format, documented at the top of the file.

Audio is 48 kHz mono, Opus in low-delay mode with 5 ms frames at 64 kbps. Each packet also
carries the previous frame, so a single lost packet is recovered. Each listener uses about
130 kbps, so 50 listeners use about 6.5 Mbps.

## Office computer setup (Windows)

1. Install Python 3.12 from python.org (tick "Add python.exe to PATH").
2. Copy the `live` folder to `C:\HearChurch\live`.
3. In a terminal:
   ```
   cd C:\HearChurch
   py -3.12 -m venv .venv
   .venv\Scripts\pip install -r live\requirements.txt
   .venv\Scripts\python -m live.sender --list-devices
   ```
4. Find the input the receiver is plugged into (prefer the `[Windows WASAPI]` entry), then:
   ```
   .venv\Scripts\python -m live.sender --device "Line In"
   ```
   or set `DEVICE` in `live\run-sender.bat` and double-click it.
5. Allow Python through Windows Firewall when asked (Private and Public, since the church network
   may be classed as Public). It needs incoming UDP on port 47300.

The status line shows the input level, listener count and dropped frames. Aim for peaks around
-12 to -6 dB. "CLIPPING" means turn the receiver's volume down.

Options: `--channel left|right` if only one side of a stereo input carries audio; `--exclusive`
for WASAPI exclusive mode (lowest delay, but nothing else can use that input);
`--tone` or `--file sermon.mp3` to test without the receiver.

## Sunday test

1. On the office computer: `python -m live.sender --tone` (or the real device).
2. On a laptop on the **church Wi-Fi** (not the office network), with this repo or the `live` folder:
   ```
   pip install -r live/requirements.txt
   python -m live.listen <office computer IP> --no-play
   ```
   - "No audio yet" means the Wi-Fi blocks phones from reaching the office computer
     (client isolation or a separate network). That's the main thing this test settles.
   - Otherwise note the **jitter p99** and **lost %** from a few seats in the chapel, with people in it.
     Ctrl+C prints the buffer size the phone would need at that spot.
3. Drop `--no-play` and use wired headphones to hear it. Use `--buffer-ms 20` or `40` to compare.
4. Clap test for the real delay: hold an earbud next to a second phone that is recording, clap at
   the pulpit microphone, and measure the gap between the two claps in the recording.

| Measured | Meaning |
|---|---|
| p99 jitter under 10 ms, loss under 1% | Phone buffer around 15 ms; about 35 ms mic-to-ear is realistic |
| p99 10–30 ms | Buffer 25–35 ms; about 50 ms mic-to-ear |
| p99 over 40 ms or loss over 3% | Wi-Fi too busy; look at a dedicated access point |

## Tests

`uv run pytest tests/test_live.py` (needs `av` from the dev group).
