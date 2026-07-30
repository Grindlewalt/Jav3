"""Persistence: the saved config and the service definitions.

The invariant that matters here is where the secrets are NOT. A systemd unit is
0644 and a launchd plist is world-readable and Spotlight-indexed, so a token in
either is published to every account on the machine. They must carry a path and
nothing else.
"""
import importlib.util
import plistlib
import stat
from pathlib import Path

import pytest

CLIENT_DIR = Path(__file__).resolve().parents[1] / "clients" / "computeruse"

TOKEN = "TePz_test_pairing_token_value_here_0123456789"
CF_SECRET = "9fada4ab86dc0a5ced63092006433f3326df2058048a2a22a44a66f3a1c427f6"


def _mod(name):
    spec = importlib.util.spec_from_file_location(
        f"cu_{name}_t", CLIENT_DIR / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def cfg():
    return _mod("config")


@pytest.fixture
def svc():
    return _mod("service")


# --- the config file ---------------------------------------------------------

def test_saved_config_is_only_readable_by_its_owner(cfg, tmp_path):
    p = tmp_path / "sub" / "computeruse.json"
    cfg.save({"server": "https://x", "token": TOKEN}, p)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700


def test_a_world_readable_config_is_refused_not_used(cfg, tmp_path):
    """A pairing token the whole machine can read is not a token."""
    p = tmp_path / "computeruse.json"
    cfg.save({"token": TOKEN}, p)
    p.chmod(0o644)
    with pytest.raises(cfg.ConfigError) as e:
        cfg.load(p)
    assert "readable by other accounts" in str(e.value)
    assert "chmod 600" in str(e.value)


def test_a_group_writable_config_is_refused(cfg, tmp_path):
    p = tmp_path / "computeruse.json"
    cfg.save({"token": TOKEN}, p)
    p.chmod(0o620)
    with pytest.raises(cfg.ConfigError):
        cfg.load(p)


def test_missing_config_is_not_an_error(cfg, tmp_path):
    assert cfg.load(tmp_path / "nope.json") == {}


def test_a_mistyped_key_is_reported_rather_than_ignored(cfg, tmp_path):
    """Silently dropping 'allow_root' when the key is 'roots' would leave the
    operator believing they had granted a folder."""
    p = tmp_path / "computeruse.json"
    p.write_text('{"allow_root": ["/music"]}')
    p.chmod(0o600)
    with pytest.raises(cfg.ConfigError) as e:
        cfg.load(p)
    assert "allow_root" in str(e.value) and "roots" in str(e.value)


def test_bad_json_says_so(cfg, tmp_path):
    p = tmp_path / "computeruse.json"
    p.write_text("{not json")
    p.chmod(0o600)
    with pytest.raises(cfg.ConfigError) as e:
        cfg.load(p)
    assert "valid JSON" in str(e.value)


def test_saving_merges_rather_than_clobbers(cfg, tmp_path):
    p = tmp_path / "computeruse.json"
    cfg.save({"server": "https://x", "token": TOKEN}, p)
    cfg.save({"name": "laptop"}, p)          # a later partial save
    got = cfg.load(p)
    assert got["token"] == TOKEN and got["name"] == "laptop"


def test_empty_values_do_not_erase_saved_ones(cfg, tmp_path):
    p = tmp_path / "computeruse.json"
    cfg.save({"token": TOKEN}, p)
    cfg.save({"token": None, "server": "https://x"}, p)
    assert cfg.load(p)["token"] == TOKEN


def test_secrets_are_redacted_for_printing(cfg):
    out = cfg.redacted({"token": TOKEN, "cf_access_secret": CF_SECRET,
                        "server": "https://x"})
    assert TOKEN not in str(out)
    assert CF_SECRET not in str(out)
    assert out["server"] == "https://x"       # non-secrets stay legible


# --- the service definitions -------------------------------------------------

def test_the_systemd_unit_contains_no_secrets(svc):
    """A unit file is 0644. Anything in it is public to the machine."""
    unit = svc.systemd_unit(Path("/opt/jarvis/clients/computeruse/agent.py"))
    for secret in (TOKEN, CF_SECRET):
        assert secret not in unit
    # and nothing that would tempt someone to put one there
    assert "Environment=" not in unit
    assert "CF_ACCESS" not in unit


def test_the_launchd_plist_contains_no_secrets(svc):
    raw = svc.launchd_plist(Path("/opt/jarvis/clients/computeruse/agent.py"))
    body = plistlib.loads(raw)
    assert "EnvironmentVariables" not in body
    text = raw.decode()
    for secret in (TOKEN, CF_SECRET):
        assert secret not in text


def test_the_plist_is_built_with_plistlib_so_it_always_parses(svc):
    """Hand-templated XML is one apostrophe in a username away from a file
    launchd refuses to load."""
    raw = svc.launchd_plist(Path("/opt/j/agent.py"))
    body = plistlib.loads(raw)          # would raise on malformed XML
    assert body["Label"] == svc.LABEL
    assert body["ProgramArguments"][-1].endswith("agent.py")
    assert body["RunAtLoad"] is True and body["KeepAlive"] is True


def _sections(unit: str) -> dict[str, list[str]]:
    """Split a unit into {section: [directive lines]}, ignoring comments.

    Line-based on purpose: splitting the text on "[Service]" also matches the
    word inside a comment, which is how the first version of this test managed
    to fail against a correct unit.
    """
    out: dict[str, list[str]] = {}
    current = None
    for line in unit.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith(";"):
            continue
        if s.startswith("[") and s.endswith("]"):
            current = s[1:-1]
            out.setdefault(current, [])
        elif current:
            out[current].append(s)
    return out


def test_start_limit_lives_in_the_unit_section(svc):
    """systemd moved StartLimitIntervalSec to [Unit]; left in [Service] it is
    reported as an unknown key and ignored, so the protection is silently
    absent. `systemd-analyze verify` caught this one."""
    sec = _sections(svc.systemd_unit(Path("/opt/j/agent.py")))
    unit_keys = [d.split("=")[0] for d in sec.get("Unit", [])]
    service_keys = [d.split("=")[0] for d in sec.get("Service", [])]
    assert "StartLimitIntervalSec" in unit_keys, "must be in [Unit] to take effect"
    assert "StartLimitIntervalSec" not in service_keys


def test_the_unit_restarts_forever(svc):
    unit = svc.systemd_unit(Path("/opt/j/agent.py"))
    assert "Restart=always" in unit


def test_it_is_tied_to_a_graphical_session(svc):
    """Opening a link and playing video both need a display; running while
    nobody is logged in would just fail on every verb."""
    unit = svc.systemd_unit(Path("/opt/j/agent.py"))
    assert "WantedBy=graphical-session.target" in unit


def test_install_can_be_asked_not_to_write_anything(svc, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    target, steps = svc.install(Path("/opt/j/agent.py"), dry_run=True)
    assert not target.exists()
    assert steps, "the operator still needs to be told what to run"


# --- the macOS backend, checked as far as a Linux box can ---------------------

def test_macos_volume_failure_stays_inside_its_own_error_type():
    """MacOS.volume() catches CoreAudioError. A bare OSError from the framework
    load escapes it and surfaces to the model as a traceback rather than a
    refusal, so the load is wrapped."""
    m = _mod("macos")
    with pytest.raises(m.CoreAudioError):
        m.default_output_device()


def test_macos_backend_refuses_cleanly_rather_than_crashing(monkeypatch):
    """On a machine where CoreAudio will not load, every macOS verb should come
    back as a Refused the model can read, not an exception."""
    agent_mod = _mod("agent")
    r = agent_mod.Runner(dry_run=True)
    mac = agent_mod.MacOS(r)
    for call in (lambda: mac.volume("up"), lambda: mac.transport("next")):
        with pytest.raises(agent_mod.Refused):
            call()
    # the read-only surfaces degrade to empty instead of raising
    assert mac.audio_devices() == [] or isinstance(mac.audio_devices(), list)
    assert isinstance(mac.screens(), list)
    assert mac.players() == []


def test_the_child_environment_carries_tmpdir_for_macos():
    agent_mod = _mod("agent")
    assert "TMPDIR" in agent_mod._ENV_KEEP
