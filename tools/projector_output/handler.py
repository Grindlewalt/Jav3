"""projector_output: start/stop the projection, and the aiming overlay."""
import json

from backend import mcp


async def run(action: str = "", display: int | None = None,
              calibrate: bool | None = None) -> str:
    if not action and calibrate is None:
        return "pass action=open/close, or calibrate=true/false."

    out = []
    try:
        if action == "open":
            args = {} if display is None else {"display": int(display)}
            raw = await mcp.projector_call("pmu_open_output", args)
            try:
                info = json.loads(raw)
                n = info.get("native") or {}
                out.append(f"Output open on display {info.get('display')} "
                           f"at {n.get('width')}x{n.get('height')}.")
            except (TypeError, ValueError):
                out.append(raw)
        elif action == "close":
            await mcp.projector_call("pmu_close_output")
            out.append("Output closed. Nothing is being projected; the "
                       "alignment is untouched.")
        elif action:
            return "action must be open or close."

        if calibrate is not None:
            await mcp.projector_call("pmu_set_overlay",
                                     {"calibrate": bool(calibrate)})
            out.append("Calibration overlay "
                       + ("on — each surface is outlined with its corner pips."
                          if calibrate else "off."))
    except mcp.McpError as exc:
        return str(exc)
    return " ".join(out)
