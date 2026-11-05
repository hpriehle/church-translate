#!/usr/bin/env bash
# Build the libraries only the Wolof voice needs (torch, transformers), signed, as a download:
#
#   mac/wolof_libs.sh <python> <wolof requirements> <python version> <signing identity>
#
# Writes mac/wolof-libs/wolof-libs-<id>.tar.gz and .json, where <id> comes from the pinned versions, so
# it is built once and shared by every release until those change. The app's manifest (wolof_libs.json,
# copied in by build.sh) names the file and its hash; mac/release.sh uploads it to R2.
set -euo pipefail
PY="$1"; REQS="$2"; PY_VERSION="$3"; SIGN_ID="$4"
MAC="$(cd "$(dirname "$0")" && pwd)"
ID="wolof-libs-$( { echo "$PY_VERSION"; cat "$REQS"; } | shasum -a 256 | cut -c1-12)"
OUT="$MAC/wolof-libs"
if [[ -e "$OUT/$ID.tar.gz" && -e "$OUT/$ID.json" ]]; then echo "$ID"; exit 0; fi

WORK="$MAC/build/wolof-libs-stage"
rm -rf "$WORK" && mkdir -p "$WORK/$ID" "$OUT"
uv pip install --quiet --python "$PY" --no-deps --link-mode copy --compile-bytecode \
  --target "$WORK/$ID" -r "$REQS" >&2
# Not needed at run time. torch/bin stays: torch refuses to import without torch_shm_manager.
rm -rf "$WORK/$ID/torch/include" "$WORK/$ID/torch/bin/protoc"* "$WORK/$ID/bin"
find "$WORK/$ID" -name "*.a" -delete

LOG="$WORK/sign.log"
if ! "$PY" -I "$MAC/machos.py" "$WORK/$ID" \
    | xargs -0 -n 200 codesign --force --options runtime --timestamp --sign "$SIGN_ID" 2> "$LOG"; then
  grep -v "replacing existing signature" "$LOG" >&2
  echo "wolof_libs: signing failed" >&2
  exit 1
fi
# No ._ files in the archive. It stays inside this script until it is whole, so a failure leaves nothing.
COPYFILE_DISABLE=1 tar -C "$WORK" -czf "$WORK/$ID.tar.gz" "$ID"
"$PY" -I - "$WORK/$ID.tar.gz" "$ID" "$OUT/$ID.json" <<'PYTHON'
import hashlib, json, os, sys

path, name, dest = sys.argv[1:4]
digest = hashlib.sha256()
with open(path, "rb") as f:
    while chunk := f.read(1 << 20):
        digest.update(chunk)
with open(dest, "w") as f:
    json.dump({"id": name, "url": f"https://hearchurch.com/download/{name}.tar.gz",
               "sha256": digest.hexdigest(), "size": os.path.getsize(path)}, f)
PYTHON
mv "$WORK/$ID.tar.gz" "$OUT/$ID.tar.gz"
rm -rf "$WORK"
echo "$ID"
