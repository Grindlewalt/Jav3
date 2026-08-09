# voicedesk — Jarvis desktop mode, headless

Microphone and speaker always up, "hey Jarvis" to start, no browser tab. Starts
at login, survives reboots, reconnects on its own.

It speaks exactly the protocol the `/voice` page speaks, so the Pi's state
machine (`backend/voice.py`) does not know which client is attached. Wake word,
transcription, the double clap and every conversational rule are unchanged —
they all live on the Pi and the sidecar. **This process is a microphone, a
speaker and a socket**, which is the point: the machine sitting in the room
holds no keys, no model and no state.

## Install

```bash
cd clients/voicedesk
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`sounddevice` needs PortAudio: `brew install portaudio` on macOS,
`apt install libportaudio2` on Debian.

## Configure

The token goes in a file, not in argv — anything on the command line is visible
to every process on the machine via `ps`.

```bash
mkdir -p ~/.config/jarvis
cat > ~/.config/jarvis/voicedesk.json <<'EOF'
{
  "url": "wss://jarvis.example.com/api/voice/ws",
  "token": "the same value as JARVIS_VOICE_CLIENT_TOKEN on the Pi",
  "name": "studio"
}
EOF
chmod 600 ~/.config/jarvis/voicedesk.json
```

On the Pi, in `~/.config/jarvis/env`:

```
JARVIS_VOICE_CLIENT_TOKEN=<the same value>
```

Generate it with `openssl rand -hex 32`. Leaving it unset means the token path
does not exist at all — the WebSocket then only accepts a browser session
cookie, which is the safe default.

Optional keys: `in_device` / `out_device` (index or name — run
`--list-devices`), and `project` to pin this machine's conversations to one
project.

## Run

```bash
.venv/bin/python -m voicedesk.agent            # from clients/
.venv/bin/python -m voicedesk.agent --list-devices
.venv/bin/python -m voicedesk.agent -v         # every transcript and reply
```

Start it at login with `launchd` (macOS) — `com.jarvis.voicedesk.plist` in this
folder is a template; edit the two paths and:

```bash
cp com.jarvis.voicedesk.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.jarvis.voicedesk.plist
```

macOS will ask for microphone permission the first time. Because it is a
background agent, the prompt is attached to the terminal that loads it — run it
in the foreground once first, grant the permission, then load the agent.

## How it behaves

- **Standby.** Sessions start asleep. "Hey Jarvis" wakes it; two rising notes
  are the cue. After `JARVIS_VOICE_WAKE_TIMEOUT` seconds of quiet it dozes off
  again. "Jarvis, put something on" both wakes it and gets answered — the wake
  phrase is stripped from every utterance.
- **The double clap** plays one of the clap tracks, and is **ignored between
  22:30 and 07:30** so a dropped book at 3am does not start music. The wake
  word is not gated — it takes a spoken sentence to fire.
- **Interrupting works.** Talking over a reply pauses playback here instantly,
  then the Pi decides: real speech stops it, a guitar or a cough resumes it
  from exactly where it was. The decision needs silero and whisper, so it is
  made on the Pi; this side only provides the hair trigger.
- **Reconnects** with backoff (1→30s). Mic audio recorded while the socket is
  down is discarded rather than queued — replaying a minute of stale room noise
  at Jarvis on reconnect is worse than losing it.

## Wiring it to the projector

If the projection mapper is running with its MCP server on (see
`src/mcp/README.md` in that repo), the Pi will also mirror what it is hearing
and saying onto a chosen surface. Nothing is needed here — that feed goes Pi →
projector directly. Ask Jarvis to "put the voice display on the wall".

## Troubleshooting

| symptom | cause |
|---|---|
| exits with "need a url and a token" | no config file, and no `--url/--token` |
| closes immediately, code 4401 | token mismatch, or `JARVIS_VOICE_CLIENT_TOKEN` unset on the Pi |
| closes immediately, code 4404 | `JARVIS_VOICE_ENABLED` is not true on the Pi |
| connects, hears nothing | wrong input device — `--list-devices` and set `in_device` |
| hears itself / interrupts constantly | no echo cancellation on this box: move the mic off the speaker, or raise `VAD_PLAYING_MULT` in `agent.py` |
| "voicebox offline" in the log | the sidecar on the main server is down; the Pi retries by itself |
