from backend import writes
from backend.agent.tools.todostore import parse_todo_text, render_todos
from backend.agent.tools.toolctx import require_project


def _render(todos) -> str:
    if not todos:
        return "todo list is empty"
    return "\n".join(
        f"{i}. [{'x' if t['done'] else ' '}] {t['text']}" for i, t in enumerate(todos))


def _pick(todos, index, text):
    """Which item was meant -> (position, error).

    Text first, index second, and that order is the fix. An index is a fact
    about a list at a moment: items get added by this turn and by subagents
    running in parallel, so a position the model read three calls ago is
    routinely not the item it means any more — and checking off the WRONG item
    is worse than the error this used to raise. Matching text is a string
    problem, so it happens here rather than in the model's memory for numbers.
    """
    if text:
        want = " ".join(text.lower().split())
        exact = [i for i, t in enumerate(todos)
                 if " ".join(t["text"].lower().split()) == want]
        if len(exact) == 1:
            return exact[0], None
        near = [i for i, t in enumerate(todos)
                if want in " ".join(t["text"].lower().split())]
        if len(near) == 1:
            return near[0], None
        if len(near) > 1:
            listing = "\n".join(f"{i}. {todos[i]['text']}" for i in near[:6])
            return None, (f"error: '{text}' matches several items — pass the "
                          f"index of the one you mean:\n{listing}")
        return None, (f"error: no todo matches '{text}'. The list is:\n"
                      f"{_render(todos)}")
    if index is None:
        return None, ("error: say which item — `text` (matched against the list, "
                      "preferred) or `index`.\n" + _render(todos))
    if not (0 <= index < len(todos)):
        # the list rides along with the error, so the retry is informed instead
        # of being another guess at a number
        return None, (f"error: index {index} is out of range — the list has "
                      f"{len(todos)} item(s), so valid indexes are "
                      f"0..{max(len(todos) - 1, 0)}. Prefer `text`:\n"
                      f"{_render(todos)}")
    return index, None


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
        at, err = _pick(todos, index, text)
        if err:
            return err
        if action == "delete":
            todos.pop(at)
        else:
            todos[at]["done"] = action == "check"
    else:
        return f"error: unknown action '{action}'"
    if action != "list":
        try:
            await writes.apply_write(slug, "todo.md", render_todos(todos).encode())
        except writes.SecretLeakError as e:
            return f"error: todo update refused — {e}"
    return _render(todos)
