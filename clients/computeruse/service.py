"""Generate the service definition that keeps the client running.

  Linux  -> a systemd **user** unit (~/.config/systemd/user/), matching how
            Jarvis itself runs on the Pi.
  macOS  -> a launchd LaunchAgent (~/Library/LaunchAgents/).

Neither definition contains a secret. Both are world-readable by default —
0644 for a unit, and a plist that Spotlight will happily index — so a token in
`Environment=` or `EnvironmentVariables` would be published to every account on
the machine. They carry a path to the 0600 config file and nothing else. A test
asserts it stays that way.

The plist is built with plistlib rather than a formatted string: hand-templated
XML is one apostrophe in a username away from an unparseable file.
"""
from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path

LABEL = "network.atomos.jarvis.computeruse"
UNIT_NAME = "jarvis-computeruse.service"


def _paths(agent_path: Path) -> tuple[str, str]:
    return sys.executable, str(agent_path.resolve())


def systemd_unit(agent_path: Path) -> str:
    python, agent = _paths(agent_path)
    return f"""\
[Unit]
Description=Jarvis computer-use client
Documentation=file://{Path(agent).parent}/README.md
# the client dials out, so it needs the network up but nothing listening
After=network-online.target graphical-session.target
Wants=network-online.target
# A dropped tunnel must not trip the start-limit burst and wedge the service.
# This key lives in [Unit], not [Service] — systemd moved it, and
# `systemd-analyze verify` reports "Unknown key ... ignoring" if you get it
# wrong, which means the protection silently is not there.
StartLimitIntervalSec=0

[Service]
Type=simple
# No secrets here on purpose: a unit file is world-readable, so the token lives
# in ~/.config/jarvis/computeruse.json at 0600 and this only knows where to look.
ExecStart={python} {agent}
Restart=always
RestartSec=5

[Install]
# graphical-session, not default: opening a link and playing video both need a
# display, so there is no point running while nobody is logged in
WantedBy=graphical-session.target
"""


def launchd_plist(agent_path: Path) -> bytes:
    python, agent = _paths(agent_path)
    logs = Path.home() / "Library" / "Logs"
    plist = {
        "Label": LABEL,
        "ProgramArguments": [python, agent],
        "RunAtLoad": True,
        "KeepAlive": True,          # restart if it exits for any reason
        "ThrottleInterval": 5,
        "ProcessType": "Interactive",   # it drives the UI session
        "StandardOutPath": str(logs / "jarvis-computeruse.log"),
        "StandardErrorPath": str(logs / "jarvis-computeruse.err.log"),
        # Deliberately no EnvironmentVariables: a plist is world-readable, so a
        # secret here would be public. The client reads the 0600 config instead.
    }
    return plistlib.dumps(plist)


def install(agent_path: Path, dry_run: bool = False) -> tuple[Path, list[str]]:
    """Write the definition; return (path, the commands the operator runs).

    Enabling is left to the operator on purpose — this writes a file they can
    read first, rather than starting a background process on their machine as a
    side effect of a --install flag.
    """
    if sys.platform == "darwin":
        target = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        body = launchd_plist(agent_path)
        steps = [
            f"launchctl bootstrap gui/{os.getuid()} {target}",
            f"launchctl kickstart -p gui/{os.getuid()}/{LABEL}",
            "# stop with: launchctl bootout gui/$UID/" + LABEL,
            "# logs: ~/Library/Logs/jarvis-computeruse.log",
            "# NOTE: media keys need Accessibility permission, and it is granted",
            "#       to the python binary running this. System Settings >",
            "#       Privacy & Security > Accessibility.",
        ]
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        return target, steps

    target = (Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
              / "systemd" / "user" / UNIT_NAME)
    body = systemd_unit(agent_path).encode()
    steps = [
        "systemctl --user daemon-reload",
        f"systemctl --user enable --now {UNIT_NAME}",
        "# survive logout / run headless:  loginctl enable-linger $USER",
        f"# logs:  journalctl --user -u {UNIT_NAME} -f",
        "# if opening links or video fails, the session did not export a display:",
        "#   systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY",
    ]
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    return target, steps


def service_path() -> Path:
    """Where install() put the definition on this platform."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    return (Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            / "systemd" / "user" / UNIT_NAME)


def uninstall(dry_run: bool = False) -> tuple[list[Path], list[str]]:
    """Stop the service and take its definition away.

    Returns (files removed, what the operator still has to do). Stopping is
    theirs to run for the same reason enabling was: this removes files, it does
    not reach into a running session and kill things behind their back.

    Deliberately does NOT touch the config file — that holds the pairing token
    and the operator may only be replacing the service. --purge does that.
    """
    removed: list[Path] = []
    target = service_path()
    if sys.platform == "darwin":
        steps = [
            f"launchctl bootout gui/{os.getuid()}/{LABEL}   # if it is running",
        ]
    else:
        steps = [
            f"systemctl --user disable --now {UNIT_NAME}    # if it is running",
            "systemctl --user daemon-reload",
        ]
    if target.exists() and not dry_run:
        target.unlink()
        removed.append(target)
    elif target.exists():
        removed.append(target)
    return removed, steps
