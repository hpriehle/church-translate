#!/usr/bin/env bash
# Build "Hear Church.app" with the Python engine inside it.
#
#   mac/build.sh                 → mac/build/Hear Church.app (signed with your Apple Development identity)
#   SIGN_ID="Developer ID Application" mac/build.sh     (sign for distribution: mac/release.sh does this)
#   mac/build.sh --app-only      → rebuild the Swift app and engine code, reuse the last bundled Python
#
# The app contains a standalone CPython (python-build-standalone, via uv) with the locked dependencies,
# and the engine code (app/, web/, models shipped in the repo). Voices and speech models are downloaded
# on first use, not bundled.
set -euo pipefail
cd "$(dirname "$0")"
MAC="$PWD"
REPO="$(cd .. && pwd)"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
SIGN_ID="${SIGN_ID:-Apple Development}"
PY_VERSION="$(cat "$REPO/.python-version")"
OUT="$MAC/build/Hear Church.app"
STAGE="$MAC/build/engine-stage"     # bundled Python + engine, reused by --app-only

step() { echo "== $*"; }

if [[ "${1:-}" != "--app-only" || ! -d "$STAGE/python" ]]; then
  step "Standalone Python $PY_VERSION"
  uv python install --quiet "$PY_VERSION"
  PY_SRC="$(dirname "$(dirname "$(uv python find --no-project --system --managed-python "$PY_VERSION")")")"
  rm -rf "$STAGE" && mkdir -p "$STAGE"
  cp -R "$PY_SRC" "$STAGE/python"
  rm -f "$STAGE/python/lib/python$PY_VERSION/EXTERNALLY-MANAGED"
  PY="$STAGE/python/bin/python3"

  step "Python packages (from uv.lock)"
  (cd "$REPO" && uv export --quiet --frozen --no-dev --no-hashes --no-emit-project -o "$STAGE/requirements.txt")
  uv pip install --quiet --python "$PY" --link-mode copy --compile-bytecode -r "$STAGE/requirements.txt"
  # Not needed at run time; saves space.
  rm -rf "$STAGE/python/lib/python$PY_VERSION/test" "$STAGE/python/lib/python$PY_VERSION/idlelib" \
         "$STAGE/python/lib/python$PY_VERSION/tkinter" "$STAGE/python/lib/python$PY_VERSION/turtledemo"
  find "$STAGE/python" -name "*.a" -delete
  # Links that point back into uv's copy (e.g. bin/python) must stay inside the app: make them relative.
  find "$STAGE/python" -type l | while read -r l; do
    t="$(readlink "$l")"; if [[ "$t" == /* ]]; then ln -sfn "$(basename "$t")" "$l"; fi
  done

fi

# The engine code is small, so it is copied every time (--app-only too).
step "Engine code"
mkdir -p "$STAGE/engine/models"
rsync -a --delete --exclude "__pycache__" "$REPO/app" "$REPO/web" "$STAGE/engine/"
rsync -a "$REPO/models/silero_vad.onnx" "$REPO/models/wolof" "$STAGE/engine/models/"
cp "$REPO/config.toml" "$REPO/glossary.json" "$REPO/glossary.wo.json" "$REPO/LICENSE" "$REPO/THIRD_PARTY_NOTICES.md" "$STAGE/engine/"
"$STAGE/python/bin/python3" -m compileall -q "$STAGE/engine/app"

step "Swift app"
xcodegen generate --quiet
xcodebuild -project HearChurch.xcodeproj -scheme HearChurch -configuration Release -derivedDataPath build/dd \
  -quiet build
rm -rf "$OUT"
ditto "build/dd/Build/Products/Release/Hear Church.app" "$OUT"
ditto "$STAGE/python" "$OUT/Contents/Resources/python"
ditto "$STAGE/engine" "$OUT/Contents/Resources/engine"
# The engine shows this as its version; it never runs git on a congregation's Mac.
sed -n 's/^ *MARKETING_VERSION: "\(.*\)"/\1/p' project.yml > "$OUT/Contents/Resources/engine/VERSION"

step "Signing with \"$SIGN_ID\""
# Every Mach-O file inside Resources, innermost first, then the Python interpreter, then the app.
# (Python loads modules signed by us only, but some packages need executable memory: hence its entitlements.)
# Notarizing needs a secure timestamp, which needs the network: only Developer ID builds get one.
if [[ "$SIGN_ID" == "Developer ID"* ]]; then TS="--timestamp"; else TS="--timestamp=none"; fi
LOG="$MAC/build/sign.log"
: > "$LOG"
sign_failed() { grep -v "replacing existing signature" "$LOG" >&2; echo "signing failed" >&2; exit 1; }
sign() { codesign --force --options runtime $TS --sign "$SIGN_ID" "$@" 2>> "$LOG" || sign_failed; }
# Found by their first bytes, not their names: torch ships programs with no extension in torch/bin.
machos() {
  "$STAGE/python/bin/python3" -I -c '
import os, sys
MAGIC = {bytes.fromhex(m) for m in ("feedface", "feedfacf", "cefaedfe", "cffaedfe", "cafebabe", "bebafeca")}
for folder, _, files in os.walk(sys.argv[1]):
    for name in files:
        path = os.path.join(folder, name)
        if not os.path.islink(path) and open(path, "rb").read(4) in MAGIC:
            print(path, end="\0")
' "$1"
}
machos "$OUT/Contents/Resources" > "$MAC/build/machos.txt"
python3 -I -c '
import sys
paths = open(sys.argv[1], "rb").read().split(b"\0")
sys.stdout.buffer.write(b"".join(p + b"\0" for p in paths if p and b"/Resources/python/bin/" not in p))
' "$MAC/build/machos.txt" | xargs -0 -n 200 codesign --force --options runtime $TS --sign "$SIGN_ID" 2>> "$LOG" || sign_failed
find "$OUT/Contents/Resources/python/bin" -type f -perm -u+x -print0 | while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q Mach-O; then sign --entitlements "$MAC/HearChurch/Python.entitlements" "$f"; fi
done
# Sparkle's helpers, as its docs list them (Xcode signed them with the development identity).
SPK="$OUT/Contents/Frameworks/Sparkle.framework/Versions/B"
sign "$SPK/XPCServices/Installer.xpc"
sign --preserve-metadata=entitlements "$SPK/XPCServices/Downloader.xpc"
sign "$SPK/Autoupdate"
sign "$SPK/Updater.app"
sign "$OUT/Contents/Frameworks/Sparkle.framework"
sign --entitlements "$MAC/HearChurch/HearChurch.entitlements" "$OUT"
codesign --verify --deep --strict "$OUT"
# --deep skips what sits in Resources, so check every binary there too. Apple rejects any left unsigned.
while IFS= read -r -d '' f; do
  codesign --verify --strict "$f" 2>/dev/null || { echo "not signed: $f" >&2; exit 1; }
done < "$MAC/build/machos.txt"
rm -f "$MAC/build/machos.txt"
echo "signed"

du -sh "$OUT" | sed 's/^/size: /'
echo "built: $OUT"
