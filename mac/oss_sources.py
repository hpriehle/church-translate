#!/usr/bin/env python3
"""Build hearchurch-oss-<version>.tar.gz: the source code of the GPL/LGPL parts of the Mac app.

    python3 -I mac/oss_sources.py 0.3.0            -> mac/oss-cache/hearchurch-oss-0.3.0.tar.gz

The sources are the exact upstream tarballs listed in mac/oss-sources.json, checked against their SHA-256 and
kept in mac/oss-cache (gitignored), so a second run downloads nothing. One archive covers both apps (Apple
Silicon and Intel). It is reproducible: the same pins give the same bytes, whichever Mac builds it.
mac/release.sh runs this on every public release and uploads the archive to R2, where the hearchurch-download
Worker serves it at https://hearchurch.com/source/<file> (written offer: THIRD_PARTY_NOTICES.md).

Stops, without writing an archive, when a pinned package in uv.lock or mac/requirements-intel.txt no longer
matches the manifest, or a download doesn't match its hash.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path

MAC = Path(__file__).resolve().parent
REPO = MAC.parent
CACHE = MAC / "oss-cache"
MTIME = 1_700_000_000   # fixed timestamps: the archive's bytes depend only on its contents


def die(msg: str) -> None:
    sys.exit(f"oss_sources: {msg}")


def locked_versions(path: Path, package: str) -> set[str]:
    text = path.read_text()
    if path.name == "uv.lock":
        return set(re.findall(rf'^name = "{re.escape(package)}"\nversion = "([^"]+)"', text, re.M))
    return set(re.findall(rf"^{re.escape(package)}==(\S+)", text, re.M | re.I))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch(src: dict) -> Path:
    path = CACHE / src["file"]
    if path.exists() and sha256(path) == src["sha256"]:
        return path
    part = path.with_name(path.name + ".part")
    print(f"downloading {src['url']}", file=sys.stderr)
    subprocess.run(["curl", "-fsSL", "--retry", "3", "-o", str(part), src["url"]], check=True)
    got = sha256(part)
    if got != src["sha256"]:
        part.unlink()
        die(f"{src['file']}: sha256 {got}, expected {src['sha256']} ({src['url']})")
    part.rename(path)
    return path


def espeak_commit(piper_tarball: Path) -> str:
    with tarfile.open(piper_tarball) as tar:
        member = next(m for m in tar.getmembers() if m.name.count("/") == 1 and m.name.endswith("/CMakeLists.txt"))
        cmake = tar.extractfile(member).read().decode()
    m = re.search(r"espeak-ng\.git\s+GIT_TAG\s+([0-9a-f]+)", cmake)
    if not m:
        die(f"no eSpeak NG GIT_TAG in {piper_tarball.name}'s CMakeLists.txt")
    return m.group(1)


def check_pins(sources: list[dict], files: dict[str, Path]) -> None:
    for src in sources:
        pin = src.get("pin", {})
        for where in pin.get("in", []):
            found = locked_versions(REPO / where, pin["package"])
            if found != {pin["version"]}:
                die(f"{where} has {pin['package']} {sorted(found) or 'nothing'}, mac/oss-sources.json has "
                    f"{pin['version']}: update {src['file']} there (url, sha256) to what the app ships")
        if "espeak_commit_in" in pin:
            commit = espeak_commit(files[pin["espeak_commit_in"]])
            if not src["file"].removeprefix("espeak-ng-").startswith(commit):
                die(f"{pin['espeak_commit_in']} builds eSpeak NG {commit}, but the manifest has {src['file']}")


README = """\
Hear Church for Mac {version}: source code of the open-source parts under the GNU GPL and LGPL
=============================================================================================

The Hear Church app includes the programs below, unmodified, in its Contents/Resources/python folder.
This archive holds their complete source code, exactly as published upstream (the SHA-256 of every file
is in SHA256SUMS), and the licence texts in licenses/. The rest of the app's open-source components,
with their licences, are listed in THIRD_PARTY_NOTICES.md (also inside the app, in
Contents/Resources/engine).

How they were built: each Python package is the upstream wheel from PyPI, built by its own project
from these sources with the build files included in them (lameenc's and piper1-gpl's CMakeLists.txt
download LAME and eSpeak NG; the files they download are included here). PyAV's FFmpeg libraries were
built by pyav-ffmpeg's scripts/build-ffmpeg.py, whose scripts/pkg.py lists the same tarballs and hashes.

Replacing a library: lameenc (LAME), soxr (libsoxr) and PyAV's FFmpeg are loaded by Python as separate
files (lameenc*.so, soxr_ext*.so, av/.dylibs/*.dylib). You may replace them with versions you built
from modified sources. macOS only loads code signed by the same developer into this app, so after
replacing a file, re-sign the app on your Mac:  codesign --force --deep -s - "/Applications/Hear Church.app"

Questions, or to ask for this source on a physical medium: support@hearchurch.com

Contents
--------
{contents}
"""


def build(version: str) -> Path:
    manifest = json.loads((MAC / "oss-sources.json").read_text())
    sources = manifest["sources"]
    CACHE.mkdir(exist_ok=True)
    files = {src["file"]: fetch(src) for src in sources}
    check_pins(sources, files)

    root = f"hearchurch-oss-{version}"
    contents = "\n".join(
        f"sources/{s['file']}\n    {s['what']}\n    licence: {s['licence']}; in the app for: {s['macs']} Macs\n    from: {s['url']}"
        for s in sources)
    entries: list[tuple[str, bytes | Path]] = [
        ("README.txt", README.format(version=version, contents=contents).encode()),
        ("SHA256SUMS", "".join(f"{s['sha256']}  sources/{s['file']}\n" for s in sources).encode()),
        ("THIRD_PARTY_NOTICES.md", (REPO / "THIRD_PARTY_NOTICES.md").read_bytes()),
    ]
    entries += [(f"licenses/{p.name}", p) for p in sorted((REPO / "licenses").iterdir())]
    entries += [(f"sources/{name}", path) for name, path in files.items()]

    out = CACHE / f"{root}.tar.gz"
    part = out.with_name(out.name + ".part")
    with part.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in entries:
            info = tarfile.TarInfo(f"{root}/{name}")
            info.mtime, info.mode, info.uname, info.gname = MTIME, 0o644, "", ""
            if isinstance(data, Path):
                info.size = data.stat().st_size
                with data.open("rb") as f:
                    tar.addfile(info, f)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    part.rename(out)
    return out


if __name__ == "__main__":
    if len(sys.argv) != 2 or not re.fullmatch(r"\d+\.\d+\.\d+", sys.argv[1]):
        die("usage: python3 -I mac/oss_sources.py <version, e.g. 0.3.0>")
    print(build(sys.argv[1]))
