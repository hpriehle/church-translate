#!/usr/bin/env bash
# Build "Hear Church.app" with the Python engine inside it.
#
#   mac/build.sh                 → mac/build/Hear Church.app (signed with your Apple Development identity)
#   SIGN_ID="Developer ID Application" mac/build.sh     (sign for distribution; notarizing is Phase 6)
#   mac/build.sh --app-only      → rebuild the Swift app, reuse the last bundled engine
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

  step "Engine code"
  mkdir -p "$STAGE/engine/models"
  rsync -a --exclude "__pycache__" "$REPO/app" "$REPO/web" "$STAGE/engine/"
  rsync -a "$REPO/models/silero_vad.onnx" "$REPO/models/wolof" "$STAGE/engine/models/"
  cp "$REPO/config.toml" "$REPO/glossary.json" "$REPO/glossary.wo.json" "$REPO/LICENSE" "$REPO/THIRD_PARTY_NOTICES.md" "$STAGE/engine/"
  "$PY" -m compileall -q "$STAGE/engine/app"
fi

step "Swift app"
xcodegen generate --quiet
xcodebuild -project HearChurch.xcodeproj -scheme HearChurch -configuration Release -derivedDataPath build/dd \
  -quiet build
rm -rf "$OUT"
ditto "build/dd/Build/Products/Release/Hear Church.app" "$OUT"
ditto "$STAGE/python" "$OUT/Contents/Resources/python"
ditto "$STAGE/engine" "$OUT/Contents/Resources/engine"

step "Signing with \"$SIGN_ID\""
# Every Mach-O file inside Resources, innermost first, then the Python interpreter, then the app.
# (Python loads modules signed by us only, but some packages need executable memory: hence its entitlements.)
sign() { codesign --force --options runtime --timestamp=none --sign "$SIGN_ID" "$@" 2>&1 | grep -v "replacing existing signature" || true; }
find "$OUT/Contents/Resources" -type f \( -name "*.so" -o -name "*.dylib" \) -print0 \
  | xargs -0 -n 200 codesign --force --options runtime --timestamp=none --sign "$SIGN_ID" 2>&1 | grep -v "replacing existing signature" || true
find "$OUT/Contents/Resources/python/bin" -type f -perm -u+x -print0 | while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q Mach-O; then sign --entitlements "$MAC/HearChurch/Python.entitlements" "$f"; fi
done
sign --entitlements "$MAC/HearChurch/HearChurch.entitlements" "$OUT"
codesign --verify --strict "$OUT"
echo "signed"

du -sh "$OUT" | sed 's/^/size: /'
echo "built: $OUT"
