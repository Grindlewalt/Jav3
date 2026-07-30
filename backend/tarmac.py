"""TARMAC (MyTube-Music): the operator's self-hosted music server.

    https://github.com/the-shadow-walker/MyTube-Music

A separate service, not a computer-use client. It has its own library, its own
players (the PWA, open on a phone or a desktop), and a documented agent API, so
Jarvis drives it over HTTP from here rather than through the desktop client.

Playback deliberately goes to TARMAC's OWN players via POST /api/remote, not
through the desktop client's mpv. /stream/:id sits behind Cloudflare Access, so
feeding it to mpv would mean putting the Access secret into mpv's argv on the
operator's machine — visible in `ps`, for no benefit. The PWA is already
authenticated and already the thing they listen on.

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
REMOTE_ACTIONS = ("play", "pause", "resume", "next", "prev")
TAGS = ("drive", "fast")

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


async def _call(method: str, path: str, *, json_body: dict | None = None,
                params: dict | None = None, timeout: float = 20.0):
    """One request to TARMAC, with the Access headers attached here.

    Every error is turned into a sentence the model can act on, because "500"
    reaching a chat window helps nobody.
    """
    import httpx
    base, cf_id, cf_secret = await get_config()
    if not base:
        raise TarmacError(
            "the music server is not configured — the operator adds its URL on "
            "the Computer use tab")
    headers = {}
    if cf_id and cf_secret:
        headers["CF-Access-Client-Id"] = cf_id
        headers["CF-Access-Client-Secret"] = cf_secret
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as c:
            r = await c.request(method, f"{base}{path}", json=json_body,
                                params=params, headers=headers)
    except Exception as e:
        raise TarmacError(f"could not reach the music server: {e}")

    # A 302 to a Cloudflare login page is the signature of a service token that
    # is missing, expired, or has no Service Auth policy on THAT application.
    # Access policies are per-application, so a token that works for Jarvis is
    # not automatically accepted by the music host.
    if r.status_code in (301, 302, 303, 307, 308):
        loc = r.headers.get("location", "")
        if "cloudflareaccess.com" in loc or "/cdn-cgi/access" in loc:
            raise TarmacError(
                "Cloudflare Access refused the request — the music server is a "
                "SEPARATE Access application from Jarvis, so it needs its own "
                "Service Auth policy naming the service token, and the token "
                "has to be current. The operator can check both in Zero Trust.")
        raise TarmacError(f"unexpected redirect to {loc[:80]}")
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


async def remote(action: str, ids: list[int] | None = None) -> dict:
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
    return await _call("POST", "/api/remote", json_body=body)


async def download(url: str) -> dict:
    u = urlsplit((url or "").strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise TarmacError("give a full http(s) YouTube URL")
    return await _call("POST", "/api/download", json_body={"url": url}, timeout=30)


async def download_status(job: str) -> dict:
    if not str(job).strip():
        raise TarmacError("which job?")
    return await _call("GET", f"/api/download/{urlsplit(str(job)).path.strip('/')}")
