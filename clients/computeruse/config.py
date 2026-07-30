"""Persistent settings for the computer-use client.

Everything the client needs to come back after a reboot without anyone typing a
secret: the server URL, the pairing token, the Cloudflare Access service token,
and the folder roots.

    ~/.config/jarvis/computeruse.json      (mode 0600)

Why a file rather than the service manager's environment: both systemd units and
launchd plists are world-readable by default. Putting a secret in one publishes
it to every account on the machine. Secrets live here, at 0600, and the unit
holds nothing but a path — enforced by a test.

Precedence is command line > environment > this file, so a one-off override
still works and nothing about the existing flags changes.

This file is the operator's, and it is the trust root: `roots` here is the
ceiling on what Jarvis can ever play, exactly as --allow-root was. Nothing the
server sends can edit it — the client only ever reads it.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "jarvis"
CONFIG_PATH = CONFIG_DIR / "computeruse.json"

# what may appear in the file; anything else is a typo worth reporting rather
# than ignoring silently
KEYS = ("server", "token", "name", "roots", "cf_access_id", "cf_access_secret")
SECRET_KEYS = ("token", "cf_access_secret")


class ConfigError(Exception):
    pass


def load(path: Path | None = None) -> dict:
    """Read the config, or {} if there isn't one.

    Refuses a file other accounts can read: a pairing token that the whole
    machine can see is not a token.
    """
    p = path or CONFIG_PATH
    if not p.exists():
        return {}
    mode = p.stat().st_mode
    if mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
        raise ConfigError(
            f"{p} is readable by other accounts (mode {stat.S_IMODE(mode):04o}). "
            f"It holds a pairing token and possibly a Cloudflare secret.\n"
            f"    chmod 600 {p}")
    try:
        data = json.loads(p.read_text() or "{}")
    except json.JSONDecodeError as e:
        raise ConfigError(f"{p} is not valid JSON: {e}")
    if not isinstance(data, dict):
        raise ConfigError(f"{p} should contain a JSON object")
    unknown = set(data) - set(KEYS)
    if unknown:
        raise ConfigError(
            f"{p} has key(s) I don't understand: {', '.join(sorted(unknown))}. "
            f"Known: {', '.join(KEYS)}")
    if "roots" in data and not isinstance(data["roots"], list):
        raise ConfigError("'roots' should be a list of folder paths")
    return data


def save(values: dict, path: Path | None = None) -> Path:
    """Write the config at 0600, creating the directory 0700.

    The mode is set before the content is written, so there is no window where
    the secret exists in a world-readable file.
    """
    p = path or CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    existing = {}
    if p.exists():
        try:
            existing = json.loads(p.read_text() or "{}")
        except json.JSONDecodeError:
            existing = {}
    merged = {**existing, **{k: v for k, v in values.items() if v not in (None, "", [])}}
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(merged, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.chmod(p, 0o600)
    return p


def redacted(values: dict) -> dict:
    """For printing. Never show a secret back at the terminal — it ends up in
    scrollback, and from there in a screenshot."""
    out = dict(values)
    for k in SECRET_KEYS:
        if out.get(k):
            out[k] = f"<set, {len(str(out[k]))} chars>"
    return out
