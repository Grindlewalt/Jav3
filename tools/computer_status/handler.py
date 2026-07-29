"""computer_status: what the connected desktops can drive."""
from backend import computeruse as cu


async def run(client: str = "") -> str:
    conn = cu.clients()
    if not conn:
        return ("no computer is connected. The operator starts the client with "
                "clients/computeruse/agent.py on the machine they want driven.")
    try:
        r = await cu.dispatch("status", {}, client or None)
    except cu.VerbError as e:
        return f"error: {e}"
    if not r.get("ok"):
        return f"error: {r.get('error')}"
    d = r.get("result", {})
    lines = [f"connected: {', '.join(c.name for c in conn)}",
             f"platform: {d.get('platform')}"]
    if d.get("dry_run"):
        lines.append("NOTE: this client is in dry-run — it reports what it "
                     "would do and does not touch the machine.")
    screens = d.get("screens") or []
    lines.append("screens: " + (", ".join(
        f"{s['index']}={s.get('id')} {s.get('geometry', '')}".strip()
        for s in screens) or "none detected"))
    devs = d.get("audio_devices") or []
    lines.append("audio devices: " + (", ".join(
        f"{a['id']} ({a['label']})" for a in devs) or "none detected"))
    lines.append("running players: " + (", ".join(d.get("players") or []) or "none"))
    lines.append("granted folders: " + (", ".join(d.get("roots") or []) or "none"))
    return "\n".join(lines)
