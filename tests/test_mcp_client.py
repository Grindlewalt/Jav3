"""The MCP client's security properties.

Jarvis reaching OUT to a server is a different threat model from everything
else in this system, and the defence is structural rather than a check: our
`tools/projector_*/TOOL.md` manifest is the authority for what exists, and the
server's own `tools/list` is used only to confirm the things we intend to call
are there. These tests pin that down, plus the taint and the bounding of
anything the server sends back.

No network: a fake ASGI transport stands in for the projection mapper, so the
whole file runs anywhere.
"""
import json

import httpx
import pytest

from backend import mcp
from backend.db import get_db, init_db


class FakeProjector:
    """Answers MCP JSON-RPC the way the real Electron app does, and records
    what it was asked, so a test can assert on the wire and not just the
    return value."""

    def __init__(self, *, tools=None, result=None, status=200):
        self.tools = tools if tools is not None else sorted(mcp.PROJECTOR_MANIFEST)
        self.result = result
        self.status = status
        self.calls = []
        self.lists = 0            # how many times tools/list was requested
        self.auth = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.auth.append(request.headers.get("authorization"))
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "nope"})
        body = json.loads(request.content or b"{}")
        method, params = body.get("method"), body.get("params") or {}
        if method == "tools/list":
            self.lists += 1
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": body.get("id"),
                "result": {"tools": [{"name": n, "description": "x",
                                      "inputSchema": {"type": "object"}}
                                     for n in self.tools]}})
        if method == "tools/call":
            self.calls.append((params.get("name"), params.get("arguments")))
            res = self.result if self.result is not None else {
                "content": [{"type": "text", "text": "ok"}],
                "structuredContent": {"ok": True}, "isError": False}
            return httpx.Response(200, json={"jsonrpc": "2.0",
                                             "id": body.get("id"),
                                             "result": res})
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": body.get("id"),
            "error": {"code": -32601, "message": "method not found"}})


# The pristine class, captured once at import. Every patch below builds from
# THIS rather than from whatever httpx.AsyncClient currently is — nesting one
# patch inside another silently reuses the outer transport, which is a very
# convincing way to test nothing at all.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _route(monkeypatch, handler):
    """Send every httpx request in backend.mcp through `handler`."""
    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)
    monkeypatch.setattr(mcp.httpx, "AsyncClient", patched)


def _configure(monkeypatch, url="http://projector.test/mcp", token="tok"):
    from backend.config import settings
    monkeypatch.setattr(settings, "mcp_projector_url", url)
    monkeypatch.setattr(settings, "mcp_projector_token", token)
    mcp._projector = None            # settings changed; drop the cached client


@pytest.fixture
def projector(monkeypatch):
    """Point the client at a fake server and hand the test the fake."""
    _configure(monkeypatch)
    fake = FakeProjector()
    _route(monkeypatch, fake.handler)
    return fake


# ---- the pin ---------------------------------------------------------------

async def test_unpinned_tools_are_ignored_and_alerted(projector, tmp_env):
    """The rug pull: a server that grows a tool overnight must not have it
    quietly become callable."""
    await init_db()
    projector.tools = sorted(mcp.PROJECTOR_MANIFEST) + [
        "pmu_set_corners", "run_shell", "git_commit_request"]

    usable = await mcp.projector().verify(set(mcp.PROJECTOR_MANIFEST))

    # nothing outside the manifest survives
    assert usable == set(mcp.PROJECTOR_MANIFEST)
    assert "run_shell" not in usable and "pmu_set_corners" not in usable

    # ...and the operator is told, in the Review Center
    db = await get_db()
    try:
        async with db.execute(
            "SELECT kind, severity, summary, detail FROM security_events "
            "WHERE kind = 'mcp_unpinned_tools'") as cur:
            rows = await cur.fetchall()
    finally:
        await db.close()
    assert len(rows) == 1
    detail = json.loads(rows[0]["detail"])
    assert set(detail["ignored"]) == {"pmu_set_corners", "run_shell",
                                      "git_commit_request"}


