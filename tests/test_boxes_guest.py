"""WP1 guest side: box.json replaces the literal addresses (absent = today's),
and the run-turn server answers `mode: "ps"`. Runs the real guest package in
a subprocess (its `backend` package would shadow the host's)."""
import io
import json
import os
import subprocess
import sys
import tarfile

from backend.vm.guest_pkg import build_package_tar


def _pkg(tmp_path, box=None):
    d = tmp_path / "pkg"
    with tarfile.open(fileobj=io.BytesIO(build_package_tar()), mode="r:gz") as t:
        t.extractall(d, filter="data")
    if box is not None:
        (d / "box.json").write_text(json.dumps(box))
    return d


def _run(d, script):
    r = subprocess.run([sys.executable, "-S", "-c", script], cwd=d,
                       env={"PYTHONPATH": str(d), "PATH": os.environ.get("PATH", "")},
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_boxinfo_defaults_are_todays_literals(tmp_path):
    out = _run(_pkg(tmp_path), "from backend import boxinfo; import json\n"
               "print(json.dumps([boxinfo.net(), boxinfo.kind(), boxinfo.unix_gateway()]))")
    net, kind, unix = json.loads(out)
    assert net == {"guest_ip": "10.201.0.2", "prefix": 24, "gateway": "10.201.0.1",
                   "dns": "10.201.0.1", "proxy": "http://10.201.0.1:8443"}
    assert kind == "shared" and unix is False


def test_boxinfo_reads_box_json(tmp_path):
    box = {"v": 1, "id": "p-a", "kind": "project", "runtime": "kvm",
           "net": {"guest_ip": "10.201.10.2", "prefix": 30, "gateway": "10.201.10.1",
                   "dns": "10.201.10.1", "proxy": "http://10.201.10.1:8443"},
           "gateway": {"transport": "unix", "path": "/run/jav3/gateway.sock"}}
    out = _run(_pkg(tmp_path, box), "from backend import boxinfo; import json\n"
               "print(json.dumps([boxinfo.net(), boxinfo.kind(), boxinfo.unix_gateway()]))")
    net, kind, unix = json.loads(out)
    assert net["guest_ip"] == "10.201.10.2" and net["prefix"] == 30
    assert net["proxy"] == "http://10.201.10.1:8443" and kind == "project" and unix


def test_boxinfo_unix_listen_and_connect(tmp_path):
    import tempfile
    short = tempfile.mkdtemp(dir="/tmp")         # sun_path is ~104-108 bytes
    d = _pkg(tmp_path, {"gateway": {"transport": "unix", "path": f"{short}/g.sock"},
                        "listen": {"runturn": {"transport": "unix",
                                               "path": f"{short}/5556.sock"}}})
    out = _run(d, "import socket\nfrom backend import boxinfo\n"
               f"g = socket.socket(socket.AF_UNIX); g.bind({short + '/g.sock'!r}); g.listen(1)\n"
               "c = boxinfo.gateway_connect(); print('GW', c.family == socket.AF_UNIX)\n"
               "s = boxinfo.listen('runturn', 5556); print('L', s.getsockname())\n")
    assert "GW True" in out and "5556.sock" in out


def test_run_turn_server_ps_mode(tmp_path):
    d = _pkg(tmp_path)
    script = (
        "import asyncio, json, socket\n"
        # macOS has no vsock constants; the guest modules read them at import
        "socket.VMADDR_CID_HOST = getattr(socket, 'VMADDR_CID_HOST', 2)\n"
        "from backend import server\n"
        "async def main():\n"
        "    a, b = socket.socketpair(); a.setblocking(False); b.setblocking(False)\n"
        "    loop = asyncio.get_running_loop()\n"
        "    t = asyncio.create_task(server._handle(loop, b))\n"
        "    await loop.sock_sendall(a, b'{\"mode\": \"ps\"}\\n')\n"
        "    data = await loop.sock_recv(a, 65536)\n"
        "    await t\n"
        "    print('PS', data.decode().strip())\n"
        "asyncio.run(main())\n")
    out = _run(d, script)
    ev = json.loads(out.split("PS ", 1)[1])
    # procwatch.py is WP4's: until it ships the stub says so, and never crashes
    assert ev["type"] == "ps"
    assert ev["ok"] is True or "procwatch" in ev["error"]
