"""WP5 image variants: recipe parsing + hashing, effective/layer sets, the KVM
steps and the Dockerfile from one recipe, the image resolver, the builder
package/report path, a mocked build, and the build_base.sh baseline/package
additions. Offline: no QEMU, no network."""
import asyncio
import base64
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path

import pytest

from backend import packages
from backend.config import settings
from backend.db import get_db, init_db
from backend.vm import boxes, images

ROOT = Path(__file__).resolve().parents[1]


# --- recipes -----------------------------------------------------------------

def test_builtin_recipes_parse():
    r = images.builtin_recipes()
    assert set(r) == {"main", "dev", "desktop", "svc"}
    assert r["main"]["from"] is None and r["dev"]["from"] == "main"
    assert r["desktop"]["min_mem_mb"] >= 1280
    names = {p["package"] for p in r["desktop"]["packages"]}
    assert {"xvfb", "chromium", "scrot", "python3-pil"} <= names
    dev = {(p["manager"], p["package"]) for p in r["dev"]["packages"]}
    assert ("pip", "uv") in dev and ("npm", "typescript") in dev


def test_parse_recipe_pins_and_errors():
    r = images.parse_recipe("name x1\nfrom none\napt jq curl=8.0-1 # c\n"
                            "pip Flask==3.0.0\nnpm @types/node@22.1.0 typescript\n")
    got = {(p["manager"], p["package"], p["version"]) for p in r["packages"]}
    assert got == {("apt", "jq", None), ("apt", "curl", "8.0-1"),
                   ("pip", "flask", "3.0.0"), ("npm", "@types/node", "22.1.0"),
                   ("npm", "typescript", None)}
    for bad in ("apt jq", "name x\nrun curl evil | sh", "name x\napt $(id)",
                "name x\npip git+https://e/x", "name x\nnpm https://e/x.tgz",
                "name X", "name x\nfrom x", "name x\nmin_mem_mb lots",
                "name x\napt", "name x\npip requests>=1"):
        with pytest.raises(images.RecipeError):
            images.parse_recipe(bad)


def test_render_roundtrip_and_hash_is_order_independent():
    a = images.parse_recipe("name v\napt bb aa\npip zz yy\n")
    b = images.parse_recipe(images.render_recipe(a))
    assert a == b
    h1 = images.recipe_sha256("v", a["packages"], None)
    h2 = images.recipe_sha256("v", list(reversed(a["packages"])), None)
    assert h1 == h2 and len(h1) == 64
    assert images.recipe_sha256("v", a["packages"], 1280) != h1
    c = images.parse_recipe("name v\napt bb aa=1.0\npip zz yy\n")
    assert images.recipe_sha256("v", c["packages"], None) != h1


def test_effective_and_layer():
    r = images.builtin_recipes()
    extras = {"main": [{"manager": "apt", "package": "ffmpeg", "version": "7.1-1"}]}
    eff = images.effective_packages("desktop", r, extras)
    keys = {(p["manager"], p["package"]) for p in eff}
    assert ("apt", "git") in keys and ("apt", "chromium") in keys and ("apt", "ffmpeg") in keys
    layer = images.layer_packages(eff, r["main"]["packages"])
    lk = {p["package"] for p in layer}
    assert "git" not in lk and {"chromium", "ffmpeg"} <= lk
    assert images.layer_packages(images.effective_packages("main", r), r["main"]["packages"]) == []
    assert images.layer_packages(images.effective_packages("svc", r), r["main"]["packages"]) == []
    with pytest.raises(images.RecipeError):
        images.effective_packages("a", {"a": {"from": "b", "packages": []},
                                         "b": {"from": "a", "packages": []}})


def test_kvm_steps_and_dockerfile_from_one_recipe():
    pk = [images.package_entry("apt", "golang"), images.package_entry("pip", "uv", "0.8.3"),
          images.package_entry("npm", "typescript", "5.6.2"),
          images.package_entry("apt", "rustc")]
    steps = images.kvm_steps(pk)
    assert [s["kind"] for s in steps] == ["apt", "venv", "pip", "npm"]
    assert steps[0]["argv"][:4] == ["apt-get", "install", "-y", "--no-install-recommends"]
    assert steps[0]["argv"][4:] == ["golang", "rustc"]
    assert steps[2]["argv"][-1] == "uv==0.8.3" and steps[3]["argv"][-1] == "typescript@5.6.2"
    df = images.dockerfile("dev", pk, sha256="ab" * 32)
    assert df.startswith("# generated") and "FROM debian:trixie-slim" in df
    for line in df.splitlines():
        if line.startswith("RUN "):
            argv = json.loads(line[4:])            # exec form only: never a shell
            assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
    assert "USER 10001:10001" in df and "python3-venv" in df
    assert '"uv==0.8.3"' in df


