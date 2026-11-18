# Hear Church (iPhone and Android)

Expo app for listeners: find your congregation once (it's remembered), then live captions and the
translated voice, playing in the background with lock-screen controls. On iPhone, **English (room
audio)** appears when the office Live English sender (`../live/sender.py`) is on the same Wi-Fi;
it plays through the native `modules/live-english` (UDP, libopus, ~30 ms).

Server: `src/lib/config.ts` (hearchurch.com). Same endpoints as the web listener:
`/api/hc/search`, `/api/live`, `/c/<slug>/ws/listen`, `/c/<slug>/stream/<lang>.mp3`.

- Typecheck: `npx tsc --noEmit`
- Run on the Mac: `npx expo run:ios` (needs Xcode); Android: `npx expo run:android`
- TestFlight: bump `expo.ios.buildNumber` in app.json, then `./testflight.sh` in Terminal on the Mac.

Not yet: Live English on Android, push notifications, microphone mode, one-on-one translation,
turning Live English on from the operator page.
