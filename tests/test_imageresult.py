"""Inline images: a HOST-brokered tool (a desk screenshot) can hand the guest
loop an image. The bytes ride the broker reply beside the text, the guest
re-attaches them, and the loop shows them with the mime the BYTES say and the
caption the tool gave — not a hardcoded image/png "current page"."""
import asyncio
import base64
import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import tempfile

from backend.agent import imageresult, loop as loop_mod
from backend.agent.tools import registry
from backend.vm import broker
from backend.vm.gateway_server import handle_conn
from backend.vm.guest_pkg import build_package_tar

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def test_sniff():
    assert imageresult.sniff(PNG) == "image/png"
    assert imageresult.sniff(JPEG) == "image/jpeg"
    assert imageresult.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert imageresult.sniff(b"GIF89a....") == "image/gif"
    assert imageresult.sniff(b"<html>") is None


def test_inline_roundtrip_and_text_stays_clean():
    r = imageresult.with_inline("clicked", JPEG, mime="image/png", caption="desk 'mac'")
    text, img = imageresult.split(r)
    assert text == "clicked" and imageresult.MARKER not in text
    assert img.data(10_000) == JPEG and img.caption == "desk 'mac'"
    # the declared mime is not trusted: wire() re-sniffs it
    assert img.wire(10_000)["mime"] == "image/jpeg"


def test_path_form_and_legacy_bare_path(tmp_path):
    p = tmp_path / "s.png"
    p.write_bytes(PNG)
    _, img = imageresult.split(imageresult.with_image("t", str(p), caption="c"))
    assert img.path == str(p) and img.caption == "c" and img.data(10_000) == PNG
    # the pre-JSON form (MARKER + bare path) still reads as a path
    _, legacy = imageresult.split("t" + imageresult.MARKER + str(p))
    assert legacy.path == str(p)
    assert imageresult.split("plain") == ("plain", None)


def test_bad_inline_payloads_are_dropped():
    assert imageresult.Image(b64="!!!not base64").data(100) is None
    big = base64.b64encode(PNG * 100).decode()
    assert imageresult.Image(b64=big).data(50) is None
    assert imageresult.Image(b64=base64.b64encode(b"<svg/>").decode()).wire(100) is None


def test_loop_uses_sniffed_mime_and_tool_caption():
    img = imageresult.Image(b64=base64.b64encode(JPEG).decode(), mime="image/png",
                            caption="screenshot of the computer 'mac'")
    msg = loop_mod._image_message(img)
    text = msg["content"][0]["text"]
    url = msg["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert "mac" in text and "current page" not in text
    # no caption -> a neutral default, still not "the current page"
    plain = loop_mod._image_message(imageresult.Image(b64=base64.b64encode(PNG).decode()))
    assert "current page" not in plain["content"][0]["text"]
    # a non-image payload never reaches the model
    assert loop_mod._image_message(
        imageresult.Image(b64=base64.b64encode(b"hello").decode())) is None


async def test_broker_ships_image_inline(monkeypatch):
    async def fake_dispatch(name, args):
        return imageresult.with_inline("shot taken", PNG, caption="cap")
    monkeypatch.setattr(registry, "dispatch", fake_dispatch)
    broker.register_turn(broker.TurnEnvelope(op_id="op-img"))
    broker.register_token("op-img", "tok")
    a, b = socket.socketpair()
    a.setblocking(False)
    b.setblocking(False)
    loop = asyncio.get_running_loop()
    server = asyncio.create_task(handle_conn(loop, b))
    try:
        await loop.sock_sendall(a, (json.dumps({
            "op": "tool_broker_call", "op_id": "op-img", "op_token": "tok",
            "name": "desk_screenshot", "args": {}}) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            buf += await asyncio.wait_for(loop.sock_recv(a, 65536), 5)
    finally:
        a.close()
        broker.release_turn("op-img")
        broker.release_token("op-img")
        await asyncio.wait_for(server, 5)
    ev = json.loads(buf.split(b"\n", 1)[0])
    assert ev["type"] == "broker_result"
    # text and image travel separately; the text carries no marker/bytes
    assert ev["result"] == "shot taken" and imageresult.MARKER not in ev["result"]
    assert ev["image"]["mime"] == "image/png" and ev["image"]["caption"] == "cap"
    assert base64.b64decode(ev["image"]["b64"]) == PNG
    assert ev["taint"] == "untrusted"          # desk tools are untrusted by name


async def test_broker_plain_result_has_no_image(monkeypatch):
    async def fake_dispatch(name, args):
        return "just text"
    monkeypatch.setattr(registry, "dispatch", fake_dispatch)
    broker.register_turn(broker.TurnEnvelope(op_id="op-txt"))
    try:
        res = await broker.broker_dispatch("op-txt", "read_file", {})
    finally:
        broker.release_turn("op-txt")
    assert res == {"result": "just text", "taint": "trusted"}


def test_guest_registry_reattaches_the_inline_image():
    """In the real guest package: a broker_result carrying `image` comes out of
    the guest registry as a result the loop can split into text + image."""
    d = tempfile.mkdtemp()
    with tarfile.open(fileobj=io.BytesIO(build_package_tar()), mode="r:gz") as t:
        t.extractall(d, filter="data")
    reply = json.dumps({"type": "broker_result", "result": "shot", "taint": "untrusted",
                        "image": {"b64": base64.b64encode(PNG).decode(),
                                  "mime": "image/png", "caption": "cap"}})
    script = (
        "import asyncio, socket\n"
        "from backend.agent.tools import registry\n"
        "from backend.agent import imageresult\n"
        "a, b = socket.socketpair()\n"
        "class S(socket.socket):\n"
        "    def connect(self, addr): pass\n"
        "fd = a.detach()\n"
        "import types\n"
        "registry.socket = types.SimpleNamespace(AF_VSOCK=0, SOCK_STREAM=0,\n"
        "    socket=lambda *x, **k: S(fileno=fd))\n"
        f"b.sendall({(reply + chr(10)).encode()!r})\n"
        "registry.set_turn('op', 1, 'tok')\n"
        "r = asyncio.run(registry.dispatch('desk_screenshot', {}))\n"
        "text, img = imageresult.split(r)\n"
        "print('OUT', text, img.caption, len(img.data(10000)))\n")
    r = subprocess.run([sys.executable, "-S", "-c", script], cwd=d,
                       env={"PYTHONPATH": d, "PATH": os.environ.get("PATH", "")},
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert f"OUT shot cap {len(PNG)}" in r.stdout, r.stdout + r.stderr
