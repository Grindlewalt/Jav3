# What Jarvis needs from TARMAC

For whoever works on **MyTube-Music / TARMAC** (`github.com/the-shadow-walker/MyTube-Music`).
Written from a read of `server/server.js` at the current HEAD, not from the
README — line numbers are against that file.

Jarvis now has its own in-page music player. It does **not** replace TARMAC's
PWA; it is a second listening surface for when the operator is already in
Jarvis. Nothing below is required for what already ships — the player works
today against the API as it stands. These are the things that are currently
either impossible or forced into a workaround.

## How Jarvis talks to TARMAC now, so nothing here surprises you

- All calls carry `CF-Access-Client-Id` / `CF-Access-Client-Secret`. TARMAC is a
  **separate Cloudflare Access application** from Jarvis (`aud` `3cf396d2…` vs
  `0548b1f3…`), so a browser holding a Jarvis session gets nothing from the music
  host. That is not a bug on your side, it is how Access works.
- Because of that, **Jarvis proxies the audio**: the host fetches
  `GET /stream/:id` with the service token and re-serves it on Jarvis's own
  origin, forwarding `Range` and passing the `206` straight back. Your README
  already blesses this ("agents can still stream the audio themselves via
  `/stream/:id`"). `res.sendFile` (server.js:196) gives us working Range for
  free — please keep it, or keep whatever replaces it Range-capable. Seeking and
  Safari both break the moment a `206` becomes a `200`.
- Jarvis counts a play through `POST /api/play` when its own player starts a
  track, so in-Jarvis listening still lands in your `plays` table.
- Jarvis's player reports its state **to Jarvis**, not to `POST
  /api/player/state`. See request 2 for why, and why that is a workaround rather
  than a preference.

## 1. Album art  — the biggest single gap

**Today:** there is no artwork anywhere. The `tracks` table (server.js:26-35) has
`id, path, title, artist, album, duration, tag, added_at` and no art column, and
no route serves an image.

**Why it matters:** the whole ask for the Jarvis player was "glassy, translucent,
clean". A player with no cover art is a text row with buttons. This is the one
item that changes how the thing feels rather than what it can do.

**Why it should be cheap for you:** you are already downloading the art. The
yt-dlp invocation at server.js:258 passes `--embed-thumbnail
--convert-thumbnails jpg`, so the cover is sitting inside the audio files
already — it just is not exposed.

**What we would use:**

```
GET /api/tracks/:id/art     → image bytes, or 404 when the file has none
```

Ideally with `Cache-Control: public, max-age=…` and an ETag, since the player
will request it once per track change per listener. A `has_art: true|false` field
on the track objects returned by `/api/search`, `/api/tracks/:id` and
`/api/library` would let a client skip the request entirely rather than 404 on
every trackless-art row.

Extracting on demand with ffmpeg is fine; a cached derivative directory is
better. We do not need multiple sizes — one reasonably sized square is plenty.

## 2. Let `/api/remote` target one player

**Today:** `players` is a `Set` of raw SSE response objects (server.js:380) with
no identity, and `/api/remote` writes the event to **every** one of them
(server.js:423). `playerState` is a single global (server.js:348) that whichever
player reported last overwrites, and `/api/status` reads that one global
(server.js:439-449).

**Why it matters:** with the phone PWA and a desktop PWA both open, "pause"
pauses both, and "what's playing" answers for whichever device spoke most
recently. There is no way to ask for or address a specific device. This is also
exactly why the Jarvis player reports to Jarvis instead of to
`POST /api/player/state` — if it reported to you it would clobber the phone's
state, and the operator would get a "now playing" that flickers between devices.

**What we would use:**

```
GET  /api/players            → [{id, name, connected_at, now_playing}]
POST /api/remote             → accepts an optional {target: "<player id>"};
                               absent target keeps today's broadcast behaviour
GET  /api/events?name=Phone  → the player names itself on subscribe
POST /api/player/state       → carries the player's own id
GET  /api/status             → now_playing stays as-is for compatibility,
                               plus a per-player breakdown
```

An id assigned by the server on subscribe and handed back down the SSE stream is
enough; it does not need to survive a reconnect. If players could self-report a
name we would show the operator real device names instead of "player 1".

Once this exists, Jarvis's player can register as a normal TARMAC player and the
two surfaces stop being separate worlds — that is the version worth building
toward, but it needs your side first.

## 3. Saved playlists

**Today:** `/api/playlist/random` (server.js:392) is the only playlist concept on
the listening side. `playlistId` elsewhere in the file is about *downloading* a
YouTube playlist, which is a different thing.

**What we would use:** somewhere to persist and fetch a named ordered list.

```
GET    /api/playlists              → [{id, name, count}]
GET    /api/playlists/:id          → the tracks, in order
POST   /api/playlists              → {name, ids}
PUT    /api/playlists/:id          → replace ids (reorder / add / remove)
DELETE /api/playlists/:id
```

Lower priority than 1 and 2. It only becomes interesting once the agent is
building queues the operator wants to keep, and right now Jarvis constructs a
queue per request and throws it away.

## 4. Small things

- **`GET /api/agent` (server.js:452) does not list `POST /api/player/state` or
  document `GET /api/events`'s event shape**, and both are load-bearing for
  anything that wants to be a player. The cheat sheet is a good idea — it is
  worth keeping complete, since an agent reading it blind is the stated point.
- **`/api/search` is `LIKE %q%` over title/artist/album** (server.js:355-361), so
  it misses ordinary near-misses. Jarvis does its own ranking on top
  (`backend/musicpick.py`) and does not need this changed — flagging it only so
  you know why we over-fetch (`limit=60`) and re-rank locally rather than
  trusting the order you return.
- **Nothing to report here on auth.** The service-token flow works once the music
  application has its own Service Auth policy. When it does not, the 302 to
  `cloudflareaccess.com` is the signature, and `service_token_status: false` in
  the redirect's meta JWT means the token was not evaluated at all.

## What we are NOT asking for

- Volume or output-device control in your API. Those belong to whatever is
  actually rendering audio; for the Jarvis player that is an `<audio>` element we
  control, and for a desktop file it is mpv on the operator's machine.
- CORS headers. Jarvis proxies server-side, so the browser never talks to you
  directly and cross-origin never enters into it.
- Any change to `/stream/:id` beyond keeping Range working.
