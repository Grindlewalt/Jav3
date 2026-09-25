"""LLM provider registry: which endpoints Jav3 can call, which of their models
the operator switched on, and the default model.

Three layers, each with one owner:
  - the CATALOGUE (backend/providers_catalog.json — models.dev data, MIT,
    rebuilt by scripts/refresh_providers.py) says what exists: id, label, kind
    (the wire format: openai | anthropic | google | ollama), default base_url,
    auth style, and per-model ctx + $/Mtok. An operator file at
    ~/.config/jarvis/providers.json is merged on top (new providers, or fields
    and models overriding the shipped ones).
  - STATE (<data_dir>/providers_state.json) is what the operator chose:
    provider enabled / base_url override, per-model enabled, the discovered
    models a /test call listed, and the default model.
  - KEYS live in the 0600 secrets store (backend/secrets.py) as
    PROVIDER_<ID>_API_KEY, unbound to any web host so the agent can never
    substitute one into a fetch; the old JARVIS_DEEPSEEK_API_KEY env stays the
    deepseek fallback. Nothing here ever returns or logs a key value.

Model ids are `provider/model` everywhere outside this file; a bare id means
"this model on the default model's provider". `resolve()` turns an id (plus an
optional guest-requested base_url) into the Route the model gateway calls.
"""
import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .config import settings

CATALOG_PATH = Path(__file__).with_name("providers_catalog.json")
KINDS = ("openai", "anthropic", "google", "ollama")
# the brief's seed names for two providers models.dev spells differently
ALIASES = {"together": "togetherai", "fireworks": "fireworks-ai"}
DISCOVERED_CAP = 2000     # models a /test listing may add per provider
ANTHROPIC_VERSION = "2023-06-01"


class ProviderError(ValueError):
    """A bad provider id / model / setting — the routes map it to a 4xx."""


# --- endpoints + the key-exfil allowlist ---------------------------------------

def endpoint(url: str) -> tuple:
    """(scheme, host, port) — the identity a key is bound to. Path is ignored:
    a key goes to a HOST, whatever path the request names."""
    p = urlsplit(url if "://" in url else "http://" + url)
    return (p.scheme, (p.hostname or "").lower(), p.port)


def needs_base_url(url: str) -> bool:
    """A catalogue base_url with a {VAR} placeholder (an account id, a host)
    is unusable until the operator PUTs a real one."""
    return "{" in (url or "")


def base_url_allowed(url: str) -> bool:
    """A guest-supplied model base_url is honoured only if it is the configured
    DeepSeek endpoint, on model_base_url_allowlist, or an ENABLED provider's
    base_url. The host attaches the API key to the request, so an unchecked
    base_url lets a compromised guest harvest a key by naming its own endpoint.
    Provider state is operator-only (cookie routes), so the guest can't widen
    this list."""
    t = endpoint(url)
    allowed = [settings.deepseek_base_url, *settings.model_base_url_allowlist,
               *enabled_base_urls()]
    return any(endpoint(a) == t for a in allowed)


# --- catalogue ----------------------------------------------------------------

_cat_lock = threading.Lock()
_cat_cache: dict = {"key": None, "data": None}


def _override_path() -> Path:
    return settings.secrets_path.parent / "providers.json"


def _state_path() -> Path:
    return settings.data_dir / "providers_state.json"


def _mtime(p: Path) -> float | None:
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def _merge_provider(base: dict | None, over: dict) -> dict | None:
    out = dict(base or {})
    for k, v in over.items():
        if k != "models":
            out[k] = v
    models = {m["id"]: dict(m) for m in (base or {}).get("models") or []}
    for m in over.get("models") or []:
        if isinstance(m, dict) and m.get("id"):
            models[m["id"]] = {**models.get(m["id"], {}), **m}
    out["models"] = list(models.values())
    if out.get("kind") not in KINDS or not out.get("base_url") or not out.get("id"):
        return base          # a malformed override never breaks the shipped entry
    out.setdefault("label", out["id"])
    out.setdefault("auth", "bearer")
    out.setdefault("lists_models", True)
    return out


