"""installpage/server.py — the public, stdlib-only install page.

Every test talks to a real server on a loopback port over a raw socket, so
what is asserted is the bytes on the wire, not a handler's return value.
"""
import importlib.util
import io
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SERVER_PY = ROOT / "installpage" / "server.py"
BOOTSTRAP = ROOT / "scripts" / "bootstrap.sh"

_spec = importlib.util.spec_from_file_location("installpage_server", SERVER_PY)
ip = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ip)

SECURITY = {
    "x-content-type-options": "nosniff",
    "content-security-policy": "default-src 'none'",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
}


@pytest.fixture
def serve():
    started = []

    def start(**kw):
        opts = dict(title="Jav3", command=ip.DEFAULT_COMMAND,
                    bootstrap=BOOTSTRAP.read_bytes(), rate_per_minute=1000,
                    log_stream=io.StringIO())
        opts.update(kw)
        srv = ip.InstallPageServer("127.0.0.1", 0, **opts)
        t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                             daemon=True)
        t.start()
        started.append(srv)
        return srv

    yield start
    for srv in started:
        srv.shutdown()
        srv.server_close()


def raw(srv, data: bytes, timeout: float = 3.0) -> bytes:
    with socket.create_connection(srv.server_address[:2], timeout=timeout) as s:
        s.sendall(data)
        out = b""
        while chunk := s.recv(65536):
            out += chunk
        return out


def parse(resp: bytes):
    head, _, body = resp.partition(b"\r\n\r\n")
    lines = head.decode("ascii").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        headers[k.strip().lower()] = v.strip()
    return status, headers, body


def req(srv, method="GET", path="/", extra=b""):
    return parse(raw(srv, f"{method} {path} HTTP/1.1\r\nHost: x\r\n".encode() + extra + b"\r\n"))


def test_page_is_bare_html_with_title_and_one_liner(serve):
    srv = serve()
    status, h, body = req(srv)
    assert status == 200
    assert h["content-type"] == "text/html; charset=utf-8"
    assert int(h["content-length"]) == len(body)
    page = body.decode()
    assert page.startswith("<!doctype html><html lang=en><head><meta charset=utf-8>")
    assert "<title>Jav3</title>" in page and "<h1>Jav3</h1>" in page
    assert f"<pre>{ip.DEFAULT_COMMAND}</pre>" in page
    for banned in ("<style", "style=", "<script", "<link", "src=", "http://", "<img"):
        assert banned not in page.replace(ip.DEFAULT_COMMAND, "")


def test_title_and_command_are_configurable_and_escaped(serve):
    srv = serve(title="Mine <b>", command="curl -fsSL http://h/bootstrap.sh | sh && echo '<x>'")
    _, _, body = req(srv)
    page = body.decode()
    assert "<title>Mine &lt;b&gt;</title>" in page
    assert "&amp;&amp; echo &#x27;&lt;x&gt;&#x27;" in page
    assert "<x>" not in page


def test_bootstrap_bytes_equal_the_file(serve):
    srv = serve()
    status, h, body = req(srv, path="/bootstrap.sh")
    assert status == 200
    assert h["content-type"].startswith("text/x-shellscript")
    assert body == BOOTSTRAP.read_bytes()
    assert int(h["content-length"]) == len(body)


def test_healthz(serve):
    status, h, body = req(serve(), path="/healthz")
    assert (status, body) == (200, b"ok\n")


def test_head_has_headers_but_no_body(serve):
    srv = serve()
    status, h, body = req(srv, method="HEAD", path="/bootstrap.sh")
    assert status == 200 and body == b""
    assert int(h["content-length"]) == len(BOOTSTRAP.read_bytes())
    status, h, body = req(srv, method="HEAD", path="/nope")
    assert status == 404 and body == b""


@pytest.mark.parametrize("path", ["/index.html", "/bootstrap.sh/", "//", "/?x=1",
                                  "/../scripts/bootstrap.sh", "/healthz?", "/%2e%2e/"])
def test_everything_else_is_404(serve, path):
    status, h, body = req(serve(), path=path)
    assert status == 404
    assert body == b"404 not found\n"


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "OPTIONS", "TRACE", "PATCH"])
def test_other_methods_are_405(serve, method):
    status, h, _ = req(serve(), method=method)
    assert status == 405
    assert h["allow"] == "GET, HEAD"


@pytest.mark.parametrize("path,method", [("/", "GET"), ("/bootstrap.sh", "HEAD"),
                                         ("/nope", "GET"), ("/", "POST")])
def test_security_headers_on_every_response(serve, path, method):
    _, h, body = req(serve(), method=method, path=path)
    for k, v in SECURITY.items():
        assert h[k] == v
    assert "server" not in h
    assert "content-length" in h
    assert h["connection"] == "close"


def test_cache_control_public_on_routes_and_no_store_on_refusals(serve):
    srv = serve(rate_per_minute=1)
    assert req(srv)[1]["cache-control"] == "public, max-age=300"
    status, h, _ = req(srv)
    assert status == 429 and h["cache-control"] == "no-store"
    for k, v in SECURITY.items():
        assert h[k] == v


def test_rate_limit_per_ip(serve):
    srv = serve(rate_per_minute=5)
    codes = [req(srv, path="/healthz")[0] for _ in range(7)]
    assert codes == [200] * 5 + [429] * 2
    assert req(srv)[1]["retry-after"] == "60"


def test_token_bucket_refills():
    rl = ip.RateLimiter(60)  # one token a second
    assert all(rl.allow("a", now=0.0) for _ in range(60))
    assert not rl.allow("a", now=0.0)
    assert rl.allow("b", now=0.0)  # buckets are per IP
    assert rl.allow("a", now=1.01)
    assert not rl.allow("a", now=1.02)


