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