def _build_catalog(override: Path | None) -> dict:
    data = json.loads(CATALOG_PATH.read_text())
    providers: dict[str, dict] = {}
    for p in data.get("providers") or []:
        providers[p["id"]] = p
    source = data.get("source") or "unknown"
    if override is not None:
        try:
            over = json.loads(override.read_text())
            for p in over.get("providers") or []:
                if isinstance(p, dict) and p.get("id"):
                    merged = _merge_provider(providers.get(p["id"]), p)
                    if merged is not None:
                        providers[p["id"]] = merged
            source += " + local overrides"
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            pass           # a broken override file must not take the models down
    for p in providers.values():
        p["_models"] = {m["id"]: m for m in p.get("models") or []}
    return {"source": source, "providers": providers}


def catalog() -> dict:
    """{"source": str, "providers": {id: provider}} — reloaded when the shipped
    file or the operator override changes."""
    ov = _override_path()
    key = (str(ov), _mtime(ov), _mtime(CATALOG_PATH))
    with _cat_lock:
        if _cat_cache["key"] != key:
            _cat_cache["data"] = _build_catalog(ov if key[1] is not None else None)
            _cat_cache["key"] = key
        return _cat_cache["data"]


def _norm_id(pid: str) -> str:
    return ALIASES.get(pid, pid)


def provider(pid: str) -> dict:
    p = catalog()["providers"].get(_norm_id(pid))
    if p is None:
        raise ProviderError(f"unknown provider: {pid}")
    return p


def is_provider(pid: str) -> bool:
    return _norm_id(pid) in catalog()["providers"]


# --- state --------------------------------------------------------------------

_state_lock = threading.Lock()


def _load_state() -> dict:
    try:
        st = json.loads(_state_path().read_text())
        if not isinstance(st, dict):
            st = {}
    except (OSError, json.JSONDecodeError):
        st = {}
    st.setdefault("default", None)
    if not isinstance(st.get("providers"), dict):
        st["providers"] = {}
    return st


def _save_state(st: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2))
    os.replace(tmp, p)


def _pstate(st: dict, pid: str) -> dict:
    return st["providers"].get(pid) or {}


def _edit_pstate(st: dict, pid: str) -> dict:
    return st["providers"].setdefault(pid, {})


def _enabled(st: dict, pid: str) -> bool:
    en = _pstate(st, pid).get("enabled")
    # deepseek is the out-of-the-box provider; everything else waits for a key
    # or an explicit switch-on
    return bool(en) if en is not None else pid == "deepseek"


def _base(st: dict, p: dict) -> str:
    return (_pstate(st, p["id"]).get("base_url") or p["base_url"]).rstrip("/")


def enabled_base_urls() -> list[str]:
    st = _load_state()
    cat = catalog()["providers"]
    out = []
    for pid in st["providers"]:
        p = cat.get(pid)
        if p is not None and _enabled(st, pid) and not needs_base_url(_base(st, p)):
            out.append(_base(st, p))
    if _enabled(st, "deepseek") and "deepseek" in cat:
        out.append(_base(st, cat["deepseek"]))
    return out


# --- keys ---------------------------------------------------------------------

def key_name(pid: str) -> str:
    return "PROVIDER_" + re.sub(r"[^A-Z0-9]", "_", _norm_id(pid).upper()) + "_API_KEY"


def _store() -> dict:
    from . import secrets as secrets_store
    return secrets_store.load()


def key_source(pid: str, store: dict | None = None) -> str | None:
    """"store" | "env" | None — where this provider's key would come from."""
    store = _store() if store is None else store
    if store.get(key_name(pid)):
        return "store"
    if _norm_id(pid) == "deepseek" and settings.deepseek_api_key:
        return "env"
    return None


def api_key(pid: str, store: dict | None = None) -> str | None:
    """The key to send to this provider, host-side only. The store wins; the
    legacy env key still covers deepseek."""
    store = _store() if store is None else store
    src = key_source(pid, store)
    if src == "store":
        return store[key_name(pid)]
    return settings.deepseek_api_key if src == "env" else None


