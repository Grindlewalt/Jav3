"""Pure todo.md read/write helpers (stdlib only), shared by the workspace API,
the todo_update tool, and the guest (which runs todo_update in-guest against the
pushed workspace). Extracted out of workspace.py so it doesn't drag FastAPI into
the guest."""
import re
from pathlib import Path

TODO_RE = re.compile(r"^- \[([ x])\] (.*)$")


def _todo_path(base: Path) -> Path:
    return base / "todo.md"


def _parse_todos(base: Path) -> list[dict]:
    path = _todo_path(base)
    if not path.exists():
        return []
    todos = []
    for line in path.read_text().splitlines():
        m = TODO_RE.match(line.strip())
        if m:
            todos.append({"done": m.group(1) == "x", "text": m.group(2)})
    return todos


def _write_todos(base: Path, todos: list[dict]) -> None:
    lines = ["# Todo", ""]
    lines += [f"- [{'x' if t['done'] else ' '}] {t['text']}" for t in todos]
    _todo_path(base).write_text("\n".join(lines) + "\n")
