import io
import zipfile

import httpx
import pytest

from app.scripture import (NOTE_HEADER, Library, Pack, RefDetector, ScriptureFeed, bible_for, install, overlap,
                           verses_from_vpl)
from app.translate.prompts import user_message


def feed_all(chunks: list[str]) -> list[list[str]]:
    d, out = RefDetector(), []
    for i, c in enumerate(chunks):
        out.append([r.usfm for r in d.feed(c, now=i * 5.0)])
    return out


@pytest.mark.parametrize("text,expected", [
    ("Turn with me to John 3:16, for God so loved the world", ["JHN.3.16"]),
    ("First Corinthians chapter 13, verse 4 through 7", ["1CO.13.4-7"]),
    ("James 1:5 if any of you lack wisdom", ["JAS.1.5"]),
    ("Read 1 John 4:8 with me", ["1JN.4.8"]),
    ("John chapter 3 verses 16 and 17", ["JHN.3.16-17"]),
    ("Psalms 23.1", ["PSA.23.1"]),
    ("Song of Solomon 2:4", ["SNG.2.4"]),
    ("Revelation 3:20 and 2 Kings 5:14", ["REV.3.20", "2KI.5.14"]),
    ("John 3:16-40", ["JHN.3.16-20"]),                 # capped at five verses
    # Book of Mormon, Doctrine and Covenants, Pearl of Great Price
    ("1 Nephi 3:7 and Doctrine and Covenants 88:118", ["1NE.3.7", "DC.88.118"]),
    ("Fourth Nephi 1:15", ["4NE.1.15"]),
    ("Alma 32:21 and Words of Mormon 1:7", ["ALM.32.21", "WOM.1.7"]),
    ("D&C section 121, verses 41 through 42", ["DC.121.41-42"]),
    ("in section 88, verse 118 we read", ["DC.88.118"]),
    ("Joseph Smith—Matthew 1:4 and Joseph Smith History 1:17", ["JSM.1.4", "JSH.1.17"]),
    ("Moses 1:39", ["MSE.1.39"]),
    # Not references we know
    ("Nephi 3:7", []),                                  # which Nephi?
    ("Corinthians 13:4", []),                           # which Corinthians?
    ("Psalm 23", []),                                   # whole chapter: not fetched
    ("the meeting starts at 10:30", []),
])
def test_detects_bible_references(text, expected):
    assert feed_all([text]) == [expected]


def test_reference_split_across_chunks_and_verse_continuation():
    assert feed_all(["Let's read Romans", "8:28 together.", "And verse 29 says", "verse 29 again"]) == \
        [[], ["ROM.8.28"], ["ROM.8.29"], []]


def test_other_scripture_ends_continuation():
    assert feed_all(["John 3:16", "then Gospel Principles 3:7", "and verse 9"]) == [["JHN.3.16"], [], []]
    assert feed_all(["John 3:16", "then 1 Nephi 3:7", "a little later, in verse 9"]) == \
        [["JHN.3.16"], ["1NE.3.7"], ["1NE.3.9"]]


def test_reference_labels():
    d = RefDetector()
    assert [r.label for r in d.feed("1 John 4:8, D&C 4:2, John 3:16-17, Joseph Smith History 1:17", 0)] == \
        ["1 John 4:8", "Doctrine and Covenants 4:2", "John 3:16-17", "Joseph Smith—History 1:17"]


def test_bible_choice():
    assert bible_for("fr", {}) == "fraLSG"
    assert bible_for("wo", {}) == ""                    # Kàddug Yàlla is non-commercial
    assert bible_for("pt", {}) == "" and bible_for("pt", {"pt": "porbrbsl"}) == "porbrbsl"
    assert bible_for("fr", {"fr": ""}) == "" and bible_for("fr", {"fr": "made-up"}) == ""


def test_vpl_codes_and_cleanup():
    raw = "\ufeffGEN 1:1 Au commencement,  Dieu créa\nJOH 3:16 ¶ Car Dieu [a] tant aimé|\n1JO 4:8 Dieu est amour.\n".encode()
    assert verses_from_vpl(raw) == {"GEN.1.1": "Au commencement, Dieu créa", "JHN.3.16": "Car Dieu a tant aimé",
                                    "1JN.4.8": "Dieu est amour."}


