from backend import writes
from backend.codeindex import NOTES_SUBDIR, build_index
from backend.config import settings
from backend.agent.tools.toolctx import require_project


async def run(subdir: str = "code") -> str:
    slug = await require_project()
    info = build_index(slug, subdir)
    if info["files"] == 0:
        return (f"no text files found under {subdir or '.'}/ — "
                "upload or write some code first")
    # in the guest only .staging/ ships home at turn end — re-route the note
    # files through the writes buffer or the whole index evaporates with the
    # disposable workspace while the tool reports success
    if getattr(settings, "in_guest", False):
        base = settings.projects_dir / slug
        for rel in info["notes"]:
            p = base / rel
            if p.is_file():
                await writes.apply_write(slug, rel, p.read_bytes())
    return (f"indexed {info['files']} files in {info['dirs']} directories "
            f"({info['bytes']} bytes) under {subdir or '.'}/.\n"
            f"notes written: {', '.join(info['notes'])}\n"
            f"Next: read {NOTES_SUBDIR}/INDEX.md for the map, use search_codebase "
            "to find symbols or strings, and read_file to open specific files.")
