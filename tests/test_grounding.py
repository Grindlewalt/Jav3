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
_REAL_CANDIDATES = grounding.candidates


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    grounding.reset_for_tests()
    monkeypatch.setattr(settings, "grounding_model", "")
    # every model the tests name is an enabled candidate unless a test says otherwise
    monkeypatch.setattr(grounding, "candidates", lambda: [
        {"id": i, "label": i, "price_in": None, "price_out": None}
        for i in ("p/bad", "p/good", "p/other", "p/m", "p/a", "p/b", "p/c", "p/k",
                  "p/err", "p/slow", "p/zzz", "q/cfg")])
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
    loc = await grounding.locate(png, 1280, 800, "the Run button", op_id="op1",
                                 refine=False)
    assert (loc.x, loc.y, loc.confidence) == (640, 200, 0.7)
    assert loc.model == "p/good" and loc.convention == "k1000"
    call = seen[0]
    assert call["model_name"] == "p/good" and call["op_id"] == "op1"
    assert call["temperature"] == 0 and call["max_tokens"] == grounding.MAX_TOKENS >= 256
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
    loc = await grounding.locate(gf.fixture(0)["png"], 1280, 800, "x", refine=False)
    assert loc.model == "p/other" and loc.convention == "px" and (loc.x, loc.y) == (10, 20)
    monkeypatch.setattr(settings, "grounding_model", "q/cfg")
    loc = await grounding.locate(gf.fixture(0)["png"], 1280, 800, "x", refine=False)
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


# --- the zoomed second pass ---------------------------------------------------------------

@pytest.mark.parametrize("text,want", [
    ('{"x": 5, "y": 6, "w": 22, "h": 20, "confidence": 1}', (22.0, 20.0)),
    ('{"x": 5, "y": 6, "width": 40, "height": 30}', (40.0, 30.0)),
    ('{"x": 5, "y": 6, "confidence": 1}', None),
    ('{"x": 5, "y": 6, "w": 0, "h": 9}', None),
    ('', None),
])
def test_parse_size(text, want):
    assert grounding.parse_size(text) == want


def test_prompt_asks_for_the_size_only_when_refining():
    png = gf.fixture(0)["png"]
    text = grounding._messages(png, 1280, 800, "Save")[-1]["content"][0]["text"]
    assert '"w"' not in text and text.endswith("Element: Save")
    sized = grounding._messages(png, 1280, 800, "Save", sized=True)[-1]["content"][0]["text"]
    assert "CENTRE" in sized and '"w"' in sized and sized.endswith("Element: Save")
    z = grounding._messages(png, 1280, 800, "Save", zoom=4)
    assert "4x enlargement" in z[-1]["content"][0]["text"]


async def test_locate_default_is_one_pass(tmp_env, monkeypatch):
    # the measured winner on DeepSeek V4.1 Flash: one pass, no size asked
    assert grounding.REFINE is False
    _write_state({"ranking": [], "pinned": "p/m"})
    seen = []
    monkeypatch.setattr(model_mod.model, "complete", _two_pass(
        '{"x": 100, "y": 20, "w": 22, "h": 22}', '{"x": 440, "y": 88}', seen))
    loc = await grounding.locate(gf.fixture(3)["png"], 1280, 800, "the undo arrow")
    assert (loc.x, loc.y) == (100, 20) and len(seen) == 1
    assert '"w"' not in seen[0]["messages"][-1]["content"][0]["text"]


def test_zoom_crop_is_clamped_and_enlarged():
    import io
    from PIL import Image
    png = gf.fixture(0)["png"]
    got = grounding._zoom(png, 1280, 800, 10, 790)          # bottom-left corner
    assert got is not None
    data, x0, y0, cw, ch = got
    assert (x0, y0, cw, ch) == (0, 600, 320, 200)
    assert Image.open(io.BytesIO(data)).size == (320 * grounding.REFINE_SCALE,
                                                  200 * grounding.REFINE_SCALE)
    assert grounding._zoom(png, 1280, 800, 640, 400)[1:3] == (480, 300)
    assert grounding._zoom(b"\x89PNG\r\n\x1a\nnot really", 1280, 800, 5, 5) is None


def test_small_gate_per_convention():
    assert grounding._small((22, 22), "px", 1280, 800)
    assert not grounding._small((520, 34), "px", 1280, 800)
    assert grounding._small((20, 30), "k1000", 1280, 800)       # 25.6 x 24 px
    assert not grounding._small((0.3, 0.05), "unit", 1280, 800)
    assert not grounding._small(None, "px", 1280, 800)


