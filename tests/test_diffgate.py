"""Deterministic diff gates: pure scan heuristics + the advisory write-time
flow (writes.apply_write lands the file, raises deduped security events, and
refuses secret leaks outright)."""
import json

import pytest

from backend import db as db_mod
from backend import diffgate, writes
from backend.config import settings


# --- pure scan ---------------------------------------------------------------

def test_new_import_flagged():
    flags = diffgate.scan("print(1)\n", "import socket\nprint(1)\n", "a.py")
    trigs = {f["trigger"] for f in flags}
    assert "new_import" in trigs
    imp = next(f for f in flags if f["trigger"] == "new_import")
    assert "socket" in imp["detail"]["modules"]


def test_network_call_flagged():
    old = "def f():\n    return 1\n"
    new = "import requests\ndef f():\n    return requests.get('http://x')\n"
    trigs = {f["trigger"] for f in diffgate.scan(old, new, "a.py")}
    assert "network_call" in trigs and "new_import" in trigs


def test_high_entropy_blob_flagged():
    blob = "aGVsbG8gd29ybGQgdGhpcyBpcyBhIHZlcnkgbG9uZyBiYXNlNjQgcGF5bG9hZA1234567890AAAA"
    trigs = {f["trigger"] for f in diffgate.scan("x = 1\n", f"payload = '{blob}'\n", "a.py")}
    assert "high_entropy" in trigs


def test_logging_removed_flagged():
    old = "import logging\nlogging.info('a')\nlogging.error('b')\n"
    new = "import logging\nlogging.info('a')\n"          # one logging call gone
    trigs = {f["trigger"] for f in diffgate.scan(old, new, "a.py")}
    assert "logging_removed" in trigs


def test_assertion_removed_flagged():
    old = "def test_x():\n    assert 1 == 1\n    assert 2 == 2\n"
    new = "def test_x():\n    assert 1 == 1\n"
    trigs = {f["trigger"] for f in diffgate.scan(old, new, "test_x.py")}
    assert "assertion_removed" in trigs


def test_clean_edit_no_flags():
    old = "def add(a, b):\n    return a + b\n"
    new = "def add(a, b):\n    # sum two numbers\n    return a + b\n"
    assert diffgate.scan(old, new, "a.py") == []


def test_noncode_file_skipped():
    # a huge base64 in a data file shouldn't gate
    assert diffgate.scan("", "AAAA" * 50, "data.csv") == []


# --- advisory write-time flow ------------------------------------------------

@pytest.fixture
async def db(tmp_env):
    await db_mod.init_db()
    (settings.projects_dir / "proj").mkdir(parents=True)
    conn = await db_mod.get_db()
    yield conn
    await conn.close()


async def test_flagged_write_lands_and_alerts(db):
    from backend import security
    triggers = await writes.apply_write(
        "proj", "mod.py", b"import subprocess\nsocket.socket()\n")
    assert "new_import" in triggers and "network_call" in triggers
    # ADVISORY: the write landed despite the flags
    assert (settings.projects_dir / "proj" / "mod.py").exists()
    # ...and each trigger raised a security event for the Review Center
    assert await security.count_unacknowledged(db) >= 2


async def test_flag_events_dedup_per_path_trigger(db):
    from backend import security
    await writes.apply_write("proj", "mod.py", b"import socket\n")
    first = await security.count_unacknowledged(db)
    # iterating on the same flagged file must not drown the bell
    await writes.apply_write("proj", "mod.py", b"import socket\nx = 1\n")
    assert await security.count_unacknowledged(db) == first


async def test_secret_leak_write_refused(db):
    from backend import security
    (settings.secrets_path).write_text(json.dumps({"API_KEY": "supersecretvalue123"}))
    with pytest.raises(writes.SecretLeakError):
        await writes.apply_write("proj", "leak.py", b"KEY = 'supersecretvalue123'\n")
    # refused: nothing landed, and the alert fired
    assert not (settings.projects_dir / "proj" / "leak.py").exists()
    assert await security.count_unacknowledged(db) >= 1


async def test_clean_write_no_events(db):
    from backend import security
    triggers = await writes.apply_write("proj", "notes.md", b"# hello\n")
    assert triggers == []
    assert (settings.projects_dir / "proj" / "notes.md").read_text() == "# hello\n"
    assert await security.count_unacknowledged(db) == 0


async def test_write_refuses_protected_paths(db):
    with pytest.raises(ValueError):
        await writes.apply_write("proj", ".git/config", b"x")
    with pytest.raises(ValueError):
        await writes.apply_write("proj", ".staging/sneak.py", b"x")
