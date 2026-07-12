"""Tests for the EPUB reconstructor.

These build a synthetic `books/<id>/` directory that mirrors the shape the EPUB
downloader produces (a manifest plus `<label>.xhtml`/`<label>.css` segments whose
HTML references remote resources), reconstruct the EPUB, and assert both structural
properties and that the result passes `epubcheck`.
"""

import base64
import json
import zipfile

import pytest

from play_book_epub_tool.play_book_epub_tool import build_epub

# Two distinct valid 1x1 PNGs (a black and a white pixel) so content-hash dedup is
# meaningfully exercised: a.png and b.png differ, but repeated refs to a.png collapse.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
PNG_WHITE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


# Stand-in for the remote resolver so tests never touch the network.
def fake_resolver(url):
    return (PNG_WHITE if url.endswith("b.png") else PNG), "image/png"


def _write_book(tmp_path):
    base = tmp_path / "books" / "TESTID01"
    base.mkdir(parents=True)

    manifest = {
        "language": "en",
        "is_right_to_left": False,
        "metadata": {
            "title": "A &amp; B: A Test Book",
            "authors": "Ada Lovelace, Alan Turing",
            "publisher": "Test Press",
            "pub_date": "2020.05.01",
            "volume_id": "TESTID01",
        },
        # Two chapters, the second nested under the first.
        "toc_entry": [
            {"label": "Chapter One", "depth": 0},
            {"label": "Section 1.1", "depth": 1},
            {"label": "Chapter Two", "depth": 0},
        ],
        "segment": [
            {"order": 1, "label": "PT1", "title": "Chapter One"},
            {"order": 2, "label": "PT2", "title": "Section 1.1"},
            {"order": 3, "label": "PT3", "title": "Chapter Two"},
        ],
    }
    (base / "manifest.json").write_text(json.dumps(manifest))

    # A local cover so the test doesn't rely on a remote/manifest cover.
    (base / "cover.png").write_bytes(PNG)

    segments = {
        # References a remote image (must get packaged + rewritten) and a <br> (void tag).
        "PT1": '<p>First chapter.<br>More.</p><img src="https://play.google.com/img/a.png" width="10" height="10">',
        # References the SAME remote image (must dedupe) plus one via inline CSS url().
        "PT2": '<p style="background:url(https://play.google.com/img/a.png)">Nested.</p>'
        '<img src="https://play.google.com/img/a.png">',
        "PT3": "<p>Second chapter &amp; the end.</p>",
    }
    for label, body in segments.items():
        (base / f"{label}.xhtml").write_text(body, encoding="utf-8")
        # CSS with a url() that must be rewritten too.
        (base / f"{label}.css").write_text(
            "body { font-family: serif; }\n"
            ".bg { background-image: url(https://play.google.com/img/b.png); }",
            encoding="utf-8",
        )
    return base


def test_build_epub_structure(tmp_path):
    base = _write_book(tmp_path)
    output = build_epub(base, resolver=fake_resolver)

    assert output.exists()
    with zipfile.ZipFile(output) as zf:
        names = zf.namelist()
        opf = next(n for n in names if n.endswith(".opf"))
        opf_text = zf.read(opf).decode("utf-8")

        # Resources were packaged and deduplicated: a.png + b.png = 2 files, not 4 refs.
        resource_files = [n for n in names if "/resources/" in n or n.startswith("resources/")]
        assert len(resource_files) == 2, resource_files

        # No content should reference the network anymore.
        for name in names:
            if name.endswith((".xhtml", ".html", ".css")):
                assert "https://play.google.com" not in zf.read(name).decode("utf-8"), name

    # Cover is marked with the EPUB3 cover-image property; title is present.
    assert 'properties="cover-image"' in opf_text
    assert "A &amp; B" in opf_text or "A &amp;amp; B" in opf_text


def test_epubcheck_passes(tmp_path):
    base = _write_book(tmp_path)
    output = build_epub(base, resolver=fake_resolver)

    try:
        from epubcheck import EpubCheck
    except ImportError:
        pytest.skip("epubcheck not installed")

    result = EpubCheck(str(output))
    errors = [m for m in result.messages if m.level in ("ERROR", "FATAL")]
    assert result.valid, "\n".join(f"{m.level}: {m.message}" for m in errors)


def test_flat_toc_fallback(tmp_path):
    base = _write_book(tmp_path)
    # Remove toc_entry so the mapping can't resolve → flat fallback.
    manifest = json.loads((base / "manifest.json").read_text())
    del manifest["toc_entry"]
    (base / "manifest.json").write_text(json.dumps(manifest))

    output = build_epub(base, resolver=fake_resolver)
    assert output.exists()
    with zipfile.ZipFile(output) as zf:
        # nav still lists all three chapters.
        nav = next((zf.read(n).decode("utf-8") for n in zf.namelist() if "nav" in n.lower()
                    and n.endswith(".xhtml")), "")
        assert "PT1.xhtml" in nav and "PT2.xhtml" in nav and "PT3.xhtml" in nav