def _two_pass(first: str, second, seen: list):
    """A fake gateway: `first` for the full screenshot, `second` (text, or an
    exception to raise) for the zoomed crop."""
    async def complete(messages, **kw):
        zoomed = "enlargement" in messages[-1]["content"][0]["text"]
        seen.append({"zoomed": zoomed, "messages": messages, **kw})
        if zoomed and isinstance(second, Exception):
            raise second
        text = second if zoomed else first
        yield {"type": "message", "content": text, "tool_calls": [], "usage": {}}
    return complete


async def test_locate_refines_a_small_element_and_maps_back(tmp_env, monkeypatch):
    _write_state({"ranking": [], "pinned": "p/m"})
    seen = []
    # pass one: (100, 20), a 22x22 icon; the crop is then x0=0, y0=0 and the
    # zoomed answer (440, 88) is (110, 22) in the screenshot
    monkeypatch.setattr(model_mod.model, "complete", _two_pass(
        '{"x": 100, "y": 20, "w": 22, "h": 22, "confidence": 0.6}',
        '{"x": 440, "y": 88, "w": 88, "h": 88, "confidence": 0.9}', seen))
    loc = await grounding.locate(gf.fixture(3)["png"], 1280, 800, "the undo arrow",
                                 refine=True)
    assert (loc.x, loc.y, loc.confidence) == (110, 22, 0.9)
    assert [s["zoomed"] for s in seen] == [False, True]
    zoom_text = seen[1]["messages"][-1]["content"][0]["text"]
    assert "1280x800" in zoom_text and "the undo arrow" in zoom_text

    # refine=False: one call, pass one stands
    seen.clear()
    loc = await grounding.locate(gf.fixture(3)["png"], 1280, 800, "x", refine=False)
    assert (loc.x, loc.y) == (100, 20) and len(seen) == 1


async def test_locate_keeps_pass_one_for_large_unsized_or_failed(tmp_env, monkeypatch):
    _write_state({"ranking": [], "pinned": "p/m"})
    png = gf.fixture(5)["png"]
    seen = []
    # a 520-px field is never refined; neither is an answer with no size
    for first in ('{"x": 640, "y": 191, "w": 520, "h": 34}', '{"x": 640, "y": 191}'):
        seen.clear()
        monkeypatch.setattr(model_mod.model, "complete",
                            _two_pass(first, '{"x": 1, "y": 1}', seen))
        loc = await grounding.locate(png, 1280, 800, "the Full name field", refine=True)
        assert (loc.x, loc.y) == (640, 191) and len(seen) == 1
    # the second pass finds nothing, or fails: pass one stands
    small = '{"x": 300, "y": 20, "w": 20, "h": 20, "confidence": 0.7}'
    for second in ('{"x": null, "y": null, "confidence": 0}',
                   model_mod.ModelError("boom")):
        seen.clear()
        monkeypatch.setattr(model_mod.model, "complete", _two_pass(small, second, seen))
        loc = await grounding.locate(png, 1280, 800, "an icon", refine=True)
        assert (loc.x, loc.y, loc.confidence) == (300, 20, 0.7) and len(seen) == 2


async def test_locate_refine_in_k1000(tmp_env, monkeypatch):
    _write_state({"ranking": [{"model": "p/k", "hit_rate": 0.9, "unusable": False,
                               "convention": "k1000"}], "pinned": ""})
    seen = []
    # pass one: k1000 (500, 500) = (640, 400), 16x16 px; crop x0=480 y0=300;
    # zoomed k1000 (250, 750) = (320, 600) of 1280x800 = (80, 150) in the crop
    monkeypatch.setattr(model_mod.model, "complete", _two_pass(
        '{"x": 500, "y": 500, "w": 12.5, "h": 20}',
        '{"x": 250, "y": 750}', seen))
    loc = await grounding.locate(gf.fixture(0)["png"], 1280, 800, "a checkbox", refine=True)
    assert (loc.x, loc.y) == (560, 450) and loc.convention == "k1000"


async def test_run_probe_refine_reports_first_and_refined(tmp_env, monkeypatch, tmp_path):
    monkeypatch.setattr(grounding, "STATE_DIR", tmp_path / "s")
    monkeypatch.setattr(grounding, "candidates",
                        lambda: [{"id": "p/m", "label": "p/m", "price_in": None,
                                  "price_out": None}])
    seen = []
    monkeypatch.setattr(model_mod.model, "complete", _two_pass(
        '{"x": 100, "y": 20, "w": 22, "h": 22}', '{"x": 440, "y": 88}', seen))
    steps = []
    await grounding.run_probe(["p/m"], targets=3, on_step=steps.append, refine=True)
    assert len(seen) == 6
    assert steps[0]["first"][:2] == (100.0, 20.0) and steps[0]["answer"][:2] == (110.0, 22.0)
    seen.clear()
    steps.clear()
    await grounding.run_probe(["p/m"], targets=3, on_step=steps.append)
    assert len(seen) == 3 and steps[0]["answer"] is steps[0]["first"]


