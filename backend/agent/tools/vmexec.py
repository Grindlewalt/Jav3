"""Run project code in the sandbox VM with staged-write semantics.

Flow per run: build the project as Jarvis sees it (canonical files overlaid
with its own staged edits) -> push to /workspace/<slug> -> run -> pull back
-> anything the run changed or created lands in staging, instantly durable
on the host but untouchable until the operator approves.

The workspace is replaced on every run; anything the VM wants to keep
between runs (venvs, caches) belongs outside /workspace.
"""
import shutil
import tempfile
from pathlib import Path

from ... import secrets as secrets_mod
from ...config import settings
from ...staging import STAGING, list_staged, stage_write
from . import vm

# junk we neither push nor pull back
SKIP = vm.PUSH_EXCLUDE | {STAGING}


def _build_merged(slug: str) -> Path:
    """Canonical project files overlaid with staged edits, in a temp dir."""
    project = settings.projects_dir / slug
    merged = Path(tempfile.mkdtemp(prefix=f"jarvis-{slug}-"))
    for src in sorted(project.rglob("*")):
        rel = src.relative_to(project)
        if any(part in SKIP for part in rel.parts):
            continue
        if src.is_file():
            dest = merged / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
    staging = project / STAGING
    for entry in list_staged(slug):
        dest = merged / entry["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(staging / entry["path"], dest)
    return merged


def _stage_changes(slug: str, merged: Path, pulled: Path) -> list[str]:
    """Everything the run changed or created goes to staging."""
    staged = []
    for src in sorted(pulled.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(pulled)
        if any(part in SKIP for part in rel.parts):
            continue
        before = merged / rel
        data = src.read_bytes()
        if before.is_file() and before.read_bytes() == data:
            continue
        stage_write(slug, str(rel), data)
        staged.append(str(rel))
    return staged


async def run_in_project(slug: str, command: str, timeout: float | None = None,
                         input: str | None = None) -> dict:
    # secrets: the placeholder text is what the model wrote (and what the DB
    # logged); the real value exists only between here and the VM. Outputs are
    # scrubbed on the way back so an echoed key never re-enters context.
    try:
        command = secrets_mod.substitute(command)
        input = secrets_mod.substitute(input)
    except KeyError as e:
        raise vm.VMError(str(e.args[0])) from e
    merged = _build_merged(slug)
    pulled_dir = Path(tempfile.mkdtemp(prefix=f"jarvis-pull-{slug}-"))
    try:
        await vm.push(merged, slug)
        result = await vm.run(command, timeout=timeout,
                              cwd=f"{settings.vm_workspace}/{slug}", input=input)
        pulled = await vm.pull(slug, pulled_dir)
        result["stdout"] = secrets_mod.scrub(result.get("stdout"))
        result["stderr"] = secrets_mod.scrub(result.get("stderr"))
        result["staged"] = _stage_changes(slug, merged, pulled_dir / slug)
        result["pulled_bytes"] = pulled["bytes"]
        leaked = [f for f in result["staged"] if secrets_mod.find_in_bytes(
            (settings.projects_dir / slug / STAGING / f).read_bytes())]
        if leaked:
            result["secret_files"] = leaked
        return result
    finally:
        shutil.rmtree(merged, ignore_errors=True)
        shutil.rmtree(pulled_dir, ignore_errors=True)
