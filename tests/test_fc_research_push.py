"""FC: the research document reaches the guest, and research cost has an owner.

benchmark-game, 2026-10-01: `research` writes its document on the HOST and told
the model "Document written to research/X.md. Read it with read_file", but a
guest turn's copy of the project is built at turn start, so the next read_file
said "no such file" and the agents went on without the research. The tool now
puts the file into the live guest workspace and carries the head of it in its
result. Its scout/reader/head nodes also had no model calls of their own: every
call was a model_calls row with a NULL conversation id.
"""
import base64
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile

from backend import research, webtools
from backend.agent import budget as budget_mod
from backend.agent.model import Model, model
from backend.config import settings
from backend.db import get_db, init_db
from backend.vm import broker, guest_turn, workspace_xfer
from backend.vm.guest_pkg import build_package_tar

DOC = "# Research: pi\n\n" + "".join(f"Line {i} of the findings, with some words.\n"
                                   for i in range(400))
R = {"topic": "pi", "job_id": "j1", "root_id": 1, "doc_path": "research/pi.md",
     "doc_status": "canonical", "doc": DOC}


# --- what the tool tells the model --------------------------------------------

async def test_the_result_carries_the_head_and_the_file_is_pushed(monkeypatch):
    sent = {}

    async def fake_push(slug, files):
        sent.update(slug=slug, files=files)
        return True
    monkeypatch.setattr(guest_turn, "push_files", fake_push)
    out = await research.result_text("demo", R)
    assert sent["slug"] == "demo" and sent["files"] == {"research/pi.md": DOC.encode()}
    assert "read_file research/pi.md for all of it" in out
    assert "full document at research/pi.md" in out
    assert DOC.startswith(out.split("---\n", 1)[1][:200])         # the head, not a summary
    assert 4000 < len(out) < 6500 and "more chars]" in out        # ~5 KB, and it says it was cut
    assert f"{len(DOC):,} chars" in out


async def test_a_push_that_failed_is_said_and_the_head_is_still_there(monkeypatch):
    async def fake_push(slug, files):
        return False
    monkeypatch.setattr(guest_turn, "push_files", fake_push)
    out = await research.result_text("demo", R)
    assert "could not be copied into this turn's workspace" in out
    assert "Line 0 of the findings" in out


async def test_a_host_run_turn_reads_the_project_itself(monkeypatch):
    async def fake_push(slug, files):
        return None                                    # not a brokered guest tool call
    monkeypatch.setattr(guest_turn, "push_files", fake_push)
    out = await research.result_text("demo", R)
    assert "could not be copied" not in out and "Document written to research/pi.md" in out


async def test_a_held_document_is_not_pushed_and_says_so(monkeypatch):
    async def boom(slug, files):
        raise AssertionError("a held document must not reach the guest")
    monkeypatch.setattr(guest_turn, "push_files", boom)
    out = await research.result_text("demo", {**R, "doc_status": "held for approval"})
    assert "held for the operator's approval" in out and "Line 0 of the findings" in out


async def test_a_short_document_comes_whole(monkeypatch):
    async def fake_push(slug, files):
        return True
    monkeypatch.setattr(guest_turn, "push_files", fake_push)
    out = await research.result_text("demo", {**R, "doc": "# Research: x\n\nshort\n"})
    assert out.rstrip().endswith("short") and "more chars" not in out


def test_the_head_is_cut_at_a_line_end():
    head = research._doc_head("a" * 40 + "\n" + "b" * 40, limit=60)
    assert head == "a" * 40
    assert research._doc_head("x" * 30, limit=60) == "x" * 30


# --- push_files, the host half --------------------------------------------------

def _guest_turn_op(op_id="op-fc"):
    broker.register_turn(broker.TurnEnvelope(op_id=op_id, active_project="demo"))
    return budget_mod.active_op_id.set(op_id)