def test_overlap():
    verse = "Car Dieu a tant aimé le monde qu'il a donné son Fils unique, afin que quiconque croit en lui ne périsse point"
    assert overlap("Car Dieu a tant aimé le monde qu'il a donné son Fils unique", verse) > 0.9
    assert overlap("Frères et sœurs, nous allons maintenant chanter le cantique", verse) < 0.3
    assert overlap("Oui", verse) == 0.0


def test_user_message_carries_note():
    assert user_message("hello", False) == "hello"
    assert user_message("hello", False, "SCRIPTURE NOTE\nx") == "hello\n\nSCRIPTURE NOTE\nx"


VERSES = {"JHN.3.16": "Car Dieu a tant aimé le monde qu'il a donné son Fils unique,",
          "JHN.3.17": "Dieu, en effet, n'a pas envoyé son Fils dans le monde pour qu'il juge le monde,"}


def make_library(tmp_path) -> Library:
    Pack.write(tmp_path / "fr.fraLSG.txt.gz", "Louis Segond 1910", "Louis Segond 1910 · public domain", VERSES)
    Pack.write(tmp_path / "fr.lds.txt.gz", "Le Livre de Mormon", "© IRI", {"1NE.3.7": "J'irai et je ferai."})
    Pack.write(tmp_path / "fr.porbrbsl.txt.gz", "Another Bible", "", {"JHN.3.16": "not the Bible this language uses"})
    return Library(tmp_path)


async def test_feed_looks_up_offers_and_credits(tmp_path):
    lib = make_library(tmp_path)
    assert [p.name for p in lib.files("fr", "fraLSG")] == ["fr.fraLSG.txt.gz", "fr.lds.txt.gz"]
    feed = ScriptureFeed(lib, "fr", "fraLSG", window_s=120)
    feed.prefetch(RefDetector().feed("John 3:16-17 and 1 Nephi 3:7", now=0), now=0)
    await feed.settle(1)
    note = feed.note(now=10)
    assert note.startswith(NOTE_HEADER)
    assert 'John 3:16-17 (Louis Segond 1910): "Car Dieu a tant aimé' in note
    assert '1 Nephi 3:7 (Le Livre de Mormon): "J\'irai et je ferai."' in note
    credit = feed.credit_for("Car Dieu a tant aimé le monde qu'il a donné son Fils unique", now=10)
    assert credit == {"reference": "John 3:16-17", "source": "Louis Segond 1910",
                      "notice": "Louis Segond 1910 · public domain"}
    assert feed.credit_for("Nous allons chanter le cantique numéro deux cent", now=10) is None
    assert feed.note(now=500) == ""                     # out of the window


async def test_unknown_verse_and_disabled_feed(tmp_path):
    feed = ScriptureFeed(make_library(tmp_path), "fr", "fraLSG")
    feed.prefetch(RefDetector().feed("Alma 32:21", now=0), now=0)
    await feed.settle(1)
    assert feed.note(now=1) == ""
    off = ScriptureFeed(None)
    off.prefetch(RefDetector().feed("John 3:16", now=0))
    await off.settle(0.1)
    assert off.note() == "" and off.credit_for("anything at all here") is None


async def test_install_converts_ebible_zip(tmp_path):
    lines = "".join(f"MAT {c}:{v} verse {c}.{v}\n" for c in range(1, 30) for v in range(1, 300))
    lines += "JOH 3:16 Car Dieu a tant aimé le monde\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("fraLSG_vpl.txt", lines)

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/Scriptures/fraLSG_vpl.zip"
        return httpx.Response(200, content=buf.getvalue())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        path = await install("fr", "fraLSG", tmp_path, client)
    pack = Pack.load(path)
    assert path.name == "fr.fraLSG.txt.gz" and pack.title == "Louis Segond 1910"
    assert pack.verses["JHN.3.16"] == "Car Dieu a tant aimé le monde"


async def test_install_rejects_partial_download(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("fraLSG_vpl.txt", "JOH 3:16 Car Dieu\n")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=buf.getvalue()))) as c:
        with pytest.raises(ValueError):
            await install("fr", "fraLSG", tmp_path, c)
    assert not list(tmp_path.iterdir()) or all(p.suffix == ".part" for p in tmp_path.iterdir())
