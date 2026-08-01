"""workspace_panel: arrange the active project's control board server-side.

Edits the same .workspace.json the GUI saves; a layout_changed event makes any
open board refetch, so the change is visible immediately AND durable.

open_file resolves what it is given rather than demanding the exact path. The
commonest miss by far was a file that had just been written under dashboards/
being asked for by its bare name — the name the write result reported, and the
name a person would say. It is the same file; finding it is a string problem,
not a reason to fail the turn.
"""
import re

from backend import gui
from backend.agent.tools import toolctx
from backend.config import settings
from backend.fsutil import find_file, safe_join

# What the Renderer panel can actually show. Kept in step with MEDIA_EXT in
# frontend/src/pages/Workspace.jsx — this list IS the render menu, so a file the
# panel offers must be a file this tool will open.
RENDER_EXT = re.compile(r"\.(html?|pdf|png|jpe?g|gif|svg|webp)$", re.I)

SHOWN = 25


def _live_note(n: int) -> str:
    return ("visible now" if n else
            "no board is open right now — it will show on the next visit")


def _menu(candidates: list[str]) -> str:
    if not candidates:
        return "  (nothing renderable in this project yet)"
    shown = "\n".join(f"  {p}" for p in candidates[:SHOWN])
    more = "" if len(candidates) <= SHOWN else f"\n  ... and {len(candidates) - SHOWN} more"
    return shown + more


async def run(action: str = "", panel: str = "", path: str = "") -> str:
    slug = await toolctx.active_slug()
    if not slug:
        return "error: no active project — load one first"
    panels = gui.load_panels(slug)
    project = settings.projects_dir / slug

    if action == "list":
        # The render menu, answered without a guess — the same list the panel's
        # own dropdown shows. Asking beats opening the wrong file.
        _, renderable = find_file(project, "", only=RENDER_EXT)
        on_board = ", ".join(sorted({p["type"] for p in panels})) or "none"
        return (f"panels on '{slug}': {on_board}\n"
                f"files the Renderer can open:\n{_menu(sorted(renderable))}")

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

    resolved_note = ""
    if action == "open_file":
        if not path:
            return "error: open_file needs `path`"
        renderable = bool(RENDER_EXT.search(path))
        found, candidates = find_file(project, path,
                                      only=RENDER_EXT if renderable else None)
        if found is None:
            what = "can be rendered" if renderable else "are in this project"
            return (f"error: nothing matching '{path}' in '{slug}'. These {what}:\n"
                    f"{_menu(sorted(candidates))}")
        if found != path.strip().lstrip("./").replace("\\", "/"):
            resolved_note = f" — resolved '{path}' to '{found}'"
        path = found
        panel = "renderer" if RENDER_EXT.search(path) else "editor"
        action = "add"

    if action == "add":
        if panel not in gui.PANEL_SIZES:
            return ("error: unknown panel type "
                    f"'{panel}'. One of: {', '.join(sorted(gui.PANEL_SIZES))}")
        state = {}
        if path and panel in ("editor", "renderer"):
            try:
                if not safe_join(project, path).is_file():
                    return f"error: no such file in '{slug}': {path}"
            except Exception:
                return f"error: path '{path}' escapes the project"
            state = {"path": path}
        added = gui.add_panel(panels, panel, state)
        n = gui.save_panels(slug, panels)
        what = f"'{panel}' panel" + (f" on {path}" if path else "")
        return f"added {what} as {added['id']} ({_live_note(n)}){resolved_note}."

    return "error: action must be one of add, remove, open_file, tile, list"