def needs_key(p: dict) -> bool:
    return (p.get("auth") or "bearer") != "none"


def auth_headers(p: dict | None, key: str | None) -> dict:
    """Request headers carrying `key` in the way this provider expects."""
    kind = (p or {}).get("kind") or "openai"
    auth = (p or {}).get("auth") or "bearer"
    if kind == "anthropic" or auth == "x-api-key":
        return {"x-api-key": key or "", "anthropic-version": ANTHROPIC_VERSION}
    if kind == "google" or auth == "query":
        return {"x-goog-api-key": key or ""}
    return {"Authorization": f"Bearer {key or 'local'}"}


# --- model ids ----------------------------------------------------------------

def default_model(st: dict | None = None) -> str:
    st = st if st is not None else _load_state()
    return st.get("default") or f"deepseek/{settings.model_name}"


def default_provider() -> str:
    return split_id(default_model())[0] or "deepseek"


def split_id(model_id: str) -> tuple[str | None, str]:
    """('provider', 'model') when the id leads with a known provider id, else
    (None, id). OpenRouter-style ids keep their inner slash:
    openrouter/anthropic/x -> ('openrouter', 'anthropic/x')."""
    s = (model_id or "").strip()
    head, sep, rest = s.partition("/")
    if sep and rest and is_provider(head):
        return _norm_id(head), rest
    return None, s


def canonical(model_id: str) -> str:
    """`provider/model`; a bare id lands on the default model's provider."""
    pid, mid = split_id(model_id)
    if not mid:
        raise ProviderError("empty model id")
    return f"{pid or default_provider()}/{mid}"


def model_info(pid: str, mid: str) -> dict | None:
    p = catalog()["providers"].get(_norm_id(pid))
    return (p or {}).get("_models", {}).get(mid)


def _price(m: dict | None) -> dict | None:
    if not m or m.get("price_in") is None or m.get("price_out") is None:
        return None
    return {"in": m["price_in"], "out": m["price_out"],
            "cache": m.get("price_cache") if m.get("price_cache") is not None
            else m["price_in"]}


def price_for(model_id: str | None) -> tuple[bool, dict | None]:
    """(known, prices $/Mtok {in, out, cache}) for a model id as ledgered —
    `provider/model`, or a bare id from before providers (those were all
    DeepSeek, so deepseek is searched first). known=False means no catalogue
    entry at all; known with prices None means catalogued but unpriced."""
    if not model_id:
        return False, None
    pid, mid = split_id(model_id)
    if pid:
        m = model_info(pid, mid)
        return (m is not None), _price(m)
    order = ["deepseek", *[p for p in catalog()["providers"] if p != "deepseek"]]
    for p in order:
        m = model_info(p, mid)
        if m is not None:
            return True, _price(m)
    return False, None


def _model_enabled(st: dict, pid: str, mid: str, dflt: str) -> bool:
    en = (_pstate(st, pid).get("models") or {}).get(mid, {}).get("enabled")
    # models are opt-in (OpenRouter alone lists hundreds); the default model is
    # always on so the picker is never empty out of the box
    return bool(en) if en is not None else f"{pid}/{mid}" == dflt


def _provider_models(st: dict, p: dict) -> list[dict]:
    """Catalogue models plus the ones a /test listing discovered."""
    models = list(p.get("models") or [])
    known = p.get("_models") or {}
    for mid in _pstate(st, p["id"]).get("discovered") or []:
        if mid not in known:
            models.append({"id": mid, "label": mid, "ctx": None, "price_in": None,
                           "price_out": None, "discovered": True})
    return models


def _known_model(st: dict, p: dict, mid: str) -> bool:
    return mid in (p.get("_models") or {}) or \
        mid in (_pstate(st, p["id"]).get("discovered") or [])


# --- views (the API shapes) ---------------------------------------------------

