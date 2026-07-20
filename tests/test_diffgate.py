"""Deterministic diff gates (Layer 6): pure scan heuristics + the persist/
block/acknowledge flow over real staged files."""
import json

import pytest

from backend import db as db_mod
from backend import diffgate, staging
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


# --- persist / block / acknowledge -------------------------------------------

@pytest.fixture
async def db(tmp_env):
    await db_mod.init_db()
    (settings.projects_dir / "proj").mkdir(parents=True)
    conn = await db_mod.get_db()
    yield conn
    await conn.close()


async def test_rescan_flags_and_blocks_approval(db):
    staging.stage_write("proj", "mod.py", b"import subprocess\nsocket.socket()\n")
    flags = await diffgate.rescan_project(db, "proj")
    trigs = {f["trigger"] for f in flags}
    assert "new_import" in trigs and "network_call" in trigs
    # unacknowledged -> approval blocked for that path
    assert await diffgate.blocking_paths(db, "proj") == {"mod.py"}
    # acknowledge every flag -> unblocked
    for f in flags:
        await diffgate.acknowledge(db, f["id"])
    assert await diffgate.blocking_paths(db, "proj") == set()


async def test_secret_leak_flagged_and_blocks(db):
    # save an operator secret, then stage a file that leaks its value
    (settings.secrets_path).write_text(json.dumps({"API_KEY": "supersecretvalue123"}))
    staging.stage_write("proj", "leak.py", b"KEY = 'supersecretvalue123'\n")
    await diffgate.rescan_project(db, "proj")
    flags = await diffgate.list_flags(db, "proj")
    assert any(f["trigger"] == "secret_leak" for f in flags)
    assert "leak.py" in await diffgate.blocking_paths(db, "proj")


async def test_rescan_clears_stale_flags(db):
    staging.stage_write("proj", "mod.py", b"import socket\n")
    await diffgate.rescan_project(db, "proj")
    assert await diffgate.blocking_paths(db, "proj") == {"mod.py"}
    # replace with a clean version, rescan -> flag gone
    staging.stage_write("proj", "mod.py", b"x = 1\n")
    await diffgate.rescan_project(db, "proj")
    assert await diffgate.blocking_paths(db, "proj") == set()


async def test_gate_flag_raises_security_event(db):
    from backend import security
    staging.stage_write("proj", "mod.py", b"import socket\n")
    await diffgate.rescan_project(db, "proj")
    assert await security.count_unacknowledged(db) >= 1
