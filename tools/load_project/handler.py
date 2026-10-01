from backend.agent.tools.toolctx import trashed_pin, trashed_pin_message
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
            async with db.execute(
                    "SELECT 1 FROM projects WHERE slug = ? AND deleted_at IS NOT NULL",
                    (slug,)) as cur:
                if await cur.fetchone():
                    return (f"error: project '{slug}' is in the trash (Recently deleted). Tell the "
                            "operator and offer to restore it (Projects > Recently "
                            "deleted > Restore); it cannot be loaded until then.")
            options = ", ".join(valid) or "(none exist)"
            return f"error: no project '{slug}'. Available: {options}"
        # a chat pinned to a project that was trashed: the model picking another
        # project to carry on in is not its call (WEBA-06: it chose a look-alike)
        trashed = await trashed_pin()
        if trashed:
            return "error: " + trashed_pin_message(*trashed) + " Nothing was changed."
        cid = runtime.conversation_id.get()
        if cid:
            async with db.execute(
                "SELECT project_id, project_locked FROM conversations WHERE id = ?",
                (cid,)) as cur:
                conv = await cur.fetchone()
            if conv and conv["project_locked"] and conv["project_id"] is None:
                # the operator opened this chat with "No project": its files go
                # to the chat's artifacts, and the model re-pointing it at
                # another project (one it happened to see listed, maybe another
                # agent's live one) is not its call (WEBA-01)
                return ("error: this chat was opened with No project, so the "
                        "project cannot be changed from inside it. Files you "
                        "write here go to this chat's artifacts; if the work "
                        "belongs in a project, tell the operator to pick that "
                        "project for the chat (or open a new chat in it).")
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
    in_guest_turn, previous, scratch = False, None, None
    try:
        from backend.agent import budget as budget_mod
        from backend.vm import broker
        env = broker.get_turn(budget_mod.active_op_id.get() or "")
        if env is not None:
            previous = env.active_project   # what the guest was given at turn start
            scratch = env.artifact_slug     # no project: the chat's artifact store
            env.active_project = slug       # brokered children resolve the new pin
            in_guest_turn = True
    except Exception:  # noqa: BLE001 — envelope update is best-effort
        pass
    md = read_project_md(slug)
    note = ""
    if in_guest_turn and previous != slug:
        # the guest unpacked (at most) the turn's first project, and the host
        # cannot reach into a running guest: say which case this is, or the
        # model trusts a workspace that is not there (conv 574: 'previous
        # project' with none, then list_files failed)
        note = ("\n(note: file tools finish this turn on the previous project's "
                "sandbox workspace; the switch is fully live next turn)"
                if previous else
                "\n(note: this turn started with no project, so the file tools "
                "finish it on this chat's artifact workspace, not this project's; "
                "the switch is fully live next turn)"
                if scratch else
                "\n(note: this turn started with no project, so the file tools "
                "(read_file, write_file, edit_file, list_files, search_codebase) "
                "have no workspace until the next turn. Use run_code for scratch "
                "work, or tell the operator to send another message to continue "
                "in this project)")
    return f"loaded project '{slug}'.{note} Its project.md:\n\n{md[:4000]}"
