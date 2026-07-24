"""Co-working guest shell: token auth, guest-package inclusion, and the broker
relay + guest pinning (the vsock/PTY ends are exercised live on the Pi)."""
import asyncio

import pytest

from backend import auth, guest_shell
from backend.config import settings


def test_user_from_token_roundtrip():
    tok = auth.make_token(7, "grant")
    assert auth.user_from_token(tok) == {"id": 7, "username": "grant"}
    assert auth.user_from_token(None) is None
    assert auth.user_from_token("garbage.token.value") is None


def test_guest_package_includes_shell():
    import io
    import tarfile
    from backend.vm import guest_pkg
    tar = guest_pkg.build_package_tar()
    with tarfile.open(fileobj=io.BytesIO(tar)) as t:
        names = t.getnames()
    assert "backend/shell.py" in names        # the PTY server ships to the guest
    assert "backend/server.py" in names


class _FakeWriter:
    def __init__(self):
        self.sent = []
        self.closed = False

    def write(self, data):
        self.sent.append(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True


class _FakeReader:
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""


class _FakeVM:
    def __init__(self):
        self.acquired = 0
        self.released = 0

    async def acquire(self):
        self.acquired += 1

    def release(self):
        self.released += 1


@pytest.fixture
def wired(monkeypatch):
    fake_vm = _FakeVM()
    writer = _FakeWriter()
    # guest -> client: one output frame then EOF
    reader = _FakeReader([b'{"type":"o","data":"aGk="}\n'])
    import backend.vm.lifecycle as lifecycle
    monkeypatch.setattr(lifecycle, "vm", fake_vm)
    monkeypatch.setattr(lifecycle, "VMError", RuntimeError, raising=False)

    async def fake_connect(port):
        return reader, writer

    async def fake_prime(slug):
        fake_prime.calls.append(slug)
    fake_prime.calls = []

    monkeypatch.setattr(guest_shell, "_connect_guest", fake_connect)
    monkeypatch.setattr(guest_shell, "_prime_project", fake_prime)
    return fake_vm, reader, writer, fake_prime


async def test_session_relays_and_releases(wired, monkeypatch):
    monkeypatch.setattr(settings, "guest_shell_enabled", True)
    fake_vm, reader, writer, fake_prime = wired
    sent_to_client = []
    inbox = ['{"type":"i","data":"bHM="}', None]   # one input frame, then hang up

    async def client_recv():
        await asyncio.sleep(0)
        return inbox.pop(0)

    async def client_send(line):
        sent_to_client.append(line)

    await asyncio.wait_for(
        guest_shell._session(client_recv, client_send, "demo"), timeout=2)

    assert fake_vm.acquired == 1 and fake_vm.released == 1   # pinned then freed
    assert fake_prime.calls == ["demo"]                     # project primed
    assert any(b'"type":"i"' in f for f in writer.sent)     # input reached guest
    assert any('"type":"o"' in f for f in sent_to_client)   # output reached client


async def test_session_disabled_never_acquires(wired, monkeypatch):
    monkeypatch.setattr(settings, "guest_shell_enabled", False)
    fake_vm, *_ = wired
    import base64
    import json
    sent = []
    await guest_shell._session(lambda: _none(), lambda l: _collect(sent, l), None)
    assert fake_vm.acquired == 0                     # never boots the guest
    payload = base64.b64decode(json.loads(sent[0])["data"]).decode()
    assert "disabled" in payload


async def _none():
    return None


async def _collect(bucket, line):
    bucket.append(line)
