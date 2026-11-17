# The Windows app (W2)

`windows/HearChurch` is the native Windows shell: WinUI 3 (Windows App SDK 1.6), .NET 8, x64, unpackaged and
self-contained (no runtime to install), for Windows 10 1809+ and 11. It starts the engine sidecar from W1
(`engine\hearchurch-engine.exe` beside `HearChurch.exe`) and drives it exactly as the Mac app does
(docs/windows-engine.md). Everything real stays in the engine; the shell is screens, sign-in and the system bits.
Unsigned, no installer yet (W3).

CI: the `app` job of `.github/workflows/windows.yml` builds it (`dotnet publish`), builds the sidecar and puts it in
`engine\`, uploads the whole folder as the artifact **hearchurch-win-x64** (7 days), then walks the real app through
its screens and uploads **windows-screenshots**. The pictures from the last green run are in
`docs/windows-screenshots/`.

To try it on a PC: download `hearchurch-win-x64` from a green run (Actions → windows → the run → Artifacts), unzip,
run `HearChurch.exe`. SmartScreen warns (unsigned): More info → Run anyway.

## What it has (the Mac's core)

| | Mac source | Windows |
|---|---|---|
| Engine: start, restart (5 tries, back-off), pid file and orphan reaping, 90 s start deadline, log `logs\engine-output.log` trimmed to ~2 MB | `Engine.swift` | `Core/Engine.cs` |
| Status / settings / packages polling, `/ws/operator` feed (transcript, log), meeting changes | `Engine.swift` | `Core/Engine.cs`, `Core/EngineModels.cs` |
| Sign-in: e-mailed code and "connect by code" (`/api/hc/email-code`, `/api/hc/pair`), revoked-device check, billing mode | `HubClient.swift`, `OnboardingView.swift` | `Core/HubClient.cs`, `UI/OnboardingView.cs` |
| Setup: Welcome (sign-in) → Languages (every language, downloads start) → Microphone (input picker, level, share logs) → own API key (only for own-key congregations) | `OnboardingView.swift` | `UI/OnboardingView.cs` |
| Main window: Start/Stop top-left (confirm before stopping), LIVE + elapsed time, level meter, listeners, connection, congregation, settings | `MainView.swift` | `UI/MainView.cs` |
| Sidebar: checks before the meeting / Translating during it, Sound (input by name, channel, gain), Languages under Gain during a meeting, Stats; Listeners + QR code + link + Print a sign + Play a test sentence pinned at the bottom | `MainView.swift` | `UI/MainView.cs` |
| Language grids (speaker / translation, centered, download state), toggles live during a meeting | `LanguageSelector.swift` | `UI/LanguageSelector.cs` |
| Transcript columns (original + one per translation language), "N s behind" per column and per line (orange from 8 s, red from 15 s), Activity log | `MainView.swift` | `UI/TranscriptView.cs` |
| Pre-meeting level meter, only while the window is visible | `System.swift` MicMonitor | `Core/MicMonitor.cs` (WASAPI via NAudio) |
| Menu bar item → notification-area icon: open, Start/Stop, start at login, quit (asks during a meeting); closing the window keeps it running | `HearChurchApp.swift` | `UI/Tray.cs`, `App.xaml.cs` |
| Start at login (on after setup, as the Mac; toggle in Settings and in the icon's menu) | `LoginItem` | `Core/System.cs` (HKCU `Run`, started with `--background`) |
| Secrets (device token, API key) | Keychain | Windows Credential Manager (`Core/System.cs`) |
| Keep awake during a meeting | `KeepAwake` | `SetThreadExecutionState` |
| Settings | 8 tabs | a stub: General (start at login, share logs, log, help links, sign out) |

Data folder: `%LOCALAPPDATA%\HearChurch` (`profile.json` and `app-settings.json` for the app, everything else the
engine's). For testing: `HEARCHURCH_HOME` (another data folder), `HEARCHURCH_APP_HUB` (another hub, e.g.
`http://127.0.0.1:8000`), `HEARCHURCH_APP_ENGINE` (another sidecar exe).

## Wording

Customer-visible text is the Mac's, word for word, except where it names the Mac: "this Mac" → "this PC",
"Mac default" → "PC default", "a code from another Mac" → "a code from another computer" (and "On a Mac or PC already
signed in…"), "Keychain" → "Windows Credential Manager", the microphone privacy path (Settings → Privacy & security →
Microphone), and Wolof's voice ("No Wolof voice on Windows yet…"). Flags are left out of the language buttons: Windows
has no flag emoji (it shows the two letters).

## Screenshots in CI

`scripts/windows_screens.py` starts this repo's hub with a fake Claude (as the sermon smoke test), signs up a test
congregation, downloads speech recognition and the English and French voices into a fresh data folder through the
sidecar, asks the hub for a pairing code, and starts `HearChurch.exe` with `HEARCHURCH_APP_SCREENSHOTS`. The app
(`UI/Screenshots.cs`) then really signs in with that code, finishes setup, waits for the engine, plays the sermon
(`samples\sermon.mp3` in the data folder) as a meeting, opens Settings, stops the meeting and quits, rendering each
screen to a PNG (`RenderTargetBitmap`: the window's content, without its title bar). `screens.json` lists every step.
No real hub credentials are used anywhere.

## Not done

- Installer, updates, signing (W3); Windows Firewall rule for "This Wi-Fi only" sharing (asked by Windows the first
  time instead).
- Settings tabs beyond General (W5): Languages (remove), Speaking, Glossary, Congregation (Add device, e-mails,
  link), Billing, AI. "Add or remove languages…" opens the stub.
- Notifications use the icon's balloon (shown as a toast on Windows 10/11), not the Windows App SDK notification API.
- Live input on a real PC (the runner has no audio input): how the hearing-assist receiver shows up (line-in vs USB)
  and whether the engine's MME names match the WASAPI names the pre-meeting meter uses (matched by prefix, since MME
  cuts names at 31 characters) needs the office PC (W4).
- Send logs to support, the Wolof voice (see docs/windows-engine.md).
