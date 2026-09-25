#!/usr/bin/env python3
"""The public install page: a white page with a title and the one-line install.

    python3 installpage/server.py --bind 0.0.0.0 --port 8080

Standalone on purpose: stdlib only, and it imports nothing from the Jav3
backend, so it can run on a box that has no checkout and no venv.

It serves exactly three routes and nothing else:

    GET /             the page (HTML, no CSS, no JS, no external resources)
    GET /bootstrap.sh the bootstrap script, read once at startup
    GET /healthz      "ok"

It is written to face the internet. The parser is ours rather than
http.server's, because http.server reads a 64 KiB request line, keeps
connections alive and parses paths; we want none of that. The whole shape:
one request per connection, the head read under a hard byte cap and a
wall-clock deadline, the body never read, exact-match routes, and every
response a byte string rendered at startup — nothing a client sends ever
reaches the bytes written back.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import signal
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path

DEFAULT_TITLE = "Jav3"
DEFAULT_COMMAND = ("curl -fsSL https://raw.githubusercontent.com/Grindlewalt/Jav3"
                   "/main/scripts/bootstrap.sh | sh")
DEFAULT_BOOTSTRAP = Path(__file__).resolve().parent.parent / "scripts" / "bootstrap.sh"

MAX_HEAD = 8 * 1024          # request line + headers, bytes
READ_TIMEOUT = 5.0           # seconds to deliver the WHOLE head, not per recv
MAX_BOOTSTRAP = 1024 * 1024  # refuse to serve something that is clearly not a script
DRAIN_BYTES = 64 * 1024      # lingering close: most unread input we swallow
DRAIN_SECONDS = 1.0

SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("Content-Security-Policy", "default-src 'none'"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
)

REASONS = {
    200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
    408: "Request Timeout", 414: "URI Too Long", 429: "Too Many Requests",
    431: "Request Header Fields Too Large", 503: "Service Unavailable",
    505: "HTTP Version Not Supported",
}

TEXT = "text/plain; charset=utf-8"


def render_page(title: str, command: str) -> bytes:
    t, c = html.escape(title), html.escape(command)
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content=\"width=device-width, initial-scale=1\">"
        f"<title>{t}</title></head><body><h1>{t}</h1>"
        "<p>Install on a Linux box (as the user that will run it):</p>"
        f"<pre>{c}</pre></body></html>\n"
    ).encode("utf-8")


def load_bootstrap(path: Path) -> bytes:
    """Read the script once. Any failure is fatal at startup, never at request time."""
    data = Path(path).read_bytes()
    if not data:
        raise ValueError(f"{path} is empty")
    if len(data) > MAX_BOOTSTRAP:
        raise ValueError(f"{path} is larger than {MAX_BOOTSTRAP} bytes")
    return data


class RateLimiter:
    """Per-IP token bucket, in memory: `per_minute` requests a minute, burst the same."""

    def __init__(self, per_minute: int, max_entries: int = 10000):
        self.capacity = float(per_minute)
        self.refill = per_minute / 60.0
        self.max_entries = max_entries
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, ip: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            tokens, last = self._buckets.get(ip, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.refill)
            ok = tokens >= 1.0
            self._buckets[ip] = (tokens - 1.0 if ok else tokens, now)
            if len(self._buckets) > self.max_entries:
                self._prune(now)
            return ok

    def _prune(self, now: float) -> None:
        # A bucket that has refilled to capacity carries no state, so drop it. If
        # a spray of distinct addresses keeps the table full anyway, drop the
        # older half rather than grow without bound.
        full = [ip for ip, (tok, last) in self._buckets.items()
                if tok + (now - last) * self.refill >= self.capacity]
        for ip in full:
            del self._buckets[ip]
        if len(self._buckets) > self.max_entries:
            by_age = sorted(self._buckets, key=lambda ip: self._buckets[ip][1])
            for ip in by_age[: len(by_age) // 2]:
                del self._buckets[ip]


def build_response(status: int, body: bytes, content_type: str, *, head: bool = False,
                   extra: tuple[tuple[str, str], ...] = ()) -> bytes:
    # Caching an error is how one abusive client denies everyone behind a shared
    # cache, so only the stable answers (the routes and 404) are cacheable.
    cache = "public, max-age=300" if status in (200, 404) else "no-store"
    lines = [f"HTTP/1.1 {status} {REASONS[status]}",
             f"Content-Type: {content_type}",
             f"Content-Length: {len(body)}",
             f"Cache-Control: {cache}",
             "Connection: close"]
    lines += [f"{k}: {v}" for k, v in SECURITY_HEADERS + extra]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + (b"" if head else body)


class HeadError(Exception):
    def __init__(self, status: int):
        self.status = status


def read_head(sock: socket.socket, deadline: float) -> bytes:
    """Read up to the blank line that ends the head, or fail.

    Never reads more than MAX_HEAD+1 bytes in total, and never waits past
    `deadline` in total — a client trickling one byte every few seconds gets the
    same 408 as one that sends nothing. Whatever follows the head (a body) is
    never read.
    """
    buf = b""
    while b"\r\n\r\n" not in buf and b"\n\n" not in buf:
        if len(buf) > MAX_HEAD:
            raise HeadError(431 if b"\n" in buf else 414)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HeadError(408)
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(MAX_HEAD + 1 - len(buf))
        except socket.timeout:
            raise HeadError(408)
        if not chunk:
            raise HeadError(400)
        buf += chunk
    return buf


def parse_request_line(head: bytes) -> tuple[str, str]:
    line = head.split(b"\n", 1)[0].rstrip(b"\r")
    try:
        text = line.decode("ascii")
    except UnicodeDecodeError:
        raise HeadError(400)
    parts = text.split(" ")
    if len(parts) != 3:
        raise HeadError(400)
    method, target, version = parts
    if not (1 <= len(method) <= 16 and method.isalpha() and method.isupper()):
        raise HeadError(400)
    if not target or not all(33 <= ord(ch) < 127 for ch in target):
        raise HeadError(400)
    if version not in ("HTTP/1.0", "HTTP/1.1"):
        raise HeadError(505 if version.startswith("HTTP/") else 400)
    return method, target


def lingering_close(sock: socket.socket) -> None:
    """Close without resetting the connection under the client's feet.

    Closing a socket that still holds unread input makes the kernel send RST,
    and a client that gets RST can lose the response we just wrote. So: stop
    writing, swallow a bounded amount of whatever is still arriving, close.
    """
    try:
        sock.shutdown(socket.SHUT_WR)
        deadline = time.monotonic() + DRAIN_SECONDS
        drained = 0
        while drained < DRAIN_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            chunk = sock.recv(8192)
            if not chunk:
                break
            drained += len(chunk)
    except OSError:
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass


class Handler(socketserver.BaseRequestHandler):
    server: "InstallPageServer"

    def handle(self) -> None:
        sock: socket.socket = self.request
        peer = self.client_address[0]  # the socket's peer; no header is ever trusted
        start = time.monotonic()
        method, path, status = "-", "-", 0
        try:
            try:
                head = read_head(sock, start + READ_TIMEOUT)
                method, path = parse_request_line(head)
                if not self.server.limiter.allow(peer):
                    raise HeadError(429)
                status, payload = self.server.route(method, path)
            except HeadError as e:
                status, payload = e.status, self.server.error_response(e.status, method)
            sock.settimeout(READ_TIMEOUT)
            sock.sendall(payload)
        except OSError:
            pass
        finally:
            lingering_close(sock)
            self.server.log(method, path, status, peer, start)


class InstallPageServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = False
    block_on_close = True
    request_queue_size = 128

    def __init__(self, bind: str, port: int, *, title: str, command: str,
                 bootstrap: bytes, rate_per_minute: int = 30, max_conns: int = 64,
                 log_stream=None):
        self.address_family = socket.AF_INET6 if ":" in bind else socket.AF_INET
        self.limiter = RateLimiter(rate_per_minute)
        self._slots = threading.BoundedSemaphore(max_conns)
        self._log_stream = log_stream if log_stream is not None else sys.stdout
        self._log_lock = threading.Lock()
        routes = {
            "/": (render_page(title, command), "text/html; charset=utf-8"),
            "/bootstrap.sh": (bootstrap, "text/x-shellscript; charset=utf-8"),
            "/healthz": (b"ok\n", TEXT),
        }
        self._get = {p: build_response(200, b, t) for p, (b, t) in routes.items()}
        self._head = {p: build_response(200, b, t, head=True) for p, (b, t) in routes.items()}
        self._errors: dict[tuple[int, bool], bytes] = {}
        for code in REASONS:
            if code == 200:
                continue
            extra: tuple[tuple[str, str], ...] = ()
            if code == 405:
                extra = (("Allow", "GET, HEAD"),)
            elif code == 429:
                extra = (("Retry-After", "60"),)
            elif code == 503:
                extra = (("Retry-After", "5"),)
            body = f"{code} {REASONS[code].lower()}\n".encode("ascii")
            for head in (False, True):
                self._errors[(code, head)] = build_response(code, body, TEXT, head=head,
                                                            extra=extra)
        super().__init__((bind, port), Handler)

    def route(self, method: str, path: str) -> tuple[int, bytes]:
        if method not in ("GET", "HEAD"):
            return 405, self.error_response(405, method)
        table = self._head if method == "HEAD" else self._get
        if path in table:
            return 200, table[path]
        return 404, self.error_response(404, method)

    def error_response(self, status: int, method: str = "-") -> bytes:
        return self._errors[(status, method == "HEAD")]

    # One thread per connection, but never more than max_conns of them: past
    # that, the accept thread answers 503 itself and moves on.
    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            start = time.monotonic()
            try:
                request.settimeout(0.5)
                request.sendall(self._errors[(503, False)])
                request.shutdown(socket.SHUT_WR)
                request.setblocking(False)
                request.recv(MAX_HEAD + 1)  # only what already arrived; never wait here
            except OSError:
                pass
            request.close()
            self.log("-", "-", 503, client_address[0], start)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def shutdown_request(self, request):  # the handler already closed it
        try:
            request.close()
        except OSError:
            pass

    def handle_error(self, request, client_address):
        # Never print a traceback: it would echo attacker-shaped input to the log.
        exc = sys.exc_info()[0]
        self.log("-", "-", 0, client_address[0], time.monotonic(),
                 error=exc.__name__ if exc else "error")

    def log(self, method: str, path: str, status: int, peer: str, start: float,
            **extra) -> None:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "method": method, "path": path[:200], "status": status, "peer": peer,
               "ms": round((time.monotonic() - start) * 1000, 1), **extra}
        line = json.dumps(rec, ensure_ascii=True)  # escapes control chars: no log injection
        with self._log_lock:
            try:
                self._log_stream.write(line + "\n")
                self._log_stream.flush()
            except (OSError, ValueError):
                pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    env = os.environ
    p = argparse.ArgumentParser(description="Serve the Jav3 install page.")
    p.add_argument("--bind", default=env.get("INSTALLPAGE_BIND", "127.0.0.1"),
                   help="address to listen on (default 127.0.0.1; '::' for all)")
    p.add_argument("--port", type=int, default=int(env.get("INSTALLPAGE_PORT", "8080")))
    p.add_argument("--bootstrap", type=Path,
                   default=Path(env.get("INSTALLPAGE_BOOTSTRAP", str(DEFAULT_BOOTSTRAP))),
                   help="the script served at /bootstrap.sh (read once at startup)")
    p.add_argument("--title", default=env.get("INSTALLPAGE_TITLE", DEFAULT_TITLE))
    p.add_argument("--command", default=env.get("INSTALLPAGE_COMMAND", DEFAULT_COMMAND),
                   help="the one-liner shown on the page")
    p.add_argument("--rate", type=int, default=int(env.get("INSTALLPAGE_RATE", "30")),
                   help="requests per minute per client IP (default 30)")
    p.add_argument("--max-conns", type=int,
                   default=int(env.get("INSTALLPAGE_MAX_CONNS", "64")),
                   help="concurrent connections before answering 503 (default 64)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        script = load_bootstrap(args.bootstrap)
    except (OSError, ValueError) as e:
        print(f"installpage: cannot read bootstrap script: {e}", file=sys.stderr)
        return 2
    if args.rate < 1 or args.max_conns < 1:
        print("installpage: --rate and --max-conns must be at least 1", file=sys.stderr)
        return 2
    server = InstallPageServer(args.bind, args.port, title=args.title, command=args.command,
                               bootstrap=script, rate_per_minute=args.rate,
                               max_conns=args.max_conns)

    # serve_forever runs on this thread and shutdown() blocks until it returns,
    # so the signal handler hands shutdown to another thread.
    def stop(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    host, port = server.server_address[:2]
    print(json.dumps({"event": "listening", "bind": host, "port": port}), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()  # joins in-flight requests, each bounded by the timeouts
    print(json.dumps({"event": "stopped"}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
