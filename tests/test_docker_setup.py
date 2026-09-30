"""docker-setup: the installer's Docker step (backend/docker_setup.py)."""
import subprocess

from backend import docker_setup


def test_unknown_flag_prints_usage(capsys):
    assert docker_setup.run(["--help"]) == 2
    assert "usage" in capsys.readouterr().out


def test_no_docker_skips_with_instructions(monkeypatch, capsys):
    monkeypatch.setattr(docker_setup.shutil, "which", lambda name: None)
    assert docker_setup.run([]) == 1
    out = capsys.readouterr().out
    assert "not installed" in out and "docker-setup" in out


def test_src_sha_tracks_the_build_context(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM x\n")
    a = docker_setup.src_sha(tmp_path)
    (tmp_path / "bootstrap.py").write_text("print(1)\n")
    assert docker_setup.src_sha(tmp_path) != a


def test_current_image_is_not_rebuilt(monkeypatch, capsys):
    calls = []

    def fake_docker(*args, **kw):
        calls.append(args)
        if args[:2] == ("image", "inspect"):
            return subprocess.CompletedProcess(args, 0, docker_setup.src_sha() + "\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(docker_setup, "_docker", fake_docker)
    monkeypatch.setattr(docker_setup.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("built")))
    assert docker_setup.ensure_image(docker_setup.Plan(False), rebuild=False)
    assert "is current" in capsys.readouterr().out
    assert not any(c[0] == "build" for c in calls)


def _build_env(monkeypatch, buildx: bool):
    """Run ensure_image against a fake docker; return the env `docker build` got."""
    seen = {}

    def fake_docker(*args, **kw):
        if args[:2] == ("buildx", "version"):
            return subprocess.CompletedProcess(args, 0 if buildx else 1, "", "")
        if args[:2] == ("image", "inspect"):
            return subprocess.CompletedProcess(args, 1, "", "no such image")
        return subprocess.CompletedProcess(args, 0, "", "")

    def fake_run(argv, **kw):
        seen["argv"], seen["env"] = argv, kw.get("env") or {}
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(docker_setup, "_docker", fake_docker)
    monkeypatch.setattr(docker_setup.subprocess, "run", fake_run)
    monkeypatch.setattr(docker_setup, "_image_sha", lambda ref: None if ref == docker_setup.settings.docker_image_turn
                        else docker_setup.src_sha())
    assert docker_setup.ensure_image(docker_setup.Plan(False), rebuild=False)
    return seen["env"]


def test_build_uses_buildkit_only_with_buildx(monkeypatch, capsys):
    assert _build_env(monkeypatch, buildx=True)["DOCKER_BUILDKIT"] == "1"
    assert "classic builder" not in capsys.readouterr().out
    # Arch ships buildx as its own package: forcing BuildKit there aborted the build
    assert _build_env(monkeypatch, buildx=False)["DOCKER_BUILDKIT"] == "0"
    assert "classic builder" in capsys.readouterr().out


def test_dockerfile_builds_under_the_classic_builder():
    # the classic builder rejects `COPY --chmod` (BuildKit only)
    lines = [ln for ln in (docker_setup.SRC / "Dockerfile").read_text().splitlines()
             if ln.split("#")[0].strip().startswith("COPY")]
    assert lines and not any("--chmod" in ln for ln in lines)


def test_done_line_names_this_instances_unit(monkeypatch, capsys):
    """A named instance (--name test) runs as jarvis-test, not jarvis."""
    from backend import doctor
    monkeypatch.setattr(doctor, "unit_name", lambda: "jarvis-test.service")
    monkeypatch.setattr(docker_setup, "check_docker", lambda: None)
    monkeypatch.setattr(docker_setup, "_docker",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "true\n", ""))
    monkeypatch.setattr(docker_setup.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(docker_setup, "ensure_image", lambda plan, rebuild: True)
    monkeypatch.setattr(docker_setup, "smoke", lambda plan: True)
    monkeypatch.setattr(docker_setup, "set_env", lambda *a, **k: None)
    assert docker_setup.run([]) == 0
    assert "systemctl --user restart jarvis-test)" in capsys.readouterr().out
