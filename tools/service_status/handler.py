from backend.agent.tools.toolctx import require_project
from backend.vm import services


async def run(name: str = "") -> str:
    try:
        slug = await require_project()
    except LookupError as e:
        return f"error: {e}"
    rows = await services.list_services(slug)
    if name:
        rows = [r for r in rows if r["name"] == name]
    if not rows:
        return f"no services{f' named {name!r}' if name else ''} for '{slug}'"
    lines = []
    for r in rows[:30]:
        j = services.row_json(r)
        exp = ", ".join(f"{e['port']}/{e['bind']}" for e in j["expose_ports"]) or "none"
        lines.append(
            f"#{j['id']} {j['name']}: {j['status']}"
            + (f", {j['state']}" if j["status"] == "approved" else "")
            + f", placement {j['placement']}, exposed {exp}"
            + (f", last report {j['last_reported_at']}" if j["last_reported_at"] else "")
            + (f", error: {j['error']}" if j.get("error") else "")
            + (f", note: {j['decision_note']}" if j.get("decision_note") else ""))
    return "\n".join(lines)