# --- candidates + probe ------------------------------------------------------------------

def test_candidates_filters_enabled_vision_models(monkeypatch):
    monkeypatch.setattr(grounding, "candidates", _REAL_CANDIDATES)
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
    assert good["cost_per_1k"] == round((grounding.EST_TOKENS_IN * 1.0 + grounding.EST_TOKENS_OUT * 2.0) / 1e6 * 1000, 4)
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


# --- the foreground probe (scripts/grounding_probe.py) ------------------------------

async def test_run_probe_foreground_steps_and_state_dir(tmp_env, monkeypatch, tmp_path):
    monkeypatch.setattr(grounding, "STATE_DIR", tmp_path / "scratch")
    _probe_models(monkeypatch, ["p/bad", "p/good"])
    steps = []
    ranking = await grounding.run_probe(["p/good", "p/bad"], targets=12,
                                        on_step=steps.append)
    assert [r["model"] for r in ranking] == ["p/good", "p/bad"]
    assert ranking[0]["n"] == 12 and ranking[0]["hit_rate"] == 1.0
    assert len(steps) == 24 and steps[0]["model"] == "p/good"
    s = steps[0]
    assert s["answer"] is not None and s["error"] is None and len(s["box"]) == 4
    saved = json.loads((tmp_path / "scratch" / "grounding.json").read_text())
    assert saved["ranking"][0]["model"] == "p/good"
    assert not (settings.data_dir / "grounding.json").exists()
    with pytest.raises(ValueError):
        await grounding.run_probe(["p/nope"], targets=1)


async def test_run_probe_without_candidates(tmp_env, monkeypatch):
    monkeypatch.setattr(grounding, "candidates", lambda: [])
    with pytest.raises(grounding.NotConfigured):
        await grounding.run_probe(targets=1)


async def test_probe_script_dry_run_and_real_run_never_print_a_key(
        tmp_env, monkeypatch, tmp_path, capsys):
    from scripts import grounding_probe as gp
    key = "sk-THIS-IS-A-TEST-KEY-123456"
    monkeypatch.setattr(gp, "_keys", lambda: [key])
    _probe_models(monkeypatch, ["p/good", "p/err"])
    real = model_mod.model.complete

    async def leaky(messages, **kw):     # an error that echoes the key
        if kw["model_name"] == "p/err":
            raise model_mod.ModelError(f"bad auth for {key}")
        async for ev in real(messages, **kw):
            yield ev
    monkeypatch.setattr(model_mod.model, "complete", leaky)
    monkeypatch.setattr(settings, "db_path", settings.db_path)   # restored after
    monkeypatch.setattr(grounding, "STATE_DIR", None)            # restored after
    assert await gp.main(["--dry-run", "--targets", "5",
                          "--state-dir", str(tmp_path / "s")]) == 0
    out = capsys.readouterr().out
    assert "candidates (2)" in out and "24 screens" in out
    assert not (tmp_path / "s" / "grounding.json").exists()
    assert await gp.main(["--targets", "5", "--state-dir", str(tmp_path / "s")]) == 0
    out = capsys.readouterr().out
    assert key not in out and "***" in out
    assert "misses for p/err" in out and "grounding.json written" in out
    assert (tmp_path / "s" / "grounding.json").exists()
    assert (tmp_path / "s" / "ledger.db").exists()      # the ledger moved too


# --- thinking off for DeepSeek (the `extra` seam in agent/model.py) ----------------

def _capture_payloads(monkeypatch, reply='{"x": 10, "y": 20, "confidence": 0.9}'):
    sent = []

    async def fake_stream(self, base, key, payload):
        sent.append({"base": base, "payload": payload})
        yield {"type": "raw", "content": reply, "tool_calls": [], "usage": None}

    monkeypatch.setattr(model_mod.ModelClient, "_stream_once", fake_stream)
    return sent


def test_extra_only_for_deepseek(tmp_env):
    assert grounding._extra_kw("deepseek/deepseek-flash") == {
        "extra": {"thinking": {"type": "disabled"}}}
    assert grounding._extra_kw("openai/gpt-5-mini") == {}
    assert grounding._extra_kw("anthropic/claude-x") == {}
    assert grounding._extra_kw("") == {}