def _model_view(st: dict, pid: str, m: dict, dflt: str) -> dict:
    return {"id": m["id"], "label": m.get("label") or m["id"],
            "ctx": m.get("ctx"), "price_in": m.get("price_in"),
            "price_out": m.get("price_out"),
            "enabled": _model_enabled(st, pid, m["id"], dflt),
            "default": f"{pid}/{m['id']}" == dflt,
            "discovered": bool(m.get("discovered")),
            "tools": m.get("tools"), "vision": m.get("vision"),
            "reasoning": m.get("reasoning")}


def _provider_view(st: dict, p: dict, dflt: str, include_models: bool = True,
                   store: dict | None = None) -> dict:
    store = _store() if store is None else store
    base = _base(st, p)
    out = {"id": p["id"], "label": p.get("label") or p["id"], "kind": p["kind"],
           "base_url": base, "default_base_url": p["base_url"],
           "base_url_overridden": bool(_pstate(st, p["id"]).get("base_url")),
           "docs": p.get("docs"), "key_set": key_source(p["id"], store) is not None,
           "key_source": key_source(p["id"], store), "needs_key": needs_key(p),
           "needs_base_url": needs_base_url(base),
           "lists_models": bool(p.get("lists_models", True)),
           "enabled": _enabled(st, p["id"])}
    if include_models:
        out["models"] = [_model_view(st, p["id"], m, dflt)
                         for m in _provider_models(st, p)]
    return out


def list_providers(include_models: bool = True) -> list[dict]:
    st = _load_state()
    dflt = default_model(st)
    store = _store()
    return [_provider_view(st, p, dflt, include_models, store)
            for p in catalog()["providers"].values()]


def get_provider(pid: str) -> dict:
    st = _load_state()
    return _provider_view(st, provider(pid), default_model(st))


def enabled_models() -> list[dict]:
    """The flat picker list: every enabled model of every usable provider."""
    st = _load_state()
    dflt = default_model(st)
    out = []
    for p in catalog()["providers"].values():
        if not _enabled(st, p["id"]) or needs_base_url(_base(st, p)):
            continue
        for m in _provider_models(st, p):
            if _model_enabled(st, p["id"], m["id"], dflt):
                out.append({"id": f"{p['id']}/{m['id']}",
                            "label": m.get("label") or m["id"],
                            "provider": p["id"], "provider_label": p.get("label"),
                            "ctx": m.get("ctx"), "price_in": m.get("price_in"),
                            "price_out": m.get("price_out")})
    return out


def models_payload() -> dict:
    return {"default": default_model(), "models": enabled_models()}


# --- mutations (operator-only callers: the cookie routes, setup, CLI) ---------

def set_key(pid: str, key: str | None) -> None:
    """Store (or with ""/None clear) a provider's key. The first key for a
    provider the operator never toggled also switches it on."""
    from . import secrets as secrets_store
    p = provider(pid)
    key = (key or "").strip()
    if any(c.isspace() for c in key):
        raise ProviderError("api_key must not contain whitespace")
    raw = secrets_store._load_raw()
    name = key_name(p["id"])
    if key:
        raw[name] = key            # unbound: never substitutable into a web fetch
    else:
        raw.pop(name, None)
    secrets_store.save(raw)
    if key:
        with _state_lock:
            st = _load_state()
            ps = _edit_pstate(st, p["id"])
            if ps.get("enabled") is None and not needs_base_url(_base(st, p)):
                ps["enabled"] = True
                _save_state(st)


def _check_base_url(url: str) -> str:
    url = url.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ProviderError("base_url must be an http(s) URL")
    if needs_base_url(url):
        raise ProviderError("base_url still contains a {PLACEHOLDER}")
    if parts.username or parts.password:
        raise ProviderError("base_url must not carry credentials")
    return url


