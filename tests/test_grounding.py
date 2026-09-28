"""Grounding + the model finder (backend/grounding.py, grounding_fixtures.py,
grounding_api.py): the labelled screens, scoring, coordinate conventions,
locate() through a fake gateway, the probe's ranking and grounding.json, and
the operator routes."""
import asyncio
import hashlib
import json
import random

import httpx
import pytest

from backend import grounding, grounding_fixtures as gf, providers
from backend.agent import model as model_mod
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app

PIL = pytest.importorskip("PIL")


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    grounding.reset_for_tests()
    monkeypatch.setattr(settings, "grounding_model", "")
    yield
    grounding.reset_for_tests()


# --- fixtures + scoring ---------------------------------------------------------

def test_fixtures_render_with_targets_inside():
    fs = gf.fixtures()
    assert len(fs) == gf.count() >= 20
    names = set()
    for f in fs:
        assert f["png"].startswith(b"\x89PNG") and (f["w"], f["h"]) == (1280, 800)
        assert 3 <= len(f["targets"]) <= 6, f["name"]
        names.add(f["name"])
        for t in f["targets"]:
            x, y, w, h = t["box"]
            assert t["description"] and w > 0 and h > 0
            assert x >= 0 and y >= 0 and x + w <= f["w"] and y + h <= f["h"], (f["name"], t)
    assert len(names) == len(fs)
    assert gf.fixtures() is fs            # cached in-process


def test_fixtures_are_deterministic():
    a = [hashlib.sha256(f["png"]).hexdigest() for f in gf.fixtures()]
    gf._cache = None
    b = [hashlib.sha256(f["png"]).hexdigest() for f in gf.fixtures()]
    assert a == b


def test_duplicate_save_is_disambiguated():
    f = next(f for f in gf.fixtures() if f["name"] == "dialog-duplicate-save")
    saves = [t for t in f["targets"] if t["description"].startswith("the Save button")]
    assert len(saves) == 2 and saves[0]["box"] != saves[1]["box"]


def test_score():
    box = (100, 100, 20, 10)
    assert gf.score((110, 105), box) == (True, 0.0)
    assert gf.score((98, 105), box) == (True, 2.0)          # 2-px tolerance
    hit, err = gf.score((97, 105), box)
    assert not hit and err == 3.0
    hit, err = gf.score((123, 113), box)                     # 4 right, 4 below
    assert not hit and abs(err - (4 ** 2 + 4 ** 2) ** 0.5) < 1e-9
    assert gf.score(None, box) == (False, None)


# --- conventions --------------------------------------------------------------------

def test_to_pixels_conventions_and_clamping():
    assert grounding.to_pixels(640, 400, "px", 1280, 800) == (640, 400)
    assert grounding.to_pixels(500, 500, "k1000", 1280, 800) == (640, 400)
    assert grounding.to_pixels(0.5, 0.25, "unit", 1280, 800) == (640, 200)
    assert grounding.to_pixels(-5, 9999, "px", 1280, 800) == (0, 799)
    assert grounding.to_pixels(1000, 1000, "k1000", 1280, 800) == (1279, 799)
    assert grounding.to_pixels(1.2, -0.1, "unit", 1280, 800) == (1279, 0)
    with pytest.raises(ValueError):
        grounding.to_pixels(1, 1, "inches", 10, 10)


def _targets():
    out = []
    for f in gf.fixtures():
        for t in f["targets"]:
            out.append((f, t))
    return out


def test_score_model_detects_k1000():
    rows = _targets()[:30]
    answers = []
    for f, t in rows:
        x, y, w, h = t["box"]
        cx, cy = x + w / 2, y + h / 2
        answers.append((cx * 1000 / f["w"], cy * 1000 / f["h"], 0.9))
    r = grounding.score_model("p/m", answers, [t["box"] for _, t in rows],
                              [(f["w"], f["h"]) for f, _ in rows], [100] * 30, 0)
    assert r["convention"] == "k1000" and r["hit_rate"] >= 0.9 and not r["unusable"]
    assert r["median_px"] == 0.0 and r["n"] == 30


def test_score_model_errors_are_misses():
    rows = _targets()[:4]
    f0, t0 = rows[0]
    x, y, w, h = t0["box"]
    answers = [(x + w / 2, y + h / 2, 1.0), None, None, None]
    r = grounding.score_model("p/m", answers, [t["box"] for _, t in rows],
                              [(f["w"], f["h"]) for f, _ in rows], [50], 3, "boom")
    assert r["hit_rate"] == 0.25 and r["unusable"] and r["errors"] == 3
    assert r["convention"] == "px" and r["last_error"] == "boom"