async def test_push_files_sends_the_file_and_marks_it_shipped(tmp_env, monkeypatch):
    got = {}

    async def fake_rpc(spec, box):
        got.update(spec)
        return {"type": "put", "ok": True, "written": list(spec["files"])}

    async def fake_box(slug):
        return object()
    monkeypatch.setattr(guest_turn, "_pinned_rpc", fake_rpc)
    monkeypatch.setattr(guest_turn.boxes, "for_project", fake_box)
    tok = _guest_turn_op()
    try:
        assert await guest_turn.push_files("demo", {"research/pi.md": b"# hi\n"}) is True
    finally:
        budget_mod.active_op_id.reset(tok)
        broker.release_turn("op-fc")
    assert got["mode"] == "put_files" and got["active_slug"] == "demo"
    assert base64.b64decode(got["files"]["research/pi.md"]) == b"# hi\n"
    assert workspace_xfer._shipped["demo"]["research/pi.md"] == workspace_xfer._sha(b"# hi\n")


async def test_push_files_does_nothing_outside_a_guest_turn(monkeypatch):
    async def boom(spec, box):
        raise AssertionError("no guest turn: nothing to push into")
    monkeypatch.setattr(guest_turn, "_pinned_rpc", boom)
    assert await guest_turn.push_files("demo", {"a.md": b"x"}) is None
    tok = budget_mod.active_op_id.set("no-such-turn")
    try:
        assert await guest_turn.push_files("demo", {"a.md": b"x"}) is None
    finally:
        budget_mod.active_op_id.reset(tok)


async def test_push_files_says_false_when_the_guest_cannot_take_it(tmp_env, monkeypatch):
    async def fake_box(slug):
        return object()
    monkeypatch.setattr(guest_turn.boxes, "for_project", fake_box)
    tok = _guest_turn_op()
    try:
        for reply in (None, {"type": "final", "error": "KeyError"},      # an older guest
                      {"type": "put", "ok": False, "error": "no workspace"}):
            async def rpc(spec, box, reply=reply):
                return reply
            monkeypatch.setattr(guest_turn, "_pinned_rpc", rpc)
            assert await guest_turn.push_files("demo", {"a.md": b"x"}) is False

        async def dead(spec, box):
            raise OSError("no guest")
        monkeypatch.setattr(guest_turn, "_pinned_rpc", dead)
        assert await guest_turn.push_files("demo", {"a.md": b"x"}) is False
        big = {"a.md": b"x" * (guest_turn.PUSH_MAX_BYTES + 1)}
        assert await guest_turn.push_files("demo", big) is False        # never sent
    finally:
        budget_mod.active_op_id.reset(tok)
        broker.release_turn("op-fc")


# --- the guest half, with the guest's own server code ---------------------------

_SHIM = ("import socket\n"
         "socket.VMADDR_CID_HOST = getattr(socket, 'VMADDR_CID_HOST', 2)\n"
         "socket.VMADDR_CID_ANY = getattr(socket, 'VMADDR_CID_ANY', 0xFFFFFFFF)\n")


