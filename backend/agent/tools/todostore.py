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
ARCHIVE_FILE = ".todo-archive.md"   # finished items pruned from a long list; never loaded into a prompt

# A long list rides along in every `list` result and (on the board) in every
# poll: one orchestrator's reached 149 items, 15 of them done, 11.5 KB. Above
# PRUNE_ABOVE items the finished ones beyond the latest DONE_KEEP move to the
# archive file, and a `list` shows at most OPEN_VIEW_CAP open items.
OPEN_VIEW_CAP = 40
DONE_KEEP = 5
PRUNE_ABOVE = OPEN_VIEW_CAP + DONE_KEEP


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


def prune(todos: list[dict]) -> tuple[list[dict], list[dict]]:
    """(the list to keep, the finished items to archive).

    Only finished items ever leave, and only from a list longer than PRUNE_ABOVE:
    an open item is work somebody owes, and a short plan keeps its ticks so
    progress stays visible. The latest DONE_KEEP finished items (by position:
    a plan is checked off roughly in the order it was written) stay."""
    if len(todos) <= PRUNE_ABOVE:
        return todos, []
    done_at = [i for i, t in enumerate(todos) if t["done"]]
    gone = set(done_at[:max(len(done_at) - DONE_KEEP, 0)])
    if not gone:
        return todos, []
    return ([t for i, t in enumerate(todos) if i not in gone],
            [todos[i] for i in sorted(gone)])


def archive_text(existing: str, items: list[dict], day: str) -> str:
    """The archive file with `items` appended, under a heading for `day` (the
    last heading is reused when it is the same day: a list at its limit sheds an
    item or two on every call)."""
    text = existing if existing.strip() else "# Todo archive\n"
    if not text.endswith("\n"):
        text += "\n"
    heading = f"## archived {day}"
    last = next((l for l in reversed(text.splitlines()) if l.startswith("## ")), None)
    if last != heading:
        text += f"\n{heading}\n\n"
    return text + "".join(f"- [x] {t['text']}\n" for t in items)