async def test_a_missing_tool_is_reported_not_invented(projector, tmp_env):
    await init_db()
    projector.tools = ["pmu_status"]          # the app is an older build
    usable = await mcp.projector().verify(set(mcp.PROJECTOR_MANIFEST))
    assert usable == {"pmu_status"}


async def test_calls_outside_the_manifest_are_refused_locally(projector):
    """Even if a handler is edited to name a new verb, the pin catches it
    before anything reaches the wire."""
    with pytest.raises(mcp.McpError, match="pinned projector manifest"):
        await mcp.projector_call("pmu_set_corners", {"surface": 1})
    assert projector.calls == []


def test_the_manifest_has_no_geometry():
    """The thing an agent must never reach is absent from the vocabulary, not
    guarded within it."""
    for verb in mcp.PROJECTOR_MANIFEST:
        for word in ("corner", "canvas", "warp", "resize", "move", "feather",
                     "fit", "save", "load", "delete", "exec", "shell"):
            assert word not in verb, f"{verb} touches {word}"


# ---- what comes back -------------------------------------------------------

async def test_results_are_tainted_while_in_flight(projector, monkeypatch):
    """Anything the projector says is outside-authored text, so it carries the
    same write taint a fetched web page does — that is what keeps it out of
    binding memory notes."""
    from backend import runtime
    seen = {}

    async def spy(self, name, args):
        seen["taint"] = runtime.write_taint.get()
        return "ok"

    monkeypatch.setattr(mcp.McpClient, "call", spy)
    assert runtime.write_taint.get() is None
    await mcp.projector_call("pmu_status")
    assert seen["taint"] == "mcp:projector"
    assert runtime.write_taint.get() is None      # and it is reset after


async def test_oversized_results_are_capped(projector):
    """A server flooding the context window gets truncated, not obeyed."""
    projector.result = {"content": [{"type": "text", "text": "A" * 50_000}],
                        "isError": False}
    out = await mcp.projector_call("pmu_status")
    assert len(out) < mcp.MAX_RESULT_CHARS + 200
    assert "truncated" in out


async def test_tool_errors_come_back_as_text_not_exceptions(projector):
    projector.result = {"content": [{"type": "text",
                                     "text": "no surface with id 99"}],
                        "isError": True}
    out = await mcp.projector_call("pmu_set_opacity", {"surface": 99})
    assert "could not do that" in out and "no surface with id 99" in out


async def test_an_unreachable_projector_is_a_message_not_a_crash(monkeypatch):
    _configure(monkeypatch)

    def boom(request):
        raise httpx.ConnectError("connection refused")

    _route(monkeypatch, boom)
    with pytest.raises(mcp.McpError, match="not responding"):
        await mcp.projector_call("pmu_status")


async def test_a_refused_token_says_so(monkeypatch):
    _configure(monkeypatch)
    _route(monkeypatch, lambda r: httpx.Response(401, json={"error": "unauthorized"}))
    with pytest.raises(mcp.McpError, match="refused our token"):
        await mcp.projector_call("pmu_status")


async def test_unconfigured_says_so(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "mcp_projector_url", "")
    monkeypatch.setattr(settings, "mcp_projector_token", "")
    mcp._projector = None
    with pytest.raises(mcp.McpError, match="not configured"):
        await mcp.projector_call("pmu_status")


async def test_the_token_is_sent_as_a_bearer(projector):
    await mcp.projector_call("pmu_status")
    assert projector.auth[-1] == "Bearer tok"


# ---- surface resolution ----------------------------------------------------

SURFACES = [
    {"id": 1, "name": "Wall", "showing": "scene"},
    {"id": 2, "name": "Ceiling panel", "showing": "sim"},
    {"id": 3, "name": "Ceiling edge", "showing": "grid"},
]


@pytest.mark.parametrize("wanted,expect", [
    (2, 2), ("2", 2),                      # by id, as int or spoken digit
    ("Wall", 1), ("wall", 1),              # exact name, any case
    ("panel", 2),                          # unique substring
])
def test_resolve_surface_finds_what_the_operator_meant(wanted, expect):
    assert mcp.resolve_surface(SURFACES, wanted)["id"] == expect