async def test_ask_sends_thinking_disabled_to_deepseek(tmp_env, monkeypatch):
    sent = _capture_payloads(monkeypatch)
    monkeypatch.setattr(model_mod, "model", model_mod.ModelGateway(api_key="test"))
    ans, _ms, _size = await grounding._ask("deepseek/deepseek-flash",
                                           gf.fixture(0)["png"], 1280, 800,
                                           "the Run button", None)
    assert ans[:2] == (10, 20)
    p = sent[0]["payload"]
    assert p["thinking"] == {"type": "disabled"}
    assert p["model"] == "deepseek-flash" and p["stream"] is True
    assert p["max_tokens"] == grounding.MAX_TOKENS


async def test_client_extra_merges_but_never_overrides_protected(tmp_env, monkeypatch):
    sent = _capture_payloads(monkeypatch)
    m = model_mod.ModelClient(api_key="test")
    msgs = [{"role": "user", "content": "x"}]
    tools = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
    extra = {"model": "evil", "messages": [], "stream": False, "tools": [],
             "thinking": {"type": "disabled"}, "max_tokens": 7}
    [ev async for ev in m.complete(msgs, tools=tools, model_name="deepseek-flash",
                                   extra=extra)]
    p = sent[0]["payload"]
    assert p["model"] == "deepseek-flash" and p["stream"] is True
    assert p["tools"] == tools and p["messages"][0]["content"] == "x"
    assert p["thinking"] == {"type": "disabled"} and p["max_tokens"] == 7
    # no extra = today's payload, no thinking field
    [ev async for ev in m.complete(msgs, model_name="deepseek-flash")]
    assert "thinking" not in sent[1]["payload"]


@pytest.mark.parametrize("conv,answer,reason", [
    ("px", '{"x": -1, "y": -1, "confidence": 0.9}', "negative"),
    ("px", '{"x": 100, "y": -5, "confidence": 0.9}', "negative"),
    ("px", '{"x": 1300, "y": 100, "confidence": 0.9}', "outside the image"),
    ("px", '{"x": 100, "y": 900, "confidence": 0.9}', "outside the image"),
    ("k1000", '{"x": 1200, "y": 100, "confidence": 0.9}', "outside 0-1000"),
    ("unit", '{"x": 640, "y": 400, "confidence": 0.9}', "outside 0-1"),
    ("px", '{"x": 300, "y": 300, "confidence": 0}', "confidence"),
    ("px", '{"x": 300, "y": 300, "confidence": 0.1}', "confidence"),
    ("px", '{"x": 0, "y": 0, "confidence": 0.9}', "top-left"),
    ("k1000", '{"x": 0, "y": 0, "confidence": 0.9}', "top-left"),
])
async def test_locate_rejects_out_of_range_low_confidence_and_corner(
        tmp_env, monkeypatch, caplog, conv, answer, reason):
    _write_state({"ranking": [{"model": "p/m", "hit_rate": 0.9, "unusable": False,
                               "convention": conv}]})
    monkeypatch.setattr(model_mod.model, "complete", _fake_complete(answer))
    with caplog.at_level("INFO", logger="backend.grounding"):
        assert await grounding.locate(gf.fixture(0)["png"], 1280, 800, "x",
                                      refine=False) is None
    assert any(reason in r.getMessage() and "p/m" in r.getMessage()
               for r in caplog.records)


async def test_locate_accepts_normal_and_edge_slack_answers(tmp_env, monkeypatch):
    _write_state({"ranking": [{"model": "p/m", "hit_rate": 0.9, "unusable": False,
                               "convention": "px"}]})
    png = gf.fixture(0)["png"]
    monkeypatch.setattr(model_mod.model, "complete",
                        _fake_complete('{"x": 300, "y": 200, "confidence": 0.2}'))
    loc = await grounding.locate(png, 1280, 800, "x", refine=False)
    assert (loc.x, loc.y) == (300, 200)
    monkeypatch.setattr(model_mod.model, "complete",
                        _fake_complete('{"x": 1282, "y": 802}'))
    loc = await grounding.locate(png, 1280, 800, "x", refine=False)
    assert (loc.x, loc.y) == (1279, 799)          # within 2 px: clamped, accepted


def test_score_model_counts_rejected_answers_as_misses():
    box = (0, 0, 40, 40)
    row = grounding.score_model("p/m", [(0.0, 0.0, 0.9)], [box], [(1280, 800)], [10], 0)
    assert row["hit_rate"] == 0.0


