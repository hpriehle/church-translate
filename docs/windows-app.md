# The Windows app (W2)

`windows/HearChurch` is the native Windows shell: WinUI 3 (Windows App SDK 1.6), .NET 8, x64, unpackaged and
self-contained (no runtime to install), for Windows 10 1809+ and 11. It starts the engine sidecar from W1
(`engine\hearchurch-engine.exe` beside `HearChurch.exe`) and drives it exactly as the Mac app does
(docs/windows-engine.md). Everything real stays in the engine; the shell is screens, sign-in and the system bits.
Unsigned; installer and automatic updates: docs/windows-release.md (W3).

CI: the `app` job of `.github/workflows/windows.yml` builds it (`dotnet publish`), builds the sidecar and puts it in
`engine\`, uploads the whole folder as the artifact **hearchurch-win-x64** (7 days), then walks the real app through
its screens and uploads **windows-screenshots**. The pictures in `docs/windows-screenshots/` come from run
[44014614181](https://github.com/hpriehle/hear-church/actions/runs/44014614181) (2026-11-17, `windows-latest`, its
1024×768 screen): `1-sign-in`, `2-setup-languages`, `3-main-idle`, `4-main-meeting` (the sermon, 26 s in, French
3.5 s behind at that moment), then every Settings tab during the meeting: `5-settings-1-general`, `-2-billing`,
`-3-languages`, `-3b-languages-french` (a language's voices), `-4-speaking`, `-5-glossary` (French),
`-6-congregation`, `-7-about`; `*-whole` is the whole tab where it scrolls; and `screens.json` (every step, timed). The
CI congregation is on credit, so Billing shows (not AI), with the local hub's answers (no Stripe: "Buying credit isn't
available yet"). The whole walk-through takes about 110 s; the app folder (app + sidecar) is the `hearchurch-win-x64`
artifact.

To try it on a PC: download `hearchurch-win-x64` from a green run (Actions → windows → the run → Artifacts), unzip,
run `HearChurch.exe`. SmartScreen warns (unsigned): More info → Run anyway. Or install it: the **hearchurch-windows-dev**
artifact's `Setup.exe` (docs/windows-release.md).

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
| Settings: General (start at login, share logs, Send logs to support…, help links), Billing (credit, free trial, bundles, card, automatic top-up, meetings and payments) or AI (own key: the key, quality), Languages (download, remove, voices with Listen, speech recognition, Wolof speech recognition), Speaking (test sentence, speed per language, volume), Glossary, Congregation (details, listener link, e-mails, devices + Add device, sign out) | `SettingsView.swift`, `AISettings.swift` | `UI/SettingsWindow.cs`, `UI/Settings/*.cs` (W5) |
| About: version + Check for Updates, help links, voice credits (the CC BY attributions), Wolof speech recognition's credit, open-source notices | the About panel, `HelpLinks`, hearchurch.com/credits | `UI/Settings/AboutTab.cs` |
| "This Wi-Fi only": the Windows Firewall rule for the engine (below) | – (macOS asks by itself) | `Core/Firewall.cs` |
| Sparkle updates (dev / public channel) | `Updater` | `Core/Updates.cs` (Velopack), docs/windows-release.md |

Data folder: `%LOCALAPPDATA%\HearChurch` (`profile.json` and `app-settings.json` for the app, everything else the
engine's). For testing: `HEARCHURCH_HOME` (another data folder), `HEARCHURCH_APP_HUB` (another hub, e.g.
`http://127.0.0.1:8000`), `HEARCHURCH_APP_ENGINE` (another sidecar exe).

## Wording

Customer-visible text is the Mac's, word for word, except where it names the Mac: "this Mac" → "this PC",
"Mac default" → "PC default", "a code from another Mac" → "a code from another computer" (and "On a Mac or PC already
signed in…"), "Keychain" → "Windows Credential Manager", the microphone privacy path (Settings → Privacy & security →
Microphone), "Not on this Mac" → "Not on this PC", and in Congregation
"another Mac" → "another computer" (e-mails, devices, Add device's code). Settings has an About tab the Mac doesn't
need (its About panel and the links under General): version, credits, notices. Flags are left out of the language buttons: Windows
has no flag emoji (it shows the two letters).

## Windows Firewall ("This Wi-Fi only")

With "This Wi-Fi only" the phones connect straight to the engine, which then listens on the network (0.0.0.0) on the
port the app picked at launch. Windows Firewall blocks inbound connections unless a rule allows them. The port
changes every launch, so the rule is for the program, not a port: inbound TCP to `engine\hearchurch-engine.exe`
(this install's path), every network type (Windows calls most church Wi-Fis "Public"), named
**Hear Church (This Wi-Fi only)**. The app adds it the first time the operator picks "This Wi-Fi only" and only if it
isn't there yet (`netsh advfirewall firewall show rule`, no elevation needed): it runs
`netsh advfirewall firewall add rule …` elevated, so Windows asks once (UAC: "Do you want to allow this app to make
changes?"). Saying No shows a message in the main window ("Windows Firewall may keep phones on this Wi-Fi from
connecting…"); choosing "This Wi-Fi only" again asks again. "Everyone with the link" (hearchurch.com) needs no rule.
To remove it: `netsh advfirewall firewall delete rule name="Hear Church (This Wi-Fi only)"` (elevated), or Windows
Security → Firewall → Advanced settings → Inbound Rules. If someone pressed Cancel on Windows' own "Allow access"
prompt earlier, Windows may have added a *block* rule for the engine, which wins over this one: delete it there.

## Screenshots in CI

`scripts/windows_screens.py` starts this repo's hub with a fake Claude (as the sermon smoke test), signs up a test
congregation, downloads speech recognition and the English and French voices into a fresh data folder through the
sidecar, asks the hub for a pairing code, and starts `HearChurch.exe` with `HEARCHURCH_APP_SCREENSHOTS`. The app
(`UI/Screenshots.cs`) then really signs in with that code, finishes setup, waits for the engine, plays the sermon
(`samples\sermon.mp3` in the data folder) as a meeting, opens every Settings tab, stops the meeting and quits, rendering each
screen to a PNG (`RenderTargetBitmap`: the window's content, without its title bar). `screens.json` lists every step.
No real hub credentials are used anywhere.

## Not done

- Signing (before any other church).
- The AI tab is in no screenshot: the CI congregation is on credit (the hub decides; the tab shows only for "own_key").
  Buying, the card and automatic top-up open Stripe's pages in the browser, which CI's local hub (no Stripe) can't:
  its Billing tab shows "Buying credit isn't available yet". Try them against the real hub on a PC.
- Listen (Settings → Languages → a language) plays the Mac's recorded samples (`Assets\VoiceSamples`, Media
  Foundation's AAC decoder: absent on Windows "N" editions without the Media Feature Pack). Not heard on the runner.
- The firewall rule's UAC prompt can't be clicked on the runner: try "This Wi-Fi only" on a real PC.
- Notifications use the icon's balloon (shown as a toast on Windows 10/11), not the Windows App SDK notification API.
- Live input on a real PC (the runner has no audio input): how the hearing-assist receiver shows up (line-in vs USB)
  and whether the engine's MME names match the WASAPI names the pre-meeting meter uses (matched by prefix, since MME
  cuts names at 31 characters) needs the office PC (W4).
- The Wolof voice is offered like every other (Download with progress, Moussa and Fatou on the voice page): the
  same SpeechT5 voice as the Apple Silicon Mac, on onnxruntime (docs/windows-engine.md, "The Wolof voice").