def update_provider(pid: str, base_url: str | None = None,
                    enabled: bool | None = None) -> dict:
    """Mirror of PUT /api/providers/{id}: base_url "" clears the override."""
    p = provider(pid)
    with _state_lock:
        st = _load_state()
        ps = _edit_pstate(st, p["id"])
        if base_url is not None:
            if base_url.strip():
                ps["base_url"] = _check_base_url(base_url)
            else:
                ps.pop("base_url", None)
        if enabled is not None:
            if enabled and needs_base_url(_base(st, p)):
                raise ProviderError(
                    f"{p['id']} needs its base_url set before it can be enabled")
            if not enabled and split_id(default_model(st))[0] == p["id"]:
                raise ProviderError(
                    "that provider serves the default model; pick another default first")
            ps["enabled"] = bool(enabled)
        _save_state(st)
    return get_provider(p["id"])


def update_model(pid: str, model: str, enabled: bool | None = None,
                 default: bool | None = None) -> dict:
    """Mirror of PUT /api/providers/{id}/models/{model}. Returns the model view."""
    p = provider(pid)
    with _state_lock:
        st = _load_state()
        if not _known_model(st, p, model):
            raise ProviderError(
                f"unknown model {model!r} for {p['id']} (run the provider test "
                "to discover what it serves)")
        full = f"{p['id']}/{model}"
        ms = _edit_pstate(st, p["id"]).setdefault("models", {})
        if default:
            if not _enabled(st, p["id"]):
                raise ProviderError(f"enable {p['id']} before making its model the default")
            prev = default_model(st)
            if prev != full:
                # the old default was on only BY being default; keep it in the
                # picker instead of making it vanish with the switch
                ppid, pmid = split_id(prev)
                if ppid:
                    pms = _edit_pstate(st, ppid).setdefault("models", {})
                    pms.setdefault(pmid, {}).setdefault("enabled", True)
            st["default"] = full
            ms.setdefault(model, {})["enabled"] = True
        elif default is False and default_model(st) == full:
            st["default"] = None
        if enabled is not None:
            if not enabled and default_model(st) == full:
                raise ProviderError("the default model can't be disabled; pick another default first")
            ms.setdefault(model, {})["enabled"] = bool(enabled)
        _save_state(st)
        dflt = default_model(st)
        m = next(x for x in _provider_models(st, p) if x["id"] == model)
        return _model_view(st, p["id"], m, dflt)


def checked(model_id: str) -> str:
    """Canonical id of a model the operator switched on — what a chat, a
    schedule or the nav switcher may pick. ProviderError otherwise."""
    full = canonical(model_id)
    if full not in {m["id"] for m in enabled_models()}:
        raise ProviderError(f"model {model_id!r} is not an enabled model "
                            "(Settings → Providers)")
    return full


def turn_model_id(model_name: str | None, base_url: str | None = None) -> str:
    """The id a turn runs on and reports (start event, message rows): the
    canonical provider/model, resolved once at turn start so a default switch
    mid-turn doesn't split it. A pinned local endpoint keeps its raw name."""
    if base_url:
        return model_name or default_model()
    try:
        return canonical(model_name or default_model())
    except ProviderError:
        return model_name or default_model()


def peak_priced(model_id: str | None) -> bool:
    """Whether a turn on this model (None = the default) spends in DeepSeek's
    peak-priced hours — the only provider the peak gate applies to."""
    try:
        return split_id(canonical(model_id or default_model()))[0] == "deepseek"
    except ProviderError:
        return True


def set_default(model_id: str) -> str:
    """The nav switcher (PUT /api/model): the id must already be an enabled
    model. Returns the canonical id."""
    full = checked(model_id)
    pid, mid = split_id(full)
    update_model(pid, mid, default=True)
    return full


def record_discovered(pid: str, ids: list[str]) -> None:
    p = provider(pid)
    with _state_lock:
        st = _load_state()
        ps = _edit_pstate(st, p["id"])
        known = p.get("_models") or {}
        new = [i for i in dict.fromkeys(ids) if isinstance(i, str) and i and i not in known]
        ps["discovered"] = new[:DISCOVERED_CAP]
        _save_state(st)


