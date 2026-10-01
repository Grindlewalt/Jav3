"""Tool sections: a small core in every prompt, the rest loaded on demand.

Eighty tool schemas rode every model call (about 20k tokens with everything
connected). Published practice agrees that a model picks tools worse as the
list grows and that the fix is a small always-on core plus on-demand loading
(Anthropic's tool search, OpenAI's tool_search/namespaces, Cursor's dynamic MCP
tools). So each TOOL.md names its `section:`, `core: true` tools (as the model
sees them, merged ones counted once) number at most CORE_MAX - 1, and one
meta-tool, `tools`, loads a section for the rest of the turn.

What this module decides is only what the model is SHOWN. The granted set is
computed on the host exactly as before (autonomy dial, agent exclusions,
requirements, local/ephemeral filters) and arrives here as tool specs; a
section can only ever reveal specs that are already in it. Every call still
dispatches under its real tool name, so the broker's gates, taint, approvals
and the tool_calls ledger see exactly what they saw before.

* `action:` in a TOOL.md folds that tool into one merged tool named after its
  section (desk, browser, git, services, memory, web, media, project, agents,
  plans, projector, system): `browser(action="click", ...)` runs
  browser_click. The old names still work when called directly. A tool that
  has an `action` argument of its own (music_control, plan_fix, ...) keeps it
  as `do`: `media(action="control", do="pause")` runs
  music_control(action="pause"). Folding happens only when a turn is
  sectioned; a granted set of FLAT_MAX tools or fewer is shown as granted (the
  voice tier's eight tools stay eight).
* A call to a granted tool that is not loaded yet loads its section and runs
  (argcheck still checks the arguments); a name that is not granted gets a
  did-you-mean instead of a dispatch.
* A section also loads at the start of a turn when the latest user message
  plainly needs it (SECTIONS triggers), when it is `autoload` (plan tools in a
  plan item, the /local tools in a local chat), or when the host marked its
  specs `load` (used earlier in this conversation; a Gitea-backed project).
  A core tool used earlier never loads its section: it was shown anyway.

Pure: copied verbatim into the guest package (backend/vm/guest_pkg.py), so a
host turn and a guest turn expose tools the same way.
"""
import difflib
import json
import re

CORE_MAX = 11            # tools in the prompt before any section loads, META included
FLAT_MAX = 14            # a granted set this small is shown as granted: no sections, no folding
META = "tools"           # the meta-tool's name
SUB = "do"               # a merged tool's name for a member's own `action` argument
MERGED_NOTES_MAX = 300   # per-action Notes kept in a merged tool's description

