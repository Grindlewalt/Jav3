"""computer_status: what the operator's connected machines can drive.

With no `client`, this describes EVERY connected machine rather than picking
one — otherwise, with two computers connected, the model has to guess a name
before it can find out what the names are.

It also reports why a granted folder is NOT reachable. That used to be missing:
a folder the client had rejected and a folder nobody had granted both arrived
here as the same empty list, so "I added the folder" and "there are no folders"
were both true and neither side could see the other's half.
"""
from backend import computeruse as cu


def _describe(name, d):
    lines = [f"{name} ({d.get('platform')})"
             + ("  [DRY RUN — reports, does not act]" if d.get("dry_run") else "")]
    screens = d.get("screens") or []
    lines.append("  screens: " + (", ".join(
        f"{s['index']}={s.get('id')} {s.get('geometry', '')}".strip()
        for s in screens) or "none detected"))
    devs = d.get("audio_devices") or []
    lines.append("  mixer outputs (computer_volume device): " + (", ".join(
        a["id"] for a in devs) or "none detected"))
    plays = d.get("play_devices") or []
    lines.append("  playback outputs (computer_play device): " + (", ".join(
        a["id"] for a in plays[:10]) or "none detected"))
    lines.append("  players running: " + (", ".join(d.get("players") or []) or "none"))

    # Folders, each with its actual state rather than a bare list of the ones
    # that survived. A rejected grant is the commonest reason a play fails, and
    # the reason is something only that machine knows.
    detail = d.get("roots_detail") or []
    ok = [r for r in detail if r.get("ok", True)]
    bad = [r for r in detail if not r.get("ok", True)]
    if ok:
        lines.append("  folders reachable:")
        lines.extend(f"    {r['path']}  ({r.get('audio', 0)} audio, "
                     f"{r.get('video', 0)} video)" for r in ok)
    else:
        lines.append("  folders reachable: none — nothing on this machine is playable")
    if bad:
        lines.append("  granted but NOT usable on this machine (the operator has "
                     "to fix these; you cannot):")
        lines.extend(f"    {r['path']} — {r.get('why', 'refused')}" for r in bad)
    if not detail and d.get("grant_note"):
        lines.append(f"  folders: {d['grant_note']}")

    # mpv missing is not a folder problem, and from a failed play it looks like
    # one. Say it here, where it is cheap to notice.
    bins = d.get("binaries")
    if bins is not None and "mpv" not in bins:
        lines.append("  mpv: NOT INSTALLED — this machine can play nothing from "
                     "disk until the operator installs it")
    return "\n".join(lines)


def _tabs() -> str:
    """Open Jarvis tabs are the OTHER place sound can come out, and the model
    has to be able to name one — "put it on the mac" is a tab name as often as
    it is a machine name."""
    from backend import gui
    open_tabs = gui.tab_list()
    if not open_tabs:
        return "\nJarvis tabs open in a browser: none."
    names = ", ".join(t["name"] for t in open_tabs)
    return (f"\nJarvis tabs open in a browser: {names}. Music plays in the one "
            f"the operator asked from unless you pass `tab` to music_play.")


async def run(client: str = "") -> str:
    conn = cu.clients()
    if not conn:
        return ("no computer is running the desktop client. The operator starts "
                "it on the machine they want driven — the Computer use tab has "
                "the command." + _tabs())
    targets = [client] if client else [c.name for c in conn]
    out = []
    if not client and len(conn) > 1:
        out.append(f"{len(conn)} machines connected. Pass `client` to any "
                   f"computer_* tool to choose one.\n")
    served = cu.served_build_id()
    for t in targets:
        try:
            r = await cu.dispatch("status", {}, t)
        except cu.VerbError as e:
            out.append(f"{t}: {e}")
            continue
        if not r.get("ok"):
            out.append(f"{t}: error — {r.get('error')}")
            continue
        d = r.get("result", {})
        text = _describe(t, d)
        if d.get("version") and d["version"] != served:
            text += ("\n  NOTE: this machine runs an older build of the client "
                     "than Jarvis serves. If something behaves wrongly, say so — "
                     "the operator re-runs the set-up command to update it.")
        out.append(text)
    return "\n\n".join(out) + _tabs()
