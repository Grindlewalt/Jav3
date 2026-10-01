"""The built-in tools as the model sees them, for the Tools page and the docs.

tools/<name>/TOOL.md folders are how tools are authored and dispatched (and what
a transcript's tool_calls ledger names). The model is shown fewer: a folder
with `action:` frontmatter folds into one merged tool named after its section
(`browser(action="click")` runs browser_click), and a turn shows only the core
tools until a section is loaded. backend/agent/tools/toolsections.py decides
all of that; this module only asks it, over every built-in folder as if all of
them were granted, and describes the answer: for each folder its section, the
merged tool it folds into and its action name there, whether it is core, the
frontmatter flag `internal` (harness-only tools: plan_status, plan_fix,
plan_report, inbox_fetch), and what gates it. GET /api/tools serves the rows;
`python -m backend.toolcatalog` prints docs/tool-map.md, and a test fails when
that file is stale, so the old-name map cannot drift from the folders.
"""
from .agent.tools import inguest, toolsections
from .agent.tools.registry import _parse_md, _sectioned
from .config import settings

# docs/tool-map.md says where it comes from; a test compares the whole file
DOC_NAME = "tool-map.md"


def builtin_entries(entries: list[dict] | None = None) -> list[dict]:
    """The tool-folder entries (no skills) that can take part in the model's
    view: from `entries` (a registry load), or read straight off tools/."""
    if entries is None:
        entries = []
        for p in sorted(settings.tools_dir.glob("*/TOOL.md")):
            e = _parse_md(p)
            if e:
                entries.append(e)
    return [e for e in entries if e.get("kind") == "tool" and not e.get("clash")]


def gating(e: dict) -> list[str]:
    """Why a tool is not offered on every turn, as short labels (empty = it is,
    wherever it has a project). Read off the same frontmatter the registry's
    _requirements_met reads, plus where it runs."""
    out = []
    if e["name"] in inguest.IN_GUEST_TOOLS:
        out.append("in-guest")
    if e.get("requires_local") is True:
        out.append("local chats only")
    if e.get("requires_desk"):
        out.append("needs desk")
        if e["requires_desk"] == "shell":
            out.append("needs shell")      # granted in Settings, allowed at that computer
    if e.get("requires_browser") is True:
        out.append("needs extension")
    if e.get("requires_project"):
        out.append("needs project")
    if e.get("requires_settings"):
        out.append("needs setup")
    if e.get("enabled") is False:
        # switched off in its folder. A section that loads itself for one kind
        # of turn (plans) has its tools granted by whoever runs that turn; the
        # loop's own tools are never offered; anything else is just off
        if (toolsections.SECTIONS.get(e.get("section") or "other") or {}).get("autoload"):
            out.append("plan items only")
        elif e.get("internal") is True:
            out.append("harness only")
        else:
            out.append("disabled")
    return out


def _spec(e: dict) -> dict:
    return _sectioned({"type": "function", "function": {
        "name": e["name"], "description": e.get("description", ""),
        "parameters": e.get("parameters") or {"type": "object", "properties": {}}}}, e)


def catalogue(entries: list[dict] | None = None) -> dict:
    """{"rows": {name: fields}, "order": [name, ...], "sections": [...]}.

    `rows[name]` is the per-tool part of GET /api/tools: section, action
    (the name inside the merged tool, None for a tool shown by its own name),
    core (the model always has it: any member of a merged tool is core),
    merged_into (the merged tool's name, or None), internal, gating.
    `order` lists the folders as the model's tool list would: by section, then
    by tool, a merged tool's actions in the order its description gives them.
    `sections` is [{name, about, merged}] for the sections in use."""
    entries = builtin_entries(entries)
    by = {e["name"]: e for e in entries}
    view = toolsections.View([_spec(e) for e in entries])
    rows: dict[str, dict] = {}
    for u in view.units:
        for real in (view.groups[u].values() if u in view.groups else [u]):
            e = by[real]
            merged = u if u in view.groups else None
            rows[real] = {
                "section": e.get("section") or "other",
                "action": view.member_of[real][1] if merged else None,
                "core": view.is_core(u),
                "merged_into": merged,
                "internal": e.get("internal") is True,
                "gating": gating(e),
            }
    order_of = list(toolsections.SECTIONS)
    pos = {n: i for i, n in enumerate(rows)}      # view order within a section
    names = sorted(rows, key=lambda n: (
        order_of.index(rows[n]["section"]) if rows[n]["section"] in order_of else len(order_of),
        pos[n]))
    seen = []
    for n in names:
        if rows[n]["section"] not in seen:
            seen.append(rows[n]["section"])
    sections = [{"name": s, "about": (toolsections.SECTIONS.get(s) or {}).get("about", ""),
                 "merged": (toolsections.SECTIONS.get(s) or {}).get("merged", "")}
                for s in seen]
    return {"rows": rows, "order": names, "sections": sections}


