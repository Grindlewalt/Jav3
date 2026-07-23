"""Host egress proxy — the fine-grained enforcement + observation point (A2).

Every guest HTTP(S) request crosses this: nftables drops the LAN, forces DNS
through the host resolver, and redirects 80/443 here; the guest has no other
route off-box. For each request the proxy:

  1. attributes it to the operation driving the guest (egress.current_context),
  2. resolves the target host and applies the per-project policy
     (egress.decide -> allow | deny | cut),
  3. injects {{secret:X}} the project is *granted* to use (Layer 2 on the wire —
     the guest never holds the key),
  4. forwards it, meters bytes, records the event (live feed + volume baseline),
  5. runs anomaly detection and, on a trip, auto-cuts the host.

HTTP is intercepted in full (headers/body seen, secrets injected). HTTPS arrives
as CONNECT and is TUNNELLED — host, byte volume and cadence are still observed
and policy/cut still enforced, but the payload is opaque, so secret injection
into TLS bodies is the documented follow-up (needs the baked MITM CA). That does
not weaken containment: the guest still holds no secrets, and authenticated
HTTPS calls the agent needs can go through the host tools that substitute
host-side. This proxy's job is watch + policy + cut + HTTP injection.
"""
import asyncio
import re
from urllib.parse import urlsplit

import httpx

from .. import egress, secrets as secrets_mod
from .. import anomaly, security, websec
from ..config import settings
from ..db import get_db

_REQ_LINE = re.compile(rb"^([A-Z]+)\s+(\S+)\s+HTTP/(\d\.\d)\r?\n")
_HOST_HDR = re.compile(rb"\r\nHost:\s*([^\r\n]+)", re.I)
_SECRET_PLACEHOLDER = "{{secret:"


def parse_target(head: bytes) -> tuple[str, str, str] | None:
    """(method, host, port) from a request/CONNECT head, or None if unparseable.
    Handles CONNECT (host:port), absolute-form (proxy) and origin-form+Host."""
    m = _REQ_LINE.match(head)
    if not m:
        return None
    method, target = m.group(1).decode(), m.group(2).decode()
    if method == "CONNECT":
        host, _, port = target.partition(":")
        return method, host.lower(), (port or "443")
    if "://" in target:                     # absolute-form: GET http://host/path
        try:
            u = urlsplit(target)
            port = u.port or 80             # .port raises ValueError on a bad port
        except ValueError:
            return None
        return method, (u.hostname or "").lower(), str(port)
    hm = _HOST_HDR.search(head)             # origin-form: Host header
    if hm:
        host, _, port = hm.group(1).decode().strip().partition(":")
        return method, host.lower(), (port or "80")
    return None


async def inject_secrets(db, slug: str | None, host: str, text: str) -> tuple[str, list[str]]:
    """Replace {{secret:X}} in an outbound request with the real value, but ONLY
    if (a) a project owns the request, (b) that project is granted the secret, and
    (c) the secret's host binding — when set — covers the destination host. A
    missing project or grant refuses ALL secrets (fail CLOSED); the placeholder is
    left intact so it fails at the origin rather than leaking. Returns
    (text, refused_names)."""
    if _SECRET_PLACEHOLDER not in text:
        return text, []
    values = secrets_mod.load()
    refused: list[str] = []

    async def _allowed(name: str) -> bool:
        if name not in values:
            return False
        if not slug:                        # no project context -> no injection at all
            return False
        if not await egress.may_use_secret(db, slug, name):
            return False
        bound = secrets_mod.hosts_for(name)  # respect an explicit host binding, if any
        if bound and not secrets_mod._host_allowed(host, bound):
            return False
        return True

    out, last = [], 0
    for m in secrets_mod.PLACEHOLDER.finditer(text):
        name = m.group(1).upper()
        out.append(text[last:m.start()])
        if await _allowed(name):
            out.append(values[name])
        else:
            refused.append(name)
            out.append(m.group(0))          # leave the placeholder; do not leak
        last = m.end()
    out.append(text[last:])
    return "".join(out), sorted(set(refused))


def _request_path(head: bytes) -> str:
    """Origin-form path (query preserved) from a request line. Absolute-form
    targets (explicit proxy clients) are reduced to path?query too."""
    line = _REQ_LINE.match(head)
    path = line.group(2).decode()
    if "://" in path:
        u = urlsplit(path)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    return path


async def _record(host, method, path, bo, bi, verdict, reason):
    ctx = egress.current_context()
    db = await get_db()
    try:
        await egress.record_event(db, slug=ctx["project"], host=host, method=method,
                                  path=path, bytes_out=bo, bytes_in=bi, verdict=verdict,
                                  reason=reason, op_id=ctx["op_id"],
                                  conversation_id=ctx["conversation_id"])
        if verdict == "deny":
            await egress.note_denied(db, ctx["project"] or egress.GENERAL, host)
        if verdict == "allow":
            a = await anomaly.check_host(db, ctx["project"], host)
            if a:
                egress.mark_cut(ctx["project"], host)
                await _nft_drop(host)
                await security.raise_event(db, kind="egress_anomaly", severity="critical",
                                           project=ctx["project"], summary=a["summary"],
                                           detail=a["detail"])
                await egress.record_event(db, slug=ctx["project"], host=host, verdict="cut",
                                          reason=f"auto-cut: {a['kind']}", op_id=ctx["op_id"])
    finally:
        await db.close()


