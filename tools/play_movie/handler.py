"""play_movie: start the GUI's floating video player, in ONE tab.

Same change as play_music: the tab that asked plays it, not every open tab.
"""
from backend import gui, runtime
from backend.agent.tools import toolctx


async def run(source: str = "", title: str = "", tab: str = "") -> str:
    slug = await toolctx.active_slug()
    src, err = gui.media_src(source, slug)
    if err:
        return f"error: {err}"
    shown = title.strip() or source.rsplit("/", 1)[-1]
    target, where = gui.resolve_tab(tab or None, runtime.gui_tab.get())
    if target is None:
        return f"nothing is playing — {where}."
    n = gui.push({"type": "play_media", "kind": "video", "src": src,
                  "title": shown}, tab=target)
    if n == 0:
        return f"'{where}' closed before it could play."
    return f"playing '{shown}' on {where}."