def counts(cat: dict, internal: bool = False) -> tuple[int, int]:
    """(tools, actions) the model could see: a merged tool counts once, each
    folder is one action. Internal ones only when asked for."""
    rows = [r for r in cat["rows"].values() if internal or not r["internal"]]
    units = {r["merged_into"] or id(r) for r in rows}
    return len(units), len(rows)


def markdown(cat: dict | None = None) -> str:
    """docs/tool-map.md: every folder name, and the tool and action the model
    sees for it."""
    cat = cat or catalogue()
    tools, actions = counts(cat)
    itools, iactions = counts(cat, internal=True)
    off = sum(1 for r in cat["rows"].values() if "disabled" in r["gating"] and not r["internal"])
    out = [
        "# Tools as the model sees them",
        "",
        "Generated by `python -m backend.toolcatalog --write` from the `tools/*/TOOL.md`",
        "frontmatter and `backend/agent/tools/toolsections.py`; do not edit by hand",
        "(tests/test_toolcatalog.py fails when it is stale). The Tools page shows the same",
        "list, and searching it by an old name finds the action.",
        "",
        f"The model can be offered **{tools} tools with {actions} actions**"
        + (f" ({off} of the actions is switched off in its folder)." if off == 1 else
           f" ({off} of the actions are switched off in their folders)." if off else "."),
        f"{itools - tools} more tools with {iactions - actions} more actions are internal "
        f"({', '.join(f'`{n}`' for n in cat['order'] if cat['rows'][n]['internal'])}): the "
        "harness grants or calls them, and the model is not offered them on a normal turn. "
        "A TOOL.md marks one with `internal: true`; nothing else reads the key, so it changes "
        "what the Tools page lists and nothing about what is granted.",
        "",
        f"A turn lists only the core tools (at most {toolsections.CORE_MAX} names, with `tools`) "
        "until the model loads a section with `tools(section=...)`. Every old name still "
        "works when called directly, and a transcript's tool call keeps its old name.",
        "",
        "A merged tool is called `tool(action=\"...\", ...)`; a tool with no action is called by "
        "its own name. Gating says what must be true before the model is offered it.",
    ]
    for sec in cat["sections"]:
        out += ["", f"## {sec['name']}", ""]
        if sec["about"]:
            out += [f"{sec['about'][0].upper()}{sec['about'][1:]}.", ""]
        out += ["| Old name | The model calls | Core | Gating |", "| --- | --- | --- | --- |"]
        for n in cat["order"]:
            r = cat["rows"][n]
            if r["section"] != sec["name"]:
                continue
            call = (f"`{r['merged_into']}(action=\"{r['action']}\")`" if r["merged_into"]
                    else f"`{n}`")
            gate = ", ".join(r["gating"] + (["internal"] if r["internal"] else []))
            out.append(f"| `{n}` | {call} | {'core' if r['core'] else ''} | {gate} |")
    return "\n".join(out) + "\n"


if __name__ == "__main__":      # pragma: no cover
    import sys
    from pathlib import Path
    text = markdown()
    if "--write" in sys.argv:
        (Path(__file__).resolve().parent.parent / "docs" / DOC_NAME).write_text(text)
    else:
        sys.stdout.write(text)