def _cands(monkeypatch, ids):
    monkeypatch.setattr(grounding, "candidates", lambda: [
        {"id": i, "label": i, "price_in": None, "price_out": None} for i in ids])


def _row(model, conv, hit=0.9, **kw):
    return {"model": model, "hit_rate": hit, "unusable": False, "convention": conv,
            "n": 5, "probed_at": "2026-09-01T00:00:00Z", **kw}


async def test_subset_probe_keeps_other_rows_and_their_convention(tmp_env, monkeypatch):
    _cands(monkeypatch, ["p/a", "p/b"])
    _write_state({"ranking": [_row("p/a", "k1000"), _row("p/b", "px", 0.8)],
                  "probed_at": "2026-09-01T00:00:00Z", "pinned": "p/b"})
    monkeypatch.setattr(model_mod.model, "complete", _fake_complete('{"x": 5, "y": 5}'))
    ranking = await grounding.run_probe(["p/a"], targets=3)
    by = {r["model"]: r for r in ranking}
    assert set(by) == {"p/a", "p/b"}
    assert by["p/b"]["probed_at"] == "2026-09-01T00:00:00Z" and not by["p/b"]["stale"]
    assert by["p/b"]["convention"] == "px" and by["p/b"]["hit_rate"] == 0.8
    assert by["p/a"]["probed_at"] > "2026-09-01T00:00:00Z"
    st = json.loads(grounding._path().read_text())
    assert st["probed_at"] == by["p/a"]["probed_at"]
    assert len(st["ranking"]) == 2


async def test_pinned_model_keeps_its_measured_convention_after_subset_probe(
        tmp_env, monkeypatch):
    _cands(monkeypatch, ["p/a", "p/b"])
    _write_state({"ranking": [_row("p/a", "px"), _row("p/b", "unit", 0.8)],
                  "probed_at": "x", "pinned": "p/b"})
    monkeypatch.setattr(model_mod.model, "complete", _fake_complete('{"x": 5, "y": 5}'))
    await grounding.run_probe(["p/a"], targets=3)
    assert grounding._resolve() == ("p/b", "unit")


async def test_stale_rows_are_marked_and_never_auto_selected(tmp_env, monkeypatch):
    _cands(monkeypatch, ["p/a", "p/c"])
    _write_state({"ranking": [_row("p/gone", "px", 0.99), _row("p/a", "px", 0.6)],
                  "probed_at": "x", "pinned": ""})
    monkeypatch.setattr(model_mod.model, "complete", _fake_complete('{"x": 5, "y": 5}'))
    ranking = await grounding.run_probe(["p/c"], targets=3)
    by = {r["model"]: r for r in ranking}
    assert by["p/gone"]["stale"] is True and by["p/a"]["stale"] is False
    assert ranking[-1]["model"] == "p/gone"
    assert grounding._resolve()[0] != "p/gone"
    _write_state({"ranking": [by["p/gone"]], "pinned": ""})
    with pytest.raises(grounding.NotConfigured):
        grounding._resolve()


async def test_disabled_pin_is_skipped_for_the_next_usable_ranked_row(tmp_env, monkeypatch):
    _cands(monkeypatch, ["p/a"])
    _write_state({"ranking": [_row("p/a", "k1000", 0.7)], "pinned": "p/off"})
    seen = []
    monkeypatch.setattr(model_mod.model, "complete",
                        _fake_complete('{"x": 500, "y": 500}', seen))
    loc = await grounding.locate(gf.fixture(0)["png"], 1280, 800, "x", refine=False)
    assert loc.model == "p/a" and loc.convention == "k1000"
    assert [c["model_name"] for c in seen] == ["p/a"]


async def test_disabled_top_row_and_pin_with_nothing_left_raises_naming_it(
        tmp_env, monkeypatch):
    _cands(monkeypatch, [])
    seen = []
    monkeypatch.setattr(model_mod.model, "complete", _fake_complete('{"x": 5, "y": 5}', seen))
    _write_state({"ranking": [_row("p/a", "px")], "pinned": "p/off"})
    with pytest.raises(grounding.NotConfigured, match="p/off"):
        await grounding.locate(gf.fixture(0)["png"], 1280, 800, "x")
    _write_state({"ranking": [_row("p/a", "px")], "pinned": ""})
    with pytest.raises(grounding.NotConfigured, match="p/a"):
        await grounding.locate(gf.fixture(0)["png"], 1280, 800, "x")
    monkeypatch.setattr(settings, "grounding_model", "q/cfg")
    with pytest.raises(grounding.NotConfigured, match="q/cfg"):
        await grounding.locate(gf.fixture(0)["png"], 1280, 800, "x")
    assert seen == []
