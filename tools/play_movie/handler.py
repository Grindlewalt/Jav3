"""play_movie: start the GUI's floating video player."""
from backend import gui
from backend.agent.tools import toolctx


async def run(source: str = "", title: str = "") -> str:
    slug = await toolctx.active_slug()
    src, err = gui.media_src(source, slug)
    if err:
        return f"error: {err}"
    shown = title.strip() or source.rsplit("/", 1)[-1]
    n = gui.push({"type": "play_media", "kind": "video", "src": src,
                  "title": shown})
    if n == 0:
        return "no GUI tab is connected right now — nothing is playing."
    return f"playing '{shown}' in {n} open tab(s)."
