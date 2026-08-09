"""projector_universe: the simulation's transport and each surface's lens.

`regen` and `coldOpen` exist on the server but are NOT reachable from here —
they restart the universe and lose a run the operator may have had going for
days. The server refuses them too unless the operator turned them on, so this
is the second of two doors, not the only one.
"""
from backend import mcp

ACTIONS = {"pause": ("pause", None), "resume": ("resume", None),
           "skip_opening": ("skipColdOpen", None), "focus": ("focus", "target")}


async def run(action: str = "", target: str = "", surface: str = "",
              level: str = "", zoom: float | None = None,
              follow: str = "") -> str:
    if not action and not (surface and (level or zoom is not None or follow)):
        return ("pass an action (pause/resume/skip_opening/focus), or a "
                "surface with a level/zoom/follow to aim it.")

    out = []
    try:
        if action:
            if action not in ACTIONS:
                return f"action must be one of: {', '.join(ACTIONS)}."
            cmd, needs = ACTIONS[action]
            args = {"command": cmd}
            if needs:
                if not target:
                    return f"action={action} needs `target`."
                args["target"] = target
            await mcp.projector_call("pmu_sim_command", args)
            out.append({"pause": "Simulation paused.",
                        "resume": "Simulation running.",
                        "skip_opening": "Skipped to the steady state.",
                        "focus": f"Camera aimed at {target}."}[action])

        if surface and (level or zoom is not None or follow):
            surfaces = await mcp.projector_surfaces()
            t = mcp.resolve_surface(surfaces, surface)
            args: dict = {"surface": t["id"]}
            if level:
                args["level"] = level
            if zoom is not None:
                args["zoom"] = float(zoom)
            if follow:
                args["follow"] = follow
            await mcp.projector_call("pmu_set_lens", args)
            bits = [b for b in (f"level {level}" if level else "",
                                f"zoom {zoom}" if zoom is not None else "",
                                f"following {follow}" if follow else "") if b]
            out.append(f"{t.get('name')}: {', '.join(bits)}.")
    except mcp.McpError as exc:
        return str(exc)
    return " ".join(out)
