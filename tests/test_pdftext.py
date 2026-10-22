"""web_read on PDFs (backend/pdftext.py): the first read (outline + opening pages),
#page=N-M, #search=word, one download for many slices, the size cap, and the
binary-content guard. 2026-10-02: a PDF used to come back as decoded junk."""
import io

import httpx
import pytest

pypdf = pytest.importorskip("pypdf")

from backend import pdftext, webtools  # noqa: E402
from backend.config import settings  # noqa: E402
from backend.db import init_db  # noqa: E402


def make_pdf(texts: list[str], outline: tuple = ()) -> bytes:
    """A real PDF: one Helvetica text line per page, plus outline entries
    (title, 0-based page)."""
    n = len(texts)
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
            3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"}
    kids = []
    for i, t in enumerate(texts):
        page, content = 4 + 2 * i, 5 + 2 * i
        kids.append(f"{page} 0 R")
        safe = t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode()
        objs[page] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                      f"/Contents {content} 0 R /Resources << /Font << /F1 3 0 R >> >> >>").encode()
        objs[content] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
    objs[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {n} >>".encode()
    out, offsets = io.BytesIO(), {}
    out.write(b"%PDF-1.4\n")
    for k in sorted(objs):
        offsets[k] = out.tell()
        out.write(b"%d 0 obj\n" % k + objs[k] + b"\nendobj\n")
    xref = out.tell()
    size = max(objs) + 1
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % size)
    for k in range(1, size):
        out.write(b"%010d 00000 n \n" % offsets[k])
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref))
    raw = out.getvalue()
    if not outline:
        return raw
    w = pypdf.PdfWriter(clone_from=pypdf.PdfReader(io.BytesIO(raw)))
    for title, page in outline:
        w.add_outline_item(title, page)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


MANUAL = make_pdf([f"Section {i} text. " + ("The robot may climb the TOWER. " if i in (3, 7) else "")
                   for i in range(1, 11)],
                  outline=(("Introduction", 0), ("Scoring", 2), ("Endgame", 6)))


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(webtools, "_page_cache", {})
    monkeypatch.setattr(pdftext, "_docs", {})


def test_split_fragment_takes_only_ours():
    assert pdftext.split_fragment("https://x/a.pdf#page=3-5") == ("https://x/a.pdf", "page", "3-5")
    assert pdftext.split_fragment("https://x/a.pdf#search=climb%20rung") == (
        "https://x/a.pdf", "search", "climb rung")
    assert pdftext.split_fragment("https://x/doc.html#intro") == ("https://x/doc.html#intro", None, None)


def test_first_read_has_outline_pages_and_how_to_read_more():
    doc = pdftext._parse(MANUAL)
    assert doc["total"] == 10 and "Section 4 text" in doc["pages"][3]
    out = pdftext.render("https://x/m.pdf", doc, None, None, 12_000)
    assert "PDF, 10 pages" in out
    assert "- Scoring → 3" in out and "- Endgame → 7" in out
    assert "--- page 1 ---\nSection 1 text." in out
    assert "https://x/m.pdf#page=N" in out and "https://x/m.pdf#search=WORD" in out


def test_page_range_and_the_size_limit():
    doc = pdftext._parse(MANUAL)
    out = pdftext.render("https://x/m.pdf", doc, "page", "3-4", 12_000)
    assert "--- page 3 ---" in out and "--- page 4 ---" in out and "--- page 5 ---" not in out
    tight = pdftext.render("https://x/m.pdf", doc, "page", "1-10", 120)
    assert "continue at https://x/m.pdf#page=" in tight
    assert "past the end" in pdftext.render("https://x/m.pdf", doc, "page", "40", 12_000)
    assert "wants N or N-M" in pdftext.render("https://x/m.pdf", doc, "page", "x", 12_000)


def test_search_lists_the_pages_that_mention_a_word():
    doc = pdftext._parse(MANUAL)
    out = pdftext.render("https://x/m.pdf", doc, "search", "tower", 12_000)
    assert "2 pages mention 'tower'" in out
    assert "page 3 (1x)" in out and "page 7 (1x)" in out
    assert "no page mentions" in pdftext.render("https://x/m.pdf", doc, "search", "zebra", 12_000)


def test_not_a_pdf_says_so():
    with pytest.raises(pdftext.PdfError, match="could not parse"):
        pdftext._parse(b"%PDF-1.4 this is not really a pdf")


def mock_client(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(**kw):
        kw.pop("http2", None)
        return real(transport=httpx.MockTransport(handler), timeout=kw.get("timeout"))
    monkeypatch.setattr(webtools.httpx, "AsyncClient", factory)


async def test_web_read_pdf_once_then_slices_from_the_parsed_copy(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(webtools, "is_safe_url", lambda u: u)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=MANUAL)
    mock_client(monkeypatch, handler)
    url = "https://first.example/2026GameManual.pdf"
    first = await webtools.read(url, "s1")
    assert "PDF, 10 pages" in first and "- Endgame → 7" in first and "%PDF" not in first
    pages = await webtools.read(url + "#page=7", "s1")
    assert "--- page 7 ---" in pages and "climb the TOWER" in pages
    hits = await webtools.read(url + "#search=TOWER", "s1")
    assert "page 3" in hits and "page 7" in hits
    assert calls == [url]                      # one download, three reads


async def test_web_read_a_page_slice_first_downloads_once(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(webtools, "is_safe_url", lambda u: u)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        # served with a generic type: the %PDF- magic still says what it is
        return httpx.Response(200, headers={"content-type": "application/octet-stream"},
                              content=MANUAL)
    mock_client(monkeypatch, handler)
    out = await webtools.read("https://first.example/get?id=9#page=2", "s1")
    assert "--- page 2 ---" in out and calls == ["https://first.example/get?id=9"]


async def test_web_read_pdf_over_the_cap(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(webtools, "is_safe_url", lambda u: u)
    monkeypatch.setattr(settings, "web_max_pdf_bytes", 100)
    mock_client(monkeypatch, lambda r: httpx.Response(
        200, headers={"content-type": "application/pdf"}, content=MANUAL))
    out = await webtools.read("https://first.example/big.pdf", "s1")
    assert out.startswith("error: the PDF at https://first.example/big.pdf is over")


async def test_web_read_binary_is_refused_not_dumped(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(webtools, "is_safe_url", lambda u: u)
    mock_client(monkeypatch, lambda r: httpx.Response(
        200, headers={"content-type": "image/png"}, content=b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"))
    out = await webtools.read("https://first.example/x.png", "s1")
    assert out.startswith("error:") and "binary content (image/png)" in out
