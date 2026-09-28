"""Grounding: turn "the Save button" into a point on a screenshot, with the
vision model the model finder measured best on our own labelled screens.

Contract: docs/navigation-contract.md section C. The surface (names,
signatures, exceptions) is frozen by the contract commit.

    desk_click(target="Save button") -> desk.py -> locate(image, w, h, text)
                                                -> Located(x, y, ...) -> act()

Model selection: `settings.grounding_model` if set, else the operator's pin
from Settings (grounding.json), else the winner of the last probe, else
NotConfigured. Every call goes through the gateway
(`backend.agent.model.model.complete`, model_name="provider/model", which
`providers.resolve` turns into the provider's base_url + host-side key) so
budgets and the ledger see it; the key never leaves the host.

The model finder (`start_probe`) asks every enabled image-capable model to
find each target on the fixtures in grounding_fixtures.py, reads its raw
answers under all three coordinate conventions (pixels, 0-1000, 0-1), keeps
the convention that hits most, and ranks by hit rate, then cost, then p95
latency. The ranking and each model's convention live in
`<data_dir>/grounding.json`, beside providers_state.json.
"""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import logging
import os
import re
import secrets
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from . import providers
from .config import settings

log = logging.getLogger(__name__)

CONVENTIONS = ("px", "k1000", "unit")
DEFAULT_CONVENTION = "px"         # what the prompt asks for; a pin with no probe
UNUSABLE_BELOW = 0.5              # hit rate under which a model is never picked
EST_TOKENS_IN = 1100              # one screenshot + the prompt
EST_TOKENS_OUT = 24               # {"x": 640, "y": 410, "confidence": 0.9}
MAX_TOKENS = 64


@dataclasses.dataclass
class Located:
    x: int                  # image pixels of the image passed to locate()
    y: int
    confidence: float       # 0..1; 0.5 when the model gave none
    model: str              # "provider/model-id"
    convention: str         # one of CONVENTIONS
    latency_ms: int


class NotConfigured(Exception):
    """No grounding model: nothing pinned and no probe has run (or every
    candidate was unusable). The caller tells the model to click by element
    id or coordinates instead."""


def to_pixels(rx: float, ry: float, convention: str, width: int, height: int) -> tuple[int, int]:
    """Map a model's raw answer to image pixels under `convention`, clamped
    into the image."""
    if convention == "k1000":
        x, y = rx * width / 1000.0, ry * height / 1000.0
    elif convention == "unit":
        x, y = rx * width, ry * height
    elif convention == "px":
        x, y = rx, ry
    else:
        raise ValueError(f"unknown convention {convention!r}")
    return (max(0, min(width - 1, int(round(x)))),
            max(0, min(height - 1, int(round(y)))))


# --- state (grounding.json) ------------------------------------------------------

def _path() -> Path:
    return providers._state_path().parent / "grounding.json"


def _load() -> dict:
    try:
        st = json.loads(_path().read_text())
        if not isinstance(st, dict):
            st = {}
    except (OSError, json.JSONDecodeError):
        st = {}
    if not isinstance(st.get("ranking"), list):
        st["ranking"] = []
    st.setdefault("probed_at", None)
    if not isinstance(st.get("pinned"), str):
        st["pinned"] = ""
    return st


def _save(st: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2))
    os.replace(tmp, p)


def set_pinned(model_id: str) -> dict:
    """Settings' pin selector: "" = automatic (the best measured). The id must
    be a current candidate. Returns status()."""
    model_id = (model_id or "").strip()
    if model_id and model_id not in {c["id"] for c in candidates()}:
        raise ValueError(f"{model_id!r} is not an enabled image-capable model")
    st = _load()
    st["pinned"] = model_id
    _save(st)
    return status()


def _entry(st: dict, model_id: str) -> dict | None:
    return next((r for r in st["ranking"] if r.get("model") == model_id), None)


def _resolve() -> tuple[str, str]:
    """(model id, convention) or NotConfigured."""
    st = _load()
    pinned = (settings.grounding_model or "").strip() or st["pinned"]
    if pinned:
        e = _entry(st, pinned)
        conv = e.get("convention") if e else None
        return pinned, conv if conv in CONVENTIONS else DEFAULT_CONVENTION
    for e in st["ranking"]:
        if not e.get("unusable") and e.get("model"):
            conv = e.get("convention")
            return e["model"], conv if conv in CONVENTIONS else DEFAULT_CONVENTION
    raise NotConfigured("no grounding model configured")


# --- candidates -------------------------------------------------------------------

