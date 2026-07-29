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
| volume up/down/set/mute | ✅ wpctl or pactl | ❌ not implemented |
| pause / next / previous / stop | ✅ MPRIS over D-Bus | ❌ not implemented |
| open an http(s) link | ✅ xdg-open | ✅ open |
| play audio/video, chosen screen + audio device | ✅ mpv | ✅ mpv |
| list screens / audio devices | ✅ xrandr, pactl | ❌ returns empty |

macOS volume and transport are deliberately absent rather than shimmed through
`osascript`: that would mean handing a scripting interpreter a string, which is
the one thing this client exists to avoid. They need CoreAudio and MediaRemote
bindings.

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