def test_ambiguous_and_missing_names_explain_themselves():
    """A miss must be self-correcting: the model gets the real list back, so
    it does not guess twice."""
    with pytest.raises(mcp.McpError, match="more than one surface"):
        mcp.resolve_surface(SURFACES, "ceiling")
    with pytest.raises(mcp.McpError) as e:
        mcp.resolve_surface(SURFACES, "floor")
    assert "Wall (id 1)" in str(e.value)


# ---- the voice feed --------------------------------------------------------

async def test_voice_feed_is_not_a_tool():
    """The model must have no way to write to the wall's voice display: it is
    a stream, not a verb. If this ever fails, a prompt injection gained a
    channel it should not have."""
    assert not any("voice" in v and v != "pmu_show_voice"
                   for v in mcp.PROJECTOR_MANIFEST)
    # pmu_show_voice only ROUTES the display; it carries no text
    assert "push_voice" not in mcp.PROJECTOR_MANIFEST


async def test_voice_feed_posts_to_the_voice_door(monkeypatch):
    from backend.config import settings
    _configure(monkeypatch)
    monkeypatch.setattr(settings, "voice_projector_feed", True)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"ok": True})

    _route(monkeypatch, handler)
    await mcp.push_voice("speaking", heard="play something", reply="On it.")

    assert seen["url"] == "http://projector.test/voice"     # not /mcp
    assert seen["body"]["state"] == "speaking"
    assert seen["body"]["heard"] == "play something"
    assert seen["auth"] == "Bearer tok"


async def test_voice_feed_is_off_when_unconfigured(monkeypatch):
    from backend.config import settings
    _configure(monkeypatch, url="", token="")
    monkeypatch.setattr(settings, "voice_projector_feed", True)
    _route(monkeypatch, lambda r: pytest.fail("must not have called out"))
    await mcp.push_voice("listening")


async def test_voice_feed_failure_is_silent(monkeypatch):
    """A projector that is switched off must not add a millisecond to a turn,
    let alone raise into one."""
    from backend.config import settings
    _configure(monkeypatch)
    monkeypatch.setattr(settings, "voice_projector_feed", True)

    def boom(request):
        raise httpx.ConnectTimeout("no route to host")

    _route(monkeypatch, boom)
    await mcp.push_voice("listening")        # must simply return


# ---- the tools, through the real registry ----------------------------------

def _with_surfaces(fake):
    """Make the fake answer pmu_list_surfaces with a real-looking project."""
    base = fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        params = body.get("params") or {}
        if body.get("method") == "tools/call" and \
                params.get("name") == "pmu_list_surfaces":
            fake.calls.append(("pmu_list_surfaces", params.get("arguments")))
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": body.get("id"),
                "result": {"structuredContent": {"surfaces": SURFACES},
                           "isError": False}})
        return base(request)
    return handler


PROJECTOR_TOOLS = ("projector_show", "projector_status", "projector_output",
                   "projector_universe")


async def test_projector_tools_are_discovered_by_the_registry():
    from backend.agent.tools.registry import load_registry
    names = {e["name"] for e in load_registry()}
    for tool in PROJECTOR_TOOLS:
        assert tool in names, f"{tool} did not load"


def test_projector_tools_are_not_offered_without_a_projector(monkeypatch):
    """A tool that can only answer "not configured" costs tokens on every turn
    and invites the model to promise something that cannot happen."""
    from backend.agent.tools.registry import load_registry, openai_tool_specs
    from backend.config import settings
    entries = load_registry()

    monkeypatch.setattr(settings, "mcp_projector_url", "")
    monkeypatch.setattr(settings, "mcp_projector_token", "")
    offered = {s["function"]["name"] for s in openai_tool_specs(entries)}
    assert not (set(PROJECTOR_TOOLS) & offered)
    # ...but they are still catalogued, so the Tools tab can show them
    assert set(PROJECTOR_TOOLS) <= {e["name"] for e in entries}

    # half-configured is still not configured
    monkeypatch.setattr(settings, "mcp_projector_url", "http://projector.test/mcp")
    offered = {s["function"]["name"] for s in openai_tool_specs(entries)}
    assert not (set(PROJECTOR_TOOLS) & offered)

    monkeypatch.setattr(settings, "mcp_projector_token", "tok")
    offered = {s["function"]["name"] for s in openai_tool_specs(entries)}
    assert set(PROJECTOR_TOOLS) <= offered


