#!/usr/bin/env bash
# Ship a new version of Hear Church to everyone who has it, in two steps, so the DMG you test is the
# one that ships:
#
#   mac/release.sh 0.2.0             build and notarize mac/dist/HearChurch-0.2.0.dmg; nothing public
#   mac/release.sh 0.2.0 --publish   put that DMG on hearchurch.com
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
# Every released DMG stays in mac/dist/: generate_appcast needs the old ones to make deltas.
set -euo pipefail
VERSION="${1:?usage: mac/release.sh <version, e.g. 0.2.0> [--publish]}"
PUBLISH="${2:-}"
cd "$(dirname "$0")"
MAC="$PWD"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
SIGN_ID="Developer ID Application: Harrison Riehle (5473W6M839)"
NOTARY=hearchurch-notary
BUCKET=hearchurch-downloads
URL=https://hearchurch.com/download/
DIST="$MAC/dist"
DMG="$DIST/HearChurch-$VERSION.dmg"
SPARKLE="$MAC/build/dd/SourcePackages/artifacts/sparkle/Sparkle/bin"

step() { echo "== $*"; }
die() { echo "release: $*" >&2; exit 1; }
r2_setup() {
  local key secret
  key="$(security find-generic-password -s hearchurch-r2 2>/dev/null | sed -n 's/.*"acct"<blob>="\(.*\)"/\1/p')"
  secret="$(security find-generic-password -s hearchurch-r2 -w 2>/dev/null)" || die "no R2 keys: see the top of this file"
  : "${CLOUDFLARE_ACCOUNT_ID:?CLOUDFLARE_ACCOUNT_ID is not set}"
  export RCLONE_CONFIG_R2_TYPE=s3 RCLONE_CONFIG_R2_PROVIDER=Cloudflare RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true \
         RCLONE_CONFIG_R2_ACCESS_KEY_ID="$key" RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$secret" \
         RCLONE_CONFIG_R2_ENDPOINT="https://$CLOUDFLARE_ACCOUNT_ID.r2.cloudflarestorage.com"
}

[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "version must look like 1.2.3"
[[ -z "$PUBLISH" || "$PUBLISH" == "--publish" ]] || die "unknown option $PUBLISH"

if [[ -z "$PUBLISH" ]]; then
  step "Checks"
  [[ -e "$DMG" ]] && die "$DMG already exists: publish it with --publish, or delete it to rebuild"
  xcrun notarytool history --keychain-profile "$NOTARY" >/dev/null 2>&1 || die "no notary credentials: see the top of this file"
  r2_setup
  mkdir -p "$DIST"

  step "Version $VERSION"
  BUILD=$(( $(sed -n 's/^ *CURRENT_PROJECT_VERSION: "\([0-9]*\)"/\1/p' project.yml) + 1 ))
  sed -i '' -e "s/MARKETING_VERSION: \"[^\"]*\"/MARKETING_VERSION: \"$VERSION\"/" \
            -e "s/CURRENT_PROJECT_VERSION: \"[^\"]*\"/CURRENT_PROJECT_VERSION: \"$BUILD\"/" project.yml
  echo "build $BUILD"

  SIGN_ID="$SIGN_ID" ./build.sh
  APP="$MAC/build/Hear Church.app"

  # The app downloads these when someone picks the Wolof voice, so the DMG can't be tested without them.
  # Nothing links to them until a copy of the app that names them is out.
  step "Uploading the Wolof libraries"
  rclone copy --stats-one-line --stats 30s --include "*.tar.gz" "$MAC/wolof-libs" "r2:$BUCKET"

  step "DMG"
  STAGE="$MAC/build/dmg-stage"
  rm -rf "$STAGE" && mkdir -p "$STAGE"
  ditto "$APP" "$STAGE/Hear Church.app"
  ln -s /Applications "$STAGE/Applications"
  hdiutil create -quiet -volname "Hear Church" -srcfolder "$STAGE" -fs APFS -format ULFO -ov "$DMG.tmp.dmg"
  rm -rf "$STAGE"
  codesign --sign "$SIGN_ID" --timestamp "$DMG.tmp.dmg"

  step "Notarizing (several minutes for a DMG this size)"
  RESULT="$(xcrun notarytool submit "$DMG.tmp.dmg" --keychain-profile "$NOTARY" --wait --output-format json)"
  STATUS="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["status"])' "$RESULT")"
  if [[ "$STATUS" != "Accepted" ]]; then
    ID="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["id"])' "$RESULT")"
    xcrun notarytool log "$ID" --keychain-profile "$NOTARY" | head -60 >&2
    die "notarization $STATUS"
  fi
  xcrun stapler staple -q "$DMG.tmp.dmg"
  spctl --assess --type open --context context:primary-signature "$DMG.tmp.dmg"
  mv "$DMG.tmp.dmg" "$DMG"
  du -h "$DMG" | sed 's/^/ready to test: /'
  echo "nothing is public yet. To ship this DMG: mac/release.sh $VERSION --publish"
  exit 0
fi

step "Checks"
[[ -e "$DMG" ]] || die "no $DMG: run mac/release.sh $VERSION first"
xcrun stapler validate -q "$DMG" || die "$DMG is not notarized"
r2_setup
BUILD="$(sed -n 's/^ *CURRENT_PROJECT_VERSION: "\([0-9]*\)"/\1/p' project.yml)"

step "Appcast"
"$SPARKLE/generate_appcast" --account hearchurch --download-url-prefix "$URL" \
  --link https://hearchurch.com/download --maximum-deltas 3 "$DIST"
SIZE="$(stat -f%z "$DMG")"
printf '{"version":"%s","build":%s,"file":"%s","size":%s,"date":"%s"}\n' \
  "$VERSION" "$BUILD" "$(basename "$DMG")" "$SIZE" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$DIST/latest.json"

step "Uploading to R2"
# Files first, the feed last: the appcast never points at something not uploaded yet.
rclone copy --stats-one-line --stats 30s --include "*.dmg" --include "*.delta" "$DIST" "r2:$BUCKET"
rclone copyto "$DIST/appcast.xml" "r2:$BUCKET/appcast.xml"
rclone copyto "$DIST/latest.json" "r2:$BUCKET/latest.json"

step "Live"
curl -fsS https://hearchurch.com/appcast.xml | grep -q "<sparkle:shortVersionString>$VERSION<" \
  && echo "appcast: $VERSION" || die "appcast on hearchurch.com does not show $VERSION yet"
curl -fsSI https://hearchurch.com/download/HearChurch.dmg | grep -i -E "^(content-length|content-disposition)"
echo "released $VERSION (build $BUILD). Commit mac/project.yml and tag it: git tag mac-v$VERSION"
