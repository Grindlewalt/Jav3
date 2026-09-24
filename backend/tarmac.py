"""TARMAC (MyTube-Music): the operator's self-hosted music server.

    https://github.com/the-shadow-walker/MyTube-Music

A separate service with its own library, its own players (the PWA, open on a
phone or a desktop), and a documented agent API, so Jav3 drives it over HTTP.

Playback goes one of two ways.

**TARMAC's own players**, via POST /api/remote — the PWA on a phone or desktop.

**Jav3's own in-page player**, via `open_stream` below. The host fetches
/stream/:id and re-serves the bytes on Jav3's own origin, so the browser only
ever talks to Jav3 — one session, one origin, no mixed content. TARMAC's README
blesses exactly this: "agents can still stream the audio themselves via
/stream/:id". This is the path that fixes the autoplay silence, because the
Jav3 tab is the one the operator is already touching.

Deliberately NOT behind the SSRF guard in websec.py. That guard refuses
non-public hosts, which is right for a URL the agent chose and wrong here — this
one is typed in by the operator and is normally a LAN address.
"""
from __future__ import annotations

import time
from urllib.parse import urlsplit

from .db import get_db, get_state, set_state

# TARMAC's own vocabulary: it has no "stop",
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


class TarmacError(RuntimeError):
    """Something went wrong reaching or using TARMAC."""


async def get_config() -> str:
    """The music server's base URL, or "" when it was never configured."""
    db = await get_db()
    try:
        return await get_state(db, _URL_KEY) or ""
    except Exception:
        return ""
    finally:
        await db.close()


async def set_config(url: str) -> None:
    url = (url or "").strip().rstrip("/")
    if url:
        u = urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise TarmacError("TARMAC URL must be http(s) with a host")
    db = await get_db()
    try:
        await set_state(db, _URL_KEY, url or None)
    finally:
        await db.close()


def _check_redirect(status: int, location: str) -> None:
    """A redirect from an API or stream route means something in front of the
    music server (an auth proxy, a login page) answered instead of TARMAC.
    Shared by the JSON calls and the audio stream so both name the same cause."""
    if status not in (301, 302, 303, 307, 308):
        return
    raise TarmacError(
        f"the music server redirected to {location[:80]} instead of answering — "
        f"something in front of it (a login page or auth proxy) is intercepting "
        f"Jav3's requests. Point the TARMAC URL at the server directly.")


async def _base() -> str:
    """The base url. Raises if TARMAC was never configured."""
    base = await get_config()
    if not base:
        raise TarmacError(
            "the music server is not configured — the operator adds its URL in "
            "Settings")
    return base


async def _call(method: str, path: str, *, json_body: dict | None = None,
                params: dict | None = None, timeout: float = 20.0):
    """One request to TARMAC.

    Every error is turned into a sentence the model can act on, because "500"
    reaching a chat window helps nobody.
    """
    import httpx
    base = await _base()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as c:
            r = await c.request(method, f"{base}{path}", json=json_body,
                                params=params)
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


# The whole library, cached, for the voice tier's system prompt. A ~30-track
# library is a few hundred tokens — cheaper than the music_search round trip it
# replaces, and it lets the model match a half-heard spoken title against real
# ones instead of inventing a plausible song. (limit, expiry, rows)
_library_cache: tuple[int, float, list[dict]] = (0, 0.0, [])


async def cached_library(limit: int, ttl: int) -> list[dict]:
    """Every track, memoised for `ttl` seconds. Returns [] rather than raising:
    this feeds a prompt, and a music server that is down must not take voice
    mode with it."""
    global _library_cache
    want, expiry, rows = _library_cache
    now = time.monotonic()
    if rows and want == limit and now < expiry:
        return rows
    try:
        fresh = await search("", None, limit)
    except Exception:  # noqa: BLE001 — unreachable/unconfigured/timeout alike
        return rows if want == limit else []
    _library_cache = (limit, now + max(1, ttl), fresh)
    return fresh


async def voice_library_prompt() -> str:
    """The library block for the voice local tier's system prompt, or ''.

    ONE definition on purpose: chat.py builds the real turn's prompt and
    voice.py pre-warms the same prefix into llama.cpp's slot KV, and if those
    two ever disagree by a byte the warm is wasted.
    """
    from .config import settings
    from .voice_text import library_block
    if not settings.voice_library_in_prompt:
        return ""
    tracks = await cached_library(settings.voice_library_max_tracks,
                                  settings.voice_library_ttl_seconds)
    block = library_block(tracks)
    return f"\n\n{block}" if block else ""


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
    Jav3 would never show up in TARMAC's play counts. Best-effort: a missed
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


# --- audio, re-served on Jav3's origin --------------------------------------

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
    """Open GET /stream/:id with Range forwarded.

    Streamed, never buffered — a Pi with 3.7 GB should not hold a whole track in
    memory per listener. The caller owns the handle and must consume or close it.
    """
    import httpx
    base = await _base()
    headers = {"Range": range_header} if range_header else {}
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
