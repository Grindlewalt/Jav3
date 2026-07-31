"""TARMAC (MyTube-Music): the operator's self-hosted music server.

    https://github.com/the-shadow-walker/MyTube-Music

A separate service, not a computer-use client. It has its own library, its own
players (the PWA, open on a phone or a desktop), and a documented agent API, so
Jarvis drives it over HTTP from here rather than through the desktop client.

Playback goes one of two ways.

**TARMAC's own players**, via POST /api/remote — the PWA on a phone or desktop.
Not through the desktop client's mpv: /stream/:id sits behind Cloudflare Access,
so feeding it to mpv would mean putting the Access secret into mpv's argv on the
operator's machine, visible in `ps`, for no benefit.

**Jarvis's own in-page player**, via `open_stream` below. TARMAC is a SEPARATE
Cloudflare Access application from Jarvis (different `aud`), so a browser holding
a Jarvis session cannot fetch music.atomos.network/stream/:id itself — the Access
cookie is per-application. The host holds the service token, so it fetches and
re-serves the bytes on Jarvis's own origin. TARMAC's README blesses exactly this:
"agents can still stream the audio themselves via /stream/:id". This is the path
that fixes the autoplay silence, because the Jarvis tab is the one the operator
is already touching.

Credentials live here and are never returned by any API or tool: the tab learns
only whether a token is set, exactly like the Jellyfin key.

Deliberately NOT behind the SSRF guard in websec.py. That guard refuses
non-public hosts, which is right for a URL the agent chose and wrong here — this
one is typed in by the operator and is usually a LAN address
(http://10.0.0.58:8788) or a Cloudflare hostname.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from .db import get_db, get_state, set_state

# TARMAC's own vocabulary. Not the same as computer_playback's: it has no "stop",
# and "prev" rather than "previous". Using its words avoids a translation layer
# that would silently drop an action.
# "shuffle" was missing here while the server has always accepted it
# (server.js REMOTE_ACTIONS) — so every shuffle request was refused by us, not
# by TARMAC. It takes `n` and `tag` instead of ids.
REMOTE_ACTIONS = ("play", "shuffle", "pause", "resume", "next", "prev")
TAGS = ("drive", "fast")

# Response headers worth passing through when re-serving audio. Range support is
# the whole point of the list: without Content-Range and Accept-Ranges an
# <audio> element cannot seek, and Safari will not begin playing at all.
_STREAM_HEADERS = ("content-type", "content-length", "content-range",
                   "accept-ranges", "etag", "last-modified")

_URL_KEY = "tarmac_url"
_ID_KEY = "tarmac_cf_id"
_SECRET_KEY = "tarmac_cf_secret"


class TarmacError(RuntimeError):
    """Something went wrong reaching or using TARMAC."""


async def get_config() -> tuple[str, str, str]:
    db = await get_db()
    try:
        return (await get_state(db, _URL_KEY) or "",
                await get_state(db, _ID_KEY) or "",
                await get_state(db, _SECRET_KEY) or "")
    except Exception:
        return "", "", ""
    finally:
        await db.close()


async def set_config(url: str, cf_id: str = "", cf_secret: str = "") -> None:
    url = (url or "").strip().rstrip("/")
    if url:
        u = urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise TarmacError("TARMAC URL must be http(s) with a host")
    db = await get_db()
    try:
        await set_state(db, _URL_KEY, url or None)
        # blank leaves the stored pair alone, so the tab can show the URL
        # without ever round-tripping the secret
        if cf_id:
            await set_state(db, _ID_KEY, cf_id.strip())
        if cf_secret:
            await set_state(db, _SECRET_KEY, cf_secret.strip())
        if not url:
            await set_state(db, _ID_KEY, None)
            await set_state(db, _SECRET_KEY, None)
    finally:
        await db.close()


def _check_redirect(status: int, location: str) -> None:
    """A 302 to a Cloudflare login page is the signature of a service token that
    is missing, expired, or has no Service Auth policy on THAT application.
    Access policies are per-application, so a token that works for Jarvis is not
    automatically accepted by the music host. Shared by the JSON calls and the
    audio stream so both name the same real cause."""
    if status not in (301, 302, 303, 307, 308):
        return
    if "cloudflareaccess.com" in location or "/cdn-cgi/access" in location:
        raise TarmacError(
            "Cloudflare Access refused the request — the music server is a "
            "SEPARATE Access application from Jarvis, so it needs its own "
            "Service Auth policy naming the service token, and the token "
            "has to be current. The operator can check both in Zero Trust.")
    raise TarmacError(f"unexpected redirect to {location[:80]}")


async def _auth_headers() -> tuple[str, dict]:
    """(base url, Access headers). Raises if TARMAC was never configured."""
    base, cf_id, cf_secret = await get_config()
    if not base:
        raise TarmacError(
            "the music server is not configured — the operator adds its URL on "
            "the Computer use tab")
    headers = {}
    if cf_id and cf_secret:
        headers["CF-Access-Client-Id"] = cf_id
        headers["CF-Access-Client-Secret"] = cf_secret
    return base, headers


async def _call(method: str, path: str, *, json_body: dict | None = None,
                params: dict | None = None, timeout: float = 20.0):
    """One request to TARMAC, with the Access headers attached here.

    Every error is turned into a sentence the model can act on, because "500"
    reaching a chat window helps nobody.
    """
    import httpx
    base, headers = await _auth_headers()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as c:
            r = await c.request(method, f"{base}{path}", json=json_body,
                                params=params, headers=headers)
    except Exception as e:
        raise TarmacError(f"could not reach the music server: {e}")

    _check_redirect(r.status_code, r.headers.get("location", ""))
    if r.status_code == 409:
        raise TarmacError(
            "no TARMAC player is open, so there is nothing to play on — ask the "
            "operator to open the music app on a device first")
    if r.status_code == 404:
        raise TarmacError("that track does not exist on the music server")
    if r.status_code >= 400:
        detail = ""
        try:
            detail = (r.json() or {}).get("error", "")
        except Exception:
            detail = (r.text or "")[:160]
        raise TarmacError(f"music server said {r.status_code}: {detail}")
    try:
        return r.json()
    except Exception:
        raise TarmacError("the music server did not return JSON")


# --- the operations the tools use --------------------------------------------

async def status() -> dict:
    return await _call("GET", "/api/status")


async def search(query: str, tag: str | None = None, limit: int = 25) -> list[dict]:
    if tag and tag not in TAGS:
        raise TarmacError(f"tag must be one of {', '.join(TAGS)}")
    params: dict = {"q": (query or "").strip(),
                    "limit": max(1, min(int(limit or 25), 100))}
    if tag:
        params["tag"] = tag
    rows = await _call("GET", "/api/search", params=params)
    return rows if isinstance(rows, list) else []


async def track(track_id: int) -> dict:
    """One track's metadata. The in-page player needs the duration, which
    /api/search does return but a caller working from bare ids will not have."""
    return await _call("GET", f"/api/tracks/{int(track_id)}")


async def random_playlist(n: int = 20, tag: str | None = None) -> list[dict]:
    if tag and tag not in TAGS:
        raise TarmacError(f"tag must be one of {', '.join(TAGS)}")
    params: dict = {"n": max(1, min(int(n or 20), 200))}
    if tag:
        params["tag"] = tag
    rows = await _call("GET", "/api/playlist/random", params=params)
    return rows if isinstance(rows, list) else []


async def scrobble(track_id: int) -> None:
    """Count a play. The in-page player streams straight from /stream/:id, which
    does not touch the plays table, so without this a track listened to inside
    Jarvis would never show up in TARMAC's play counts. Best-effort: a missed
    scrobble must never break playback."""
    try:
        await _call("POST", "/api/play", json_body={"id": int(track_id)})
    except (TarmacError, ValueError, TypeError):
        pass


async def remote(action: str, ids: list[int] | None = None, *,
                 n: int | None = None, tag: str | None = None) -> dict:
    if action not in REMOTE_ACTIONS:
        raise TarmacError(f"action must be one of {', '.join(REMOTE_ACTIONS)}")
    body: dict = {"action": action}
    if action == "play":
        if not ids:
            raise TarmacError("play needs at least one track id")
        clean = []
        for i in ids:
            if isinstance(i, bool) or not isinstance(i, int):
                raise TarmacError("track ids must be whole numbers")
            clean.append(i)
        body["ids"] = clean
    if action == "shuffle":
        if tag:
            if tag not in TAGS:
                raise TarmacError(f"tag must be one of {', '.join(TAGS)}")
            body["tag"] = tag
        if n is not None:
            body["n"] = max(1, min(int(n), 200))
    return await _call("POST", "/api/remote", json_body=body)


# --- audio, re-served on Jarvis's origin --------------------------------------

class StreamHandle:
    """An open /stream/:id response: status and headers now, bytes on demand.

    Not an async context manager on purpose. A StreamingResponse consumes its
    iterator *after* the route function returns, so a `with` block in the route
    would close the connection before a single byte was sent. Instead the route
    reads .status/.headers straight away and hands .chunks() to the response;
    that generator closes the client in its own finally, which also fires when
    the browser disconnects mid-track (seeking does exactly that).
    """

    def __init__(self, client, response):
        self._client = client
        self._response = response
        self.status = response.status_code
        self.headers = {k: v for k, v in response.headers.items()
                        if k.lower() in _STREAM_HEADERS}

    async def chunks(self):
        try:
            async for chunk in self._response.aiter_bytes(65536):
                yield chunk
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        try:
            await self._response.aclose()
        finally:
            await self._client.aclose()


async def open_stream(track_id: int, range_header: str | None = None) -> StreamHandle:
    """Open GET /stream/:id with the Access headers attached, Range forwarded.

    Streamed, never buffered — a Pi with 3.7 GB should not hold a whole track in
    memory per listener. The caller owns the handle and must consume or close it.
    """
    import httpx
    base, headers = await _auth_headers()
    if range_header:
        headers["Range"] = range_header
    # No overall timeout: a long track is a long read by definition. The read
    # timeout is per-chunk, so a genuinely stalled connection still fails.
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
        follow_redirects=False)
    try:
        req = client.build_request("GET", f"{base}/stream/{int(track_id)}",
                                   headers=headers)
        r = await client.send(req, stream=True)
    except Exception as e:
        await client.aclose()
        raise TarmacError(f"could not reach the music server: {e}")

    try:
        _check_redirect(r.status_code, r.headers.get("location", ""))
        if r.status_code == 404:
            raise TarmacError("that track does not exist on the music server")
        if r.status_code >= 400:
            raise TarmacError(f"music server said {r.status_code} for that track")
    except TarmacError:
        await r.aclose()
        await client.aclose()
        raise
    return StreamHandle(client, r)


async def download(url: str) -> dict:
    u = urlsplit((url or "").strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise TarmacError("give a full http(s) YouTube URL")
    return await _call("POST", "/api/download", json_body={"url": url}, timeout=30)


async def download_status(job: str) -> dict:
    if not str(job).strip():
        raise TarmacError("which job?")
    return await _call("GET", f"/api/download/{urlsplit(str(job)).path.strip('/')}")
