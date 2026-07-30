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

## Making it permanent

Save the settings once, then let the OS keep it running:

```
python3 agent.py --install \
  --server https://jarvis.example \
  --token  <from the Computer use tab> \
  --allow-root ~/Music --allow-root ~/Videos \
  --cf-access-id '<id>.access' --cf-access-secret '<secret>'
```

That writes two files and prints the commands to enable the service:

| file | mode | holds |
|---|---|---|
| `~/.config/jarvis/computeruse.json` | **0600** | server, pairing token, CF token, roots |
| systemd unit / launchd plist | 0644 | a path to the above, and nothing else |

**Secrets are in the config, never in the service definition.** A systemd unit
is world-readable and a launchd plist is world-readable *and* Spotlight-indexed,
so a token in either is published to every account on the machine. The client
refuses to start if the config itself is group- or world-readable.

After `--install`, running `agent.py` with no arguments at all picks everything
up from the config. Flags and environment variables still override it, so a
one-off `--dry-run` or a different `--server` works unchanged.

**Linux** (systemd user service):

```
systemctl --user daemon-reload
systemctl --user enable --now jarvis-computeruse.service
loginctl enable-linger $USER          # survive logout
journalctl --user -u jarvis-computeruse.service -f
```

It is wanted by `graphical-session.target`, since opening a link and playing
video both need a display. If those verbs fail but volume works, the session
did not export one:

```
systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY
```

**macOS** (LaunchAgent, `KeepAlive` + `RunAtLoad`):

```
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/network.atomos.jarvis.computeruse.plist
launchctl kickstart -p gui/$UID/network.atomos.jarvis.computeruse
launchctl bootout   gui/$UID/network.atomos.jarvis.computeruse   # stop
tail -f ~/Library/Logs/jarvis-computeruse.log
```

One macOS wrinkle: Accessibility permission for media keys is granted to the
**python binary** running the client, not to the script. If you change
interpreter (a new venv, a Homebrew upgrade) you will have to grant it again —
`--selftest` reports whether it currently holds.

## Through a reverse proxy / Cloudflare Tunnel

The client makes one outbound WebSocket to `<server>/api/computeruse/agent`, so
`--server https://jarvis.example` dials `wss://jarvis.example/api/computeruse/agent`.
Nothing listens on the machine being driven; it works fine behind NAT.

Cloudflare proxies WebSockets by default, but **resets an idle one after about
100 seconds**, and cloudflared drops idle HTTP/2 streams to the origin sooner
than that. The client pings every 20s to stay under both, and reconnects with
backoff if it is dropped anyway. Measured against a real tunnel: 240s of
complete idleness, one connect and zero disconnects.

**Cloudflare Access needs a service token.** If the hostname is protected by
Access, an unauthenticated request is redirected to an SSO login page — fine for
a browser, impossible for a daemon. Create a service token in Zero Trust
(Access → Service Auth), add a policy on the app with action *Service Auth*
allowing that token, and give the client:

```
export CF_ACCESS_CLIENT_ID='<id>.access'
export CF_ACCESS_CLIENT_SECRET='<secret>'
python3 agent.py --server https://jarvis.example --token ... --allow-root ~/Music
```

It sends them as `CF-Access-Client-Id` / `CF-Access-Client-Secret` on the
upgrade request. Use the environment rather than the `--cf-access-*` flags where
you can — a command line is visible in `ps`.

Scope the Access policy to the path `/api/computeruse/agent` if you can, so the
service token cannot be used to reach the rest of Jarvis.

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