# --- parsing + locate -------------------------------------------------------------------

@pytest.mark.parametrize("text,want", [
    ('{"x": 640, "y": 410, "confidence": 0.82}', (640.0, 410.0, 0.82)),
    ('Sure! Here it is:\n```json\n{"x":12.5,"y":7}\n```', (12.5, 7.0, 0.5)),
    ('{"note": "hi"} then {"x": 1, "y": 2, "confidence": 85}', (1.0, 2.0, 0.85)),
    ('x=300, y=200', (300.0, 200.0, 0.5)),
    ('{"x": null, "y": null, "confidence": 0}', None),
    ('I cannot see it.', None),
    ('', None),
])
def test_parse_answer(text, want):
    assert grounding.parse_answer(text) == want


def _fake_complete(reply, seen=None, delay=0.0):
    async def complete(messages, **kw):
        if seen is not None:
            seen.append({"messages": messages, **kw})
        if delay:
            await asyncio.sleep(delay)
        text = reply(messages, kw) if callable(reply) else reply
        yield {"type": "token", "text": text}
        yield {"type": "message", "content": text, "tool_calls": [], "usage": {}}
    return complete


def _write_state(st):
    p = grounding._path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st))


async def test_locate_not_configured(tmp_env):
    with pytest.raises(grounding.NotConfigured):
        await grounding.locate(gf.fixture(0)["png"], 1280, 800, "the Run button")
    # a ranking where every model is unusable is still not configured
    _write_state({"ranking": [{"model": "p/bad", "hit_rate": 0.1, "unusable": True,
                               "convention": "px"}]})
    with pytest.raises(grounding.NotConfigured):
        await grounding.locate(gf.fixture(0)["png"], 1280, 800, "the Run button")
    assert grounding.status()["model"] is None


