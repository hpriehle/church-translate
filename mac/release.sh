#!/usr/bin/env bash
# Ship a new version of Hear Church to everyone who has it, in two steps, so the DMG you test is the
# one that ships:
#
#   mac/release.sh 0.2.0             build and notarize mac/dist/HearChurch-0.2.0.dmg; nothing public
#   mac/release.sh 0.2.0 --publish   put that DMG on hearchurch.com
#   mac/release.sh 0.2.0 --intel [--publish]   the same for Intel Macs, after the Apple Silicon build of
#     that version: mac/dist-intel/HearChurch-Intel-0.2.0.dmg, feed /appcast-intel.xml, full-DMG updates
#   mac/release.sh 0.3.0 --dev [--intel] [--publish]   a test build for the dev channel only:
#     mac/dist/HearChurch-0.3.0-dev<build>.dmg. Its appcast item gets <sparkle:channel>dev</sparkle:channel>
#     (generate_appcast --channel dev), in the same feed (appcast.xml / appcast-intel.xml). Sparkle 2
#     (every copy ever shipped: 2.10.1+) skips items whose channel the app doesn't allow, so only Macs
#     with dev updates turned on see it:
#       defaults write com.hearchurch.HearChurch HCDevUpdates -bool true    (false or delete: public only)
#     then Check for Updates. latest.json and /download/HearChurch.dmg are not touched. Each dev build
#     bumps the build number, so a dev build can be repeated at the same version, and the public release
#     of that version later (a higher build) reaches dev Macs too. Intel: --dev --intel after the
#     Apple Silicon --dev build, as for public releases.
#
# The first bumps the version in project.yml, builds with the Developer ID identity, makes a DMG,
# notarizes and staples it. The second writes Sparkle's appcast (signed with the "hearchurch" EdDSA key
# in the login Keychain, with deltas from earlier versions) and uploads it all to R2, where the
# hearchurch-download Worker (download/) serves hearchurch.com/download and /appcast.xml. Running
# copies find the update within a day and install it when they quit.
#
# Needs, once per Mac:
#   xcrun notarytool store-credentials hearchurch-notary --apple-id hpriehle@gmail.com --team-id 5473W6M839
#   security add-generic-password -s hearchurch-r2 -a <R2 Access Key ID> -w    (asks for the secret)
#
# Then, to release over SSH too, copy those into a release-only keychain (once, in Terminal):
#   mac/release-keychain.sh
#
# Every released DMG stays in mac/dist/: generate_appcast needs the old ones to make deltas.
set -euo pipefail
VERSION="${1:?usage: mac/release.sh <version, e.g. 0.2.0> [--intel] [--dev] [--publish]}"
shift
PUBLISH=""
INTEL=""
DEV=""
for arg in "$@"; do
  case "$arg" in
    --publish) PUBLISH="$arg" ;;
    --intel) INTEL=1 ;;
    --dev) DEV=1 ;;
    *) echo "release: unknown option $arg" >&2; exit 1 ;;
  esac
done
cd "$(dirname "$0")"
MAC="$PWD"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
SIGN_ID="Developer ID Application: Harrison Riehle (5473W6M839)"
NOTARY=hearchurch-notary
BUCKET=hearchurch-downloads
URL=https://hearchurch.com/download/
DIST="$MAC/dist"
DMG="$DIST/HearChurch-$VERSION.dmg"
DMG_BASE=HearChurch
SPARKLE="$MAC/build/dd/SourcePackages/artifacts/sparkle/Sparkle/bin"
APP="$MAC/build/Hear Church.app"
FEED=appcast.xml
LATEST=latest.json
PUBLIC_DMG=HearChurch.dmg
DELTAS=3
if [[ -n "$INTEL" ]]; then
  DIST="$MAC/dist-intel"
  DMG_BASE=HearChurch-Intel
  SPARKLE="$MAC/build/dd-intel/SourcePackages/artifacts/sparkle/Sparkle/bin"
  APP="$MAC/build/intel/Hear Church.app"
  FEED=appcast-intel.xml
  LATEST=latest-intel.json
  PUBLIC_DMG=HearChurch-Intel.dmg
  DELTAS=0     # generate_appcast's delta names would collide with the Apple Silicon ones in R2
  export HC_ARCH=intel
fi

RELEASE_KC="$HOME/Library/Keychains/hearchurch-release.keychain-db"   # made by mac/release-keychain.sh
step() { echo "== $*"; }
die() { echo "release: $*" >&2; exit 1; }
# Secrets come from the release keychain when it exists (works over SSH), else from the login keychain.
KC_ARGS=()
if [[ -e "$RELEASE_KC" ]]; then
  security unlock-keychain -p "$(cat "$HOME/.config/hearchurch/release-keychain.pass")" "$RELEASE_KC" \
    || die "cannot unlock $RELEASE_KC"
  KC_ARGS=("$RELEASE_KC")
  export SIGN_KEYCHAIN="$RELEASE_KC"
  # codesign looks identities up through the search list: add the release keychain for this run only.
  ORIG_KCS="$(security list-keychains -d user | tr -d '"' | grep -v hearchurch-release | xargs)"
  security list-keychains -d user -s "$RELEASE_KC" $ORIG_KCS
  trap 'security list-keychains -d user -s $ORIG_KCS' EXIT