def candidates() -> list[dict]:
    """Enabled provider models with vision=true, as
    [{"id": "provider/model", "label", "price_in", "price_out"}]. Only
    providers that could actually take the call: switched on, endpoint set,
    and a key when they need one."""
    out = []
    for p in providers.list_providers(include_models=True):
        if not p.get("enabled") or p.get("needs_base_url"):
            continue
        if p.get("needs_key") and not p.get("key_set"):
            continue
        for m in p.get("models") or []:
            if m.get("enabled") and m.get("vision"):
                out.append({"id": f"{p['id']}/{m['id']}",
                            "label": f"{m.get('label') or m['id']} · {p.get('label') or p['id']}",
                            "price_in": m.get("price_in"),
                            "price_out": m.get("price_out")})
    return out


# --- one ask ------------------------------------------------------------------------

PROMPT = (
    "You are a GUI grounding model. The screenshot is {w}x{h} pixels. Find the "
    "one on-screen element described below and give the point to click: its "
    "centre, in pixels of this image, measured from the top-left corner.\n"
    "Answer with ONE JSON object and nothing else:\n"
    '{{"x": <int>, "y": <int>, "confidence": <0..1>}}\n'
    'If the element is not visible, answer {{"x": null, "y": null, "confidence": 0}}.\n\n'
    "Element: {description}")

SYSTEM = "You locate UI elements on screenshots. Reply with JSON only."


def _messages(image: bytes, width: int, height: int, description: str) -> list[dict]:
    from .agent import imageresult
    mime = imageresult.sniff(image)
    if mime is None:
        raise ValueError("not an image (PNG, JPEG, WebP or GIF bytes expected)")
    b64 = base64.b64encode(image).decode()
    text = PROMPT.format(w=width, h=height, description=description.strip()[:300])
    # the same shape as loop._image_message: a user message whose content is a
    # text part plus an image_url data-URI part
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]}]


_NUM = r"-?\d+(?:\.\d+)?"


def parse_answer(text: str) -> tuple[float, float, float] | None:
    """(rx, ry, confidence) from a model's reply, leniently: the first {...}
    that parses as JSON with numeric x and y; failing that, "x": n / "y": n
    pairs anywhere. None when there is no point (not found, or garbage)."""
    if not text:
        return None
    for m in re.finditer(r"\{[^{}]*\}", text):
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            continue
        if not isinstance(obj, dict) or "x" not in obj or "y" not in obj:
            continue
        x, y = obj.get("x"), obj.get("y")
        if isinstance(x, (list, tuple)) and len(x) == 2 and y is None:
            x, y = x
        try:
            rx, ry = float(x), float(y)
        except (TypeError, ValueError):
            return None
        return rx, ry, _conf(obj.get("confidence"))
    mx = re.search(r'"?\bx"?\s*[:=]\s*(' + _NUM + ")", text)
    my = re.search(r'"?\by"?\s*[:=]\s*(' + _NUM + ")", text)
    if mx and my:
        mc = re.search(r'"?confidence"?\s*[:=]\s*(' + _NUM + ")", text)
        return float(mx.group(1)), float(my.group(1)), _conf(mc.group(1) if mc else None)
    return None


def _conf(v) -> float:
    try:
        c = float(v)
    except (TypeError, ValueError):
        return 0.5
    if c > 1.0 and c <= 100.0:
        c /= 100.0
    return max(0.0, min(1.0, c))


async def _ask(model_id: str, image: bytes, width: int, height: int,
               description: str, op_id: str | None) -> tuple[tuple | None, int]:
    """One gateway call, drained. (parsed answer or None, latency ms). Raises
    whatever the gateway raises, or asyncio.TimeoutError."""
    from .agent.model import model
    messages = _messages(image, width, height, description)

    async def drain() -> str:
        parts = []
        async for ev in model.complete(messages, model_name=model_id, op_id=op_id,
                                       temperature=0, max_tokens=MAX_TOKENS):
            if ev.get("type") == "message":
                parts.append(ev.get("content") or "")
        return "".join(parts)

    t0 = time.monotonic()
    text = await asyncio.wait_for(drain(), timeout=settings.grounding_timeout_s)
    return parse_answer(text), int((time.monotonic() - t0) * 1000)