def _guest_run(gdir: str, body: str) -> str:
    r = subprocess.run([sys.executable, "-S", "-c", _SHIM + body], cwd=gdir,
                       env={"PYTHONPATH": gdir, "PATH": os.environ.get("PATH", "")},
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def _guest_pkg() -> str:
    gdir = tempfile.mkdtemp()
    with tarfile.open(fileobj=io.BytesIO(build_package_tar()), mode="r:gz") as t:
        t.extractall(gdir, filter="data")
    return gdir


def _tar_b64(files: dict[str, str]) -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, text in files.items():
            data = text.encode()
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode()


def test_the_guest_takes_a_pushed_file_beside_the_rest_of_its_copy():
    spec = {"mode": "put_files", "active_slug": "demo",
            "files": {"research/pi.md": base64.b64encode(b"# findings\n").decode()}}
    out = _guest_run(_guest_pkg(), (
        "import asyncio, json, socket\n"
        "from backend import server\n"
        f"server._unpack_workspace('demo', {_tar_b64({'keep.txt': 'k', 'edit.txt': 'old'})!r})\n"
        "root = server.guest_config.settings.projects_dir / 'demo'\n"
        # the turn already wrote a stale research/pi.md into its buffer
        "(root / '.staging/research').mkdir(parents=True)\n"
        "(root / '.staging/research/pi.md').write_text('stale')\n"
        "(root / '.staging/edit.txt').write_text('new')\n"
        "async def main():\n"
        "    a, b = socket.socketpair(); a.setblocking(False); b.setblocking(False)\n"
        "    loop = asyncio.get_running_loop()\n"
        "    t = asyncio.create_task(server._handle(loop, b))\n"
        f"    await loop.sock_sendall(a, {(json.dumps(spec) + chr(10)).encode()!r})\n"
        "    reply = (await loop.sock_recv(a, 65536)).decode().strip()\n"
        "    await t\n"
        "    from backend import writes\n"
        "    seen = writes.resolve('demo', 'research/pi.md').read_text()\n"
        "    packed = server._pack_staging('demo')\n"
        "    print(json.dumps({'reply': json.loads(reply), 'seen': seen, 'packed': packed,\n"
        "        'keep': (root / 'keep.txt').read_text(),\n"
        "        'overlay': (root / '.staging/research/pi.md').exists()}))\n"
        "asyncio.run(main())\n"))
    res = json.loads(out)
    assert res["reply"]["ok"] is True and res["reply"]["written"] == ["research/pi.md"]
    assert res["seen"] == "# findings\n"            # read_file finds it now
    assert res["keep"] == "k" and res["overlay"] is False      # the host's text won
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(res["packed"])), mode="r:gz") as t:
        names = {m.name for m in t.getmembers()}
    # the turn's own edit still goes home; the pushed file is neither an edit nor a deletion
    assert names == {"edit.txt"}


def test_the_guest_refuses_what_it_should_and_never_invents_a_workspace():
    ok = base64.b64encode(b"x").decode()
    out = _guest_run(_guest_pkg(), (
        "import json\n"
        "from backend import server\n"
        f"server._unpack_workspace('demo', {_tar_b64({'keep.txt': 'k'})!r})\n"
        "root = server.guest_config.settings.projects_dir / 'demo'\n"
        f"bad = server._put_files('demo', {{'.staging/x': {ok!r}, '.git/config': {ok!r}, "
        f"'../escape': {ok!r}, 'a/../.staging/y': {ok!r}, 'fine.md': {ok!r}}})\n"
        f"none = server._put_files('nope', {{'a.md': {ok!r}}})\n"
        f"junk = server._put_files('demo', ['a.md'])\n"
        "print(json.dumps({'bad': bad, 'none': none, 'junk': junk,\n"
        "    'fine': (root / 'fine.md').exists(), 'staged': (root / '.staging').exists(),\n"
        "    'escape': (root.parent / 'escape').exists(),\n"
        "    'nope': (root.parent / 'nope').exists()}))\n"))
    res = json.loads(out)
    assert res["bad"]["ok"] is False and res["bad"]["written"] == ["fine.md"]
    assert set(res["bad"]["refused"]) == {".staging/x", ".git/config", "../escape",
                                          "a/../.staging/y"}
    assert res["none"]["ok"] is False and res["junk"]["ok"] is False
    assert res["fine"] is True and res["staged"] is False
    assert res["escape"] is False and res["nope"] is False


async def test_a_pushed_file_the_guest_then_deletes_is_deleted_on_the_host(tmp_env):
    d = settings.projects_dir / "demo"
    (d / "research").mkdir(parents=True)
    (d / "research" / "pi.md").write_bytes(b"# hi\n")
    workspace_xfer.build_merged_tar("demo")                       # a turn started
    (d / "research" / "new.md").write_bytes(b"# new\n")           # written host-side mid-turn
    workspace_xfer.note_shipped("demo", "research/new.md", b"# new\n")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        data = json.dumps(["research/new.md"]).encode()
        ti = tarfile.TarInfo(workspace_xfer.DELETED_MEMBER)
        ti.size = len(data)
        t.addfile(ti, io.BytesIO(data))
    res = await workspace_xfer.apply_guest_writes("demo", buf.getvalue())
    assert res["deleted"] == ["research/new.md"] and not (d / "research" / "new.md").exists()


