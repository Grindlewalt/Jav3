"""In-guest image-builder runner (WP5). NOT imported on the host.

backend/vm/images.py packs this file as `backend/server.py` in the package a
builder box fetches over the gateway, so the baked bootstrap (`python3 -m
backend.server`) runs it unchanged. Stdlib only. It:

  1. brings the box's network up from /opt/jarvis/box.json (static; builder
     boxes get no DHCP) and points apt/pip/npm at the box's own proxy listener
     (the only way out; attributed to `__image_build__`);
  2. mode "resolve": pins version + integrity for each item (apt-cache,
     pip --dry-run --report, npm view) and reports them;
     mode "build": re-resolves every item and refuses a mismatch with the
     approved integrity, runs the host-built install argv (never a shell),
     records baseline.json, scrubs the build-only config;
  3. reports over the gateway (`build_report`) and powers off.

Everything it reports is treated by the host as untrusted (install scripts ran
as root in this guest before the report was written).
"""
import datetime
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile

DEST = "/opt/jarvis"
APT_PROXY_CONF = "/etc/apt/apt.conf.d/90jav3-builder-proxy"
_TOKEN = ""
_GATEWAY: dict = {}


def _connect():
    g = _GATEWAY or {"transport": "vsock", "cid": 2, "port": 5555}
    if g.get("transport") == "unix":
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(g["path"])
    else:
        s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        s.connect((int(g.get("cid", 2)), int(g.get("port", 5555))))
    return s


def report(payload: dict) -> None:
    for _ in range(5):
        try:
            s = _connect()
            try:
                s.sendall((json.dumps({"op": "build_report", "token": _TOKEN,
                                       **payload}) + "\n").encode())
                s.makefile("rb").readline()
            finally:
                s.close()
            return
        except OSError:
            import time
            time.sleep(1)


def log(line: str) -> None:
    print(f"BUILDER: {line}", flush=True)
    report({"phase": "log", "line": line[:400]})


def run(argv, *, env=None, timeout=1800, check=True) -> subprocess.CompletedProcess:
    p = subprocess.run(argv, env={**os.environ, **(env or {})}, timeout=timeout,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"{argv[0]} {argv[1] if len(argv) > 1 else ''} exited "
                           f"{p.returncode}: {p.stdout[-800:]}")
    return p


# --- network -----------------------------------------------------------------

