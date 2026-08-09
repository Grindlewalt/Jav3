"""projector_show: content onto a projected surface, in one call.

Resolving "the ceiling" to a surface id happens HERE, with an algorithm, rather
than by making the model call a list tool and then act on what it read. That is
the operator's standing preference and it is also safer: fewer round trips
through the model means fewer chances for something in a tool result to steer
the next call.
"""
from backend import mcp

MEDIA = {"image", "video"}


async def run(surface: str = "", show: str = "", path: str = "",
              color: str = "", seed: int | None = None,
              visible: bool | None = None,
              opacity: float | None = None) -> str:
    if not str(surface).strip():
        return "which surface? Name it the way the operator did."
    if not show and visible is None and opacity is None:
        return ("nothing to do — pass `show` for content, or `visible` / "
                "`opacity` to hide or fade it.")

    try:
        surfaces = await mcp.projector_surfaces()
        target = mcp.resolve_surface(surfaces, surface)
    except mcp.McpError as exc:
        return str(exc)

    sid = target["id"]
    done = []
    try:
        if show:
            args = {"surface": sid, "kind": show}
            if show == "color":
                if not color:
                    return "show=color needs a hex colour, like #1a2b3c."
                args["color"] = color
            if show in MEDIA:
                if not path:
                    return f"show={show} needs `path` — the file to play."
                args["path"] = path
            if seed is not None:
                args["seed"] = int(seed)
            if show == "voice":
                # one verb, not two: this both sets the source AND routes the
                # live feed at it, so the panel is never a dead placeholder
                done.append(await mcp.projector_call("pmu_show_voice",
                                                     {"surface": sid}))
            else:
                done.append(await mcp.projector_call("pmu_set_source", args))
        if opacity is not None:
            done.append(await mcp.projector_call(
                "pmu_set_opacity", {"surface": sid, "opacity": float(opacity)}))
        if visible is not None:
            done.append(await mcp.projector_call(
                "pmu_set_visible", {"surface": sid, "visible": bool(visible)}))
    except mcp.McpError as exc:
        return str(exc)

    name = target.get("name") or f"surface {sid}"
    bits = []
    if show:
        bits.append(f"showing {show}")
    if opacity is not None:
        bits.append(f"opacity {float(opacity):.2f}")
    if visible is not None:
        bits.append("visible" if visible else "hidden")
    # if anything came back an error, that text is the honest answer
    failed = [d for d in done if d.lower().startswith("projector could not")]
    if failed:
        return " ".join(failed)
    return f"{name}: {', '.join(bits)}."
