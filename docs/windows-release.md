# Windows releases: installer and automatic updates (W3)

The Windows app (docs/windows-app.md) ships as a [Velopack](https://velopack.io) installer: one `Setup.exe` that
installs for the signed-in Windows user only (no admin rights, no UAC prompt), plus update packages that installed
copies fetch on their own. **Unsigned** for now (testing on the church office PC); it must be signed before any other
church gets it.

| | |
|---|---|
| Installs to | `%LOCALAPPDATA%\HearChurchApp\` (`current\HearChurch.exe`, `current\engine\…`, `Update.exe`, `packages\`) |
| Data (kept on uninstall) | `%LOCALAPPDATA%\HearChurch\` (settings, logs, glossaries, downloaded models) and the sign-in in Windows Credential Manager (`HearChurch:deviceToken`, `HearChurch:apiKey`) |
| Shortcuts | "Hear Church" on the desktop and in the Start menu |
| Uninstall | Settings → Apps → Installed apps → Hear Church → ⋯ → Uninstall (Windows 10: Apps & features → Hear Church → Uninstall) |
| Windows | 10 (1809+) and 11, x64 |

The package id is **HearChurchApp**, not HearChurch: Velopack installs to `%LOCALAPPDATA%\<package id>` and deletes
that whole folder on uninstall, which would take the data folder with it.

## What updates do

Like the Mac's Sparkle, without asking:

- The app checks its channel's feed at launch and then every 6 hours, only while no meeting runs, and downloads a newer
  version in the background. A meeting starting stops a download; it is tried again after the meeting.
- A downloaded update is installed when Hear Church quits (icon menu → Quit; quitting during a meeting asks first and
  stops the meeting), silently, without restarting it. If Hear Church never quits (the PC is shut down with it
  running), the update goes in at its next start, before any window opens, and Hear Church starts again by itself.
  It is never installed during a meeting: a second start while it runs (the shortcut clicked again) only brings the
  window to the front. (Velopack's own "apply at startup" is off for exactly that reason; `App.OnLaunched` does it, in
  the first copy only.)
- "Check for Updates…" in the notification-area icon's menu checks now and answers in a notification; Settings →
  General shows the version ("Hear Church 0.4.0 (12) for Windows (test build, dev updates)"), a "Check for Updates"
  button, and "… is ready. It is installed the next time Hear Church quits." when one is waiting.
- `logs\updates.log` in the data folder says what happened. A copy that was not installed (the unzipped
  `hearchurch-win-x64` artifact) never updates.
- Uninstalling also removes "Open Hear Church when this PC starts" (Velopack's uninstall hook, `Updates.BeforeUninstall`).

## Channels and feeds

Two channels, as on the Mac: **dev** (Harrison's office PC; dev builds may go there freely) and **public** (every
church). The channel is baked into the package (`vpk pack --channel`), and the app reads its feed from

    https://hearchurch.com/download/windows/<channel>/releases.<channel>.json

and the packages beside it. They are to live in the R2 bucket `hearchurch-downloads` under `windows/<channel>/`.
For testing, `"UpdateURL"` in `app-settings.json` points a copy at another feed (a URL or a local folder; CI uses one).

A PC moves from dev to public by installing the public `Setup.exe` over it (same package id: settings, data and sign-in
stay); updates then follow the public feed.

### The download Worker needs (not done here)

`download/src/index.js` serves `/download/<file>` only for flat names matching `[\w. -]+\.(dmg|delta|tar\.gz)`: a request
for `/download/windows/dev/releases.dev.json` is a 404 today. The route (`hearchurch.com/download*` in `wrangler.toml`)
already reaches the Worker; it needs one more branch, before the generic one:

- `/download/windows/(dev|public)/<name>` with `<name>` matching `[\w.-]+\.(json|nupkg|exe)` (plus `RELEASES-<channel>`,
  which older Velopack clients read) → R2 key `windows/<channel>/<name>`, via the existing `file()` (Range support for
  the big packages).
- Cache: `releases.*.json`, `assets.*.json` and `RELEASES-*` `max-age=300` (as `appcast.xml`); `*.nupkg`
  `public, max-age=31536000, immutable` (a version's package never changes); `*-Setup.exe` `no-store` (same name for
  every version), content type `application/octet-stream`, `content-disposition: attachment`.
- For the site (W6): a stable `/download/HearChurch-Setup.exe` → `windows/public/HearChurchApp-public-Setup.exe`,
  counted like the DMG (`countDownload(env, "<version>-windows")`).

## Versions and build numbers

Windows shares the Mac's **version** (0.4.0 on both) and keeps its **own build number**: the GitHub Actions run number of
the build (it only ever goes up). Velopack versions are SemVer, so:

| channel | package version | file version | shown in Settings |
|---|---|---|---|
| dev | `0.4.0-dev.57` | 0.4.0.57 | Hear Church 0.4.0 (57) |
| public | `0.4.0` | 0.4.0.57 | Hear Church 0.4.0 (57) |

A dev build can be repeated at one version (a higher build), as on the Mac. `0.4.0` sorts after every `0.4.0-dev.N`, so
the public release is newer than that version's dev builds; after it, the next dev builds need the next version
(`0.4.1-dev.N`). A public release can't be repeated at the same version: fix forward with the next patch version.

## Cutting a release

Nothing below has been run yet: no Windows release exists. Uploading needs the R2 keys (as `mac/release.sh`), from a
Mac or Linux machine with `gh` and `rclone`.

1. **Build.** Run the workflow on the branch with the version and channel (build empty = this run's number):

       gh workflow run windows.yml --ref chat/hc-win -f version=0.4.0 -f channel=dev     # or channel=public

   It builds the app and the sidecar, fetches the channel's latest release from hearchurch.com so Velopack can make a
   delta package from it (skipped while there is none), packages, uploads the artifact
   **hearchurch-windows-&lt;channel&gt;**, and installs, updates and uninstalls it on the runner. Use it only if the run
   is green.

2. **Try it.** Download the artifact, run `HearChurchApp-<channel>-Setup.exe` on a PC (see SmartScreen below).

       gh run download <run id> -n hearchurch-windows-dev -D windows-release/dev

3. **Publish** (dev: pre-approved; public: Harrison's yes first). Packages first, the feed last, so no PC ever sees a
   version whose package isn't there yet:

       ch=dev; dir=windows-release/$ch
       rclone copy --include "*.nupkg" --include "*Setup.exe" "$dir" "r2:hearchurch-downloads/windows/$ch/"
       rclone copy --exclude "*.nupkg" --exclude "*Setup.exe" "$dir" "r2:hearchurch-downloads/windows/$ch/"

   (`r2:` configured as in `mac/release.sh`'s `r2_setup`.) Installed PCs on that channel get it within 6 hours, or at
   "Check for Updates…". The Worker change above must be deployed first.

   To give dev PCs a public release too (as the Mac's dev Macs get public builds), run step 1 a second time with
   `channel=dev` at the same version and publish that to `windows/dev/`.

4. **Keep the folder.** Velopack only needs the latest full package to make the next delta (step 1 downloads it), so
   nothing has to be kept locally.

## Testing in CI

The `installer` job of `.github/workflows/windows.yml` takes the app job's folder, packages it (`vpk pack`, Velopack
1.2.161, the same as the app's `Velopack` NuGet package: keep them equal), uploads the release, then runs
`scripts/windows_install_test.ps1` on the runner:

1. `Setup.exe --silent`: files in `%LOCALAPPDATA%\HearChurchApp`, the Apps entry ("Hear Church", version), both
   shortcuts;
2. the app starts (Setup starts it) and its engine, the installed `engine\hearchurch-engine.exe`, answers on
   `/api/status`;
3. the update path: the job also packages a newer test version (`<next patch>-test.<build>`) of the same files into a
   local feed (with a delta package), and the data folder's `UpdateURL` points there. The app finds it at launch and
   downloads it; the test checks the old version is still installed, quits the app (`HearChurch.exe --quit`, which asks
   the running copy to quit unless a meeting runs), waits for the new version to be installed, starts it and checks
   its engine answers and the app reports the new version;
4. uninstall with the Apps entry's `QuietUninstallString` (start at login switched on first): no files left in
   `%LOCALAPPDATA%\HearChurchApp`, no shortcuts, no Apps entry, no `Run` entry, the data folder still there.

`install.json` (each step, timed) goes to the job summary; it and the app's logs are the **install-test** artifact.

## SmartScreen (unsigned installer)

Downloaded from the web, the unsigned `Setup.exe` gets two warnings. Expected; fine on the office PC.

**Edge, while downloading:** "*HearChurchApp-dev-Setup.exe* isn't commonly downloaded. Make sure you trust
*HearChurchApp-dev-Setup.exe* before you open it." → hover the download, **⋯** → **Keep** → "Show more" → **Keep anyway**.
(Chrome usually downloads it without asking.)

**Windows, when opening it:** a blue window

> **Windows protected your PC**
> Microsoft Defender SmartScreen prevented an unrecognized app from starting. Running this app might put your PC at risk.
> More info

→ click **More info**, which adds "App: HearChurchApp-dev-Setup.exe, Publisher: Unknown publisher" → **Run anyway**.

No admin password is asked. The installer then runs on its own (a short progress window) and starts Hear Church;
updates later install without any warning (they are not downloaded by a browser). If the PC's policy blocks unknown apps
outright (no "Run anyway" button), it is managed by someone else (diocese or stake IT): it needs the signed installer.
