"""computer_status: what the operator's connected machines can drive.

With no `client`, this describes EVERY connected machine rather than picking
one — otherwise, with two computers connected, the model has to guess a name
before it can find out what the names are.
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
    lines.append("  folders reachable: " + (", ".join(d.get("roots") or [])
                                            or "none — nothing is playable"))
    return "\n".join(lines)


async def run(client: str = "") -> str:
    conn = cu.clients()
    if not conn:
        return ("no computer is connected. The operator starts the client on the "
                "machine they want driven — the Computer use tab has the command.")
    targets = [client] if client else [c.name for c in conn]
    out = []
    if not client and len(conn) > 1:
        out.append(f"{len(conn)} machines connected. Pass `client` to any "
                   f"computer_* tool to choose one.\n")
    for t in targets:
        try:
            r = await cu.dispatch("status", {}, t)
        except cu.VerbError as e:
            out.append(f"{t}: {e}")
            continue
        if not r.get("ok"):
            out.append(f"{t}: error — {r.get('error')}")
            continue
        out.append(_describe(t, r.get("result", {})))
    return "\n\n".join(out)