# The order here is the order sections are listed in. `about` is the one-line
# listing; `merged` describes the merged tool when the section has one;
# `triggers` load the section at turn start when the latest user message
# matches; `autoload` loads it whenever any of its tools is granted (they are
# only granted to the kind of turn that needs them); `with` names sections
# that load along with an autoloaded one; `guide` names the navigation
# playbook block that rides the section when it loads; `lead` puts a merged
# tool's first-step actions first; `notes_max` overrides MERGED_NOTES_MAX.
SECTIONS: dict[str, dict] = {
    "files": {"about": "project files and the sandbox: read, write, edit, search, run code"},
    "project": {
        "about": "switch project, index the code, dashboards, workspace panels, sandbox "
                 "packages and screenshots, the journal",
        "merged": "The active project: switch to another, index its code, build a dashboard, "
                  "arrange its workspace panels, request a sandbox package, screenshot the "
                  "sandbox, add a journal entry.",
        "triggers": r"\b(load|switch|open) (the |a |another )?project\b|\b(workspace|panels?|"
                    r"dashboards?|crawl|index the (code|codebase)|screenshot|journal)\b|"
                    r"\b(install|add) (a |an |the )?(apt |pip |npm )?packages?\b",
        "lead": ["load", "journal"]},
    "web": {
        "about": "search the web, read pages",
        "merged": "Search the web and read pages. Pages come back as inert text; the host "
                  "fetches and sanitizes them for you.",
        "lead": ["search", "summarize", "read", "research"], "notes_max": 450},
    "browser": {
        "about": "operate web pages in Jav3's own tabs in the operator's browser",
        "merged": "Operate web pages in Jav3's own tabs in the operator's browser: read a "
                  "page's text and element ids, then act on those ids; open, list, "
                  "navigate and close tabs.",
        "triggers": r"\b(browser|tabs?|web ?page|website|site|log ?in|sign ?in|click|"
                    r"fill (in|out)|form|navigate)\b|https?://",
        "guide": "browser", "lead": ["read", "open_tab", "list_tabs", "navigate"]},
    "desk": {
        "about": "see and operate the operator's connected computer",
        "merged": "See and operate the operator's connected computer: screenshot with an "
                  "element list, then click, type, press keys, scroll, drag, open apps "
                  "or URLs, wait for the screen, run a shell command.",
        "triggers": r"\b(click|double[- ]click|screen|screenshot|desktop|my (computer|mac|laptop|pc)|"
                    r"mouse|cursor|windows?|launch|apps?)\b",
        "guide": "desk", "lead": ["screenshot"]},
    "git": {
        "about": "the project's git: status, diff, commit, pull request, GitHub remote",
        "merged": "The project's git: status and diff, and requests to commit, to open a "
                  "pull request on the host's Gitea, or to connect a GitHub remote. "
                  "Requests wait for the operator's approval.",
        "triggers": r"\b(git|commits?|push|pull request|PR|diff|branch|gitea|github|remote|merge)\b",
        "lead": ["status", "diff"]},
    "services": {
        "about": "the project's long-running services: status, logs, request one",
        "merged": "The project's long-running services (servers, workers, bots): status, "
                  "recent logs, or a request for a new one that the operator approves.",
        "triggers": r"\b(services?|daemons?|systemd|logs?|keep (it )?running|long[- ]running|"
                    r"server|bot|worker)\b"},
    "agents": {
        "about": "run or define agents, agent teams, plan runs, message a running agent",
        "merged": "Other agents: run a saved one, spawn a disposable one, deploy a team, "
                  "define a new agent, turn a big ask into a plan, or message a running agent.",
        "triggers": r"\b(agents?|subagents?|team|delegate|spawn|orchestrate|in parallel|"
                    r"checklist)\b",
        "lead": ["spawn", "spawn_temp", "send"]},
    "plans": {"about": "the running plan: status, repairs, an item's report",
              "merged": "The running plan, the team's checklist: follow it, repair a failed "
                        "or blocked item, report your own item's outcome.",
              "autoload": True, "with": ["agents"], "lead": ["status"]},
    "schedules": {
        "about": "propose recurring headless runs",
        "triggers": r"\b(schedules?|scheduled|every (day|morning|evening|night|hour|week|"
                    r"weekday|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
                    r"\d+ ?(m|min|mins|minutes|h|hours))|daily|weekly|hourly|cron|remind)\b"},
    "memory": {
        "about": "your durable memory notes",
        "merged": "Your durable memory notes (they survive every restart and VM wipe): "
                  "read lists them or reads one; write saves, updates or deletes one.",
        "triggers": r"\b(remember|memory|memories|forget)\b"},
    "media": {
        "about": "the operator's music (play, pause, volume, download), the clap songs, "
                 "videos and web pages on their screen",
        "merged": "The operator's music and screen: search, play, pause and download songs "
                  "from their library, edit the double-clap list, put a video or web page on "
                  "their screen.",
        "triggers": r"\b(music|songs?|play|playing|playlist|album|artist|tracks?|volume|"
                    r"pause|skip|movie|video|clap|youtube)\b",
        "lead": ["play", "search", "control", "status"]},
    "projector": {
        "about": "the projection mapper: surfaces, scenes, output window, universe",
        "merged": "The projection mapper: put something on a surface, see what is showing, "
                  "run the output window, drive the universe simulation.",
        "triggers": r"\b(projector|projection|universe|surfaces?)\b",
        "lead": ["show", "status"]},
    "local": {"about": "files and shell on the operator's own computer (a /local chat)",
              "autoload": True},
    "system": {
        "about": "your own manual, reporting a harness fault",
        "merged": "Your own technical manual, and a report that the harness itself misbehaved.",
        "triggers": r"\b(jav3|jarvis|harness|your (own )?(docs|manual|architecture|tools)|"
                    r"how do you work)\b"},
    "skills": {"about": "installed skills (calling one loads its instructions)"},
    "other": {"about": "tools with no section"},
}

_WIRE_KEYS = ("type", "function")


def _fn(spec: dict) -> dict:
    return spec.get("function") or {}


def spec_name(spec: dict) -> str:
    return _fn(spec).get("name", "")


