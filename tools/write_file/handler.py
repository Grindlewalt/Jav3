from backend.writes import SecretLeakError, apply_write
from backend.agent.tools.toolctx import require_project


async def run(path: str, content: str) -> str:
    slug = await require_project()
    try:
        await apply_write(slug, path, content.encode())
    except SecretLeakError as e:
        return (f"error: write refused — the content contains the literal value "
                f"of secret(s): {', '.join(e.names)}. Reference secrets as "
                "{{secret:NAME}} placeholders; never paste their values into files.")
    return f"wrote {path} ({len(content)} chars)"