async def test_locate_uses_winner_and_its_convention(tmp_env, monkeypatch):
    _write_state({"ranking": [
        {"model": "p/bad", "hit_rate": 0.2, "unusable": True, "convention": "px"},
        {"model": "p/good", "hit_rate": 0.9, "unusable": False, "convention": "k1000"}],
        "probed_at": "2026-09-27T10:00:00Z", "pinned": ""})
    seen = []
    monkeypatch.setattr(model_mod.model, "complete",
                        _fake_complete('ok {"x": 500, "y": 250, "confidence": 0.7}', seen))
    png = gf.fixture(0)["png"]
    loc = await grounding.locate(png, 1280, 800, "the Run button", op_id="op1")
    assert (loc.x, loc.y, loc.confidence) == (640, 200, 0.7)
    assert loc.model == "p/good" and loc.convention == "k1000"
    call = seen[0]
    assert call["model_name"] == "p/good" and call["op_id"] == "op1"
    assert call["temperature"] == 0 and call["max_tokens"] == 64
    user = call["messages"][-1]
    assert user["role"] == "user" and user["content"][0]["type"] == "text"
    assert "the Run button" in user["content"][0]["text"]
    assert "1280x800" in user["content"][0]["text"]
    assert user["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


async def test_locate_pin_wins_and_config_wins_over_pin(tmp_env, monkeypatch):
    _write_state({"ranking": [{"model": "p/good", "hit_rate": 0.9, "unusable": False,
                               "convention": "k1000"}], "pinned": "p/other"})
    seen = []
    monkeypatch.setattr(model_mod.model, "complete",
                        _fake_complete('{"x": 10, "y": 20}', seen))
    loc = await grounding.locate(gf.fixture(0)["png"], 1280, 800, "x")
    assert loc.model == "p/other" and loc.convention == "px" and (loc.x, loc.y) == (10, 20)
    monkeypatch.setattr(settings, "grounding_model", "q/cfg")
    loc = await grounding.locate(gf.fixture(0)["png"], 1280, 800, "x")
    assert loc.model == "q/cfg"
    assert grounding.status()["pinned_by"] == "config"


async def test_locate_not_found_and_failures_return_none(tmp_env, monkeypatch):
    _write_state({"ranking": [], "pinned": "p/m"})
    png = gf.fixture(0)["png"]
    monkeypatch.setattr(model_mod.model, "complete",
                        _fake_complete('{"x": null, "y": null, "confidence": 0}'))
    assert await grounding.locate(png, 1280, 800, "nothing") is None

    async def boom(messages, **kw):
        raise model_mod.ModelError("image refused")
        yield  # pragma: no cover
    monkeypatch.setattr(model_mod.model, "complete", boom)
    assert await grounding.locate(png, 1280, 800, "x") is None

    monkeypatch.setattr(settings, "grounding_timeout_s", 0.05)
    monkeypatch.setattr(model_mod.model, "complete", _fake_complete('{"x":1,"y":1}', delay=1))
    assert await grounding.locate(png, 1280, 800, "x") is None


# --- candidates + probe ------------------------------------------------------------------

def test_candidates_filters_enabled_vision_models(monkeypatch):
    def fake(include_models=True):
        return [
            {"id": "a", "label": "A", "enabled": True, "needs_base_url": False,
             "needs_key": True, "key_set": True, "models": [
                 {"id": "see", "label": "See", "enabled": True, "vision": True,
                  "price_in": 1, "price_out": 2},
                 {"id": "blind", "enabled": True, "vision": False},
                 {"id": "off", "enabled": False, "vision": True}]},
            {"id": "b", "enabled": False, "models": [{"id": "x", "enabled": True, "vision": True}]},
            {"id": "c", "enabled": True, "needs_key": True, "key_set": False,
             "models": [{"id": "x", "enabled": True, "vision": True}]},
        ]
    monkeypatch.setattr(providers, "list_providers", fake)
    c = grounding.candidates()
    assert [m["id"] for m in c] == ["a/see"] and c[0]["price_in"] == 1


def _oracle():
    """(sha of png, description) -> box, over every fixture."""
    return {(hashlib.sha256(f["png"]).digest(), t["description"]): t["box"]
            for f in gf.fixtures() for t in f["targets"]}


def _probe_models(monkeypatch, ids):
    monkeypatch.setattr(grounding, "candidates",
                        lambda: [{"id": i, "label": i, "price_in": None,
                                  "price_out": None} for i in ids])
    prices = {"p/good": {"in": 1.0, "out": 2.0, "cache": 1.0}}
    monkeypatch.setattr(providers, "price_for",
                        lambda mid: (mid in prices, prices.get(mid)))
    oracle = _oracle()
    rnd = random.Random(1)

    def reply(messages, kw):
        user = messages[-1]["content"]
        desc = user[0]["text"].split("Element: ", 1)[1]
        import base64
        png = base64.b64decode(user[1]["image_url"]["url"].split(",", 1)[1])
        x, y, w, h = oracle[(hashlib.sha256(png).digest(), desc)]
        if kw["model_name"] == "p/good":       # answers in 0-1000
            return json.dumps({"x": (x + w / 2) * 1000 / 1280,
                               "y": (y + h / 2) * 1000 / 800, "confidence": 0.9})
        if kw["model_name"] == "p/err":
            raise model_mod.ModelError("images not supported")
        return json.dumps({"x": rnd.randint(0, 1279), "y": rnd.randint(0, 799)})

    async def complete(messages, **kw):
        text = reply(messages, kw)
        yield {"type": "message", "content": text, "tool_calls": [], "usage": {}}
    monkeypatch.setattr(model_mod.model, "complete", complete)


async def _wait_done(timeout=20):
    for _ in range(int(timeout / 0.02)):
        if not grounding.status()["running"]:
            return grounding.status()
        await asyncio.sleep(0.02)
    raise AssertionError("probe did not finish")


async def _events():
    db = await get_db()
    try:
        async with db.execute("SELECT summary, detail FROM security_events "
                              "WHERE kind='grounding_probe' ORDER BY id") as cur:
            return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def test_probe_ranks_and_writes_state(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "grounding_probe_targets", 30)
    _probe_models(monkeypatch, ["p/bad", "p/err", "p/good"])
    job = await grounding.start_probe(by="operator")
    assert await grounding.start_probe(by="operator") == job     # one at a time
    st = await _wait_done()
    assert st["job"]["id"] == job and st["job"]["error"] is None
    assert st["job"]["done"] == st["job"]["total"] == 90
    r = st["ranking"]
    assert [x["model"] for x in r][0] == "p/good"
    good = r[0]
    assert good["convention"] == "k1000" and good["hit_rate"] == 1.0
    assert good["cost_per_1k"] == round((1100 * 1.0 + 24 * 2.0) / 1e6 * 1000, 4)
    bad = next(x for x in r if x["model"] == "p/bad")
    err = next(x for x in r if x["model"] == "p/err")
    assert bad["unusable"] and err["unusable"]
    assert err["errors"] == 30 and err["hit_rate"] == 0 and "images" in err["last_error"]
    saved = json.loads(grounding._path().read_text())
    assert saved["ranking"][0]["model"] == "p/good" and saved["probed_at"]
    assert grounding._path().parent == settings.data_dir
    assert st["model"] == "p/good" and st["convention"] == "k1000"
    ev = await _events()
    assert len(ev) == 2 and "p/good" in ev[1]["summary"]


async def test_probe_keeps_pin_and_rejects_unknown(tmp_env, monkeypatch):
    await init_db()
    _probe_models(monkeypatch, ["p/good"])
    grounding.set_pinned("p/good")
    with pytest.raises(ValueError):
        grounding.set_pinned("p/nope")
    with pytest.raises(ValueError):
        await grounding.start_probe(["p/nope"])
    monkeypatch.setattr(settings, "grounding_probe_targets", 5)
    await grounding.start_probe(["p/good"])
    st = await _wait_done()
    assert st["pinned"] == "p/good" and st["pinned_by"] == "settings"
    grounding.set_pinned("")
    assert grounding.status()["pinned"] == ""


async def test_probe_without_candidates(tmp_env, monkeypatch):
    monkeypatch.setattr(grounding, "candidates", lambda: [])
    with pytest.raises(grounding.NotConfigured):
        await grounding.start_probe()


async def test_probe_without_pillow(tmp_env, monkeypatch):
    _probe_models(monkeypatch, ["p/good"])
    monkeypatch.setattr(gf, "HAVE_PIL", False)
    await grounding.start_probe()
    st = grounding.status()
    assert not st["running"] and st["job"]["error"] == "Pillow not installed"


async def test_probe_cancel(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(grounding, "candidates",
                        lambda: [{"id": "p/slow", "label": "", "price_in": None,
                                  "price_out": None}])
    monkeypatch.setattr(model_mod.model, "complete",
                        _fake_complete('{"x":1,"y":1}', delay=5))
    await grounding.start_probe()
    await asyncio.sleep(0.05)
    assert grounding.status()["running"]
    assert grounding.cancel_probe()
    st = await _wait_done()
    assert st["job"]["cancelled"] and st["ranking"] == []
    assert not grounding._path().exists()


# --- API -------------------------------------------------------------------------------

@pytest.fixture
async def op(tmp_env):
    await init_db()
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://jav3.lan:8000") as c:
        await c.post("/api/auth/login", json={"username": "operator", "password": "hunter2"})
        yield c


async def test_api_routes(op, monkeypatch):
    _probe_models(monkeypatch, ["p/good", "p/bad"])
    monkeypatch.setattr(settings, "grounding_probe_targets", 8)
    r = await op.get("/api/grounding")
    assert r.status_code == 200 and r.json()["model"] is None
    assert [c["id"] for c in r.json()["candidates"]] == ["p/good", "p/bad"]

    r = await op.post("/api/grounding/probe", json={"models": ["p/zzz"]})
    assert r.status_code == 400
    r = await op.post("/api/grounding/probe", json={})
    assert r.status_code == 200 and r.json()["job"]
    st = await _wait_done()
    assert st["model"] == "p/good"
    ev = await _events()
    assert any("operator" in (e["detail"] or "") for e in ev)

    r = await op.put("/api/grounding", json={"model": "p/bad"})
    assert r.status_code == 200 and r.json()["model"] == "p/bad"
    assert (await op.put("/api/grounding", json={"model": "x/y"})).status_code == 400
    r = await op.put("/api/grounding", json={"model": ""})
    assert r.json()["model"] == "p/good"

    r = await op.get("/api/grounding/fixtures/0.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content == gf.fixture(0)["png"]
    assert (await op.get(f"/api/grounding/fixtures/{gf.count()}.png")).status_code == 404
    assert (await op.post("/api/grounding/probe/cancel")).json() == {"cancelled": False}


async def test_api_needs_login(tmp_env):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://jav3.lan:8000") as c:
        assert (await c.get("/api/grounding")).status_code == 401
        assert (await c.get("/api/grounding/fixtures/0.png")).status_code == 401