# --- DB-backed ----------------------------------------------------------------

@pytest.fixture
async def db(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "vm_boxes_enabled", True)
    boxes.registry.reset()
    settings.vm_dir.mkdir(parents=True, exist_ok=True)
    await init_db()
    d = await get_db()
    await d.execute(
        "INSERT INTO security_profiles(name, builtin, box_image, allow_package_requests, "
        "service_placement, box_runtime) VALUES ('Default', 1, 'main', 1, 'per_project', 'kvm')")
    await d.execute("INSERT INTO projects(id, slug, name, path) VALUES (1, 'p1', 'p1', '/p')")
    await d.commit()
    yield d
    await d.close()
    boxes.registry.reset()


async def test_sync_variants_and_min_mem(db):
    info = await images.sync_variants(db)
    assert info["main"]["layer"] == [] and info["svc"]["layer"] == []
    assert info["desktop"]["min_mem_mb"] >= settings.vm_desktop_min_mem_mb
    assert images.min_mem_mb("desktop") >= 1280
    await images.create_variant(db, "gui2", "desktop", [])
    info = await images.sync_variants(db)
    assert info["gui2"]["min_mem_mb"] >= 1280           # inherits desktop's floor
    assert "desktop" in await images.descendants(db, "main")
    assert "gui2" in await images.descendants(db, "desktop")
    with pytest.raises(images.RecipeError):
        await images.create_variant(db, "dev", None, [])


async def test_resolver(db):
    base = settings.vm_dir / "base-v1.qcow2"
    base.write_bytes(b"q")
    await images.sync_variants(db)
    main = boxes.shared()
    assert images.resolve_image(main) == base              # main: empty layer = base
    b = boxes.allocate("project", project="p1", variant="dev")
    assert images.resolve_image(b) is None                 # dev never built: refuse
    with pytest.raises(boxes.BoxError):
        boxes.image_path(b)
    layer = images.layer_path("dev", 2)
    layer.write_bytes(b"L")
    await db.execute("INSERT INTO image_versions(variant, version, base_version, path, "
                     "status, active) VALUES ('dev', 2, 'v1', ?, 'built', 1)", (str(layer),))
    await db.commit()
    assert images.resolve_image(b) == layer
    assert boxes.image_path(b) == layer
    boxes.release(b.id)                                    # the RAM budget holds one
    builder = boxes.allocate("builder", variant="dev", version="build")
    assert images.resolve_image(builder) == base           # builders build on the base
    assert "base-v1.qcow2" in images.referenced_bases()


async def _mk_box_env(monkeypatch):
    """A fake kvm controller: boot creates the overlay; running() is false once
    the report arrives (the guest powered itself off)."""
    state = {}

    class Ctl:
        def __init__(self, box):
            self.box, self._run = box, False

        async def boot(self):
            self.box.dir.mkdir(parents=True, exist_ok=True)
            (self.box.dir / "overlay.qcow2").write_bytes(b"layer-bytes")
            self._run = True
            state["box"] = self.box
            state["ctl"] = self

        def running(self):
            return self._run

        async def teardown(self):
            self._run = False
            (self.box.dir / "overlay.qcow2").unlink(missing_ok=True)

    monkeypatch.setattr(boxes, "controller", lambda box: box.ctl or setattr(box, "ctl", Ctl(box)) or box.ctl)

    async def fake_exec(*argv, **kw):
        class P:
            returncode = 0

            async def communicate(self):
                return b"", b""
        state.setdefault("exec", []).append(argv)
        return P()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return state


