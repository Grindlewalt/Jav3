from backend.codeindex import NOTES_SUBDIR, build_index
from backend.agent.tools.toolctx import require_project


async def run(subdir: str = "code") -> str:
    slug = await require_project()
    info = build_index(slug, subdir)
    if info["files"] == 0:
        return (f"no text files found under {subdir or '.'}/ — "
                "upload or write some code first")
    return (f"indexed {info['files']} files in {info['dirs']} directories "
            f"({info['bytes']} bytes) under {subdir or '.'}/.\n"
            f"notes written: {', '.join(info['notes'])}\n"
            f"Next: read {NOTES_SUBDIR}/INDEX.md for the map, use search_codebase "
            "to find symbols or strings, and read_file to open specific files.")
