"""Security + sanitisation for web access. Pure functions, no network here.

Two jobs:
  is_safe_url  — SSRF guard: only public http/https, never internal/private
                 hosts, so the agent can't turn web_read into a probe of the
                 homelab or cloud metadata.
  html_to_text — strip a page to inert plain text: no scripts, no styles, no
                 markup. The agent (assume-compromised) only ever sees text.
"""
import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urlparse


class UnsafeURL(Exception):
    pass


def is_safe_url(url: str) -> str:
    """Return the URL if it is safe to fetch, else raise UnsafeURL. Rejects
    non-http(s) schemes and any host that resolves to a private, loopback,
    link-local, or otherwise non-global address."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURL(f"scheme '{parsed.scheme}' not allowed (http/https only)")
    host = parsed.hostname
    if not host:
        raise UnsafeURL("no host in URL")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnsafeURL(f"cannot resolve host: {e}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_reserved:
            raise UnsafeURL(f"host resolves to non-public address {ip} — refused")
    return url


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "head", "svg", "nav", "footer", "form"}

    def __init__(self):
        super().__init__()
        self.chunks: list[str] = []
        self._skip_depth = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in ("p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr", "section"):
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:          # title lives in <head>, capture before skip
            self.title += data
            return
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self.chunks.append(text)


def html_to_text(html: str) -> tuple[str, str]:
    """(title, plain_text) from an HTML document. Whitespace collapsed;
    all markup and executable content dropped."""
    p = _TextExtractor()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 — malformed HTML shouldn't crash a read
        pass
    text = " ".join(p.chunks)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return p.title.strip(), text.strip()
