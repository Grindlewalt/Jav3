"""workspace_panel: arrange the active project's control board server-side.

Edits the same .workspace.json the GUI saves; a layout_changed event makes any
open board refetch, so the change is visible immediately AND durable.
"""
import re

from backend import gui
from backend.agent.tools import toolctx
from backend.config import settings
from backend.fsutil import safe_join

RENDER_EXT = re.compile(r"\.(html?|pdf|png|jpe?g|gif|svg|webp)$", re.I)


def _live_note(n: int) -> str:
    return ("visible now" if n else
            "no board is open right now — it will show on the next visit")


async def run(action: str = "", panel: str = "", path: str = "") -> str:
    slug = await toolctx.active_slug()
    if not slug:
        return "error: no active project — load one first"
    panels = gui.load_panels(slug)

    if action == "tile":
        n = gui.save_panels(slug, gui.tile_panels(panels))
        return f"tiled {len(panels)} panels on '{slug}' ({_live_note(n)})."

    if action == "remove":
        if not panel:
            return "error: remove needs `panel` (the panel type)"
        kept, dropped = gui.remove_panels(panels, panel)
        if not dropped:
            have = ", ".join(sorted({p['type'] for p in panels}))
            return f"no '{panel}' panel on the board. Currently there: {have}"
        n = gui.save_panels(slug, kept)
        return f"removed {dropped} '{panel}' panel(s) ({_live_note(n)})."

    if action == "open_file":
        if not path:
            return "error: open_file needs `path`"
        panel = "renderer" if RENDER_EXT.search(path) else "editor"
        action = "add"

    if action == "add":
        if panel not in gui.PANEL_SIZES:
            return ("error: unknown panel type "
                    f"'{panel}'. One of: {', '.join(sorted(gui.PANEL_SIZES))}")
        state = {}
        if path and panel in ("editor", "renderer"):
            try:
                if not safe_join(settings.projects_dir / slug, path).is_file():
                    return f"error: no such file in '{slug}': {path}"
            except Exception:
                return f"error: path '{path}' escapes the project"
            state = {"path": path}
        added = gui.add_panel(panels, panel, state)
        n = gui.save_panels(slug, panels)
        what = f"'{panel}' panel" + (f" on {path}" if path else "")
        return f"added {what} as {added['id']} ({_live_note(n)})."

    return "error: action must be one of add, remove, open_file, tile"
