"""Guest toolctx stub. On the host, require_project reads the DB session_state +
a contextvar and can mint a hidden artifact project. In the guest none of that
exists: the host has already resolved the active project and pushed its workspace,
so this just returns that slug (task-local, set per turn by the run-turn server).
Dropping the DB/contextvar/artifact coupling is what lets the clean file tools run
in-guest; keeping the slug task-local is what lets a nested turn use its own."""
from ... import turnctx


class NoProjectError(LookupError):
    """No project in this guest turn. `for_model`: the message is written for the
    model, so argcheck.crash_message hands it back as is instead of calling it a
    harness fault (same class as the host toolctx, which the tool handlers never
    tell apart)."""
    for_model = True


def set_active(slug: str | None) -> None:
    turnctx.active_slug.set(slug)


async def require_project() -> str:
    slug = turnctx.active_slug.get()
    if not slug:
        # load_project mid-turn only takes effect next turn (its result says so),
        # so with no project at all the file tools have nowhere to work this turn
        raise NoProjectError(
            "no project is loaded, so the file tools have no workspace this turn. "
            "Use run_code for scratch files, or call load_project with a slug (see "
            "'All projects' in your context) — its files are usable from the next turn")
    return slug


async def active_slug() -> str | None:
    return turnctx.active_slug.get()


async def web_session() -> str:
    # web tools are brokered to the host; the in-guest tools never call this.
    return turnctx.active_slug.get() or "global"
