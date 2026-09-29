import posixpath

from backend.writes import SecretLeakError, apply_write
from backend.agent.tools.toolctx import require_project


async def run(path: str, content: str) -> str:
    slug = await require_project()
    # '', '.', './' and 'sub/..' all name the project folder itself, not a file
    if posixpath.normpath(path.strip().replace("\\", "/")) == ".":
        return ("error: path is required — give the file's path inside the project, "
                "e.g. notes/todo.md")
    try:
        await apply_write(slug, path, content.encode())
    except SecretLeakError as e:
        return (f"error: write refused — the content contains the literal value "
                f"of secret(s): {', '.join(e.names)}. Reference secrets as "
                "{{secret:NAME}} placeholders; never paste their values into files.")
    except ValueError as e:        # a protected path (.git, .staging)
        return f"error: write refused — {e}."
    return f"wrote {path} ({len(content)} chars)"
