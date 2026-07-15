from backend.config import settings
from backend.agent.tools.todostore import _parse_todos, _write_todos
from backend.agent.tools.toolctx import require_project


async def run(action: str, text: str | None = None, index: int | None = None) -> str:
    slug = await require_project()
    base = settings.projects_dir / slug
    todos = _parse_todos(base)
    if action == "list":
        pass
    elif action == "add":
        if not text:
            return "error: add needs text"
        todos.append({"done": False, "text": text.strip()})
    elif action in ("check", "uncheck", "delete"):
        if index is None or not (0 <= index < len(todos)):
            return f"error: index must be 0..{len(todos) - 1}"
        if action == "delete":
            todos.pop(index)
        else:
            todos[index]["done"] = action == "check"
    else:
        return f"error: unknown action '{action}'"
    if action != "list":
        _write_todos(base, todos)
    if not todos:
        return "todo list is empty"
    return "\n".join(
        f"{i}. [{'x' if t['done'] else ' '}] {t['text']}" for i, t in enumerate(todos))
