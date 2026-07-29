# Jarvis computer-use client

Runs on the machine you want Jarvis to drive. It dials **out** to Jarvis, so
nothing listens on your computer and quitting the process ends all access.

```
pip install -r requirements.txt

python3 agent.py \
  --server https://jarvis.example \
  --token  <from the Computer use tab> \
  --allow-root ~/Music \
  --allow-root ~/Videos
```

Add `--dry-run` to have it print what it would do and touch nothing. Good for
the first run.

## What it can do

| Verb | Linux | macOS |
|---|---|---|
| volume up/down/set/mute | ✅ wpctl or pactl | ✅ CoreAudio via ctypes |
| pause / next / previous / stop | ✅ MPRIS over D-Bus | ✅ synthesized media keys |
| open an http(s) link | ✅ xdg-open | ✅ open |
| play audio/video, chosen screen + audio device | ✅ mpv | ✅ mpv |
| list screens | ✅ xrandr | ✅ NSScreen |
| list audio outputs | ✅ pactl + mpv | ⚠️ default device only |

**Run `--selftest` first on any new machine.** It reports what is actually
reachable instead of failing silently later.

### macOS notes

- Volume is a direct CoreAudio call through `ctypes` — no subprocess, and no
  extra dependency. It tries `kAudioHardwareServiceDeviceProperty_VirtualMainVolume`
  first and falls back to `kAudioDevicePropertyVolumeScalar`, because many
  devices have no master channel.
- Transport synthesizes the keyboard's own media keys, so it drives whatever
  has the system's attention rather than one named app. This needs
  **Accessibility permission** (System Settings → Privacy & Security →
  Accessibility). On macOS 15 that grant lapses after a reboot; the client
  checks with `CGPreflightPostEventAccess` and tells you rather than doing
  nothing.
- Picking a specific *mixer* output on macOS is not wired up — volume applies to
  the default device. Choosing where a *played file* goes does work
  (`--audio-device`, from mpv's own list).
- `osascript` is deliberately not in the binary allowlist. AppleScript can
  `do shell script "..."`, so allowing it would reopen the exact path this
  client exists to close.

## Why there is no shell

- Commands arrive as a **verb name plus typed parameters**, checked against a
  closed table (`VERBS`) before anything happens. No verb carries a command, an
  argv array or a format string, so there is no field for an injection to
  travel in.
- The client **re-validates everything itself** and refuses what it does not
  recognise. It does not trust the server — if Jarvis is compromised, that must
  not widen what this will do.
- Only the binaries in `BINARIES` can ever run. Each is resolved to an absolute
  path once at startup and frozen, then spawned with an argv **list** and
  `shell=False` in a scrubbed environment. No shell is in that list.
- `--allow-root` is the ceiling. Folder grants made in the Jarvis GUI are
  intersected with it, so a grant for a folder you did not name here is ignored.
- mpv is started with `--no-config --load-scripts=no`, because a Lua script in
  `~/.config/mpv` would otherwise be arbitrary code running off a play command.

`tests/test_computeruse_noshell.py` in the main repo parses this file and fails
the build if any of that stops being true — including a mutation check that the
guards actually fire.

## What it can reach on disk

Only the folders you passed `--allow-root` **and** granted in the GUI, and only
real audio/video files in them. Paths are resolved before the containment check,
so a symlink pointing out of a granted tree is refused. There is no verb that
lists a directory.
