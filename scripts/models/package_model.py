"""Package a converted Wolof speech model for the download host and record it in app/wolof_asr.json.

  python -I scripts/models/package_model.py /tmp/wo-models/kiriku-q8 --runtime mlx \\
      --prefix wolof-asr-kiriku-mlx-q8 --name "Kiriku-Wolof-ASR, 8-bit mlx" --out mac/models-out
  python -I scripts/models/package_model.py /tmp/wo-models/omni300m --runtime onnx --exclude model.onnx \\
      --prefix wolof-asr-omni300m-onnx-int8 --name "Omnilingual ASR CTC 300M v2, int8 ONNX" --out mac/models-out
  python -I scripts/models/package_model.py /tmp/wo-models/speecht5 --runtime voice --exclude "*.onnx" \
      --include encoder.int8.onnx --include decoder.int8.onnx --include vocoder.onnx \
      --prefix wolof-voice-speecht5-onnx-int8 --name "Wolof voice (SpeechT5), ONNX" --out mac/models-out

Writes <out>/<id>.tar.gz, where <id> = <prefix>-<12 hex digits of the files' hash> (the same files always give the
same id and the same archive, byte for byte), and puts {"id", "url", "sha256", "size", "unpacked", "name"} under
the runtime's key in app/wolof_asr.json (--runtime voice: under "onnx" in app/wolof_voice.json, the Windows Wolof
voice; config.json there names the three graphs, which must all be packaged). The app downloads https://hearchurch.com/download/<id>.tar.gz (the
download Worker serves any <name>.tar.gz from the R2 bucket's root) and checks the hash before unpacking.

Uploading is a separate, deliberate step (it publishes the file):
  rclone copyto <out>/<id>.tar.gz r2:<bucket>/<id>.tar.gz        (mac/release.sh's r2 remote and bucket)
Commit app/wolof_asr.json only once the archive is up, or the app offers a download that 404s.
"""
from __future__ import annotations

import argparse
import fnmatch
import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

URL = "https://hearchurch.com/download/{id}.tar.gz"
MANIFEST = Path(__file__).resolve().parents[2] / "app" / "wolof_asr.json"
VOICE_MANIFEST = MANIFEST.with_name("wolof_voice.json")


def sha256(path: Path) -> str:
    d = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            d.update(chunk)
    return d.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", type=Path)
    ap.add_argument("--runtime", required=True, choices=("mlx", "onnx", "voice"))
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--exclude", action="append", default=[], help="file name pattern left out (repeatable)")
    ap.add_argument("--include", action="append", default=[], help="file name kept even if --exclude matches it")
    ap.add_argument("--manifest", type=Path)
    a = ap.parse_args()
    manifest = a.manifest or (VOICE_MANIFEST if a.runtime == "voice" else MANIFEST)

    files = sorted(p for p in a.folder.iterdir() if p.is_file()
                   and (p.name in a.include or not any(fnmatch.fnmatch(p.name, pat) for pat in a.exclude)))
    if not any(p.name == "config.json" for p in files):
        raise SystemExit("the folder has no config.json: not a converted model")
    if a.runtime == "onnx":   # the app loads the file config.json names
        cfg = json.loads((a.folder / "config.json").read_text(encoding="utf-8"))
        onnx = [p.name for p in files if p.suffix == ".onnx"]
        if len(onnx) != 1:
            raise SystemExit(f"package exactly one .onnx file (use --exclude): {onnx}")
        if cfg.get("file") != onnx[0]:
            cfg["file"] = onnx[0]
            (a.folder / "config.json").write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    if a.runtime == "voice":   # the app loads the three graphs config.json names
        cfg = json.loads((a.folder / "config.json").read_text(encoding="utf-8"))
        missing = [cfg.get(k) for k in ("encoder", "decoder", "vocoder") if cfg.get(k) not in {p.name for p in files}]
        if cfg.get("kind") != "speecht5-onnx" or missing or not any(p.name == "tokens.json" for p in files):
            raise SystemExit(f"not a packaged ONNX Wolof voice (missing: {missing or 'tokens.json / kind'})")
    hashes = {p.name: sha256(p) for p in files}
    ident = f"{a.prefix}-{hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[:12]}"
    a.out.mkdir(parents=True, exist_ok=True)
    archive = a.out / f"{ident}.tar.gz"
    part = archive.with_name(archive.name + ".part")
    with open(part, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=1, mtime=0, filename="") as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for d in (ident,):
            info = tarfile.TarInfo(d)
            info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, 0
            tar.addfile(info)
        for p in files:
            info = tarfile.TarInfo(f"{ident}/{p.name}")
            info.size, info.mode, info.mtime = p.stat().st_size, 0o644, 0
            with open(p, "rb") as f:
                tar.addfile(info, f)
    part.replace(archive)
    entry = {"id": ident, "url": URL.format(id=ident), "sha256": sha256(archive), "size": archive.stat().st_size,
             "unpacked": sum(p.stat().st_size for p in files), "name": a.name}
    data = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
    data["onnx" if a.runtime == "voice" else a.runtime] = entry
    manifest.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(entry, indent=1))
    print(f"\nupload: rclone copyto {archive} r2:<bucket>/{archive.name}")


if __name__ == "__main__":
    io.DEFAULT_BUFFER_SIZE = 1 << 20
    main()