fi
NOTARY_ARGS=(--keychain-profile "$NOTARY")
[[ -n "${SIGN_KEYCHAIN:-}" ]] && NOTARY_ARGS+=(--keychain "$SIGN_KEYCHAIN")
r2_setup() {
  local key secret
  key="$(security find-generic-password -s hearchurch-r2 ${KC_ARGS[@]+"${KC_ARGS[@]}"} 2>/dev/null | sed -n 's/.*"acct"<blob>="\(.*\)"/\1/p')"
  secret="$(security find-generic-password -s hearchurch-r2 -w ${KC_ARGS[@]+"${KC_ARGS[@]}"} 2>/dev/null)" || die "no R2 keys: see the top of this file"
  : "${CLOUDFLARE_ACCOUNT_ID:?CLOUDFLARE_ACCOUNT_ID is not set}"
  export RCLONE_CONFIG_R2_TYPE=s3 RCLONE_CONFIG_R2_PROVIDER=Cloudflare RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true \
         RCLONE_CONFIG_R2_ACCESS_KEY_ID="$key" RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$secret" \
         RCLONE_CONFIG_R2_ENDPOINT="https://$CLOUDFLARE_ACCOUNT_ID.r2.cloudflarestorage.com"
}

[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "version must look like 1.2.3"
# The build number this run is about: the next one when building for Apple Silicon (step "Version"
# bumps it), else the one in project.yml. Dev DMGs carry it in their name, so dev builds can repeat.
BUILD="$(sed -n 's/^ *CURRENT_PROJECT_VERSION: "\([0-9]*\)"/\1/p' project.yml)"
[[ -z "$PUBLISH" && -z "$INTEL" ]] && BUILD=$(( BUILD + 1 ))
DMG="$DIST/$DMG_BASE-$VERSION${DEV:+-dev$BUILD}.dmg"
FLAGS="${INTEL:+--intel }${DEV:+--dev }"

if [[ -z "$PUBLISH" ]]; then
  step "Checks"
  [[ -e "$DMG" ]] && die "$DMG already exists: publish it with --publish, or delete it to rebuild"
  xcrun notarytool history "${NOTARY_ARGS[@]}" >/dev/null 2>&1 || die "no notary credentials: see the top of this file"
  r2_setup
  mkdir -p "$DIST"

  step "Version $VERSION"
  if [[ -n "$INTEL" ]]; then
    grep -q "MARKETING_VERSION: \"$VERSION\"" project.yml || die "project.yml is not at $VERSION: Intel builds use the version the Apple Silicon release set"
  else
    sed -i '' -e "s/MARKETING_VERSION: \"[^\"]*\"/MARKETING_VERSION: \"$VERSION\"/" \
              -e "s/CURRENT_PROJECT_VERSION: \"[^\"]*\"/CURRENT_PROJECT_VERSION: \"$BUILD\"/" project.yml
    echo "build $BUILD"
  fi

  SIGN_ID="$SIGN_ID" ./build.sh

  # The app downloads these when someone picks the Wolof voice, so the DMG can't be tested without them.
  # Nothing links to them until a copy of the app that names them is out.
  if [[ -z "$INTEL" ]]; then
    step "Uploading the Wolof libraries"
    rclone copy --stats-one-line --stats 30s --include "*.tar.gz" "$MAC/wolof-libs" "r2:$BUCKET"
  fi

  step "DMG"
  STAGE="$MAC/build/dmg-stage"
  rm -rf "$STAGE" && mkdir -p "$STAGE"
  ditto "$APP" "$STAGE/Hear Church.app"
  ln -s /Applications "$STAGE/Applications"
  hdiutil create -quiet -volname "Hear Church" -srcfolder "$STAGE" -fs APFS -format ULFO -ov "$DMG.tmp.dmg"
  rm -rf "$STAGE"
  codesign --sign "$SIGN_ID" ${SIGN_KEYCHAIN:+--keychain "$SIGN_KEYCHAIN"} --timestamp "$DMG.tmp.dmg"

  step "Notarizing (several minutes for a DMG this size)"
  RESULT="$(xcrun notarytool submit "$DMG.tmp.dmg" "${NOTARY_ARGS[@]}" --wait --output-format json)"
  STATUS="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["status"])' "$RESULT")"
  if [[ "$STATUS" != "Accepted" ]]; then
    ID="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["id"])' "$RESULT")"
    xcrun notarytool log "$ID" "${NOTARY_ARGS[@]}" | head -60 >&2
    die "notarization $STATUS"
  fi
  xcrun stapler staple -q "$DMG.tmp.dmg"
  spctl --assess --type open --context context:primary-signature "$DMG.tmp.dmg"
  mv "$DMG.tmp.dmg" "$DMG"
  du -h "$DMG" | sed 's/^/ready to test: /'
  echo "nothing is public yet. To ship this DMG: mac/release.sh $VERSION $FLAGS--publish"
  exit 0
fi

step "Checks"
[[ -e "$DMG" ]] || die "no $DMG: run mac/release.sh $VERSION $FLAGS first"
xcrun stapler validate -q "$DMG" || die "$DMG is not notarized"
# The appcast gets exactly this build (--versions below), so it must be the one in the DMG: a later
# build (a dev build, say) moves project.yml on, and another DMG in $DIST must never go out with it.
MNT="$(mktemp -d)"
hdiutil attach -quiet -nobrowse -readonly -mountpoint "$MNT" "$DMG"
DMG_BUILD="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$MNT/Hear Church.app/Contents/Info.plist")"
hdiutil detach -quiet "$MNT"
[[ "$DMG_BUILD" == "$BUILD" ]] || die "$DMG is build $DMG_BUILD but project.yml says $BUILD: set CURRENT_PROJECT_VERSION back to $DMG_BUILD to publish it"
r2_setup

