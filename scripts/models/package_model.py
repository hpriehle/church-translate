"""Package a converted Wolof speech model for the download host and record it in app/wolof_asr.json.

  python -I scripts/models/package_model.py /tmp/wo-models/kiriku-q8 --runtime mlx \\
      --prefix wolof-asr-kiriku-mlx-q8 --name "Kiriku-Wolof-ASR, 8-bit mlx" --out mac/models-out
  python -I scripts/models/package_model.py /tmp/wo-models/omni300m --runtime onnx --exclude model.onnx \\
      --prefix wolof-asr-omni300m-onnx-int8 --name "Omnilingual ASR CTC 300M v2, int8 ONNX" --out mac/models-out

Writes <out>/<id>.tar.gz, where <id> = <prefix>-<12 hex digits of the files' hash> (the same files always give the
same id and the same archive, byte for byte), and puts {"id", "url", "sha256", "size", "unpacked", "name"} under
the runtime's key in app/wolof_asr.json. The app downloads https://hearchurch.com/download/<id>.tar.gz (the
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


def sha256(path: Path) -> str:
    d = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            d.update(chunk)
    return d.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", type=Path)
    ap.add_argument("--runtime", required=True, choices=("mlx", "onnx"))
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--exclude", action="append", default=[], help="file name pattern left out (repeatable)")
    ap.add_argument("--manifest", type=Path, default=MANIFEST)
    a = ap.parse_args()

    files = sorted(p for p in a.folder.iterdir() if p.is_file()
                   and not any(fnmatch.fnmatch(p.name, pat) for pat in a.exclude))
    if not any(p.name == "config.json" for p in files):
        raise SystemExit("the folder has no config.json: not a converted model")
    if a.runtime == "onnx":   # the app loads the file config.json names
        cfg = json.loads((a.folder / "config.json").read_text())
        onnx = [p.name for p in files if p.suffix == ".onnx"]
        if len(onnx) != 1:
            raise SystemExit(f"package exactly one .onnx file (use --exclude): {onnx}")
        if cfg.get("file") != onnx[0]:
            cfg["file"] = onnx[0]
            (a.folder / "config.json").write_text(json.dumps(cfg, indent=1))
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
    data = json.loads(a.manifest.read_text()) if a.manifest.exists() else {}
    data[a.runtime] = entry
    a.manifest.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps(entry, indent=1))
    print(f"\nupload: rclone copyto {archive} r2:<bucket>/{archive.name}")


if __name__ == "__main__":
    io.DEFAULT_BUFFER_SIZE = 1 << 20
    main()
