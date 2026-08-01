# Jarvis computer-use client

Runs on the machine you want Jarvis to drive. It dials **out** to Jarvis, so
nothing listens on your computer and quitting the process ends all access.

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python agent.py --setup \
  --server https://jarvis.example \
  --token  <from the Computer use tab> \
  --allow-root ~/Music \
  --allow-root ~/Videos
```

`--setup` is the whole first run, in order, so a failure stops at the step that
caused it rather than three steps later:

1. **Reaches Jarvis over plain HTTP** and reports what happened. A wrong
   address, a rotated pairing token and a missing Cloudflare service token are
   indistinguishable from inside the WebSocket retry loop — all three come back
   as `server rejected the connection`, forever. Over HTTP each has its own
   status code, so each gets its own sentence.
2. **Saves the settings** to `~/.config/jarvis/computeruse.json` (0600). Every
   later run — including the service — reads them, so nothing needs the flags
   again.
3. **Runs the selftest** and prints one install command for whatever is missing,
   using the package manager this machine actually has (`pacman`, `apt`, `dnf`,
   `zypper`, `apk`, `brew`) and that manager's names for the packages.
4. **Connects**, in the foreground, so it appears on the Computer use tab.

The Computer use tab builds that command with the token filled in. Run
`--selftest` alone at any time for step 3 on its own, and add `--dry-run` to
have the client print what it would do and touch nothing.

## What it can do

| Verb | Linux | macOS |
|---|---|---|
| volume up/down/set/mute | ✅ wpctl or pactl | ✅ CoreAudio via ctypes |
| move all sound to another speaker | ✅ pactl default sink + move streams | ✅ CoreAudio default output |
| pause / next / previous / stop | ✅ MPRIS over D-Bus | ✅ synthesized media keys |
| open an http(s) link | ✅ xdg-open | ✅ open |
| play audio/video, chosen screen + audio device | ✅ mpv | ✅ mpv |
| list screens | ✅ xrandr | ✅ NSScreen |
| list audio outputs | ✅ pactl + mpv | ✅ CoreAudio device list |

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
- Speakers are enumerated from CoreAudio with the names Sound preferences uses,
  and identified by their UID — which is also what mpv calls a device
  (`coreaudio/<uid>`), so the mixer list and the playback list finally speak one
  vocabulary. `volume action=output` moves the *system* default, the way the
  menu-bar picker does, so Spotify and Safari move with it.
- Raising the volume also clears the mute. Mute is a separate CoreAudio property
  from the level, so setting a level on a muted Mac used to report "40%" into
  silence.
- Binaries are looked up on PATH **and** in `/opt/homebrew/bin`, `/usr/local/bin`
  and friends. A launchd agent inherits launchd's PATH, not a login shell's, so
  Homebrew's mpv is invisible to it — "mpv is not installed" on a machine where
  `which mpv` answers fine, and only once you make the client permanent.
- `osascript` is deliberately not in the binary allowlist. AppleScript can
  `do shell script "..."`, so allowing it would reopen the exact path this
  client exists to close.

## Making it permanent

After `--setup` the settings are already saved, so this takes no flags:

```
.venv/bin/python agent.py --install
```

Setting it up and making it permanent in one go works too — every flag
`--setup` takes, `--install` takes as well:

```
.venv/bin/python agent.py --install \
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

The service runs the *same interpreter you ran `--install` with*, so run it with
the venv's python — a unit pointing at a system python that never had
`websockets` installed starts and immediately dies.

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
.venv/bin/python agent.py --server https://jarvis.example --token ... --allow-root ~/Music
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
- Every path is resolved and checked for containment **on this side** before
  anything opens it, against the folders granted in the Jarvis GUI.
- mpv is started with `--no-config --load-scripts=no`, because a Lua script in
  `~/.config/mpv` would otherwise be arbitrary code running off a play command.

`--allow-root` used to be a hard ceiling that GUI grants could only narrow. That
was removed on 2026-07-30, at the operator's instruction: it made the Computer
use tab dishonest. A folder granted there but not named on the command line
looked accepted and reached nothing, and the only way to add one was to stop the
client and re-run set-up with another flag — so folders, the setting that changes
most, were the one thing the GUI could not change. The grant list in Jarvis is
now simply what this client uses, applied live.

`tests/test_computeruse_noshell.py` in the main repo parses this file and fails
the build if any of that stops being true — including a mutation check that the
guards actually fire.

## What it can reach on disk

The folders granted on Jarvis's **Computer use** tab, and only real audio/video
files in them. Paths are resolved before the containment check, so a symlink
pointing out of a granted tree is refused.

Add or remove a folder there and this client is told at once — no restart, no
re-running set-up. `--allow-root` only seeds the list for the seconds before
Jarvis answers, and is optional.

A folder that cannot be used **says why**, on the Computer use tab and in
`status`: it does not exist here, it is not a directory, or macOS privacy is
refusing the listing (Desktop, Documents, Downloads, iCloud Drive and external
volumes are TCC-protected — grant Full Disk Access to whatever runs this).
Before that, a rejected folder and a folder nobody had granted produced the
same empty list, so adding folders and then being told there were none was
consistent behaviour rather than a contradiction.

## Which build this is

`--selftest` prints a `client build` fingerprint, and the client reports it when
it connects. Jarvis compares it with the source it serves and flags the machine
if they differ. A CDN in front of Jarvis has twice cached the download and left
a machine running a build that predated the fix being chased — from the tab that
looked exactly like a broken client.

## Removing it

```bash
python3 agent.py --uninstall           # drop the service, keep the settings
python3 agent.py --uninstall --purge   # ...and delete the saved pairing token
```

It prints the one `launchctl`/`systemctl` line that stops a running copy — that
is left to you rather than done behind your back. Then delete this folder.