# The GPL/LGPL parts' source (Piper, eSpeak NG, LAME, libsoxr, and Intel's FFmpeg/x264/x265), offered in
# THIRD_PARTY_NOTICES.md at hearchurch.com/source. Built before anything goes up, so a stale pin in
# mac/oss-sources.json stops the release here. Dev builds skip it.
if [[ -z "$DEV" ]]; then
  step "Open-source archive"
  OSS="$(python3 -I "$MAC/oss_sources.py" "$VERSION")" || die "the open-source archive did not build: see above"
  du -h "$OSS"
fi

step "Appcast"
APPCAST=("$SPARKLE/generate_appcast" --download-url-prefix "$URL" --link https://hearchurch.com/download
         --maximum-deltas "$DELTAS" --versions "$BUILD")
[[ -n "$DEV" ]] && APPCAST+=(--channel dev)   # only the new item; existing items keep their channel
if [[ -n "${SIGN_KEYCHAIN:-}" ]]; then
  security find-generic-password -s hearchurch-sparkle -w "$SIGN_KEYCHAIN" | "${APPCAST[@]}" --ed-key-file - "$DIST"
else
  "${APPCAST[@]}" --account hearchurch "$DIST"
fi
# Check the new item's channel before anything goes up: dev must say dev, public must say nothing.
CHANNEL="$(python3 - "$DIST/$FEED" "$BUILD" <<'PY'
import sys, xml.etree.ElementTree as ET
S = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"
items = [i for i in ET.parse(sys.argv[1]).iter("item") if (i.findtext(S + "version") or "").strip() == sys.argv[2]]
print("missing" if not items else (items[0].findtext(S + "channel") or "public").strip())
PY
)"
EXPECT=public; [[ -n "$DEV" ]] && EXPECT=dev
[[ "$CHANNEL" == "$EXPECT" ]] || die "$FEED: build $BUILD is '$CHANNEL', expected '$EXPECT'; nothing uploaded"
if [[ -z "$DEV" ]]; then     # the download page and /download/HearChurch.dmg: public releases only
  SIZE="$(stat -f%z "$DMG")"
  printf '{"version":"%s","build":%s,"file":"%s","size":%s,"date":"%s"}\n' \
    "$VERSION" "$BUILD" "$(basename "$DMG")" "$SIZE" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$DIST/$LATEST"
fi

step "Uploading to R2"
# Files first, the feed last: the appcast never points at something not uploaded yet. The source archive
# goes before the DMG, so the app is never on offer without it (the Worker serves source/* at /source/).
[[ -z "$DEV" ]] && rclone copyto "$OSS" "r2:$BUCKET/source/$(basename "$OSS")"
rclone copy --stats-one-line --stats 30s --include "*.dmg" --include "*.delta" "$DIST" "r2:$BUCKET"
rclone copyto "$DIST/$FEED" "r2:$BUCKET/$FEED"     # generate_appcast names it after the app's SUFeedURL
[[ -z "$DEV" ]] && rclone copyto "$DIST/$LATEST" "r2:$BUCKET/$LATEST"

step "Live"
curl -fsS "https://hearchurch.com/$FEED" | grep -q "<sparkle:version>$BUILD<" \
  && echo "appcast: $VERSION (build $BUILD)${DEV:+ on the dev channel}" || die "$FEED on hearchurch.com does not show build $BUILD yet"
if [[ -n "$DEV" ]]; then
  echo "dev build $VERSION (build $BUILD) is out to Macs with HCDevUpdates on. Commit mac/project.yml."
  exit 0
fi
curl -fsSI "https://hearchurch.com/download/$PUBLIC_DMG" | grep -i -E "^(content-length|content-disposition)"
curl -fsSI "https://hearchurch.com/source/$(basename "$OSS")" | grep -i "^content-length" \
  || echo "warning: hearchurch.com/source/$(basename "$OSS") is not served: deploy the download/ Worker" >&2
echo "released $VERSION (build $BUILD). Commit mac/project.yml and tag it: git tag mac-v$VERSION"