def wire(spec: dict) -> dict:
    """A spec as the provider gets it: the section annotations stripped."""
    return {k: spec[k] for k in _WIRE_KEYS if k in spec}


def wire_specs(specs) -> list[dict]:
    return [wire(s) for s in specs or ()]


def _split_desc(desc: str) -> tuple[str, str]:
    """(description incl. "Use when", Notes body) of a registry-built spec."""
    head, _, notes = (desc or "").partition("\nNotes: ")
    return head.strip(), notes.strip()


def _clip(text: str, n: int) -> str:
    """`text` on one line, cut at the last sentence end within n chars."""
    text = " ".join((text or "").split())
    if len(text) <= n:
        return text
    cut = text[:n + 1]
    end = cut.rfind(". ")
    return cut[:end + 1] if end > n // 3 else cut[:n].rsplit(" ", 1)[0] + " ..."


def _takes(params: dict) -> str:
    """The argument list of one merged action. A member's own `action` shows
    as `do` (with its values), the name it has in the merged tool."""
    props = (params or {}).get("properties") or {}
    req = set((params or {}).get("required") or [])
    out = []
    for k, v in props.items():
        opt = "" if k in req else "?"
        if k == "action":
            enum = (v or {}).get("enum")
            out.append(SUB + opt + ("=" + "|".join(map(str, enum)) if enum else ""))
        else:
            out.append(k + opt)
    return ", ".join(out) or "nothing"


def _props(spec: dict) -> dict:
    return (_fn(spec).get("parameters") or {}).get("properties") or {}


def _own_action(spec: dict) -> dict | None:
    """The `action` argument a tool takes for itself, if it has one."""
    return _props(spec).get("action")


def _latest_user_text(history) -> str:
    for m in reversed(history or []):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
        return c if isinstance(c, str) else ""
    return ""