async def locate(image: bytes, width: int, height: int, description: str,
                 *, op_id: str | None = None) -> Located | None:
    """Ground `description` on `image` (PNG/JPEG bytes, `width` x `height`).
    Returns None when the model answered but could not find it, or the call
    failed or timed out (logged); raises NotConfigured when there is no model
    to ask. A spent budget (BudgetExceeded) propagates like any model call."""
    model_id, conv = _resolve()
    from .agent import budget as budget_mod
    try:
        ans, ms = await _ask(model_id, image, width, height, description, op_id)
    except budget_mod.BudgetExceeded:
        raise
    except asyncio.TimeoutError:
        log.warning("grounding: %s timed out after %.0fs", model_id,
                    settings.grounding_timeout_s)
        return None
    except Exception as e:  # noqa: BLE001 — a failed ground is "not found", not a crash
        log.warning("grounding: %s failed: %s", model_id, str(e)[:200])
        return None
    if ans is None:
        return None
    rx, ry, conf = ans
    x, y = to_pixels(rx, ry, conv, width, height)
    return Located(x=x, y=y, confidence=conf, model=model_id, convention=conv,
                   latency_ms=ms)


# --- the model finder -----------------------------------------------------------------

_job: dict | None = None          # the current / last probe
_task: asyncio.Task | None = None


def _job_view(j: dict | None) -> dict | None:
    if j is None:
        return None
    return {k: j[k] for k in ("id", "running", "done", "total", "current", "models",
                              "by", "started_at", "finished_at", "error", "cancelled")}


def status() -> dict:
    """What Settings shows: {model, pinned, convention, ranking, probed_at,
    running, job}. `model` is what locate() would use now (None if nothing);
    `pinned_by` says whether the pin comes from config or Settings."""
    st = _load()
    cfg = (settings.grounding_model or "").strip()
    try:
        model_id, conv = _resolve()
    except NotConfigured:
        model_id, conv = None, None
    return {"model": model_id, "pinned": cfg or st["pinned"],
            "pinned_by": "config" if cfg else ("settings" if st["pinned"] else None),
            "convention": conv, "ranking": st["ranking"], "probed_at": st["probed_at"],
            "running": bool(_job and _job["running"]), "job": _job_view(_job),
            "candidates": candidates()}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _event(summary: str, detail: dict) -> None:
    try:
        from . import security
        from .db import get_db
        db = await get_db()
        try:
            await security.raise_event(db, kind="grounding_probe", severity="info",
                                       summary=summary, detail=detail)
        finally:
            await db.close()
    except Exception:  # noqa: BLE001 — an alert must never break the probe
        pass


def _targets(fixtures: list[dict], cap: int) -> list[tuple[int, int]]:
    """(fixture index, target index) round-robin over the screens, so a small
    cap still samples every screen."""
    out = []
    depth = max((len(f["targets"]) for f in fixtures), default=0)
    for ti in range(depth):
        for fi, f in enumerate(fixtures):
            if ti < len(f["targets"]):
                out.append((fi, ti))
    return out[:max(0, cap)]


def _p95(xs: list[int]) -> int | None:
    if not xs:
        return None
    s = sorted(xs)
    return int(s[min(len(s) - 1, max(0, int(round(0.95 * len(s))) - 1))])


def _cost_per_1k(model_id: str) -> float | None:
    _known, price = providers.price_for(model_id)
    if not price:
        return None
    per_call = (EST_TOKENS_IN * price["in"] + EST_TOKENS_OUT * price["out"]) / 1e6
    return round(per_call * 1000, 4)


def score_model(model_id: str, answers: list, boxes: list, sizes: list,
                latencies: list[int], errors: int, last_error: str | None = None) -> dict:
    """The ranking row for one model. `answers[i]` is the raw (rx, ry, conf)
    or None (not found / error), for target `boxes[i]` on an image of
    `sizes[i]` = (w, h). Every convention is tried; the best hit count wins,
    ties broken by the smaller median error."""
    from .grounding_fixtures import score
    n = len(boxes)
    best = None
    for conv in CONVENTIONS:
        hits, errs = 0, []
        for ans, box, (w, h) in zip(answers, boxes, sizes):
            if ans is None:
                continue
            hit, err = score(to_pixels(ans[0], ans[1], conv, w, h), box)
            hits += hit
            errs.append(err)
        med = statistics.median(errs) if errs else None
        key = (hits, -(med if med is not None else 1e9))
        if best is None or key > best[0]:
            best = (key, conv, hits, med)
    _key, conv, hits, med = best
    rate = hits / n if n else 0.0
    return {"model": model_id, "hit_rate": round(rate, 3),
            "median_px": round(med, 1) if med is not None else None,
            "p95_ms": _p95(latencies), "cost_per_1k": _cost_per_1k(model_id),
            "convention": conv, "n": n, "errors": errors,
            "unusable": rate < UNUSABLE_BELOW, "last_error": last_error}