def net_up(net: dict) -> dict:
    """Static address from box.json; returns the proxy env."""
    mac = (net.get("mac") or "").lower()
    ifname = None
    for d in sorted(os.listdir("/sys/class/net")):
        try:
            with open(f"/sys/class/net/{d}/address") as f:
                if f.read().strip().lower() == mac:
                    ifname = d
        except OSError:
            pass
    if ifname and net.get("guest_ip"):
        run(["ip", "link", "set", "dev", ifname, "up"])
        run(["ip", "addr", "replace", f"{net['guest_ip']}/{int(net.get('prefix', 30))}",
             "dev", ifname])
        run(["ip", "route", "replace", "default", "via", net["gateway"]])
        try:
            os.replace("/etc/resolv.conf", "/etc/resolv.conf.jav3-builder")
        except OSError:
            pass
        with open("/etc/resolv.conf", "w") as f:
            f.write(f"nameserver {net.get('dns') or net['gateway']}\n")
    proxy = net.get("proxy") or ""
    if proxy:
        with open(APT_PROXY_CONF, "w") as f:
            f.write(f'Acquire::http::Proxy "{proxy}";\nAcquire::https::Proxy "{proxy}";\n')
    return {"http_proxy": proxy, "https_proxy": proxy, "HTTP_PROXY": proxy,
            "HTTPS_PROXY": proxy, "npm_config_proxy": proxy,
            "npm_config_https_proxy": proxy, "DEBIAN_FRONTEND": "noninteractive",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1"}


def scrub() -> None:
    """Leave nothing build-only in the layer: proxy config, resolver, caches,
    this runner."""
    for p in (APT_PROXY_CONF,):
        try:
            os.unlink(p)
        except OSError:
            pass
    if os.path.exists("/etc/resolv.conf.jav3-builder"):
        os.replace("/etc/resolv.conf.jav3-builder", "/etc/resolv.conf")
    run(["apt-get", "clean"], check=False)
    run(["rm", "-rf", "/var/lib/apt/lists", "/root/.cache/pip", "/root/.npm",
         "/tmp/jav3-build", f"{DEST}/backend", f"{DEST}/box.json"], check=False)
    os.makedirs("/var/lib/apt/lists/partial", exist_ok=True)


# --- resolve -----------------------------------------------------------------

def _ensure_venv(venv: str, env: dict) -> None:
    if not os.path.exists(f"{venv}/bin/pip"):
        run(["python3", "-m", "venv", venv], env=env)


def resolve_one(item: dict, env: dict, venv: str) -> dict:
    m, name, ver = item["manager"], item["package"], item.get("version")
    if m == "apt":
        pol = run(["apt-cache", "policy", name], env=env).stdout
        cand = None
        for line in pol.splitlines():
            if line.strip().startswith("Candidate:"):
                cand = line.split(":", 1)[1].strip()
        v = ver or cand
        if not v or v == "(none)":
            raise RuntimeError("not in the configured apt sources")
        show = run(["apt-cache", "show", f"{name}={v}"], env=env).stdout
        sha = next((ln.split(":", 1)[1].strip() for ln in show.splitlines()
                    if ln.startswith("SHA256:")), None)
        if not sha:
            raise RuntimeError(f"version {v} not available")
        return {"version": v, "integrity": f"sha256:{sha}"}
    if m == "pip":
        _ensure_venv(venv, env)
        with tempfile.TemporaryDirectory() as td:
            rep = os.path.join(td, "r.json")
            run([f"{venv}/bin/pip", "install", "--dry-run", "--ignore-installed",
                 "--no-deps", "--no-input", "--report", rep,
                 f"{name}=={ver}" if ver else name], env=env)
            with open(rep) as f:
                r = json.load(f)
        inst = (r.get("install") or [{}])[0]
        v = (inst.get("metadata") or {}).get("version")
        hashes = ((inst.get("download_info") or {}).get("archive_info") or {}).get("hashes") or {}
        sha = hashes.get("sha256")
        if not v or not sha:
            raise RuntimeError("pip returned no version/hash")
        return {"version": v, "integrity": f"sha256:{sha}"}
    if m == "npm":
        out = run(["npm", "view", "--json", f"{name}@{ver}" if ver else name,
                   "version", "dist.integrity"], env=env).stdout
        d = json.loads(out or "{}")
        if isinstance(d, list):
            d = d[-1] if d else {}
        v, integ = d.get("version"), d.get("dist.integrity")
        if not v or not integ:
            raise RuntimeError("npm returned no version/integrity")
        return {"version": v, "integrity": integ}
    raise RuntimeError(f"unknown manager {m}")


# --- baseline ----------------------------------------------------------------

def baseline(venv: str) -> dict:
    def out(argv):
        try:
            return run(argv, check=False, timeout=120).stdout
        except Exception:  # noqa: BLE001
            return ""
    dpkg = {}
    for line in out(["dpkg-query", "-W", "-f", "${Package}\t${Version}\n"]).splitlines():
        if "\t" in line:
            k, v = line.split("\t", 1)
            dpkg[k] = v
    pip = {}
    if os.path.exists(f"{venv}/bin/pip"):
        try:
            pip = {d["name"]: d["version"] for d in
                   json.loads(out([f"{venv}/bin/pip", "list", "--format", "json"]) or "[]")}
        except ValueError:
            pass
    npm = {}
    try:
        deps = json.loads(out(["npm", "ls", "-g", "--prefix", "/usr/local", "--depth", "0",
                               "--json"]) or "{}").get("dependencies") or {}
        npm = {k: (v or {}).get("version") for k, v in deps.items()}
    except ValueError:
        pass
    units = [ln.split()[0] for ln in out(["systemctl", "list-unit-files", "--state=enabled",
                                          "--no-legend"]).splitlines() if ln.strip()]
    setuid = out(["find", "/", "-xdev", "-perm", "-4000", "-type", "f"]).split()
    listening = [ln.strip() for ln in out(["ss", "-ltnupH"]).splitlines() if ln.strip()]
    procs = sorted({ln.strip() for ln in out(["ps", "-eo", "comm="]).splitlines() if ln.strip()})
    return {"v": 1, "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds"), "dpkg": dpkg, "pip": pip, "npm": npm,
        "units_enabled": units, "setuid": setuid, "listening": listening,
        "processes": procs}