def test_requires_settings_does_not_disturb_ordinary_tools(monkeypatch):
    from backend.agent.tools.registry import load_registry, openai_tool_specs
    from backend.config import settings
    monkeypatch.setattr(settings, "mcp_projector_url", "")
    offered = {s["function"]["name"] for s in openai_tool_specs(load_registry())}
    for tool in ("read_file", "write_file", "web_search"):
        assert tool in offered


async def test_show_resolves_a_spoken_name_in_one_call(monkeypatch):
    """"put the sim on the ceiling panel" must become exactly two calls: read
    the surfaces, set the source. No model round trip to find an id."""
    from backend.agent.tools.registry import dispatch
    _configure(monkeypatch)
    fake = FakeProjector()
    _route(monkeypatch, _with_surfaces(fake))

    out = await dispatch("projector_show", {"surface": "panel", "show": "sim"})

    assert [c[0] for c in fake.calls] == ["pmu_list_surfaces", "pmu_set_source"]
    assert fake.calls[1][1] == {"surface": 2, "kind": "sim"}
    assert "Ceiling panel" in out


async def test_show_refuses_media_without_a_path(monkeypatch):
    from backend.agent.tools.registry import dispatch
    _configure(monkeypatch)
    fake = FakeProjector()
    _route(monkeypatch, _with_surfaces(fake))
    out = await dispatch("projector_show", {"surface": "Wall", "show": "video"})
    assert "needs `path`" in out
    assert "pmu_set_source" not in [c[0] for c in fake.calls]


async def test_show_voice_routes_the_feed_as_well_as_the_source(monkeypatch):
    """A voice panel that showed the source but never got the feed would be a
    permanently blank rectangle on a wall."""
    from backend.agent.tools.registry import dispatch
    _configure(monkeypatch)
    fake = FakeProjector()
    _route(monkeypatch, _with_surfaces(fake))
    await dispatch("projector_show", {"surface": "Wall", "show": "voice"})
    assert fake.calls[-1] == ("pmu_show_voice", {"surface": 1})


async def test_a_bad_surface_name_comes_back_as_the_real_list(monkeypatch):
    from backend.agent.tools.registry import dispatch
    _configure(monkeypatch)
    fake = FakeProjector()
    _route(monkeypatch, _with_surfaces(fake))
    out = await dispatch("projector_show", {"surface": "floor", "show": "grid"})
    assert "no surface called 'floor'" in out and "Wall (id 1)" in out


async def test_universe_cannot_restart_the_universe(monkeypatch):
    """regen/coldOpen throw away a run the operator may have had going for
    days. They are not reachable from the tool at all."""
    from backend.agent.tools.registry import dispatch
    _configure(monkeypatch)
    fake = FakeProjector()
    _route(monkeypatch, fake.handler)
    for action in ("regen", "coldOpen", "restart"):
        out = await dispatch("projector_universe", {"action": action})
        assert "action must be one of" in out
    assert fake.calls == []


async def test_status_renders_prose_not_raw_json(monkeypatch):
    """A voice turn has to be able to say this out loud."""
    from backend.agent.tools.registry import dispatch
    _configure(monkeypatch)
    fake = FakeProjector(result={"structuredContent": {
        "output": {"open": False},
        "displays": [{"id": 22, "label": "EPSON PJ", "isPrimary": False}],
        "surfaces": SURFACES,
        "calibrate": False,
        "sim": {"era": "civilisation", "civs": 3, "paused": False}}, "isError": False})
    _route(monkeypatch, fake.handler)

    out = await dispatch("projector_status", {})
    assert "Output is CLOSED" in out
    assert "EPSON PJ" in out
    assert "Ceiling panel" in out
    assert "civilisation era, 3 civilisations" in out
    assert "{" not in out                      # no raw JSON reached the model


