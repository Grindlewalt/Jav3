"""projector_status: the whole picture in one call, spoken plainly.

Renders the projector's JSON into prose rather than handing the model raw
state. Two reasons: a voice turn has to be able to say this out loud, and the
less outside-authored text goes into the context verbatim, the better.
"""
import json

from backend import mcp


async def run() -> str:
    try:
        raw = await mcp.projector_call("pmu_status")
        st = json.loads(raw)
    except mcp.McpError as exc:
        return str(exc)
    except (TypeError, ValueError):
        return "the projector answered something unreadable"

    out = []
    output = st.get("output") or {}
    if output.get("open"):
        n = output.get("native") or {}
        out.append(f"Output is OPEN on display {output.get('display')} "
                   f"at {n.get('width')}x{n.get('height')}.")
    else:
        out.append("Output is CLOSED — nothing is being projected right now.")

    displays = st.get("displays") or []
    if displays:
        out.append("Displays: " + ", ".join(
            f"{d.get('label')} (id {d.get('id')}"
            + (", primary)" if d.get("isPrimary") else ")")
            for d in displays))

    surfaces = st.get("surfaces") or []
    if surfaces:
        out.append("Surfaces:")
        for s in surfaces:
            bits = [f"  {s.get('name')} (id {s.get('id')}): {s.get('showing')}"]
            if not s.get("visible", True):
                bits.append("[hidden]")
            op = s.get("opacity")
            if isinstance(op, (int, float)) and op < 1:
                bits.append(f"[opacity {op:.2f}]")
            lens = s.get("lens")
            if lens:
                bits.append(f"[lens {lens.get('level')}, following "
                            f"{lens.get('follow')}]")
            out.append(" ".join(bits))
    else:
        out.append("No surfaces in the current project.")

    if st.get("calibrate"):
        out.append("The calibration overlay is ON (outlines and corner pips are "
                   "drawn over the image).")
    sim = st.get("sim") or {}
    if sim.get("era"):
        out.append(f"Universe: {sim['era']} era, {sim.get('civs', 0)} "
                   f"civilisations{', paused' if sim.get('paused') else ''}.")
    return "\n".join(out)
