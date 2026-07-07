"""Beacon-catcher: render an agent-built HTML artifact inside the sandbox VM.

The artifact loads in jsdom (Node) with every network API instrumented, so a
dashboard that tries to phone home on render fires those requests against the
deny-by-default tap — caught by the gate pipeline exactly like executed code.
The harness also logs each attempt's URL and payload before the tap drops it.

This is what lets the operator safely *preview* something the agent built
before opening it in a real browser.
"""
import shlex
from pathlib import Path

from . import gate
from .config import settings
from .staging import effective_read

_HARNESS = Path(__file__).resolve().parent.parent / "vm" / "render" / "render.js"
NODE_PATH = "/opt/jarvis/node_modules"


async def render_gated(slug: str, rel: str, timeout: float | None = 60,
                       fresh: bool = True) -> dict:
    """Render projects/<slug>/<rel> (an .html file) under monitoring."""
    if not rel.endswith((".html", ".htm")):
        raise ValueError("render target must be an .html file")
    src = effective_read(slug, rel)       # canonical or staged view
    if src is None:
        raise LookupError(f"no such file: {rel}")
    remote = f"{settings.vm_workspace}/{slug}/{rel}"
    harness = _HARNESS.read_text()
    command = (f"NODE_PATH={NODE_PATH} node - {shlex.quote(remote)} "
               f"2>/dev/null")
    return await gate.run_gated(slug, command, timeout=timeout, fresh=fresh,
                               input=harness, render_of=rel)
