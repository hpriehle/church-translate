"""The Mac app leaves torch and transformers out and fetches them with the Wolof voice."""
import hashlib
import importlib.util
import json
import sys
import tarfile
from pathlib import Path

import pytest

from app import packages as pk

REPO = Path(__file__).resolve().parent.parent


def _load_split():
    spec = importlib.util.spec_from_file_location("split_requirements", REPO / "mac" / "split_requirements.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dep(*names):
    return [{"name": n} for n in names]


def test_split_keeps_shared_packages_in_the_app():
    lock = {"package": [
        {"name": "church-translate", "source": {"virtual": "."},
         "dependencies": _dep("mlx-whisper", "torch", "transformers", "numpy")},
        {"name": "mlx-whisper", "dependencies": _dep("torch", "numba", "huggingface-hub")},
        {"name": "torch", "dependencies": _dep("sympy", "numpy")},
        {"name": "transformers", "dependencies": _dep("huggingface-hub", "tokenizers")},
        {"name": "sympy", "dependencies": _dep("mpmath")},
        {"name": "numba"}, {"name": "numpy"}, {"name": "huggingface-hub"}, {"name": "tokenizers"}, {"name": "mpmath"},
    ]}
    lines = ["# via something\n", "numpy==2.0\n", "mlx-whisper==1 ; sys_platform == 'darwin'\n", "torch==2.14\n",
             "sympy==1\n", "mpmath==1\n", "huggingface-hub==2\n", "numba==1\n", "tokenizers==1\n", "transformers==5\n"]
    app, wolof = _load_split().split(lock, lines)
    assert {x.split("==")[0] for x in wolof} == {"torch", "transformers", "sympy", "mpmath", "tokenizers"}
    assert {x.split("==")[0] for x in app} == {"numpy", "mlx-whisper", "huggingface-hub", "numba"}


def test_real_lockfile_leaves_speech_recognition_in_the_app():
    import tomllib

    with open(REPO / "uv.lock", "rb") as f:
        only = _load_split().wolof_only(tomllib.load(f))
    assert {"torch", "transformers", "sympy"} <= only
    assert not {"mlx-whisper", "mlx", "numba", "scipy", "numpy", "huggingface-hub", "anthropic"} & only


@pytest.fixture
def libs(tmp_path, monkeypatch):
    """A small archive laid out like the real one, a manifest for it, and an empty download folder."""
    src = tmp_path / "src" / "wolof-libs-abc"
    src.mkdir(parents=True)
    (src / "fake_wolof_lib.py").write_text("VALUE = 7\n")
    archive = tmp_path / "wolof-libs-abc.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(src, arcname="wolof-libs-abc")
    manifest = {"id": "wolof-libs-abc", "url": "https://example.invalid/x.tar.gz",
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "size": archive.stat().st_size}
    (tmp_path / "wolof_libs.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(pk, "LIBS_MANIFEST", tmp_path / "wolof_libs.json")
    monkeypatch.setattr(pk, "LIBS_DIR", tmp_path / "packages")
    (tmp_path / "packages").mkdir()
    monkeypatch.setattr(sys, "path", list(sys.path))
    return manifest, archive


def test_wolof_size_includes_the_libraries(libs):
    manifest, _ = libs
    assert pk.package_for("wo", "x").libs == manifest
    assert pk.package_for("wo", "x").size_mb == pk.SIZES_MB["wo"] + round(manifest["size"] / 1e6)


def test_no_manifest_means_no_libraries_to_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(pk, "LIBS_MANIFEST", tmp_path / "missing.json")
    p = pk.package_for("wo", "x")
    assert p.libs is None and p.size_mb == pk.SIZES_MB["wo"]


def test_unpack_activates_and_replaces_older_versions(libs):
    manifest, archive = libs
    old = pk.LIBS_DIR / "wolof-libs-old"
    old.mkdir()
    download = pk.LIBS_DIR / archive.name
    download.write_bytes(archive.read_bytes())

    pk._unpack_libs(download, manifest)

    assert (pk.LIBS_DIR / "wolof-libs-abc" / "fake_wolof_lib.py").exists()
    assert not old.exists() and not download.exists()
    assert sys.path[-1] == str(pk.LIBS_DIR / "wolof-libs-abc")   # last: what the app ships wins
    pk.activate_libs()
    assert sys.path.count(str(pk.LIBS_DIR / "wolof-libs-abc")) == 1


def test_damaged_download_is_refused_and_removed(libs):
    manifest, archive = libs
    download = pk.LIBS_DIR / archive.name
    download.write_bytes(archive.read_bytes() + b"x")

    with pytest.raises(RuntimeError, match="damaged"):
        pk._unpack_libs(download, manifest)

    assert not download.exists() and not (pk.LIBS_DIR / "wolof-libs-abc").exists()


def test_activate_ignores_a_download_not_finished(libs):
    manifest, _ = libs
    (pk.LIBS_DIR / "wolof-libs-abc.unpacking").mkdir()
    pk.activate_libs()
    assert not any("wolof-libs" in p for p in sys.path)
    assert not pk.Packages._present(pk.Package("wo", "Wolof voice", 1, libs=manifest))


# ---- English Joe/Kristin (0.2.x) → Mike/Kathleen

def test_saved_old_english_voice_becomes_the_new_one():
    from app.config import Settings, tts_for
    for old, new in (("en_US-joe-medium", "en_US-mike-medium"), ("en_US-kristin-medium", "en_US-kathleen-medium")):
        s = Settings.model_validate({"voices": {"en": {"engine": "piper", "voice": old, "speed": 1.2}}})
        assert s.voices["en"].voice == new and s.voices["en"].speed == 1.2
        assert tts_for(s, "en").voice == new
    keep = Settings.model_validate({"voices": {"en": {"engine": "piper", "voice": "en_US-mike-medium"}}})
    assert keep.voices["en"].voice == "en_US-mike-medium"


def _voice(dir, name):
    for ext in ("onnx", "onnx.json"):
        (dir / f"{name}.{ext}").write_text("x")


def test_english_with_the_old_voices_downloads_mike_and_kathleen(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setattr(pk, "PIPER_DIR", tmp_path)
    assert pk.replaced_languages() == []
    _voice(tmp_path, "en_US-joe-medium")
    _voice(tmp_path, "en_US-kristin-medium")
    assert pk.replaced_languages() == ["en"]

    async def fake_fetch(url, dest):
        dest.write_text("new")
    monkeypatch.setattr(pk, "_fetch", fake_fetch)
    packs = pk.Packages()
    en = packs.wanted(["en"])[1]
    assert en.id == "en" and set(en.piper_voices) == {"en_US-mike-medium", "en_US-kathleen-medium"}
    assert not pk.Packages._present(en)   # the old files don't count: "missing", so it downloads

    async def run():
        await packs._download(en)
    asyncio.run(run())
    assert en.state == "ready", en.error
    assert pk.Packages._present(en)
    assert not list(tmp_path.glob("en_US-joe-medium*")) and not list(tmp_path.glob("en_US-kristin-medium*"))
    assert pk.replaced_languages() == []


def test_download_endpoint_adds_languages_with_replaced_voices(tmp_path, monkeypatch):
    from app import main
    from app.config import Settings
    monkeypatch.setattr(main.S, "settings", Settings.model_validate({}), raising=False)
    monkeypatch.setattr(pk, "PIPER_DIR", tmp_path)
    _voice(tmp_path, "en_US-joe-medium")
    seen = []
    monkeypatch.setattr(pk.PACKAGES, "download_missing", lambda langs: seen.append(list(langs)))

    async def status(langs):
        return []
    monkeypatch.setattr(pk.PACKAGES, "status", status)
    monkeypatch.setattr(main, "require_operator", lambda request: None)
    from fastapi.testclient import TestClient
    r = TestClient(main.app).post("/api/packages/download", json={"languages": ["fr"]})
    assert r.status_code == 200 and seen == [["fr", "en"]]