# ---- the drift check actually runs -----------------------------------------
# `verify()` was correct and tested from the day the pin was written, and had
# no production caller at all: only `tools/call` ever went out, so
# `mcp_unpinned_tools` could not fire on a live system no matter what the
# server did. These pin the wiring, not the logic.

async def test_a_call_runs_the_drift_check_once(projector, tmp_env):
    await mcp.projector_call("pmu_status")
    assert projector.lists == 1                     # the check ran…
    assert projector.calls == [("pmu_status", {})]  # …and the call still went

    await mcp.projector_call("pmu_status")
    assert projector.lists == 1                     # cached: once per client
    assert len(projector.calls) == 2


async def test_a_token_rotation_re_checks(projector, monkeypatch, tmp_env):
    """`projector()` rebuilds the client when the settings change, so the new
    server — which may not be the same app at all — is checked afresh."""
    await mcp.projector_call("pmu_status")
    assert projector.lists == 1
    _configure(monkeypatch, token="rotated")
    _route(monkeypatch, projector.handler)
    await mcp.projector_call("pmu_status")
    assert projector.lists == 2


async def test_the_check_never_breaks_a_call(monkeypatch, tmp_env):
    """A projector that is off, or a build with no tools/list, must not turn
    into a failed preflight — being unable to check is not a reason to refuse."""
    fake = FakeProjector()

    def handler(request):
        body = json.loads(request.content or b"{}")
        if body.get("method") == "tools/list":
            return httpx.Response(500, json={"error": "no"})
        return fake.handler(request)

    _configure(monkeypatch)
    _route(monkeypatch, handler)
    assert "ok" in (await mcp.projector_call("pmu_status")).lower()
    assert fake.calls == [("pmu_status", {})]


async def test_the_automatic_check_does_not_alert_by_default(projector, tmp_env):
    """No tools/list has ever been run against the real app, and raise_event
    has no dedup — so an unpinned name found by the AUTOMATIC check logs and
    waits for the operator rather than filing a warning on every call."""
    await init_db()
    projector.tools = sorted(mcp.PROJECTOR_MANIFEST) + ["pmu_set_corners"]

    await mcp.projector_call("pmu_status")

    db = await get_db()
    try:
        async with db.execute(
                "SELECT COUNT(*) AS n FROM security_events "
                "WHERE kind = 'mcp_unpinned_tools'") as cur:
            assert (await cur.fetchone())["n"] == 0
    finally:
        await db.close()
    # ...and the unpinned name is still not callable, which is the pin's job
    assert "pmu_set_corners" not in mcp.PROJECTOR_MANIFEST


async def test_switching_the_alert_on_files_the_event(projector, monkeypatch,
                                                      tmp_env):
    from backend.config import settings
    await init_db()
    monkeypatch.setattr(settings, "mcp_alert_unpinned", True)
    projector.tools = sorted(mcp.PROJECTOR_MANIFEST) + ["run_shell"]

    await mcp.projector_call("pmu_status")

    db = await get_db()
    try:
        async with db.execute(
                "SELECT detail FROM security_events "
                "WHERE kind = 'mcp_unpinned_tools'") as cur:
            rows = await cur.fetchall()
    finally:
        await db.close()
    assert len(rows) == 1
    assert json.loads(rows[0]["detail"])["ignored"] == ["run_shell"]


async def test_the_check_can_be_switched_off_entirely(projector, monkeypatch,
                                                      tmp_env):
    from backend.config import settings
    monkeypatch.setattr(settings, "mcp_verify_manifest", False)
    await mcp.projector_call("pmu_status")
    assert projector.lists == 0
    assert projector.calls == [("pmu_status", {})]
