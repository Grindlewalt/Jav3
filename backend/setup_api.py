"""First-run setup: create the operator's login and, optionally, connect a
model provider — the web `/setup` page and `backend.cli setup` both land here.

Every route in this module is unauthenticated and exists only while the users
table is empty. The moment a user exists, all of them refuse (409), so this is
a door that closes behind the first person through it, not a second login.

The check and the insert are ONE statement (`INSERT ... SELECT ... WHERE NOT
EXISTS`), which SQLite runs atomically, so two setups racing each other cannot
both create an operator: exactly one row lands and the loser gets a 409.

The provider step is written against PR1's `backend.providers` (imported
lazily). Without it, the key still gets stored — as `PROVIDER_<ID>_API_KEY`
in the 0600 secrets store — and the catalogue falls back to a short seed list.
Keys stay host-side either way; model calls go out from the host gateway, never
from the sandbox VM.
"""
import asyncio
import re
import time

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from . import auth
from .db import get_db

router = APIRouter(prefix="/api/setup", tags=["setup"])

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MIN_PASSWORD = 8

# Used only when backend.providers is absent (or has no catalogue). Kinds match
# the contract's openai|anthropic|google|ollama; base URLs are API endpoints,
# not user-facing copy.
SEED_PROVIDERS = [
    {"id": "deepseek", "label": "DeepSeek", "kind": "openai",
     "base_url": "https://api.deepseek.com", "needs_key": True},
    {"id": "openai", "label": "OpenAI", "kind": "openai",
     "base_url": "https://api.openai.com/v1", "needs_key": True},
    {"id": "anthropic", "label": "Anthropic", "kind": "anthropic",
     "base_url": "https://api.anthropic.com", "needs_key": True},
    {"id": "google", "label": "Google", "kind": "google",
     "base_url": "https://generativelanguage.googleapis.com", "needs_key": True},
    {"id": "openrouter", "label": "OpenRouter", "kind": "openai",
     "base_url": "https://openrouter.ai/api/v1", "needs_key": True},
    {"id": "ollama", "label": "Ollama (local, no key)", "kind": "ollama",
     "base_url": "http://localhost:11434", "needs_key": False},
]
_LOCAL_KINDS = {"ollama"}
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

# Throttle keys in auth's failure table, so setup pays the same growing delay
# (and raises the same one-per-burst alert) as the login it stands in for.
_KEY_SETUP = "\0setup"
_KEY_TEST = "\0setup-test"


# ---------------------------------------------------------------- helpers ---

# Neither helper runs init_db: the app lifespan (or the CLI, once, up front)
# does. Running it per request is not just waste — two init_db migrations
# racing each other deadlock, which is exactly what two racing setups did.
async def users_exist() -> bool:
    db = await get_db()
    try:
        async with db.execute("SELECT 1 FROM users LIMIT 1") as cur:
            return await cur.fetchone() is not None
    finally:
        await db.close()


