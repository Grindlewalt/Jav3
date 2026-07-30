"""computer_library: browse the granted folders on the operator's machine.

The walk happens on the CLIENT. This used to scan the Jarvis host's disk, which
only ever worked when the client happened to run on that same host — never true
for a laptop, so "/Users/you/Movies" was invisible and the answer was always
empty.
"""
from backend import computeruse as cu


async def run(folder: str = "", kind: str = "both", limit: int = 60,
              client: str = "") -> str:
    if kind not in ("both", "audio", "video"):
        return "error: kind must be 'both', 'audio' or 'video'"
    params: dict = {"kind": kind, "limit": max(1, min(int(limit or 60), 300))}
    if folder:
        params["folder"] = folder
    try:
        r = await cu.dispatch("list", params, client or None, timeout=30)
    except cu.VerbError as e:
        return f"error: {e}"
    if not r.get("ok"):
        return f"error: {r.get('error')}"
    text = (r.get("result") or {}).get("text", "")
    if not text.strip():
        return "nothing playable in the granted folders."
    return (text + '\n\nexpand a subfolder with folder="<name>"; play something '
            'by passing its path to computer_play')