class View:
    """What the model is shown this turn, and how a call maps back to a real
    tool. Built once per turn from the granted specs (host-computed)."""

    def __init__(self, specs, history=None):
        self.specs = [s for s in (specs or []) if spec_name(s)]
        self.by_name = {spec_name(s): s for s in self.specs}
        # a set this small is shown as granted: nothing to fold or defer
        self.flat = len(self.by_name) <= FLAT_MAX
        # merged tools: section -> {action: real name}, in spec order. A group
        # is folded only when it has two members or more, its name is not a
        # real tool's, and no member already takes a `do` argument (a member's
        # own `action` argument becomes `do`)
        groups: dict[str, dict[str, str]] = {}
        for s in () if self.flat else self.specs:
            if s.get("action") and s.get("section"):
                groups.setdefault(s["section"], {})[str(s["action"])] = spec_name(s)
        for g, acts in groups.items():      # the actions to start with lead
            lead = (SECTIONS.get(g) or {}).get("lead") or []
            groups[g] = dict(sorted(acts.items(), key=lambda kv: (
                lead.index(kv[0]) if kv[0] in lead else len(lead))))
        self.groups = {g: acts for g, acts in groups.items()
                       if len(acts) > 1 and g not in self.by_name and g != META
                       and not any(SUB in (_props(self.by_name[r])) for r in acts.values())}
        self.member_of = {real: (g, act) for g, acts in self.groups.items()
                          for act, real in acts.items()}
        # members whose own `action` argument is `do` in the merged tool
        self.sub_action = {real for real in self.member_of
                           if _own_action(self.by_name[real]) is not None}
        # units: what the model sees as one tool (a merged group, or a spec)
        self.units: list[str] = []
        for s in self.specs:
            n = spec_name(s)
            u = self.member_of.get(n, (n,))[0]
            if u not in self.units:
                self.units.append(u)
        deferred = [u for u in self.units
                    if self.section_of(u) and not self.is_core(u)]
        # sectioning is on only when there is something to defer and the whole
        # set would not fit in the core budget anyway (a voice or subagent
        # toolset of a dozen stays exactly as it was)
        self.active = (not self.flat and bool(deferred) and len(self.units) > CORE_MAX
                       and META not in self.by_name)
        # a registry-built list (annotated) is the whole grant: a name outside
        # it is refused, not dispatched. A hand-built list (tests, a caller
        # that stubs the specs) keeps the old contract: whatever the model
        # calls goes to the registry, whose handlers and the broker decide
        self.strict = any("section" in s for s in self.specs)
        self.loaded: set[str] = set()
        self.guided: set[str] = set()
        if not self.active:
            return
        text = _latest_user_text(history)
        for u in deferred:
            sec = self.section_of(u)
            meta = SECTIONS.get(sec) or {}
            if (meta.get("autoload") or self._marked(u)
                    or (meta.get("triggers") and text
                        and re.search(meta["triggers"], text, re.I))
                    or self._named_in(u, text)):
                self.loaded.add(sec)
        # sections that ride along with a loaded one (the plan's team messages)
        for sec in list(self.loaded):
            for w in (SECTIONS.get(sec) or {}).get("with") or ():
                if any(self.section_of(u) == w for u in deferred):
                    self.loaded.add(w)

    # --- units ----------------------------------------------------------------

    def _members(self, unit: str) -> list[str]:
        return list(self.groups[unit].values()) if unit in self.groups else [unit]

    def section_of(self, unit: str) -> str | None:
        spec = self.by_name.get(self._members(unit)[0])
        return spec.get("section") if spec else None

    def is_core(self, unit: str) -> bool:
        return any(self.by_name[m].get("core") is True for m in self._members(unit))

    def _marked(self, unit: str) -> bool:
        return any(self.by_name[m].get("load") is True for m in self._members(unit))

    def _named_in(self, unit: str, text: str) -> bool:
        if not text:
            return False
        names = self._members(unit) + ([unit] if unit in self.groups else [])
        return any(re.search(rf"\b{re.escape(n)}\b", text) for n in names if "_" in n)

    def visible(self, unit: str) -> bool:
        if not self.active:
            return True
        sec = self.section_of(unit)
        return sec is None or self.is_core(unit) or sec in self.loaded

    def granted(self) -> set[str]:
        return set(self.by_name)

    def is_meta(self, name: str) -> bool:
        """A call to the meta-tool (never a granted tool that shares its name)."""
        return name == META and name not in self.by_name

    def core_units(self) -> list[str]:
        return [u for u in self.units if not self.active or self.is_core(u)
                or not self.section_of(u)]

    def sections(self) -> dict[str, list[str]]:
        """section -> its deferred units, in SECTIONS order."""
        out: dict[str, list[str]] = {}
        for u in self.units:
            sec = self.section_of(u)
            if sec and not self.is_core(u):
                out.setdefault(sec, []).append(u)
        order = list(SECTIONS)
        return dict(sorted(out.items(),
                           key=lambda kv: order.index(kv[0]) if kv[0] in order else len(order)))

    # --- what the model gets ---------------------------------------------------

    def wire(self) -> list[dict]:
        """The specs to send this round, in a stable order (registry order,
        so a section loaded now and preloaded next turn give the same prefix)."""
        out = []
        for u in self.units:
            if self.visible(u):
                out.append(self._merged(u) if u in self.groups else wire(self.by_name[u]))
        if self.active:
            out.append(self._meta_spec())
        return out

    def _merged(self, group: str) -> dict:
        meta = SECTIONS.get(group) or {}
        about = meta.get("merged") or f"{group} tools."
        notes_max = meta.get("notes_max") or MERGED_NOTES_MAX
        props: dict = {"action": {"type": "string", "enum": list(self.groups[group]),
                                  "description": "What to do. Each action's parameters "
                                                 "are listed in the description."}}
        lines = []
        for act, real in self.groups[group].items():
            fn = _fn(self.by_name[real])
            params = fn.get("parameters") or {}
            own = None
            for k, v in (params.get("properties") or {}).items():
                v = dict(v or {})
                if k == "action":               # the member's own action: `do`
                    k, own = SUB, v
                if k not in props:
                    props[k] = v
                elif k == SUB:                  # values of every member, or open
                    a, b = props[k].get("enum"), v.get("enum")
                    if a and b:
                        props[k]["enum"] = a + [x for x in b if x not in a]
                    else:
                        props[k].pop("enum", None)
                elif props[k].get("type") != v.get("type") and "type" in props[k]:
                    props[k].pop("type")        # same name, different types: leave it open
            head, notes = _split_desc(fn.get("description", ""))
            line = f"- {act}({_takes(params)}): {head}"
            if notes:
                line += f" {_clip(notes, notes_max)}"
            if own and len(own.get("description") or "") > 70:   # more than a restatement
                line += f" do: {_clip(own['description'], MERGED_NOTES_MAX)}"
            lines.append(line)
        if SUB in props:
            props[SUB] = {"type": "string", **{k: v for k, v in props[SUB].items()
                                                if k == "enum"},
                          "description": "For the actions that list do=...: what to do."}
        desc = (f"{about} Pass `action` plus that action's parameters "
                f"(? = optional):\n" + "\n".join(lines))
        return {"type": "function", "function": {
            "name": group, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": ["action"]}}}

    def _listing(self) -> str:
        lines = []
        for sec, units in self.sections().items():
            about = (SECTIONS.get(sec) or {}).get("about", "")
            lines.append(f"- {sec}: {about} [{', '.join(units)}]")
        return "\n".join(lines)

    def _meta_spec(self) -> dict:
        secs = list(self.sections())
        return {"type": "function", "function": {
            "name": META,
            "description": (
                "Load more tools. Your core tools are always here; the rest wait in "
                "sections. Any tool already in your tool list is loaded: call it "
                "directly, don't load its section. tools(section=\"...\") makes a "
                "section's tools callable for the rest of this turn; calling one of "
                "them by name also loads its section. Sections [their tools]:\n"
                + self._listing()),
            "parameters": {"type": "object", "properties": {"section": {
                "type": "string", "enum": secs,
                "description": "The section to load. Omit to list the sections."}}}}}

    # --- calls --------------------------------------------------------------------

    def _load(self, sec: str) -> str:
        """Load `sec`; returns the playbook to show with it, once per turn."""
        self.loaded.add(sec)
        return self._guide(sec)

    def _guide(self, sec: str) -> str:
        key = (SECTIONS.get(sec) or {}).get("guide")
        if not key or sec in self.guided:
            return ""
        self.guided.add(sec)
        try:
            from ... import navplaybook
        except ImportError:      # a package without the playbook: tools still work
            return ""
        return navplaybook.block(key)

    def start_guides(self) -> str:
        """Playbooks for the sections visible from the first round."""
        secs = []
        for u in self.units:
            sec = self.section_of(u)
            if sec and self.visible(u) and sec not in secs:
                secs.append(sec)
        return "\n\n".join(g for g in (self._guide(s) for s in secs) if g)

    def _unit_line(self, unit: str) -> str:
        if unit in self.groups:
            return f"{unit} (action: {', '.join(self.groups[unit])})"
        return unit

    def meta_call(self, args: dict) -> str:
        """tools(section=...): load one or more sections, or list them."""
        want = (args or {}).get("section")
        if isinstance(want, list):
            names = [str(w).strip() for w in want]
        else:
            names = [w.strip() for w in str(want or "").split(",")]
        names = [n for n in names if n]
        secs = self.sections()
        if not names:
            return "Sections [their tools]:\n" + self._listing()
        out, guides = [], []
        core_secs = {self.section_of(u) for u in self.core_units()}
        for n in names:
            if n not in secs and n in core_secs:
                out.append(f"Section '{n}' is part of your core tools: call them directly.")
                continue
            if n not in secs:
                close = difflib.get_close_matches(n, list(secs), n=1, cutoff=0.5)
                hint = f" (did you mean '{close[0]}'?)" if close else ""
                return (f"error: no section '{n}' in this turn{hint}. Sections: "
                        f"{', '.join(secs)}. The desk (a connected computer), browser "
                        "and local sections are offered only while one is connected: "
                        "if the one you need is not listed, none is connected, so say "
                        "that plainly.")
            was = n in self.loaded
            g = self._load(n)
            if g:
                guides.append(g)
            tools = ", ".join(self._unit_line(u) for u in secs[n])
            out.append(f"{'Already loaded' if was else 'Loaded'} section '{n}': {tools}. "
                       "Call them directly now.")
        return "\n".join(out + guides)

    def resolve(self, name: str, args: dict) -> tuple[str, dict, str, str | None]:
        """A model call -> (real tool name, its args, note for the result,
        error). The error, when set, is the whole result: nothing dispatches."""
        args = args if isinstance(args, dict) else {}
        if name in self.groups:
            acts = self.groups[name]
            act = args.get("action")
            if act not in acts:
                close = difflib.get_close_matches(str(act or ""), list(acts), n=1, cutoff=0.5)
                hint = f" (did you mean '{close[0]}'?)" if close and act else ""
                return name, args, "", (
                    f"error: {name} needs action, one of: {', '.join(acts)}{hint}. "
                    "Nothing ran.")
            real = acts[act]
            rest = {k: v for k, v in args.items() if k != "action"}
            if real in self.sub_action:         # `do` is the real tool's own `action`
                err = self._sub_error(name, act, real, rest)
                if err:
                    return name, args, "", err
                if SUB in rest:
                    rest["action"] = rest.pop(SUB)
            return real, rest, self._autoload_note(name), None
        if name in self.by_name:
            unit = self.member_of.get(name, (name,))[0]
            return name, args, self._autoload_note(unit), None
        if not self.strict:
            return name, args, "", None
        return name, args, "", self._unknown(name)

    def _sub_error(self, name: str, act: str, real: str, rest: dict) -> str | None:
        """A merged action whose real tool takes its own `action` (`do` here):
        say what to pass instead of letting the real tool's message name an
        argument the model never saw."""
        own = _own_action(self.by_name[real]) or {}
        enum = own.get("enum") or []
        req = "action" in ((_fn(self.by_name[real]).get("parameters") or {}).get("required") or [])
        do = rest.get(SUB)
        if do is None:
            if not req:
                return None
            bad = None
        elif not enum or do in enum:
            return None
        else:
            bad = do
        close = difflib.get_close_matches(str(bad or ""), enum, n=1, cutoff=0.5)
        hint = f" (did you mean '{close[0]}'?)" if close and bad else ""
        return (f"error: {name}(action=\"{act}\") needs do, one of: "
                f"{', '.join(map(str, enum))}{hint}. Nothing ran.")

    def _autoload_note(self, unit: str) -> str:
        sec = self.section_of(unit)
        if self.visible(unit) or not sec:
            return ""
        guide = self._load(sec)
        tools = ", ".join(self._unit_line(u) for u in self.sections().get(sec, []))
        note = (f"\n\n[section '{sec}' is now loaded, so its tools are callable "
                f"directly: {tools}.]")
        return note + (f"\n\n{guide}" if guide else "")

    def _unknown(self, name: str) -> str:
        if name in self.sections():
            return (f"error: '{name}' is a section, not a tool. Call "
                    f"tools(section=\"{name}\") to load it.")
        pool = list(self.by_name) + list(self.groups)
        close = difflib.get_close_matches(name, pool, n=1, cutoff=0.6)
        hint = ""
        if close:
            c = close[0]
            sec = self.section_of(self.member_of.get(c, (c,))[0])
            where = f", in section '{sec}'" if self.active and sec and not self.visible(c) else ""
            hint = f" Did you mean '{c}'{where}?"
        return (f"error: there is no tool named '{name}' in this turn.{hint} "
                "Nothing ran. Tools that need a connected computer, browser or "
                "project are offered only while one is there.")


def sections_for(names, specs) -> set[str]:
    """The sections that tool names (real, merged or 'tools' rows' sections)
    belong to, among `specs`. A core tool says nothing about its section: it
    was shown either way, and the rest of the section is not wanted for it."""
    by = {spec_name(s): s.get("section") for s in specs or ()
          if s.get("core") is not True}
    core = {spec_name(s) for s in specs or () if s.get("core") is True}
    groups = {s.get("section") for s in specs or () if s.get("action")}
    out = set()
    for n in names or ():
        if n in core:
            continue
        if n in by and by[n]:
            out.add(by[n])
        elif n in groups or n in SECTIONS:
            out.add(n)
    return out


def mark_load(specs, sections) -> list[dict]:
    """`specs` with every spec in `sections` marked to load at turn start
    (the host's hint: used earlier in this conversation, a Gitea project)."""
    sections = set(sections or ())
    return [{**s, "load": True} if s.get("section") in sections else s
            for s in specs or ()]


def expand_names(names, entries) -> set[str]:
    """Tool names as an exclusion list means them: a merged tool's or a
    section's name stands for every registry entry in it."""
    names = set(names or ())
    out = set(names)
    for e in entries or ():
        sec = e.get("section") or ("skills" if e.get("kind") == "skill" else "other")
        if sec in names:
            out.add(e["name"])
    return out


def section_sizes(specs) -> dict:
    """Diagnostics: {"core": [...], "sections": {...}, "wire_chars": n} for a
    granted spec list as a turn with no preload would see it."""
    v = View([{k: s[k] for k in s if k != "load"} for s in specs or ()])
    return {"active": v.active, "core": v.core_units(), "sections": v.sections(),
            "wire_chars": len(json.dumps(v.wire())),
            "all_chars": len(json.dumps(wire_specs(specs)))}