# --- research cost has an owner --------------------------------------------------

async def _rows(sql, args=()):
    db = await get_db()
    try:
        async with db.execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def test_every_model_call_of_a_research_run_lands_on_its_node(tmp_env, monkeypatch):
    await init_db()
    (settings.projects_dir / "demo").mkdir(parents=True)
    usage = {"prompt_tokens": 100, "completion_tokens": 20,
             "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 100}

    async def fake_stream_once(self, base, key, payload):
        sys_prompt = payload["messages"][0]["content"]
        if sys_prompt.startswith("Generate up to"):
            content = "query one\nquery two"
        elif sys_prompt.startswith("From the search results"):
            content = json.dumps([{"theme": "A", "urls": ["https://a.test/1"]},
                                  {"theme": "B", "urls": ["https://b.test/2"]}])
        elif sys_prompt.startswith("Summarize what this page"):
            content = "- a fact"
        else:
            content = "A body with a Sources list."
        yield {"type": "raw", "content": content, "tool_calls": [], "usage": usage}

    async def fake_search(q, limit=6):
        return [{"url": "https://a.test/1", "title": "A", "snippet": "a"},
                {"url": "https://b.test/2", "title": "B", "snippet": "b"}]

    async def fake_read(url, session=None):
        return f"page text of {url}"
    monkeypatch.setattr(Model, "_stream_once", fake_stream_once)
    monkeypatch.setattr(model, "api_key", "sk-test")
    monkeypatch.setattr(model.transport, "api_key", "sk-test")
    monkeypatch.setattr(webtools, "search_results", fake_search)
    monkeypatch.setattr(webtools, "read", fake_read)

    r = await research.run_research("pi", "demo", job_id="job-fc")
    assert r["doc_status"] == "canonical" and "A body" in r["doc"]

    calls = await _rows(
        "SELECT m.conversation_id AS cid, c.kind AS kind FROM model_calls m "
        "LEFT JOIN conversations c ON c.id = m.conversation_id")
    assert calls and None not in {c["cid"] for c in calls}, calls     # none unattributed
    by_kind: dict[str, int] = {}
    for c in calls:
        by_kind[c["kind"]] = by_kind.get(c["kind"], 0) + 1
    # scout: the query list and the source filter; each reader: one page; head: the document
    assert by_kind == {"scout": 2, "reader": 2, "head": 1}, by_kind
    readers = await _rows("SELECT id FROM conversations WHERE kind = 'reader'")
    for rd in readers:
        own = await _rows("SELECT 1 FROM model_calls WHERE conversation_id = ?", (rd["id"],))
        assert len(own) == 1                      # each reader pays for its own page only
    # the ambient context is clean afterwards: a later call is nobody's again
    from backend.agent.model import billed_to
    assert billed_to.get() is None


async def test_a_caller_that_names_a_conversation_still_wins(tmp_env, monkeypatch):
    await init_db()
    usage = {"prompt_tokens": 1, "completion_tokens": 1,
             "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 1}

    async def fake_stream_once(self, base, key, payload):
        yield {"type": "raw", "content": "ok", "tool_calls": [], "usage": usage}
    monkeypatch.setattr(Model, "_stream_once", fake_stream_once)
    monkeypatch.setattr(model, "api_key", "sk-test")
    monkeypatch.setattr(model.transport, "api_key", "sk-test")
    from backend.agent.model import complete_text
    with research._billed_to(5):
        await complete_text("s", "u", conversation_id=9)
        await complete_text("s", "u")
    await complete_text("s", "u")
    rows = await _rows("SELECT conversation_id AS cid FROM model_calls ORDER BY id")
    assert [r["cid"] for r in rows] == [9, 5, None]