async def _nft_drop(host: str) -> None:
    """Best-effort hard cut: drop the host's resolved IPs at nftables (Pi-side).
    A no-op where nft/sudo isn't available (dev laptop)."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
        ips = {i[4][0] for i in infos}
    except OSError:
        return
    for ip in ips:
        try:
            p = await asyncio.create_subprocess_exec(
                "sudo", "-n", "nft", "add", "element", "inet", "jarvis_vm", "cut_hosts",
                "{", ip, "}", stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            await p.wait()
        except (FileNotFoundError, OSError):
            return


async def _authorize(host: str) -> tuple[str, str]:
    ctx = egress.current_context()
    db = await get_db()
    try:
        verdict, reason = await egress.decide(db, ctx["project"] or egress.GENERAL, host)
    finally:
        await db.close()
    # Host-side SSRF floor: the proxy dials out from the HOST, so an allowlisted
    # name that resolves (or DNS-rebinds) to a private/LAN address would let the
    # proxy reach internal infra, bypassing the guest's nftables LAN drop. Refuse
    # any host that resolves to a non-public address, matching webtools' guard.
    if verdict == "allow":
        try:
            websec.is_safe_url(f"http://{host}")
        except websec.UnsafeURL as e:
            return "deny", f"blocked non-public target: {e}"
    return verdict, reason


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> int:
    total = 0
    try:
        while True:
            chunk = await src.read(65536)
            if not chunk:
                break
            total += len(chunk)
            dst.write(chunk)
            await dst.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            dst.close()
        except OSError:
            pass
    return total


async def _handle_connect(host, port, cr, cw):
    """HTTPS: tunnel, observing host + byte volume; policy/cut enforced up front."""
    verdict, reason = await _authorize(host)
    if verdict != "allow":
        cw.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
        await cw.drain(); cw.close()
        await _record(host, "CONNECT", None, 0, 0, verdict, reason)
        return
    try:
        orr, orw = await asyncio.open_connection(host, int(port))
    except OSError as e:
        cw.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        await cw.drain(); cw.close()
        await _record(host, "CONNECT", None, 0, 0, "deny", f"connect failed: {e}")
        return
    cw.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
    await cw.drain()
    up, down = await asyncio.gather(_pipe(cr, orw), _pipe(orr, cw))
    await _record(host, "CONNECT", None, up, down, "allow", reason)


async def _handle_http(method, host, port, head, cr, cw):
    """HTTP: full interception — policy, secret injection, forward, meter."""
    verdict, reason = await _authorize(host)
    if verdict != "allow":
        cw.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        await cw.drain(); cw.close()
        await _record(host, method, None, 0, 0, verdict, reason)
        return
    # read any remaining request body up to Content-Length
    body = b""
    cl = re.search(rb"\r\nContent-Length:\s*(\d+)", head, re.I)
    if cl:
        need = int(cl.group(1)) - (len(head) - (head.find(b"\r\n\r\n") + 4))
        if need > 0:
            body = await cr.readexactly(need)
    ctx = egress.current_context()
    db = await get_db()
    try:
        raw = (head + body).decode("latin-1")
        injected, _refused = await inject_secrets(db, ctx["project"], host, raw)
        # the forwarded URL is rebuilt from the request line, NOT from
        # `injected` — inject into the path separately or a query-string key
        # (the common ?api_key=... shape) forwards as the literal placeholder.
        # `path` (placeholder intact) is what gets logged; only `send_path`
        # carries the real value, and only onto the wire.
        path = _request_path(head)
        send_path, _ = await inject_secrets(db, ctx["project"], host, path)
    finally:
        await db.close()
    url = f"http://{host}:{port}{send_path}"
    hdr_block = injected.split("\r\n\r\n", 1)[0]
    # hop-by-hop + length/host headers are recomputed by httpx from the (possibly
    # injection-resized) body and target URL; forwarding the stale originals would
    # conflict or misframe the request.
    _drop = ("proxy-connection", "connection", "content-length", "host",
             "transfer-encoding", "keep-alive", "proxy-authorization")
    headers = {}
    for ln in hdr_block.split("\r\n")[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            if k.lower() not in _drop:
                headers[k.strip()] = v.strip()
    send_body = injected.split("\r\n\r\n", 1)[1].encode("latin-1") if "\r\n\r\n" in injected else b""
    bo = len(injected)
    bi = 0
    try:
        async with httpx.AsyncClient(timeout=settings.web_fetch_timeout) as c:
            r = await c.request(method, url, headers=headers,
                                content=send_body or None)
            bi = len(r.content)
            out = (f"HTTP/1.1 {r.status_code} {r.reason_phrase}\r\n").encode()
            for k, v in r.headers.items():
                if k.lower() in ("transfer-encoding", "connection"):
                    continue
                out += f"{k}: {v}\r\n".encode()
            out += b"\r\n" + r.content
            cw.write(out); await cw.drain()
    except (httpx.HTTPError, OSError) as e:
        cw.write(f"HTTP/1.1 502 Bad Gateway\r\n\r\n{e}".encode())
        await cw.drain()
    finally:
        cw.close()
    await _record(host, method, path, bo, bi, "allow", reason)


async def handle_conn(cr: asyncio.StreamReader, cw: asyncio.StreamWriter):
    try:
        head = await cr.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
        cw.close()
        return
    parsed = parse_target(head)
    if parsed is None:
        cw.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        await cw.drain(); cw.close()
        return
    method, host, port = parsed
    if method == "CONNECT":
        await _handle_connect(host, port, cr, cw)
    else:
        await _handle_http(method, host, port, head, cr, cw)


class EgressProxy:
    def __init__(self, host: str | None = None, port: int | None = None):
        self.host = host or settings.vm_egress_host_ip
        self.port = port or settings.vm_egress_proxy_port
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        if not settings.vm_egress:
            return
        try:
            self._server = await asyncio.start_server(handle_conn, self.host, self.port)
        except OSError as e:
            print(f"[egress] proxy disabled: {e}")

    async def stop(self) -> None:
        if self._server:
            self._server.close()


proxy = EgressProxy()
