#!/usr/bin/env bash
# Build "Hear Church.app" with the Python engine inside it.
#
#   mac/build.sh                 → mac/build/Hear Church.app (signed with your Apple Development identity)
#   SIGN_ID="Developer ID Application" mac/build.sh     (sign for distribution: mac/release.sh does this)
#   mac/build.sh --app-only      → rebuild the Swift app and engine code, reuse the last bundled Python
#   HC_ARCH=intel mac/build.sh   → mac/build/intel/Hear Church.app for Intel Macs: x86_64 Python and Swift,
#                                  CPU speech recognition (faster-whisper), no Wolof voice, its own update feed
#
# The app contains a standalone CPython (python-build-standalone, via uv) with the locked dependencies,
# and the engine code (app/, web/, models shipped in the repo). Voices and speech models are downloaded
# on first use, not bundled, and so are the libraries only the Wolof voice needs (torch, transformers):
# Developer ID builds make that download with wolof_libs.sh.
set -euo pipefail
cd "$(dirname "$0")"
MAC="$PWD"
REPO="$(cd .. && pwd)"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
SIGN_ID="${SIGN_ID:-Apple Development}"
PY_VERSION="$(cat "$REPO/.python-version")"
OUT="$MAC/build/Hear Church.app"
STAGE="$MAC/build/engine-stage"     # bundled Python + engine, reused by --app-only
DD=build/dd
INTEL=""
if [[ "${HC_ARCH:-}" == "intel" ]]; then
  INTEL=1
  OUT="$MAC/build/intel/Hear Church.app"
  STAGE="$MAC/build/intel/engine-stage"
  DD=build/dd-intel
  mkdir -p "$MAC/build/intel"
fi
PY_REQ="$PY_VERSION"
[[ -n "$INTEL" ]] && PY_REQ="cpython-$PY_VERSION-macos-x86_64-none"

step() { echo "== $*"; }

if [[ "${1:-}" != "--app-only" || ! -d "$STAGE/python" ]]; then
  step "Standalone Python $PY_VERSION"
  uv python install --quiet "$PY_REQ"
  PY_SRC="$(dirname "$(dirname "$(uv python find --no-project --system --managed-python "$PY_REQ")")")"
  rm -rf "$STAGE" && mkdir -p "$STAGE"
  cp -R "$PY_SRC" "$STAGE/python"
  rm -f "$STAGE/python/lib/python$PY_VERSION/EXTERNALLY-MANAGED"
  PY="$STAGE/python/bin/python3"

  step "Python packages (from uv.lock)"
  if [[ -n "$INTEL" ]]; then
    uv pip install --quiet --python "$PY" --python-platform x86_64-apple-darwin --only-binary :all: --no-deps \
      --link-mode copy --compile-bytecode -r "$MAC/requirements-intel.txt"
    # A package wrongly left out would only show up on a church's Mac, so try the imports here (runs under Rosetta).
    "$PY" -I -c 'import importlib.util as u, platform, anthropic, fastapi, huggingface_hub, faster_whisper, onnxruntime, piper; assert platform.machine() == "x86_64" and u.find_spec("torch") is None'
  else
    (cd "$REPO" && uv export --quiet --frozen --no-dev --no-hashes --no-emit-project -o "$STAGE/requirements.txt")
    "$PY" -I "$MAC/split_requirements.py" "$REPO/uv.lock" "$STAGE/requirements.txt" "$STAGE/app.txt" "$STAGE/wolof.txt"
    uv pip install --quiet --python "$PY" --no-deps --link-mode copy --compile-bytecode -r "$STAGE/app.txt"
    # A package wrongly left out would only show up on a church's Mac, so try the imports here.
    "$PY" -I -c 'import importlib.util as u, anthropic, fastapi, huggingface_hub, mlx_whisper, onnxruntime, piper; assert u.find_spec("torch") is None'
  fi
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
cp "$REPO/config.toml" "$REPO/glossary.json" "$REPO"/glossary.??.json "$REPO/LICENSE" "$REPO/THIRD_PARTY_NOTICES.md" "$STAGE/engine/"
# Intel: the multilingual CPU model (the speaker may not be speaking English), and no Wolof.
[[ -n "$INTEL" ]] && sed -i '' -e 's/^faster_whisper_model = .*/faster_whisper_model = "small"/' -e 's/^languages = .*/languages = ["fr"]/' "$STAGE/engine/config.toml"
"$STAGE/python/bin/python3" -m compileall -q "$STAGE/engine/app"
rm -f "$STAGE/engine/wolof_libs.json"
if [[ -n "$INTEL" ]]; then
  echo "(Intel build: no Wolof voice)"
elif [[ "$SIGN_ID" == "Developer ID"* ]]; then
  step "Wolof libraries"
  LIBS_ID="$("$MAC/wolof_libs.sh" "$STAGE/python/bin/python3" "$STAGE/wolof.txt" "$PY_VERSION" "$SIGN_ID")"
  cp "$MAC/wolof-libs/$LIBS_ID.json" "$STAGE/engine/wolof_libs.json"
  du -h "$MAC/wolof-libs/$LIBS_ID.tar.gz"
else
  echo "(development build: no Wolof voice unless run from a checkout with EngineDir)"
fi

step "Swift app"
xcodegen generate --quiet
# Over SSH only the release keychain is unlocked: make Xcode's own signing step use it too.
XC_SIGN=()
# It holds only the Developer ID identity (everything is re-signed with it below anyway).
[[ -n "${SIGN_KEYCHAIN:-}" ]] && XC_SIGN=("OTHER_CODE_SIGN_FLAGS=--keychain $SIGN_KEYCHAIN"
  CODE_SIGN_STYLE=Manual "CODE_SIGN_IDENTITY=$SIGN_ID" PROVISIONING_PROFILE_SPECIFIER=)
XC_ARCH=()
[[ -n "$INTEL" ]] && XC_ARCH=(ARCHS=x86_64 ONLY_ACTIVE_ARCH=NO)
xcodebuild -project HearChurch.xcodeproj -scheme HearChurch -configuration Release -derivedDataPath "$DD" \
  -quiet build ${XC_ARCH[@]+"${XC_ARCH[@]}"} ${XC_SIGN[@]+"${XC_SIGN[@]}"}
rm -rf "$OUT"
ditto "$DD/Build/Products/Release/Hear Church.app" "$OUT"
# Intel copies update from their own feed, so an Apple Silicon update never reaches them.
[[ -n "$INTEL" ]] && /usr/libexec/PlistBuddy -c "Set :SUFeedURL https://hearchurch.com/appcast-intel.xml" "$OUT/Contents/Info.plist"
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
sign() { codesign --force --options runtime $TS --sign "$SIGN_ID" ${SIGN_KEYCHAIN:+--keychain "$SIGN_KEYCHAIN"} "$@" 2>> "$LOG" || sign_failed; }
"$STAGE/python/bin/python3" -I "$MAC/machos.py" "$OUT/Contents/Resources" > "$MAC/build/machos.txt"
python3 -I -c '
import sys
paths = open(sys.argv[1], "rb").read().split(b"\0")
sys.stdout.buffer.write(b"".join(p + b"\0" for p in paths if p and b"/Resources/python/bin/" not in p))
' "$MAC/build/machos.txt" | xargs -0 -n 200 codesign --force --options runtime $TS --sign "$SIGN_ID" ${SIGN_KEYCHAIN:+--keychain "$SIGN_KEYCHAIN"} 2>> "$LOG" || sign_failed
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
