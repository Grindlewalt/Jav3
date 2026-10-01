"""A provider that refuses calls because the account is out of money (HTTP 402,
DeepSeek's "Insufficient Balance").

Left alone it surfaces as raw guest text with a doubled prefix ("(guest loop
error: ModelError: ModelError: model API 402: {...})"), no bell, and a plan
treats every item it was running as a failed attempt to retry (PLANS-14). The
model gateway (agent/model.py) hands the 402 to `refused`, which

  - turns it into one plain ModelError the operator can act on,
  - remembers the provider is empty until a later call to it succeeds (`ok`),
    so a plan runner can pause instead of burning attempts: `is_empty()` /
    `reason()`,
  - raises ONE critical security event per outage (the bell), coalesced onto its
    unacknowledged twin across restarts by security.raise_event.

`tidy` rewrites the guest's wrapped copy of that text back to the plain
message, for whoever shows a turn's final content."""
import asyncio
import logging
import time

log = logging.getLogger("jav3.balance")

# where to add credit, by catalogue provider id; others get a generic pointer
_TOPUP = {"deepseek": "platform.deepseek.com"}

_empty: dict[str, float] = {}       # provider -> when it was first refused
_messages: dict[str, str] = {}      # provider -> the text the operator reads
_noticed: set[str] = set()          # providers whose outage already rang the bell
_tasks: set[asyncio.Task] = set()   # the notice tasks, kept alive until done


def _name(route) -> str:
    return getattr(route, "provider", None) or ""


def _label(provider: str) -> str:
    if provider == "deepseek":
        return "DeepSeek"
    return provider.capitalize() if provider else "The model provider"


def message_for(provider: str) -> str:
    where = _TOPUP.get(provider, "the provider's billing page")
    return (f"{_label(provider)} balance is empty (the provider answered 402 "
            f"Insufficient Balance). Top up at {where}, then send your message "
            "again.")


def refused(route, detail: str):
    """The gateway got a 402 from `route`'s provider. Record it, ring the bell
    once, and return the ModelError to raise in place of the provider's body."""
    from .agent.adapters import ModelError
    provider = _name(route)
    msg = _messages[provider] = message_for(provider)
    _empty.setdefault(provider, time.time())
    if provider not in _noticed:
        _noticed.add(provider)
        try:
            task = asyncio.get_running_loop().create_task(
                _notice(provider, msg, detail))
        except RuntimeError:        # no loop (a bare sync caller): the log is enough
            log.warning("%s", msg)
        else:
            _tasks.add(task)
            task.add_done_callback(_tasks.discard)
    return ModelError(msg, status=402)


def ok(route) -> None:
    """A call to this provider went through: the account has credit again."""
    provider = _name(route)
    if _empty.pop(provider, None) is not None:
        _noticed.discard(provider)
        log.info("%s answered again; balance is back", provider)


def is_empty(provider: str | None = None) -> bool:
    return (provider in _empty) if provider else bool(_empty)


def reason() -> str | None:
    """The operator-facing line for the provider that has been empty longest."""
    if not _empty:
        return None
    return _messages[min(_empty, key=_empty.get)]


def tidy(text: str) -> str:
    """`text` with the guest loop's wrapper ("(guest loop error: ModelError:
    ModelError: ...)") dropped when it carries a balance message; anything else
    comes back unchanged."""
    for msg in _messages.values():
        if msg in text:
            return msg
    return text


def reset() -> None:
    """Forget everything (tests)."""
    _empty.clear()
    _messages.clear()
    _noticed.clear()


async def _notice(provider: str, msg: str, detail: str) -> None:
    try:
        from . import security
        from .db import get_db
        db = await get_db()
        try:
            await security.raise_event(
                db, kind="provider_balance", severity="critical", summary=msg[:300],
                cause=f"provider_balance:{provider}",
                detail={"provider": provider, "response": detail[:500]})
        finally:
            await db.close()
    except Exception:  # noqa: BLE001 — a failed bell must not hide the real error
        log.warning("could not raise the %s balance notice", provider, exc_info=True)
