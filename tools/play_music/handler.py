"""play_music: start the GUI's floating audio player, in ONE tab.

It used to play in every open Jarvis tab at once — the laptop, the desktop and
the phone, all slightly out of step. The tab that asked is the one that plays;
see backend/gui.py:resolve_tab for the order it falls back through.
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
    n = gui.push({"type": "play_media", "kind": "audio", "src": src,
                  "title": shown}, tab=target)
    if n == 0:
        return f"'{where}' closed before it could play."
    return f"playing '{shown}' on {where}."
