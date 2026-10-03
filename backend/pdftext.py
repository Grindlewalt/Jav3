"""PDF text for web_read (and everything else on webtools.read).

web_read used to decode a PDF's bytes as UTF-8 and hand the model junk. 2026-10-02:
an agent asked to look up a game's rules could not have read the rule book even
with the right URL (a 5 MB, ~150-page PDF).

A manual is far longer than one tool result, so a PDF is read a slice at a time
with the standard PDF URL fragments, which need no new tool parameter:

  <url>              page count, the outline (bookmarks -> pages), first pages
  <url>#page=12      one page;  #page=12-15  a range
  <url>#search=climb the pages that mention a term, with a snippet each

The parsed pages are cached per document URL (fragment dropped), so paging
through a manual downloads it once. Parsing runs in a worker thread.
"""
import asyncio
import io
import re
import time
from urllib.parse import unquote

MAX_PAGES = 600            # pages parsed per document (a 150-page manual is common)
MAX_OUTLINE = 60           # outline entries shown on the first read
MAX_HITS = 12              # pages listed for a #search
SNIPPET = 160              # characters either side of a search hit
CACHE_TTL = 1800
CACHE_MAX = 8

_FRAG = re.compile(r"#(page|search)=([^&#]*)", re.I)
_docs: dict[str, tuple[float, dict]] = {}     # document url -> (expires, parsed)


class PdfError(Exception):
    pass


def looks_like_pdf(ctype: str, head: bytes, url: str = "") -> bool:
    return ("pdf" in (ctype or "").lower() or head[:5] == b"%PDF-"
            or url.split("#", 1)[0].split("?", 1)[0].lower().endswith(".pdf"))


def split_fragment(url: str) -> tuple[str, str | None, str | None]:
    """(document url, 'page'|'search'|None, value) — only our two fragments
    are taken off; any other fragment stays part of the URL as before."""
    m = _FRAG.search(url)
    if not m:
        return url, None, None
    return url[:m.start()], m.group(1).lower(), unquote(m.group(2)).strip()


def cached(doc_url: str) -> dict | None:
    now = time.monotonic()
    for k in [k for k, (exp, _) in _docs.items() if exp <= now]:
        del _docs[k]
    hit = _docs.get(doc_url)
    return hit[1] if hit else None


def _remember(doc_url: str, doc: dict) -> None:
    _docs[doc_url] = (time.monotonic() + CACHE_TTL, doc)
    while len(_docs) > CACHE_MAX:
        del _docs[min(_docs, key=lambda k: _docs[k][0])]


def _clean(text: str) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _parse(raw: bytes) -> dict:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise PdfError("this server cannot read PDFs (the pypdf package is missing; "
                       "the operator can reinstall requirements.txt)") from e
    try:
        reader = PdfReader(io.BytesIO(raw), strict=False)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as e:  # noqa: BLE001
                raise PdfError("the PDF is password-protected") from e
        n = len(reader.pages)
        pages = []
        for i in range(min(n, MAX_PAGES)):
            try:
                pages.append(_clean(reader.pages[i].extract_text() or ""))
            except Exception:  # noqa: BLE001 — one bad page must not lose the rest
                pages.append("")
        outline = []

        def walk(items, depth):
            for it in items:
                if len(outline) >= MAX_OUTLINE * 4:
                    return
                if isinstance(it, list):
                    walk(it, depth + 1)
                    continue
                try:
                    outline.append((depth, str(it.title).strip(),
                                    reader.get_destination_page_number(it) + 1))
                except Exception:  # noqa: BLE001
                    continue
        try:
            walk(reader.outline, 0)
        except Exception:  # noqa: BLE001 — a broken outline still leaves the text
            pass
        title = ""
        try:
            title = str((reader.metadata or {}).get("/Title") or "").strip()
        except Exception:  # noqa: BLE001
            pass
    except PdfError:
        raise
    except Exception as e:  # noqa: BLE001
        raise PdfError(f"could not parse the PDF ({type(e).__name__})") from e
    return {"pages": pages, "total": n, "outline": outline, "title": title}


