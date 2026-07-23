from backend import writes
from backend.agent.tools.todostore import parse_todo_text, render_todos
from backend.agent.tools.toolctx import require_project


async def run(action: str, text: str | None = None, index: int | None = None) -> str:
    slug = await require_project()
    # read through writes.resolve so an in-guest turn sees its own pending
    # edits; write through apply_write so todo.md crosses the one chokepoint
    # (secret refusal + advisory scan on the host, .staging buffer in the guest)
    src = writes.resolve(slug, "todo.md")
    todos = parse_todo_text(src.read_text()) if src else []
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
        try:
            await writes.apply_write(slug, "todo.md", render_todos(todos).encode())
        except writes.SecretLeakError as e:
            return f"error: todo update refused — {e}"
    if not todos:
        return "todo list is empty"
    return "\n".join(
        f"{i}. [{'x' if t['done'] else ' '}] {t['text']}" for i, t in enumerate(todos))