def link_pip_entrypoints(venv: str) -> None:
    """Console scripts onto PATH, the venv's site-packages onto system
    python3's path (a plain path entry: its own .pth files do not run)."""
    skip = {"python", "python3", "pip", "pip3", "activate"}
    bindir = f"{venv}/bin"
    for n in os.listdir(bindir):
        if n in skip or n.startswith(("python", "pip", "activate", "Activate")):
            continue
        dst = f"/usr/local/bin/{n}"
        if not os.path.exists(dst):
            os.symlink(f"{bindir}/{n}", dst)
    site = run([f"{bindir}/python", "-c",
                "import sysconfig; print(sysconfig.get_paths()['purelib'])"]).stdout.strip()
    sysdir = run(["python3", "-c",
                  "import sysconfig; print(sysconfig.get_paths()['purelib'])"]).stdout.strip()
    for d in (sysdir, "/usr/lib/python3/dist-packages"):
        if os.path.isdir(d):
            with open(os.path.join(d, "jav3-py.pth"), "w") as f:
                f.write(site + "\n")
            break


# --- main --------------------------------------------------------------------

def main() -> None:
    global _TOKEN, _GATEWAY
    with open(f"{DEST}/box.json") as f:
        box = json.load(f)
    with open(f"{DEST}/backend/job.json") as f:
        job = json.load(f)
    _TOKEN = job["token"]
    _GATEWAY = box.get("gateway") or {}
    venv = job.get("pip_venv") or "/opt/jav3/py"
    result: dict = {"phase": "result", "ok": False}
    try:
        env = net_up(box.get("net") or {})
        log(f"{job['mode']} {job['variant']} v{job.get('version')}")
        run(["apt-get", "update"], env=env)
        if job["mode"] == "resolve":
            res = []
            for it in job.get("items") or []:
                try:
                    res.append({"id": it.get("id"), **resolve_one(it, env, venv)})
                except Exception as e:  # noqa: BLE001
                    res.append({"id": it.get("id"), "error": str(e)[:200]})
            result.update(ok=True, results=res)
        else:
            for it in job.get("items") or []:
                got = resolve_one(it, env, venv)
                want = it.get("integrity")
                if it.get("version") and got["version"] != it["version"]:
                    raise RuntimeError(f"{it['package']}: resolved {got['version']}, "
                                       f"approved {it['version']}")
                if want and not want.startswith("unresolved") and got["integrity"] != want:
                    raise RuntimeError(f"{it['package']}: integrity changed since approval "
                                       f"({got['integrity']} != {want})")
                log(f"verified {it['manager']} {it['package']} {got['version']}")
            for st in job.get("steps") or []:
                log(f"step {st['kind']}: {' '.join(st['argv'])[:300]}")
                run(st["argv"], env=env)
            if os.path.exists(f"{venv}/bin/pip"):
                link_pip_entrypoints(venv)
            scrub()
            bl = baseline(venv)
            result.update(ok=True, baseline=bl,
                          baseline_sha256=hashlib.sha256(
                              json.dumps(bl, sort_keys=True).encode()).hexdigest())
    except Exception as e:  # noqa: BLE001
        result.update(ok=False, error=f"{type(e).__name__}: {e}"[:800])
    report(result)
    subprocess.run(["sync"], check=False)
    subprocess.run(["systemctl", "poweroff"], check=False)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"BUILDER-CRASH: {type(e).__name__}: {e}", flush=True)
        subprocess.run(["systemctl", "poweroff"], check=False)
        sys.exit(1)
