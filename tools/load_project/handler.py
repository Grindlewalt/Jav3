from backend.db import get_db, set_state
from backend.memory import read_project_md


async def run(slug: str) -> str:
    from backend import runtime
    db = await get_db()
    try:
        async with db.execute(
            "SELECT slug, name FROM projects "
            "WHERE deleted_at IS NULL AND is_hidden = 0 ORDER BY slug"
        ) as cur:
            rows = await cur.fetchall()
        valid = {r["slug"]: r["name"] for r in rows}
        if slug not in valid:
            options = ", ".join(valid) or "(none exist)"
            return f"error: no project '{slug}'. Available: {options}"
        cid = runtime.conversation_id.get()
        if cid:
            # inside a conversation: rebind THIS conversation's pin and leave
            # the operator's global session alone — other chats/agent runs may
            # be working other projects concurrently.
            await db.execute(
                "UPDATE conversations SET project_id = "
                "(SELECT id FROM projects WHERE slug = ?) WHERE id = ?",
                (slug, cid))
        else:
            await set_state(db, "active_project", slug)
        await db.commit()
    finally:
        await db.close()
    # the rest of THIS turn resolves the new project too (host loop path — the
    # contextvar set sticks for the remainder of the turn task)
    runtime.active_project.set(slug)
    in_guest_turn = False
    try:
        from backend.agent import budget as budget_mod
        from backend.vm import broker
        env = broker.get_turn(budget_mod.active_op_id.get() or "")
        if env is not None:
            env.active_project = slug   # brokered children resolve the new pin
            in_guest_turn = True
    except Exception:  # noqa: BLE001 — envelope update is best-effort
        pass
    md = read_project_md(slug)
    note = ("\n(note: file tools finish this turn on the previous project's "
            "sandbox workspace; the switch is fully live next turn)"
            if in_guest_turn else "")
    return f"loaded project '{slug}'.{note} Its project.md:\n\n{md[:4000]}"