async def migrate_legacy_override() -> None:
    """The old nav switch kept a bare model id in session_state; adopt it as
    the default once, then drop it."""
    from .db import get_db, get_state, set_state
    db = await get_db()
    try:
        legacy = await get_state(db, "model_override")
        if not legacy:
            return
        with _state_lock:
            st = _load_state()
            if not st.get("default"):
                st["default"] = canonical(legacy)
                pid, mid = split_id(st["default"])
                _edit_pstate(st, pid).setdefault("models", {}).setdefault(
                    mid, {})["enabled"] = True
                _save_state(st)
        await set_state(db, "model_override", None)
        await db.commit()
    finally:
        await db.close()


# --- routing ------------------------------------------------------------------

@dataclass
class Route:
    """Where one model call goes. `key` is host-side only — never serialised."""
    provider: str | None       # catalogue id; None = an allowlisted bare endpoint
    kind: str                  # openai | anthropic | google | ollama
    base_url: str
    model: str                 # the provider's own model id
    model_id: str              # what we report/ledger: provider/model
    key: str = field(repr=False, default="local")
    info: dict = field(default_factory=dict)   # the catalogue model entry
    # set when the provider needs a key and has none: the gateway raises it
    # AFTER its peak/budget gates, so those still answer first
    key_error: str | None = None

    @property
    def is_deepseek(self) -> bool:
        return self.provider == "deepseek"


def resolve(model_name: str | None, base_url: str | None = None,
            deepseek_key: str | None = None) -> Route:
    """Model id (+ optional requested base_url) -> Route. `deepseek_key`
    overrides the stored/env deepseek key (the gateway's constructor arg).

    Key policy: a key is only ever attached for the provider whose CONFIGURED
    endpoint the request goes to. With an explicit base_url (an agent pinned to
    a local ollama, or a guest asking for one) the URL must pass
    base_url_allowed, and the key is the one of the enabled provider living at
    that exact (scheme, host, port) — anything else is sent "local"."""
    st = _load_state()
    cat = catalog()["providers"]
    name = (model_name or "").strip() or default_model(st)
    pid, mid = split_id(name)

    def key_for(p: dict) -> str | None:
        if p["id"] == "deepseek" and deepseek_key is not None:
            return deepseek_key or None
        return api_key(p["id"])

    if base_url:
        if not base_url_allowed(base_url):
            raise ProviderError(
                f"refused model base_url {base_url!r}: not on the endpoint "
                "allowlist (enabled providers + JARVIS_MODEL_BASE_URL_ALLOWLIST)")
        target = endpoint(base_url)
        matches = [p for p in cat.values()
                   if _enabled(st, p["id"]) and not needs_base_url(_base(st, p))
                   and endpoint(_base(st, p)) == target]
        if not matches and target == endpoint(settings.deepseek_base_url) \
                and "deepseek" in cat:
            matches = [cat["deepseek"]]
        # prefer the provider the id names (two providers can share a host)
        match = next((p for p in matches if p["id"] == pid), None) or \
            (matches[0] if matches else None)
        model = mid if pid else name
        if match is None:
            return Route(None, "openai", base_url.rstrip("/"), model, model, "local")
        return Route(match["id"], match["kind"], base_url.rstrip("/"), model,
                     f"{match['id']}/{model}", key_for(match) or "local",
                     (match.get("_models") or {}).get(model) or {})

    pid = pid or default_provider()
    p = cat.get(pid)
    if p is None:
        raise ProviderError(f"unknown provider: {pid}")
    if not _enabled(st, pid):
        raise ProviderError(f"provider {pid} is disabled (Settings → Providers)")
    base = _base(st, p)
    if needs_base_url(base):
        raise ProviderError(f"provider {pid} needs its base_url set (Settings → Providers)")
    key = key_for(p)
    missing = None
    if needs_key(p) and not key:
        missing = ("DEEPSEEK_API_KEY is not set (Settings → Providers, or "
                   "~/.config/jarvis/env JARVIS_DEEPSEEK_API_KEY=...)"
                   if pid == "deepseek" else
                   f"no API key for provider {pid} (Settings → Providers)")
    return Route(pid, p["kind"], base, mid, f"{pid}/{mid}", key or "local",
                 (p.get("_models") or {}).get(mid) or {}, missing)


