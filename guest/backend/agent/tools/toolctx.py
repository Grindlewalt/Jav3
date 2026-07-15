"""Guest toolctx stub. On the host, require_project reads the DB session_state +
a contextvar and can mint a hidden artifact project. In the guest none of that
exists: the host has already resolved the active project and pushed its workspace,
so this just returns that slug (set per turn by the run-turn server). Dropping the
DB/contextvar/artifact coupling is what lets the clean file tools run in-guest."""
_active_slug: str | None = None


def set_active(slug: str | None) -> None:
    global _active_slug
    _active_slug = slug


async def require_project() -> str:
    if not _active_slug:
        raise LookupError("no project is loaded in the guest for this turn")
    return _active_slug


async def active_slug() -> str | None:
    return _active_slug


async def web_session() -> str:
    # web tools are brokered to the host; the in-guest tools never call this.
    return _active_slug or "global"