def rank(rows: list[dict]) -> list[dict]:
    """Usable first; then hit rate (desc), cost (asc, unknown last), p95 (asc)."""
    def key(r):
        cost = r.get("cost_per_1k")
        p95 = r.get("p95_ms")
        return (bool(r.get("unusable")), -r.get("hit_rate", 0.0),
                cost is None, cost or 0.0, p95 is None, p95 or 0)
    return sorted(rows, key=key)


async def _probe_one(job: dict, model_id: str, fixtures: list[dict],
                     order: list[tuple[int, int]]) -> dict:
    from .agent import budget as budget_mod
    answers, boxes, sizes, lat = [], [], [], []
    errors, last_error = 0, None
    for fi, ti in order:
        f = fixtures[fi]
        t = f["targets"][ti]
        job["current"] = f"{model_id} · {f['name']}"
        ans = None
        try:
            ans, ms = await _ask(model_id, f["png"], f["w"], f["h"],
                                 t["description"], None)
            lat.append(ms)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            errors += 1
            last_error = f"timed out after {settings.grounding_timeout_s:.0f}s"
        except budget_mod.BudgetExceeded as e:
            errors += 1
            last_error = str(e)[:200]
        except Exception as e:  # noqa: BLE001 — a refused image is a miss, not a crash
            errors += 1
            last_error = str(e)[:200] or type(e).__name__
        answers.append(ans)
        boxes.append(t["box"])
        sizes.append((f["w"], f["h"]))
        job["done"] += 1
    return score_model(model_id, answers, boxes, sizes, lat, errors, last_error)


async def _run(job: dict) -> None:
    """The background task. Never raises: every failure lands in job["error"]."""
    try:
        from . import grounding_fixtures as gf
        fixtures = gf.fixtures()
        order = _targets(fixtures, settings.grounding_probe_targets)
        job["total"] = len(order) * len(job["models"])
        await _event(f"model finder started on {len(job['models'])} model(s)",
                     {"job": job["id"], "models": job["models"], "by": job["by"],
                      "targets": len(order)})
        rows = []
        for mid in job["models"]:
            rows.append(await _probe_one(job, mid, fixtures, order))
        ranking = rank(rows)
        st = _load()
        st["ranking"] = ranking
        st["probed_at"] = _now()
        _save(st)
        winner = next((r["model"] for r in ranking if not r["unusable"]), None)
        job["winner"] = winner
        await _event(
            f"model finder finished: {winner} is the grounding model"
            if winner else "model finder finished: no usable grounding model",
            {"job": job["id"], "winner": winner, "by": job["by"],
             "ranking": [{k: r[k] for k in ("model", "hit_rate", "median_px",
                                            "convention", "unusable")}
                         for r in ranking]})
    except asyncio.CancelledError:
        job["cancelled"] = True
        job["error"] = "cancelled"
        await _event("model finder cancelled", {"job": job["id"], "by": job["by"]})
    except Exception as e:  # noqa: BLE001 — the task must never raise
        job["error"] = str(e)[:300] or type(e).__name__
        log.exception("grounding probe failed")
    finally:
        job["running"] = False
        job["current"] = None
        job["finished_at"] = _now()


async def start_probe(models: list[str] | None = None, *, by: str = "") -> str:
    """Run the model finder in the background over `models` (default: every
    candidate) and return a job id; status() reports progress. One probe at a
    time: while one runs, its id is returned. ValueError for a model that is
    not a candidate, NotConfigured when there is nothing to test."""
    global _job, _task
    if _job and _job["running"]:
        return _job["id"]
    cands = [c["id"] for c in candidates()]
    if models:
        bad = [m for m in models if m not in cands]
        if bad:
            raise ValueError(f"not an enabled image-capable model: {', '.join(bad)}")
        chosen = list(dict.fromkeys(models))
    else:
        chosen = cands
    if not chosen:
        raise NotConfigured("no image-capable model is enabled (Settings → Providers)")
    job = {"id": secrets.token_hex(6), "running": True, "done": 0, "total": 0,
           "current": None, "models": chosen, "by": by, "started_at": _now(),
           "finished_at": None, "error": None, "cancelled": False, "winner": None}
    from . import grounding_fixtures as gf
    if not gf.HAVE_PIL:
        job.update(running=False, error="Pillow not installed", finished_at=_now())
        _job = job
        return job["id"]
    _job = job
    _task = asyncio.create_task(_run(job))
    return job["id"]


def cancel_probe() -> bool:
    """Stop a running probe; its partial results are discarded."""
    if _task is not None and not _task.done():
        _task.cancel()
        return True
    return False


def reset_for_tests() -> None:
    global _job, _task
    cancel_probe()
    _job, _task = None, None
