#!/usr/bin/env bash
# Run ONCE, in Terminal on the Mac (it needs the login keychain unlocked and may ask for passwords):
#
#   mac/release-keychain.sh
#
# Makes a separate keychain that holds only what a release needs, so mac/release.sh also works over SSH:
#   - the Developer ID signing identity (copied from the login keychain)
#   - the notarytool profile "hearchurch-notary"
#   - the R2 upload key ("hearchurch-r2")
#   - the Sparkle EdDSA private key ("hearchurch-sparkle")
# Its password is random and kept in ~/.config/hearchurch/release-keychain.pass (only you can read it);
# release.sh unlocks the keychain with it. Anyone logged in as you can therefore sign and publish
# Hear Church releases, which is the usual trade-off for build machines.
#
# Safe to run again: it rebuilds the release keychain from scratch.
set -euo pipefail
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
KC="$HOME/Library/Keychains/hearchurch-release.keychain-db"
PASSFILE="$HOME/.config/hearchurch/release-keychain.pass"
SIGN_ID="Developer ID Application: Harrison Riehle (5473W6M839)"
SPARKLE="$PWD/build/dd/SourcePackages/artifacts/sparkle/Sparkle/bin"
NOTARY=hearchurch-notary
step() { echo "== $*"; }
die() { echo "release-keychain: $*" >&2; exit 1; }

[[ -x "$SPARKLE/generate_keys" ]] || die "Sparkle tools missing: build once with mac/build.sh"
security find-identity -v -p codesigning login.keychain-db | grep -q "$SIGN_ID" || die "no \"$SIGN_ID\" in the login keychain"

step "Keychain"
mkdir -p "$(dirname "$PASSFILE")" && chmod 700 "$(dirname "$PASSFILE")"
if [[ -e "$KC" ]]; then
  security list-keychains -d user -s $(security list-keychains -d user | tr -d '"' | grep -v hearchurch-release)
  security delete-keychain "$KC"
fi
( umask 077; openssl rand -base64 32 | tr -d '\n' > "$PASSFILE" )
PASS="$(cat "$PASSFILE")"
security create-keychain -p "$PASS" "$KC"
security set-keychain-settings "$KC"            # no auto-lock timer; it still locks at logout/sleep
security unlock-keychain -p "$PASS" "$KC"

step "Signing identity (macOS may ask for your login password, maybe twice)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
P12PASS="$(openssl rand -hex 16)"
security export -k login.keychain-db -t identities -f pkcs12 -P "$P12PASS" -o "$TMP/all.p12"
security import "$TMP/all.p12" -k "$KC" -P "$P12PASS" -T /usr/bin/codesign -T /usr/bin/security >/dev/null
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$PASS" "$KC" >/dev/null
security find-identity -v -p codesigning "$KC" | grep -q "$SIGN_ID" || die "the identity did not import"
# Keep only the Developer ID identity: a second "Apple Development" copy would make other builds ambiguous.
security find-identity -v -p codesigning "$KC" | grep -v "$SIGN_ID" | sed -n 's/^ *[0-9]*) \([0-9A-F]\{40\}\) .*/\1/p' \
  | while read -r h; do security delete-identity -Z "$h" "$KC" >/dev/null 2>&1 || true; done

step "R2 upload key"
R2_KEY="$(security find-generic-password -s hearchurch-r2 login.keychain-db 2>/dev/null | sed -n 's/.*"acct"<blob>="\(.*\)"/\1/p')"
R2_SECRET="$(security find-generic-password -s hearchurch-r2 -w login.keychain-db)" || die "no hearchurch-r2 in the login keychain"
security add-generic-password -s hearchurch-r2 -a "$R2_KEY" -w "$R2_SECRET" -T /usr/bin/security "$KC"

step "Sparkle key"
"$SPARKLE/generate_keys" --account hearchurch -x "$TMP/sparkle.key" >/dev/null
security add-generic-password -s hearchurch-sparkle -a hearchurch -w "$(cat "$TMP/sparkle.key")" -T /usr/bin/security "$KC"

step "Notary profile"
# Copy the saved profile if macOS lets us read it (click Allow); otherwise store it again, which asks for an
# app-specific password from appleid.apple.com → Sign-In and Security → App-Specific Passwords.
NSVC=com.apple.gke.notary.tool; NACCT="com.apple.gke.notary.tool.saved-creds.$NOTARY"
if NBLOB="$(security find-generic-password -s "$NSVC" -a "$NACCT" -w login.keychain-db 2>/dev/null)" && [[ -n "$NBLOB" ]]; then
  security add-generic-password -s "$NSVC" -a "$NACCT" -w "$NBLOB" -A "$KC"
fi
if ! xcrun notarytool history --keychain-profile "$NOTARY" --keychain "$KC" >/dev/null 2>&1; then
  security delete-generic-password -s "$NSVC" -a "$NACCT" "$KC" >/dev/null 2>&1 || true
  xcrun notarytool store-credentials "$NOTARY" --apple-id hpriehle@gmail.com --team-id 5473W6M839 --keychain "$KC"
fi

step "Check"
security lock-keychain "$KC"
security unlock-keychain -p "$PASS" "$KC"
xcrun notarytool history --keychain-profile "$NOTARY" --keychain "$KC" >/dev/null && echo "notary: ok"
security find-generic-password -s hearchurch-sparkle -w "$KC" >/dev/null && echo "sparkle key: ok"
security find-generic-password -s hearchurch-r2 -w "$KC" >/dev/null && echo "r2 key: ok"
echo "done: mac/release.sh now uses $KC and works over SSH"
