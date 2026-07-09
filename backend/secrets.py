"""Operator API keys the agent can USE but never SEE.

Values live host-side in ~/.config/jarvis/secrets.json (0600), next to the
DeepSeek key's env file. The agent's context carries only the NAMES; it writes
`{{secret:NAME}}` in run_command/run_code/run_gated and the host substitutes
the real value at VM-execution time (vmexec.run_in_project — the single
chokepoint all three tools share). Everything persisted or fed back to the
model — args in the DB, the message history, stdout/stderr — carries the
placeholder: args are logged before substitution, and outputs are scrubbed
after the run. The API never returns a value either (names + last-4 only).
"""
import json
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_user
from .config import settings

PLACEHOLDER = re.compile(r"\{\{secret:([A-Za-z0-9_]+)\}\}")
_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _path() -> Path:
    return settings.secrets_path


def load() -> dict[str, str]:
    p = _path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save(secrets: dict[str, str]) -> None:
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
                "The operator adds keys in the Secrets panel on the Context page.")
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
    value: str


@router.get("")
async def list_secrets():
    return {"secrets": [{"name": n, "last4": v[-4:] if len(v) > 4 else "…"}
                        for n, v in sorted(load().items())]}


@router.put("/{name}")
async def set_secret(name: str, body: SetSecret):
    name = name.upper()
    if not _NAME.match(name):
        raise HTTPException(status_code=400,
                            detail="name must be LETTERS_DIGITS_UNDERSCORES")
    if not body.value.strip():
        raise HTTPException(status_code=400, detail="value is empty")
    secrets = load()
    secrets[name] = body.value.strip()
    save(secrets)
    return {"ok": True, "name": name}


@router.delete("/{name}")
async def delete_secret(name: str):
    secrets = load()
    if name.upper() not in secrets:
        raise HTTPException(status_code=404, detail="no such secret")
    del secrets[name.upper()]
    save(secrets)
    return {"ok": True}
