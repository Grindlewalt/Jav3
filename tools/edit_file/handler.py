from backend.writes import SecretLeakError, apply_write, resolve
from backend.agent.tools.toolctx import require_project


async def run(path: str, find: str, replace: str, all: bool = False) -> str:
    slug = await require_project()
    if find == replace:
        return "error: find and replace are identical — no change to make."
    p = resolve(slug, path)
    if p is None:
        return f"error: no such file: {path}"
    text = p.read_text()
    count = text.count(find)
    if count == 0:
        return (f"error: 'find' text not found in {path}. Read the file with read_file "
                "and copy the exact text, including whitespace and indentation.")
    if count > 1 and not all:
        return (f"error: 'find' matches {count} places in {path}. Set all=true to "
                "replace every occurrence, or extend 'find' with surrounding lines "
                "to make it unique.")
    new = text.replace(find, replace)
    try:
        await apply_write(slug, path, new.encode())
    except SecretLeakError as e:
        return (f"error: edit refused — the result would contain the literal value "
                f"of secret(s): {', '.join(e.names)}. Reference secrets as "
                "{{secret:NAME}} placeholders; never paste their values into files.")
    n = count if all else 1
    return f"edited {path} ({n} replacement{'s' if n != 1 else ''})"
