"""e2e BUG-9: base images left cloud-init with unmet Depends (dpkg
--force-depends), so every apt layer build exited 100. The builder finishes the
removal before installing (never `apt-get -f install`, which would reinstall
the network tools the image removed), the base build purges it at poweroff, and
the stored error keeps apt's root-cause tail."""
import subprocess

from backend.config import settings
from backend.vm import builder_guest, images

APT_TAIL = ("cloud-init : Depends: netcat-openbsd but it is not going to be "
            "installed\nE: Unmet dependencies. Try 'apt --fix-broken install'")


def _fake_run(monkeypatch, broken_until_purge=True):
    calls, state = [], {"broken": broken_until_purge}

    def run(argv, *, env=None, timeout=1800, check=True):
        calls.append(argv)
        rc = 0
        if argv[:2] == ["apt-get", "check"] and state["broken"]:
            rc = 100
        if argv[:2] == ["dpkg", "--purge"]:
            state["broken"] = False
        if check and rc:
            raise RuntimeError(f"{argv[0]} exited {rc}")
        return subprocess.CompletedProcess(argv, rc, "")
    monkeypatch.setattr(builder_guest, "run", run)
    monkeypatch.setattr(builder_guest, "log", lambda line: None)
    return calls


def test_repair_purges_cloud_init_not_fix_broken(monkeypatch):
    calls = _fake_run(monkeypatch)
    builder_guest.repair_dpkg({})
    assert ["dpkg", "--purge", "--force-depends", "cloud-init"] in calls
    assert not any("-f" in c or "--fix-broken" in c for c in calls)
    assert calls[-1][:2] == ["apt-get", "check"]


def test_repair_is_a_noop_on_a_clean_image(monkeypatch):
    calls = _fake_run(monkeypatch, broken_until_purge=False)
    builder_guest.repair_dpkg({})
    assert calls == [["apt-get", "check"]]


def test_errors_keep_the_root_cause_tail():
    long = "RuntimeError: apt-get install exited 100: " + "Reading lists...\n" * 400 + APT_TAIL
    assert "cloud-init : Depends: netcat-openbsd" in builder_guest.clip_error(long)
    assert len(builder_guest.clip_error(long)) <= builder_guest.ERR_MAX
    host = images._s_tail(long, 2000)
    assert "cloud-init : Depends: netcat-openbsd" in host and host.startswith("RuntimeError")
    assert "netcat-openbsd" in images._s_tail(long, 300)


def test_base_build_purges_cloud_init_after_it_exits():
    text = (settings.base_dir / "vm" / "build_base.sh").read_text()
    assert "jav3-finish-purge.service" in text
    assert "Before=cloud-final.service" in text
    assert "dpkg --purge --force-depends cloud-init" in text
    assert "systemctl start --no-block jav3-finish-purge.service" in text