def test_rate_limiter_table_is_bounded():
    rl = ip.RateLimiter(10, max_entries=100)
    for i in range(1000):
        rl.allow(f"10.0.{i // 256}.{i % 256}", now=float(i))
    assert len(rl._buckets) <= 100


def test_12k_request_line_rejected_without_reading_the_body(serve):
    srv = serve()
    t0 = time.monotonic()
    with socket.create_connection(srv.server_address[:2], timeout=3) as s:
        # No CRLF ever arrives and the socket stays open: the server must answer
        # from the cap alone, not wait for the rest or for the read timeout.
        s.sendall(b"GET /" + b"a" * (12 * 1024))
        resp = b""
        while chunk := s.recv(65536):
            resp += chunk
    assert parse(resp)[0] == 414
    assert time.monotonic() - t0 < 2.5


def test_oversized_headers_are_431(serve):
    status, h, _ = req(serve(), extra=b"X-Pad: " + b"a" * (9 * 1024) + b"\r\n")
    assert status == 431
    assert h["x-frame-options"] == "DENY"


def test_head_just_under_cap_is_served(serve):
    status, _, _ = req(serve(), extra=b"X-Pad: " + b"a" * 7000 + b"\r\n")
    assert status == 200


def test_body_is_never_awaited(serve):
    srv = serve()
    t0 = time.monotonic()
    # Promise a megabyte and send none of it: the answer comes straight away.
    status, _, _ = req(srv, method="POST", extra=b"Content-Length: 1048576\r\n")
    assert status == 405
    assert time.monotonic() - t0 < 2.5


def test_slow_head_times_out(serve, monkeypatch):
    monkeypatch.setattr(ip, "READ_TIMEOUT", 0.5)
    srv = serve()
    with socket.create_connection(srv.server_address[:2], timeout=3) as s:
        # Trickle: every byte arrives well inside a per-recv timeout, but the
        # whole head never does. The deadline is wall-clock, so it still fires.
        t0 = time.monotonic()
        try:
            for b in b"GET / HTTP/1.1\r\n":
                s.sendall(bytes([b]))
                time.sleep(0.1)
        except OSError:
            pass
        resp = b""
        while chunk := s.recv(65536):
            resp += chunk
    assert parse(resp)[0] == 408
    assert time.monotonic() - t0 < 2.5


@pytest.mark.parametrize("line", [b"GET /\r\n\r\n", b"GET / HTTP/1.1 x\r\n\r\n",
                                  b"get / HTTP/1.1\r\n\r\n", b"GET /\xff HTTP/1.1\r\n\r\n",
                                  b"GET / FOO/1.1\r\n\r\n"])
def test_malformed_request_line_is_400(serve, line):
    assert parse(raw(serve(), line))[0] == 400


def test_http2_version_is_505(serve):
    assert parse(raw(serve(), b"GET / HTTP/2.0\r\n\r\n"))[0] == 505


def test_connection_cap_answers_503(serve):
    srv = serve(max_conns=1)
    hog = socket.create_connection(srv.server_address[:2], timeout=3)
    try:
        time.sleep(0.2)  # let the hog's thread take the only slot
        status, h, _ = parse(raw(srv, b"GET / HTTP/1.1\r\n\r\n"))
        assert status == 503
        assert h["x-content-type-options"] == "nosniff"
    finally:
        hog.close()
    time.sleep(0.3)
    assert req(srv)[0] == 200


def test_access_log_is_structured_and_uses_socket_peer(serve):
    import json
    log = io.StringIO()
    srv = serve(log_stream=log)
    req(srv, path="/healthz", extra=b"X-Forwarded-For: 6.6.6.6\r\n")
    raw(srv, b"GET /\x1b[31mevil HTTP/1.1\r\n\r\n")
    time.sleep(0.1)
    recs = [json.loads(ln) for ln in log.getvalue().splitlines()]
    ok = recs[0]
    assert (ok["method"], ok["path"], ok["status"], ok["peer"]) == ("GET", "/healthz", 200,
                                                                   "127.0.0.1")
    assert isinstance(ok["ms"], (int, float))
    assert "\x1b" not in log.getvalue()  # a bad request line never reaches the log raw


def test_refuses_to_start_without_bootstrap(tmp_path):
    rc = subprocess.run([sys.executable, str(SERVER_PY), "--port", "0",
                         "--bootstrap", str(tmp_path / "missing.sh")],
                        capture_output=True, text=True, timeout=10)
    assert rc.returncode == 2
    assert "cannot read bootstrap" in rc.stderr


def test_standalone_no_backend_import():
    src = SERVER_PY.read_text()
    assert "backend" not in {ln.split()[1].split(".")[0] for ln in src.splitlines()
                             if ln.startswith(("import ", "from "))}


def test_sigterm_stops_cleanly(tmp_path):
    env = {**os.environ, "INSTALLPAGE_TITLE": "EnvTitle"}
    p = subprocess.Popen([sys.executable, "-I", str(SERVER_PY), "--port", "0",
                          "--bootstrap", str(BOOTSTRAP)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        import json
        first = json.loads(p.stdout.readline())
        assert first["event"] == "listening"
        with socket.create_connection(("127.0.0.1", first["port"]), timeout=3) as s:
            s.sendall(b"GET / HTTP/1.0\r\n\r\n")
            resp = b""
            while chunk := s.recv(65536):
                resp += chunk
        assert b"<h1>EnvTitle</h1>" in resp
        p.send_signal(signal.SIGTERM)
        out, _ = p.communicate(timeout=10)
        assert p.returncode == 0
        assert '"event": "stopped"' in out
    finally:
        if p.poll() is None:
            p.kill()