def provider_for_base(base_url: str) -> dict | None:
    """The catalogue entry whose configured endpoint this URL is (any state) —
    read-only shape hints for the transport, never a key."""
    st = _load_state()
    t = endpoint(base_url)
    for p in catalog()["providers"].values():
        if not needs_base_url(_base(st, p)) and endpoint(_base(st, p)) == t:
            return p
    return None


# --- connection test ----------------------------------------------------------

HTTP_TRANSPORT = None       # tests swap in an httpx.MockTransport
TEST_TIMEOUT = 10.0


def _cheapest_model(p: dict) -> str | None:
    models = p.get("models") or []
    priced = [m for m in models if m.get("price_in") is not None]
    pick = min(priced, key=lambda m: m["price_in"]) if priced else \
        (models[0] if models else None)
    return pick["id"] if pick else None


def _short(resp, key: str | None) -> str:
    body = resp.text[:200].replace("\n", " ")
    if key and len(key) >= 4:
        body = body.replace(key, "***")
    return f"HTTP {resp.status_code}: {body}".strip()


async def _one_token(client, p: dict, base: str, key: str | None) -> dict:
    mid = _cheapest_model(p)
    if not mid:
        return {"ok": False, "detail": "no model to test with — the provider "
                "has no catalogue models and can't list its own", "models_found": []}
    msgs = [{"role": "user", "content": "ping"}]
    path = "messages" if p["kind"] == "anthropic" else "chat/completions"
    r = await client.post(f"{base}/{path}", headers=auth_headers(p, key),
                          json={"model": mid, "max_tokens": 1, "messages": msgs})
    if r.status_code == 200:
        return {"ok": True, "detail": f"key accepted (1-token call to {mid})",
                "models_found": []}
    return {"ok": False, "detail": _short(r, key), "models_found": []}


async def _probe(client, p: dict, base: str, key: str | None) -> dict:
    kind = p["kind"]
    headers = auth_headers(p, key)
    if kind == "ollama":
        root = re.sub(r"/v1$", "", base)
        r = await client.get(f"{root}/api/tags")
        if r.status_code != 200:
            return {"ok": False, "detail": _short(r, key), "models_found": []}
        found = [m.get("name") for m in r.json().get("models") or [] if m.get("name")]
        return {"ok": True, "detail": f"reachable, {len(found)} local models",
                "models_found": found}
    if not p.get("lists_models", True):
        return await _one_token(client, p, base, key)
    if p["id"] == "openrouter":
        # its /models is public, so only /key proves the key
        r = await client.get(f"{base}/key", headers=headers)
        if r.status_code != 200:
            return {"ok": False, "detail": _short(r, key), "models_found": []}
    params = {"limit": 1000} if kind == "anthropic" else \
        {"pageSize": 1000} if kind == "google" else None
    r = await client.get(f"{base}/models", params=params, headers=headers)
    if r.status_code in (404, 405):
        return await _one_token(client, p, base, key)   # listing unsupported, not a bad key
    if r.status_code != 200:
        return {"ok": False, "detail": _short(r, key), "models_found": []}
    body = r.json()
    if kind == "google":
        found = [m["name"].removeprefix("models/") for m in body.get("models") or []
                 if m.get("name") and "generateContent" in
                 (m.get("supportedGenerationMethods") or ["generateContent"])]
    else:
        items = body.get("data") if isinstance(body, dict) else body
        found = [m.get("id") for m in items or [] if isinstance(m, dict) and m.get("id")]
    return {"ok": True, "detail": f"key accepted, {len(found)} models listed",
            "models_found": found}


