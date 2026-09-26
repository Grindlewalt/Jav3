"""scripts/install.sh: the read-only modes run to completion under `bash -u`
(set -u regressions die before printing a line), and the Arch commands it runs
or prints are never a partial upgrade."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "install.sh"


def _bash() -> str | None:
    # the script needs bash >= 4 (empty arrays under set -u); macOS /bin/bash is 3.2
    for b in ("/opt/homebrew/bin/bash", "/usr/local/bin/bash", shutil.which("bash")):
        if b and os.path.exists(b):
            out = subprocess.run([b, "-c", "echo ${BASH_VERSINFO[0]}"],
                                 capture_output=True, text=True).stdout.strip()
            if out.isdigit() and int(out) >= 4:
                return b
    return None


BASH = _bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="no bash >= 4")


def run(tmp_path, *args):
    env = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    return subprocess.run([BASH, "-u", str(SCRIPT), *args], capture_output=True,
                          text=True, env=env, timeout=60)


def test_never_pacman_sy_without_u():
    for f in (SCRIPT, ROOT / "scripts" / "bootstrap.sh"):
        assert not re.search(r"pacman -Sy\b(?!u)", f.read_text()), f


@needs_bash
@pytest.mark.parametrize("args", [["--check"], ["--check", "--name", "x", "--port", "8780"]])
def test_check_runs_to_the_end(tmp_path, args):
    r = run(tmp_path, *args)
    out = r.stdout + r.stderr
    assert "unbound variable" not in out
    assert "preflight" in out
    assert r.returncode in (0, 1)


@needs_bash
def test_named_instance_uses_its_own_config_dir(tmp_path):
    out = run(tmp_path, "--check", "--name", "x").stdout
    assert ".config/jarvis-x/env" in out and "jarvis-x.service" in out


@needs_bash
def test_help(tmp_path):
    r = run(tmp_path, "--help")
    assert r.returncode == 0 and "--name" in r.stdout and "--port" in r.stdout


@needs_bash
@pytest.mark.parametrize("args,msg", [(["--check", "--name", "a/b"], "--name wants"),
                                      (["--check", "--port", "abc"], "--port wants")])
def test_bad_args_are_named(tmp_path, args, msg):
    r = run(tmp_path, *args)
    assert r.returncode != 0 and msg in r.stderr


@needs_bash
def test_foreign_config_dir_is_a_conflict(tmp_path):
    cfg = tmp_path / ".config" / "jarvis"
    cfg.mkdir(parents=True)
    (cfg / "config.yaml").write_text("other: app\n")
    out = run(tmp_path, "--check").stdout
    assert "already holds files from something else" in out


@needs_bash
def test_conflict_does_not_also_advise_creating_the_env(tmp_path):
    cfg = tmp_path / ".config" / "jarvis"
    cfg.mkdir(parents=True)
    (cfg / "config.yaml").write_text("other: app\n")
    out = run(tmp_path, "--check").stdout
    assert "touch" not in out and "service not installed" not in out


def test_checkout_remembers_its_instance(tmp_path, monkeypatch):
    from backend import config
    (tmp_path / ".jarvis-instance").write_text(
        "JARVIS_INSTANCE=test\nJARVIS_CONFIG_DIR=/srv/cfg-test\n")
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    assert config._instance_config_dir() == "/srv/cfg-test"
    (tmp_path / ".jarvis-instance").unlink()
    assert config._instance_config_dir() is None


def _load_module_harness(tmp_path, modinfo_out, modprobe_rc):
    """Run install.sh's load_module() alone, with modinfo/modprobe stubbed."""
    text = SCRIPT.read_text()
    start = text.index("load_module() {")
    fn = text[start:text.index("\n}\n", start) + 3]
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "modinfo").write_text(f"#!/bin/sh\necho '{modinfo_out}'\n")
    (stub / "modprobe").write_text(f"#!/bin/sh\nexit {modprobe_rc}\n")
    for f in stub.iterdir():
        f.chmod(0o755)
    conf = tmp_path / "m.conf"
    prog = ("set -euo pipefail\nok(){ echo ok \"$*\"; }\nbad(){ echo MISS \"$*\"; }\n"
            "CHANGES=() ROOT_FAILED=0\n" + fn +
            f"load_module vhost_vsock {conf}\n"
            'echo "failed=$ROOT_FAILED changes=${#CHANGES[@]}"\n')
    env = {"PATH": f"{stub}:/usr/bin:/bin"}
    out = subprocess.run([BASH, "-c", prog], capture_output=True, text=True,
                         env=env).stdout
    return out, conf


@needs_bash
def test_builtin_module_is_not_persisted(tmp_path):
    out, conf = _load_module_harness(tmp_path, "(builtin)", 0)
    assert "built into the kernel" in out and "failed=0 changes=0" in out
    assert not conf.exists()


@needs_bash
def test_failed_modprobe_is_not_ok_and_not_persisted(tmp_path):
    out, conf = _load_module_harness(tmp_path, "/lib/modules/x/vhost_vsock.ko", 1)
    assert "MISS" in out and not out.startswith("ok") and "failed=1" in out
    assert not conf.exists()


@needs_bash
def test_loaded_module_is_persisted_once(tmp_path):
    out, conf = _load_module_harness(tmp_path, "/lib/modules/x/vhost_vsock.ko", 0)
    assert "failed=0 changes=1" in out and conf.read_text() == "vhost_vsock\n"
