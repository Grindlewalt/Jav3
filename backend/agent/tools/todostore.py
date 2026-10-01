"""Pure todo-list read/write helpers (stdlib only), shared by the workspace API,
the todo_update tool, and the guest (which runs todo_update in-guest against the
pushed workspace). Extracted out of workspace.py so it doesn't drag FastAPI into
the guest.

The list lives in the hidden `.todo.md`, the agent's own file. A project's real
`todo.md` is the operator's: it is read once to seed the list when there is no
`.todo.md` yet and is never written (BUILD-07: the tool used to rewrite it to
bare checkbox lines, and an `rm todo.md` took the agent's state with it)."""
import re
from pathlib import Path

TODO_RE = re.compile(r"^- \[([ x])\] (.*)$")


TODO_FILE = ".todo.md"       # the agent's list (hidden, harness state)
LEGACY_FILE = "todo.md"      # seeds the list while TODO_FILE does not exist; never written


def _todo_path(base: Path) -> Path:
    return base / TODO_FILE


def parse_todo_text(text: str) -> list[dict]:
    todos = []
    for line in text.splitlines():
        m = TODO_RE.match(line.strip())
        if m:
            todos.append({"done": m.group(1) == "x", "text": m.group(2)})
    return todos


def render_todos(todos: list[dict]) -> str:
    lines = ["# Todo", ""]
    lines += [f"- [{'x' if t['done'] else ' '}] {t['text']}" for t in todos]
    return "\n".join(lines) + "\n"


def _parse_todos(base: Path) -> list[dict]:
    path = _todo_path(base)
    if not path.exists():
        path = base / LEGACY_FILE
        if not path.is_file():
            return []
    return parse_todo_text(path.read_text())


def _write_todos(base: Path, todos: list[dict]) -> None:
    _todo_path(base).write_text(render_todos(todos))
