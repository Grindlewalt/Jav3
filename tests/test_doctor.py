"""backend.cli doctor: read-only install report, one stage per step, each
unfinished one with its fix and who can do it (docs/AGENT-INSTALL.md)."""
import json

from backend import doctor


def test_unknown_flag_prints_usage(capsys):
    assert doctor.run(["--fix"]) == 64
    assert "usage" in capsys.readouterr().err


def test_json_shape(capsys, monkeypatch, tmp_env):
    monkeypatch.setattr(doctor, "_health", lambda port: False)
    rc = doctor.run(["--json"])
    d = json.loads(capsys.readouterr().out)
    assert rc == (0 if d["ready"] else 1)
    ids = [s["id"] for s in d["stages"]]
    for want in ("venv", "frontend", "config", "service", "health", "docker",
                 "login", "provider", "profile", "gitea"):
        assert want in ids, want
    for s in d["stages"]:
        assert s["ok"] in (True, False, None)
        if s["ok"] is False:
            assert s["fix"] and s["who"] in ("agent", "human"), s
        else:
            assert s["fix"] == "" and s["who"] == ""
    health = next(s for s in d["stages"] if s["id"] == "health")
    assert health["ok"] is False and not d["ready"]
    assert d["next"] == next(s["id"] for s in d["stages"]
                             if s["ok"] is False and not s["optional"])


def test_unit_follows_the_config_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "CONFIG_DIR", tmp_path / "jarvis-test")
    assert doctor.unit_name() == "jarvis-test.service"
    monkeypatch.setattr(doctor, "CONFIG_DIR", tmp_path / "jarvis")
    assert doctor.unit_name() == "jarvis.service"