async def _guest(state, job_report):
    """Play the guest: fetch the package, check it, report, power off."""
    for _ in range(200):
        if "box" in state:
            break
        await asyncio.sleep(0.01)
    box = state["box"]
    tar = tarfile.open(fileobj=io.BytesIO(images.builder.package(box)))
    names = set(tar.getnames())
    assert {"backend/__init__.py", "backend/server.py", "backend/job.json"} <= names
    job = json.loads(tar.extractfile("backend/job.json").read())
    state["job"] = job

    class Loop:
        async def sock_sendall(self, conn, data):
            state.setdefault("acks", []).append(json.loads(data))
    # a wrong token is refused
    await images.builder.on_report(Loop(), None, {"token": "nope", "phase": "result"}, box)
    assert state["acks"][-1]["error"] == "unknown_job"
    await images.builder.on_report(Loop(), None, {"token": job["token"], "phase": "log",
                                                  "line": "hello"}, box)
    await images.builder.on_report(Loop(), None, {"token": job["token"], "phase": "result",
                                                  **job_report(job)}, box)
    state["ctl"]._run = False


async def test_build_mocked_end_to_end(db, monkeypatch):
    base = settings.vm_dir / "base-v1.qcow2"
    base.write_bytes(b"q")
    state = await _mk_box_env(monkeypatch)
    row = await packages.file_request(db, manager="pip", package="rich", version=None,
                                      install_command="", reason="r", project="p1",
                                      conversation_id=None)
    await packages.set_resolution(db, row["id"], resolved_version="13.9.4",
                                  integrity="sha256:" + "a" * 64)
    await packages.approve(db, row["id"], target_variant="main")

    def rep(job):
        assert job["mode"] == "build" and job["variant"] == "main"
        assert job["items"] == [{"manager": "pip", "package": "rich", "version": "13.9.4",
                                 "integrity": "sha256:" + "a" * 64}]
        assert job["steps"][-1]["argv"][-1] == "rich==13.9.4"
        return {"ok": True, "baseline": {"dpkg": {"git": "1:2.47"}, "processes": ["systemd"],
                                         "evil": "x" * 10, "setuid": ["/usr/bin/su"]}}
    res, _ = await asyncio.gather(images.builder.build("main"), _guest(state, rep))
    assert res["ok"], res
    layer = images.layer_path("main", 1)
    assert layer.exists() and not layer.with_suffix(".qcow2.part").exists()
    bl = json.loads(images.baseline_file(layer).read_text())
    assert bl["dpkg"] == {"git": "1:2.47"} and "evil" not in bl
    assert bl["variant"] == "main" and bl["version"] == 1
    assert any(a[1] == "rebase" for a in state["exec"])
    assert (await packages.get(db, row["id"]))["status"] == "built"
    async with db.execute("SELECT status, active FROM image_versions WHERE variant='main'") as c:
        assert tuple(await c.fetchone()) == ("built", 1)
    async with db.execute("SELECT severity, detail FROM security_events "
                          "WHERE kind = 'image_variant_built'") as c:
        ev = await c.fetchone()
    assert ev[0] == "info" and "p1" in json.loads(ev[1])["variant_used_by"]
    assert images.resolve_image(boxes.shared()) == layer   # the new version is live
    assert [b.id for b in boxes.all_boxes()] == ["shared"]  # the builder was released


async def test_build_failure_keeps_old_version(db, monkeypatch):
    (settings.vm_dir / "base-v1.qcow2").write_bytes(b"q")
    state = await _mk_box_env(monkeypatch)
    await images.create_variant(db, "tools", "main",
                                [{"manager": "apt", "package": "ffmpeg"}])
    res, _ = await asyncio.gather(
        images.builder.build("tools"),
        _guest(state, lambda job: {"ok": False, "error": "E: integrity changed"}))
    assert not res["ok"] and "integrity" in res["error"]
    assert not images.layer_path("tools", 1).exists()
    assert not list(settings.vm_dir.glob("*.part"))
    async with db.execute("SELECT status FROM image_versions WHERE variant='tools'") as c:
        assert (await c.fetchone())[0] == "failed"
    # a page opened later still sees why it failed
    lb = next(v for v in (await images.list_images(db))["variants"]
              if v["name"] == "tools")["last_build"]
    assert lb["ok"] is False and "integrity" in lb["error"] and lb["version"] == 1
    assert lb["log_tail"] == ["hello"] and lb["finished_at"]
    assert images._last_build({"version": 2, "status": "built", "built_at": "t",
                               "build_log": "a\nERROR: guest line"})["error"] is None
    long = {"version": 3, "status": "failed", "built_at": "t",
            "build_log": "\n".join(str(i) for i in range(500)) + "\nERROR: boom"}
    lb = images._last_build(long)
    assert lb["error"] == "boom" and len(lb["log_tail"]) == 200 and lb["log_tail"][-1] == "499"
    b = boxes.allocate("project", project="p1", variant="tools")
    assert images.resolve_image(b) is None