async def load(doc_url: str, raw: bytes) -> dict:
    doc = await asyncio.to_thread(_parse, raw)
    _remember(doc_url, doc)
    return doc


def _pages_block(doc: dict, first: int, last: int, budget: int) -> tuple[str, int]:
    """Pages first..last (1-based) inside `budget` chars -> (text, last page shown)."""
    out, used, shown = [], 0, first - 1
    for i in range(first, last + 1):
        body = doc["pages"][i - 1] if i <= len(doc["pages"]) else ""
        chunk = f"--- page {i} ---\n{body or '(no text on this page: a scan or a figure)'}\n"
        if used + len(chunk) > budget and shown >= first:
            break
        if used + len(chunk) > budget:              # one page bigger than the budget
            chunk = chunk[:budget - used] + "\n[page cut at the size limit]\n"
        out.append(chunk)
        used += len(chunk)
        shown = i
    return "".join(out), shown


def render(doc_url: str, doc: dict, kind: str | None, value: str | None,
           budget: int) -> str:
    total, parsed = doc["total"], len(doc["pages"])
    head = f"{doc_url}\nPDF, {total} pages" + (f": {doc['title']}" if doc["title"] else "")
    if parsed < total:
        head += f" (text read for the first {parsed})"
    how = (f"\nRead more with the same tool: {doc_url}#page=N or #page=N-M for pages, "
           f"{doc_url}#search=WORD for the pages that mention a word.")
    if kind == "search":
        term = (value or "").lower()
        if not term:
            return head + "\nerror: #search= needs a word." + how
        hits = []
        for i, body in enumerate(doc["pages"], 1):
            low = body.lower()
            at = low.find(term)
            if at < 0:
                continue
            count = low.count(term)
            snip = body[max(0, at - SNIPPET):at + len(term) + SNIPPET].replace("\n", " ")
            hits.append(f"page {i} ({count}x): …{snip}…")
        if not hits:
            return head + f"\nno page mentions {value!r}." + how
        more = f"\n(+{len(hits) - MAX_HITS} more pages)" if len(hits) > MAX_HITS else ""
        return (head + f"\n{len(hits)} pages mention {value!r}:\n" + "\n".join(hits[:MAX_HITS])
                + more + how)[:budget + 2000]
    if kind == "page":
        m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", value or "")
        if not m:
            return head + f"\nerror: #page= wants N or N-M, got {value!r}." + how
        first = max(1, int(m.group(1)))
        last = min(int(m.group(2) or first), parsed)
        if first > parsed:
            return head + f"\nerror: page {first} is past the end ({parsed} pages read)." + how
        text, shown = _pages_block(doc, first, max(first, last), budget)
        tail = (f"\n[stopped at page {shown} to stay under the size limit: continue at "
                f"{doc_url}#page={shown + 1}-{last}]" if shown < last else "")
        return f"{head}\n\n{text}{tail}{how}"
    # the first read: outline, then as many opening pages as fit
    lines = []
    if doc["outline"]:
        lines.append("Outline (title → page):")
        for depth, title, page in doc["outline"][:MAX_OUTLINE]:
            lines.append(f"{'  ' * min(depth, 3)}- {title[:90]} → {page}")
        if len(doc["outline"]) > MAX_OUTLINE:
            lines.append(f"  … {len(doc['outline']) - MAX_OUTLINE} more entries")
    outline = "\n".join(lines)
    room = max(1500, budget - len(outline))
    text, shown = _pages_block(doc, 1, parsed, room)
    tail = f"\n[pages 1-{shown} of {total} shown]" if shown < total else ""
    return f"{head}{how}\n\n{outline}\n\n{text}{tail}".replace("\n\n\n", "\n\n")
