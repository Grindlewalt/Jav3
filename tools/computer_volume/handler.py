"""computer_volume: the operator's system mixer."""
from backend import computeruse as cu


async def run(action: str = "", percent: int | None = None,
              device: str = "", client: str = "") -> str:
    params: dict = {"action": action}
    if percent is not None:
        params["percent"] = percent
    if device:
        params["device"] = device
    try:
        r = await cu.dispatch("volume", params, client or None)
    except cu.VerbError as e:
        return f"error: {e}"
    if not r.get("ok"):
        return f"error: {r.get('error')}"
    d = r.get("result", {})
    where = d.get("device", "the default output")
    if action in ("mute", "unmute"):
        return f"{action}d {where}."
    if action == "set":
        return f"set {where} to {percent}%."
    return f"volume {action} {percent or 5} points on {where}."
