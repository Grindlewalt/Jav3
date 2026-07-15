"""Guest toolctx stub. On the host, require_project reads the DB session_state +
a contextvar and can mint a hidden artifact project. In the guest none of that
exists: the host has already resolved the active project and pushed its workspace,
so this just returns that slug (task-local, set per turn by the run-turn server).
Dropping the DB/contextvar/artifact coupling is what lets the clean file tools run
in-guest; keeping the slug task-local is what lets a nested turn use its own."""
from ... import turnctx


def set_active(slug: str | None) -> None:
    turnctx.active_slug.set(slug)


async def require_project() -> str:
    slug = turnctx.active_slug.get()
    if not slug:
        raise LookupError("no project is loaded in the guest for this turn")
    return slug


async def active_slug() -> str | None:
    return turnctx.active_slug.get()


async def web_session() -> str:
    # web tools are brokered to the host; the in-guest tools never call this.
    return turnctx.active_slug.get() or "global"