class SetupError(Exception):
    """A refusal with an HTTP status; the CLI prints the message."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def validate_credentials(username: str, password: str) -> str:
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise SetupError(400, "username: 1-64 letters, digits, '.', '_' or '-'")
    if len(password or "") < MIN_PASSWORD:
        raise SetupError(400, f"password: at least {MIN_PASSWORD} characters")
    return username


async def create_first_user(username: str, password: str) -> int:
    """Create the operator iff no user exists yet; returns the new id.
    Raises SetupError(409) when a user already exists — including when another
    setup won a race between our status check and this insert."""
    username = validate_credentials(username, password)
    pw_hash = await asyncio.to_thread(auth.hash_password, password)
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO users (username, password_hash) "
            "SELECT ?, ? WHERE NOT EXISTS (SELECT 1 FROM users)",
            (username, pw_hash))
        await db.commit()
        if cur.rowcount != 1:
            raise SetupError(409, "setup is already done — log in instead")
        return cur.lastrowid
    finally:
        await db.close()


def _providers_mod():
    try:
        from . import providers   # PR1; absent until it merges
    except ImportError:
        return None
    return providers


# The providers people actually arrive with, listed first (PR1's "Popular"
# group); the rest of a long catalogue follows in its own order.
POPULAR = ("openai", "anthropic", "google", "deepseek", "openrouter", "groq",
           "mistral", "xai", "togetherai", "fireworks-ai", "ollama", "lmstudio")


def _light(p: dict) -> dict:
    kind = p.get("kind") or "openai"
    base = p.get("base_url") or ""
    return {"id": p["id"], "label": p.get("label") or p["id"], "kind": kind,
            "base_url": base,
            "needs_key": bool(p.get("needs_key", kind not in _LOCAL_KINDS)),
            "needs_base_url": bool(p.get("needs_base_url", "{" in base))}


def catalogue() -> list[dict]:
    """[{id, label, kind, base_url, needs_key, needs_base_url}] with no model
    lists (PR1's full payload is ~1.5 MB), Popular first. From
    backend.providers when it is there, else the seed list."""
    mod = _providers_mod()
    rows = None
    fn = getattr(mod, "list_providers", None) if mod is not None else None
    if callable(fn):
        try:
            rows = fn()
        except Exception:                  # noqa: BLE001 — degrade to the seed
            rows = None
    if isinstance(rows, dict):
        rows = rows.get("providers")
    out = [_light(p) for p in rows or [] if isinstance(p, dict) and p.get("id")]
    if not out:
        return [_light(p) for p in SEED_PROVIDERS]
    rank = {pid: i for i, pid in enumerate(POPULAR)}
    return sorted(out, key=lambda p: rank.get(p["id"], len(POPULAR)))


def _entry(pid: str) -> dict:
    mod = _providers_mod()
    get = getattr(mod, "get_provider", None) if mod is not None else None
    if callable(get):
        try:
            p = get(pid)
        except (ValueError, KeyError):
            p = None
        if isinstance(p, dict) and p.get("id"):
            return _light(p)
        raise SetupError(400, f"unknown provider '{pid}'")
    for p in catalogue():
        if p["id"] == pid:
            return p
    raise SetupError(400, f"unknown provider '{pid}'")


def _first_model(mod, pid: str) -> str | None:
    """The chosen provider's first model id (bare, as get_provider lists it)."""
    get = getattr(mod, "get_provider", None)
    try:
        prov = get(pid) if callable(get) else None
    except Exception:                      # noqa: BLE001
        return None
    models = (prov or {}).get("models") or []
    return models[0].get("id") if models and isinstance(models[0], dict) else None


def check_provider(pid: str, api_key: str = "", base_url: str = "") -> tuple:
    """Normalise + validate a provider choice -> (id, key, base_url)."""
    pid = (pid or "").strip().lower()
    if not _ID_RE.match(pid):
        raise SetupError(400, "bad provider id")
    entry = _entry(pid)
    api_key = (api_key or "").strip()
    base_url = (base_url or "").strip()
    if entry["needs_key"] and not api_key:
        raise SetupError(400, f"{entry['label']} needs an API key")
    if base_url and not re.match(r"^https?://[^\s/]+(/\S*)?$", base_url):
        raise SetupError(400, "base URL must start with http:// or https://")
    if entry["needs_base_url"] and not base_url:
        raise SetupError(400, f"{entry['label']} needs a base URL")
    return pid, api_key, base_url


def store_provider(pid: str, api_key: str = "", base_url: str = "") -> dict:
    """Store the key (and base URL), enable the provider and make its first
    model the default. Returns {stored_as, default}. With backend.providers
    absent, only the key is stored (PROVIDER_<ID>_API_KEY in secrets.json)."""
    pid, api_key, base_url = check_provider(pid, api_key, base_url)
    mod = _providers_mod()
    if mod is None or not callable(getattr(mod, "set_key", None)):
        if api_key:
            from . import secrets
            raw = secrets._load_raw()
            raw[f"PROVIDER_{pid.upper().replace('-', '_')}_API_KEY"] = api_key
            secrets.save(raw)
        return {"stored_as": "secrets", "default": None}
    try:
        if api_key:
            mod.set_key(pid, api_key)
        update = getattr(mod, "update_provider", None)
        if callable(update):
            # set_key enables on its own; a keyless local provider needs it said
            update(pid, base_url=base_url or None, enabled=True)
        default = None
        model = _first_model(mod, pid)
        set_model = getattr(mod, "update_model", None)
        if model and callable(set_model):
            # models are off by default: without enabled=True the default
            # would not even appear in the chat picker
            set_model(pid, model, enabled=True, default=True)
            default = f"{pid}/{model}"
    except ValueError as e:
        raise SetupError(400, str(e) or f"unknown provider '{pid}'")
    return {"stored_as": "providers", "default": default}


async def _probe(entry: dict, api_key: str, base_url: str) -> dict:
    """One cheap model-list call. Only a verdict and model ids come back —
    never the response body, so this cannot be used to read a URL."""
    base = (base_url or entry["base_url"]).rstrip("/")
    kind = entry["kind"]
    headers, params = {}, {}
    if kind == "anthropic":
        url = f"{base}/v1/models"
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    elif kind == "google":
        url = f"{base}/v1beta/models"
        params = {"key": api_key}
    elif kind == "ollama":
        url = f"{base}/api/tags"
    else:
        url = f"{base}/models"
        if api_key:
            headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as c:
            r = await c.get(url, headers=headers, params=params)
    except httpx.HTTPError as e:
        return {"ok": False, "detail": f"could not reach it ({type(e).__name__})",
                "models_found": []}
    if r.status_code in (401, 403):
        return {"ok": False, "detail": "the key was refused", "models_found": []}
    if r.status_code != 200:
        return {"ok": False, "detail": f"HTTP {r.status_code}", "models_found": []}
    try:
        data = r.json()
    except ValueError:
        return {"ok": False, "detail": "not a model API", "models_found": []}
    rows = (data.get("data") or data.get("models") or []) if isinstance(data, dict) else []
    ids = []
    for m in rows if isinstance(rows, list) else []:
        if isinstance(m, dict):
            mid = m.get("id") or m.get("name") or ""
            ids.append(str(mid).removeprefix("models/")[:120])
    return {"ok": True, "detail": "ok", "models_found": [i for i in ids if i][:500]}


async def test_provider(pid: str, api_key: str = "", base_url: str = "") -> dict:
    """{ok, detail, models_found} — PR1's tester when it takes a candidate key,
    else the built-in probe."""
    entry = _entry((pid or "").strip().lower())
    if base_url and not re.match(r"^https?://[^\s/]+(/\S*)?$", base_url):
        raise SetupError(400, "base URL must start with http:// or https://")
    mod = _providers_mod()
    fn = getattr(mod, "test_provider", None) if mod is not None else None
    if callable(fn):
        try:
            res = fn(entry["id"], api_key=api_key or None, base_url=base_url or None)
            if asyncio.iscoroutine(res):
                res = await res
            if isinstance(res, dict) and "ok" in res:
                return {"ok": bool(res["ok"]), "detail": str(res.get("detail") or ""),
                        "models_found": list(res.get("models_found") or [])}
        except TypeError:
            pass                            # a tester for stored keys only
        except ValueError as e:
            raise SetupError(400, str(e) or "unknown provider")
    return await _probe(entry, api_key, base_url)


async def _charge(key: str, request: Request) -> None:
    """A refused setup costs what a failed login costs."""
    count, _, _, alerted = auth._record(key, time.time())
    if count >= auth._ALERT_AT and not alerted:
        auth._failures[key][3] = True
        await auth._alert("setup", count, auth._peer(request), via="setup")
    delay = auth._delay_for(count)
    if delay:
        await asyncio.sleep(delay)


def _refuse_cross_site(request: Request) -> None:
    """/api/setup is exempt from the cookie-keyed origin middleware, but a page
    on another site must still not be able to claim this server through the
    operator's browser: refuse what the browser itself labels cross-site, and
    insist on a JSON body (a form post cannot send one without a preflight)."""
    site = (request.headers.get("sec-fetch-site") or "").lower()
    if site in ("cross-site", "same-site"):
        raise HTTPException(status_code=403, detail="cross-origin request refused")
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(status_code=415, detail="send JSON")


# ----------------------------------------------------------------- routes ---

class SetupRequest(BaseModel):
    username: str
    password: str
    provider: str | None = None
    api_key: str | None = None
    base_url: str | None = None


class TestRequest(BaseModel):
    provider: str
    api_key: str | None = None
    base_url: str | None = None


@router.get("/status")
async def status():
    return {"needed": not await users_exist()}


@router.get("/providers")
async def providers():
    if await users_exist():
        raise HTTPException(status_code=409, detail="setup is already done")
    return {"providers": catalogue()}


@router.post("/test")
async def test(body: TestRequest, request: Request):
    _refuse_cross_site(request)
    if await users_exist():
        raise HTTPException(status_code=409, detail="setup is already done")
    try:
        res = await test_provider(body.provider, body.api_key or "", body.base_url or "")
    except SetupError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    if not res["ok"]:
        await _charge(_KEY_TEST, request)
    return res


@router.post("")
async def setup(body: SetupRequest, request: Request, response: Response):
    _refuse_cross_site(request)
    try:
        if await users_exist():
            raise SetupError(409, "setup is already done — log in instead")
        # validate everything BEFORE the user row exists, so a bad provider
        # choice never leaves a half-done setup the page can no longer retry
        username = validate_credentials(body.username, body.password)
        if body.provider:
            check_provider(body.provider, body.api_key or "", body.base_url or "")
        user_id = await create_first_user(username, body.password)
    except SetupError as e:
        # only the closed door is charged: a too-short password is a typo,
        # a setup after setup is someone trying the handle
        if e.status == 409:
            await _charge(_KEY_SETUP, request)
        raise HTTPException(status_code=e.status, detail=e.detail)
    auth._clear(_KEY_SETUP)
    provider = None
    if body.provider:
        try:
            provider = store_provider(body.provider, body.api_key or "",
                                      body.base_url or "")
        except SetupError as e:
            # the login exists now; the key can be added from Settings
            provider = {"error": e.detail}
    auth.set_session_cookie(response, user_id, username)
    return {"ok": True, "username": username, "provider": provider}