async def test_provider(pid: str, api_key: str | None = None,
                        base_url: str | None = None) -> dict:
    """One cheap call proving the key/endpoint work: list models where the API
    supports it, else a 1-token completion. {ok, detail, models_found}; the key
    is never echoed. A listing from the provider's configured endpoint is kept
    as "discovered" models. The STORED key is only ever sent to the configured
    endpoint — testing an ad-hoc base_url needs the key in the request."""
    import httpx
    p = provider(pid)
    st = _load_state()
    configured = _base(st, p)
    target = _check_base_url(base_url) if base_url else configured
    if needs_base_url(target):
        return {"ok": False, "detail": "set this provider's base_url first "
                "(it still has a {PLACEHOLDER})", "models_found": []}
    key = (api_key or "").strip() or None
    if key is None and endpoint(target) == endpoint(configured):
        key = _stored_or_env_key(p["id"])
    if needs_key(p) and not key:
        return {"ok": False, "detail": "no API key to test", "models_found": []}
    try:
        async with httpx.AsyncClient(timeout=TEST_TIMEOUT,
                                     transport=HTTP_TRANSPORT) as client:
            res = await _probe(client, p, target, key)
    except httpx.TimeoutException:
        return {"ok": False, "detail": f"timed out after {TEST_TIMEOUT:.0f}s",
                "models_found": []}
    except httpx.HTTPError as e:
        return {"ok": False, "detail": f"could not reach {urlsplit(target).hostname}: "
                f"{type(e).__name__}", "models_found": []}
    except (ValueError, KeyError, TypeError, AttributeError):
        return {"ok": False, "detail": "unexpected response shape", "models_found": []}
    if res["ok"] and res["models_found"] and target == configured:
        record_discovered(p["id"], res["models_found"])
    return res


_stored_or_env_key = api_key     # test_provider's `api_key` arg shadows the function


# --- HTTP API (control plane: operator cookie only, never a device token) -----

from fastapi import APIRouter, Depends, HTTPException  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from .auth import require_user  # noqa: E402

router = APIRouter(prefix="/api", tags=["providers"],
                   dependencies=[Depends(require_user)])


def _http(e: ProviderError) -> HTTPException:
    status = 404 if str(e).startswith("unknown provider") else 400
    return HTTPException(status_code=status, detail=str(e))


class ProviderUpdate(BaseModel):
    api_key: str | None = None      # "" clears the stored key
    base_url: str | None = None     # null (sent) resets to the catalogue default
    enabled: bool | None = None


class ProviderTest(BaseModel):
    api_key: str | None = None
    base_url: str | None = None


class ModelUpdate(BaseModel):
    enabled: bool | None = None
    default: bool | None = None


@router.get("/providers")
async def http_list_providers(models: bool = True):
    """?models=0 drops the per-provider model lists (the full catalogue is
    ~1.5 MB); GET /api/providers/{id} then fetches one provider's."""
    return {"catalog_source": catalog()["source"], "default": default_model(),
            "providers": list_providers(include_models=models)}


@router.get("/providers/{pid}")
async def http_get_provider(pid: str):
    try:
        return get_provider(pid)
    except ProviderError as e:
        raise _http(e) from None


@router.put("/providers/{pid}")
async def http_update_provider(pid: str, body: ProviderUpdate):
    try:
        provider(pid)
        if body.api_key is not None:
            set_key(pid, body.api_key)
        base = None
        if "base_url" in body.model_fields_set:
            base = body.base_url or ""          # null/"" -> back to the default
        return update_provider(pid, base_url=base, enabled=body.enabled)
    except ProviderError as e:
        raise _http(e) from None


@router.post("/providers/{pid}/test")
async def http_test_provider(pid: str, body: ProviderTest | None = None):
    body = body or ProviderTest()
    try:
        return await test_provider(pid, api_key=body.api_key, base_url=body.base_url)
    except ProviderError as e:
        raise _http(e) from None


@router.put("/providers/{pid}/models/{model:path}")
async def http_update_model(pid: str, model: str, body: ModelUpdate):
    try:
        view = update_model(pid, model, enabled=body.enabled, default=body.default)
    except ProviderError as e:
        raise _http(e) from None
    return {"model": view, "default": default_model()}


@router.get("/models")
async def http_models():
    return models_payload()