async def test_resolve_job(db, monkeypatch):
    (settings.vm_dir / "base-v1.qcow2").write_bytes(b"q")
    state = await _mk_box_env(monkeypatch)
    r1 = await packages.file_request(db, manager="npm", package="left-pad", version=None,
                                     install_command="", reason="r", project="p1",
                                     conversation_id=None)
    r2 = await packages.file_request(db, manager="apt", package="nosuchpkg", version=None,
                                     install_command="", reason="r", project="p1",
                                     conversation_id=None)

    def rep(job):
        assert job["mode"] == "resolve"
        return {"ok": True, "results": [
            {"id": r1["id"], "version": "1.3.0", "integrity": "sha512-xyz"},
            {"id": r2["id"], "error": "not in the configured apt sources"},
            {"id": 9999, "version": "6.6.6"}]}
    out, _ = await asyncio.gather(images.builder.resolve_pending(), _guest(state, rep))
    assert out["resolved"] == 2
    a = await packages.get(db, r1["id"])
    b = await packages.get(db, r2["id"])
    assert a["resolved_version"] == "1.3.0" and a["integrity"] == "sha512-xyz"
    assert b["resolved_version"] is None and b["integrity"].startswith("unresolved")


def test_registration_hooks():
    assert images.resolve_image in boxes._image_resolvers


# --- build_base.sh ---------------------------------------------------------------

def test_build_base_reads_main_recipe_and_is_valid_bash():
    script = (ROOT / "vm" / "build_base.sh").read_text()
    assert subprocess.run(["bash", "-n", str(ROOT / "vm" / "build_base.sh")]).returncode == 0
    block = script[script.index('RECIPE="$SCRIPT_DIR'):script.index('[[ -n "$pkg_yaml" ]]')]
    out = subprocess.run(["bash", "-c", block + '\nprintf "%s" "$pkg_yaml"'],
                         env={"SCRIPT_DIR": str(ROOT / "vm"), "PATH": "/usr/bin:/bin"},
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    got = [ln[4:] for ln in out.stdout.splitlines()]
    want = [p["package"] for p in images.parse_recipe(
        (ROOT / "vm" / "images" / "main.recipe").read_text())["packages"]]
    assert sorted(got) == sorted(want) and "git" in got
    assert "jav3-baseline" in script


def test_build_base_baseline_parser(tmp_path):
    script = (ROOT / "vm" / "build_base.sh").read_text()
    code = re.search(r"# --- baseline-parse.*?# --- end baseline-parse ---", script, re.S).group(0)
    b = lambda s: base64.b64encode(s.encode()).decode()   # noqa: E731
    log = tmp_path / "console.log"
    log.write_text(
        "[  OK  ] noise\n"
        f"\x00JAV3-BASELINE dpkg {b('git\t1:2.47.2-0.1\njq\t1.7.1-3\n')}\r\n"
        f"[ 12.3] kernel: JAV3-BASELINE processes {b('systemd\njarvis\n')}\n"
        f"JAV3-BASELINE bogus {b('x')}\n"
        f"JAV3-BASELINE setuid !!notbase64!!\n"
        "JAV3-BASELINE-END\n")
    (tmp_path / "p.py").write_text(code)
    dst = tmp_path / "base-v9.baseline.json"
    r = subprocess.run(["python3", str(tmp_path / "p.py"), str(log), str(dst),
                        "base-v9.qcow2"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    bl = json.loads(dst.read_text())
    assert bl["dpkg"] == {"git": "1:2.47.2-0.1", "jq": "1.7.1-3"}
    assert bl["processes"] == ["systemd", "jarvis"] and "bogus" not in bl
    assert bl["image"] == "base-v9.qcow2"
    log.write_text("nothing here\n")
    r = subprocess.run(["python3", str(tmp_path / "p.py"), str(log), str(dst), "x"],
                       capture_output=True, text=True)
    assert r.returncode != 0


def test_builder_guest_is_stdlib_only():
    src = (ROOT / "backend" / "vm" / "builder_guest.py").read_text()
    imports = set(re.findall(r"^(?:import|from) ([a-z_]+)", src, re.M))
    assert imports <= {"datetime", "hashlib", "json", "os", "socket", "subprocess",
                       "sys", "tempfile", "time"}
    assert "shell=True" not in src
