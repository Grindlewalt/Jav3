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
MIN_CONFIDENCE = 0.2              # a stated confidence below this is "not found"
EDGE_SLACK_PX = 2                 # px answers may overshoot the image by this much
UNUSABLE_BELOW = 0.5              # hit rate under which a model is never picked
EST_TOKENS_IN = 1100              # one screenshot + the prompt
EST_TOKENS_OUT = 25               # the JSON answer; DeepSeek Flash with thinking
                                  # off (GROUNDING_EXTRA_DEEPSEEK): 18-21, median
                                  # 20 over 107 asks (it was ~98 thinking)
# Output cap per ask. It must cover a reasoning model's thinking as well as the
# answer: DeepSeek V4.1 Flash thinks for 35-65 tokens before it answers, and at
# the old cap of 64 a third of its replies were cut off mid-JSON (measured on
# the test box, 2026-09-27: 15/40 "no point", every one truncated). A model that
# does not think stops at ~25 tokens anyway, so a generous cap costs nothing.
# 512 still cut off the odd hard target (a calendar day took ~1000 tokens of
# thinking and was then right), hence 1536: a few seconds, well inside
# grounding_timeout_s, and ~$0.001 at Flash prices when it happens.
MAX_TOKENS = 1536
# Request fields sent with every grounding ask to a DeepSeek model (and only
# DeepSeek): thinking off. Pointing at a button needs no reasoning, and the
# thinking is what ran past MAX_TOKENS and set the p95 latency. Measured on the
# test box, 2026-09-28, 107 targets: hit rate 0.953 either way, p95 2484 ->
# 1388 ms, output 10520 -> 2119 tokens, no answer cut off. {} turns it back on.
GROUNDING_EXTRA_DEEPSEEK: dict = {"thinking": {"type": "disabled"}}


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


def reject_reason(rx: float, ry: float, conf: float | None, convention: str,
                  width: int, height: int) -> str | None:
    """Why a raw answer must be treated as NOT FOUND instead of clamped and
    clicked, or None when it is acceptable."""
    if rx < 0 or ry < 0:
        return "negative coordinates"
    if convention == "px":
        if rx > width + EDGE_SLACK_PX or ry > height + EDGE_SLACK_PX:
            return "outside the image for px"
    elif convention == "k1000":
        if rx > 1000 or ry > 1000:
            return "outside 0-1000 for k1000"
    elif convention == "unit":
        if rx > 1.0 or ry > 1.0:
            return "outside 0-1 for unit"
    if conf is not None and conf < MIN_CONFIDENCE:
        return f"confidence {conf:g} below {MIN_CONFIDENCE:g}"
    if rx == 0 and ry == 0:
        x, y = to_pixels(rx, ry, convention, width, height)
        if x <= EDGE_SLACK_PX and y <= EDGE_SLACK_PX:
            return "answer (0,0) is the top-left corner"
    return None


def checked_pixels(ans, convention: str, width: int, height: int) -> tuple[int, int] | None:
    """to_pixels for a raw (rx, ry, conf) answer, or None when it is rejected
    (or `ans` is None). The probe scores through this so it counts misses."""
    if ans is None or reject_reason(ans[0], ans[1], ans[2], convention, width, height):
        return None
    return to_pixels(ans[0], ans[1], convention, width, height)


# --- state (grounding.json) ------------------------------------------------------

# Where grounding.json lives when set (scripts/grounding_probe.py --state-dir
# points a trial run at a scratch dir so the live ranking is left alone).
STATE_DIR: Path | None = None


def _path() -> Path:
    if STATE_DIR is not None:
        return Path(STATE_DIR) / "grounding.json"
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

# With refine on, pass one also asks for the element's size, which gates the
# second pass. Measured on Flash it costs accuracy of its own: asking for the
# size makes it think longer, and 5/107 answers ran out of MAX_TOKENS in
# thought (pass one 99/107 against 102/107 with PROMPT).
PROMPT_SIZED = (
    "You are a GUI grounding model. The screenshot is {w}x{h} pixels. Find the "
    "one on-screen element described below and give the point to click: the "
    "CENTRE of the element's box, in pixels of this image, measured from the "
    "top-left corner, and the box's width and height in pixels.\n"
    "Answer with ONE JSON object and nothing else:\n"
    '{{"x": <int>, "y": <int>, "w": <int>, "h": <int>, "confidence": <0..1>}}\n'
    'If the element is not visible, answer {{"x": null, "y": null, "confidence": 0}}.\n\n'
    "Element: {description}")

SYSTEM = "You locate UI elements on screenshots. Reply with JSON only."


ZOOM_NOTE = (
    "This image is a {scale}x enlargement of a small part of a larger screenshot, "
    "cut around where the element probably is. It may be off-centre or cut by "
    "the edge. Answer in pixels of THIS image.\n")


def _messages(image: bytes, width: int, height: int, description: str,
              zoom: int = 0, sized: bool = False) -> list[dict]:
    from .agent import imageresult
    mime = imageresult.sniff(image)
    if mime is None:
        raise ValueError("not an image (PNG, JPEG, WebP or GIF bytes expected)")
    b64 = base64.b64encode(image).decode()
    text = (PROMPT_SIZED if sized else PROMPT).format(
        w=width, h=height, description=description.strip()[:300])
    if zoom:
        text = ZOOM_NOTE.format(scale=zoom) + text
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


def parse_size(text: str) -> tuple[float, float] | None:
    """The element's (w, h) from the first JSON object that has both, raw in
    the model's convention; None when it gave none."""
    for m in re.finditer(r"\{[^{}]*\}", text or ""):
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        w, h = obj.get("w", obj.get("width")), obj.get("h", obj.get("height"))
        try:
            w, h = float(w), float(h)
        except (TypeError, ValueError):
            continue
        if w > 0 and h > 0:
            return w, h
    return None


def _conf(v) -> float:
    try:
        c = float(v)
    except (TypeError, ValueError):
        return 0.5
    if c > 1.0 and c <= 100.0:
        c /= 100.0
    return max(0.0, min(1.0, c))


def _extra_kw(model_id: str) -> dict:
    """{"extra": GROUNDING_EXTRA_DEEPSEEK} for a DeepSeek model, else {}: other
    providers may reject a field they do not know."""
    try:   # a bare id runs on the default provider, as in providers.resolve
        provider, _ = providers.split_id(providers.canonical(model_id))
    except providers.ProviderError:
        return {}
    if provider == "deepseek" and GROUNDING_EXTRA_DEEPSEEK:
        return {"extra": GROUNDING_EXTRA_DEEPSEEK}
    return {}


async def _ask(model_id: str, image: bytes, width: int, height: int,
               description: str, op_id: str | None,
               zoom: int = 0, sized: bool = False
               ) -> tuple[tuple | None, int, tuple | None]:
    """One gateway call, drained. (parsed answer or None, latency ms, raw
    element size or None). Raises whatever the gateway raises, or
    asyncio.TimeoutError."""
    from .agent.model import model
    messages = _messages(image, width, height, description, zoom, sized)

    async def drain() -> str:
        parts = []
        async for ev in model.complete(messages, model_name=model_id, op_id=op_id,
                                       temperature=0, max_tokens=MAX_TOKENS,
                                       **_extra_kw(model_id)):
            if ev.get("type") == "message":
                parts.append(ev.get("content") or "")
        return "".join(parts)

    t0 = time.monotonic()
    text = await asyncio.wait_for(drain(), timeout=settings.grounding_timeout_s)
    return (parse_answer(text), int((time.monotonic() - t0) * 1000),
            parse_size(text))


# --- two-pass refine ------------------------------------------------------------------
#
# The first answer is usually on the right element or one neighbour away: the
# misses measured on DeepSeek V4.1 Flash were all 22-px glyph icons in a row,
# pointed one or two icons off. The second pass cuts REFINE_CROP around the
# first point, enlarges it REFINE_SCALE times (Pillow) and asks again; the
# answer maps back into the full image. If the second pass fails, finds
# nothing or Pillow is missing, the first answer stands.
#
# Only for SMALL elements (the first pass's own w/h, both <= REFINE_MAX_SIDE):
# refining everything was measured worse (0.953 -> 0.907 on 107 targets) —
# a 520-px text field does not fit a 320-px crop, and without its surroundings
# the model points at the label above it. Gated, it fixed 3 and broke 1 of
# 38 refined, but pass one needs PROMPT_SIZED for the gate and that prompt
# lost more than refine won back (0.944 vs 0.953, p95 7.0 s vs 2.5 s, 1.8x
# the tokens), so it is off by default: locate(refine=True) or
# `scripts/grounding_probe.py --refine` to measure it on another model.

REFINE = False                    # locate()'s default (measured: see above)
REFINE_CROP = (320, 200)          # px of the image passed to locate()
REFINE_SCALE = 4
REFINE_MAX_SIDE = 48              # px; larger (or unsized) elements keep pass one


def _small(size: tuple | None, conv: str, width: int, height: int) -> bool:
    """Whether a first-pass element size (raw, in `conv`) is small enough to
    refine."""
    if not size:
        return False
    if conv == "k1000":
        w, h = size[0] * width / 1000.0, size[1] * height / 1000.0
    elif conv == "unit":
        w, h = size[0] * width, size[1] * height
    else:
        w, h = size
    return 0 < w <= REFINE_MAX_SIDE and 0 < h <= REFINE_MAX_SIDE


def _zoom(image: bytes, width: int, height: int, x: int, y: int
          ) -> tuple[bytes, int, int, int, int] | None:
    """(png of the enlarged crop, x0, y0, crop w, crop h) in the coordinates
    of a `width` x `height` image, around (x, y); None without Pillow or for
    bytes Pillow cannot read."""
    try:
        import io
        from PIL import Image
    except ImportError:
        return None
    try:
        im = Image.open(io.BytesIO(image))
        im.load()
    except Exception:  # noqa: BLE001 — not decodable: skip the refine
        return None
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    cw, ch = min(REFINE_CROP[0], width), min(REFINE_CROP[1], height)
    x0 = max(0, min(width - cw, x - cw // 2))
    y0 = max(0, min(height - ch, y - ch // 2))
    # the bytes may not be exactly width x height (a client that sent a
    # scaled capture); crop in the image's own pixels
    sx, sy = im.width / width, im.height / height
    crop = im.crop((int(x0 * sx), int(y0 * sy), int((x0 + cw) * sx), int((y0 + ch) * sy)))
    crop = crop.resize((cw * REFINE_SCALE, ch * REFINE_SCALE), Image.LANCZOS)
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return buf.getvalue(), x0, y0, cw, ch


async def _refine(model_id: str, conv: str, image: bytes, width: int, height: int,
                  x: int, y: int, description: str, op_id: str | None
                  ) -> tuple[int, int, float, int] | None:
    """Second pass around (x, y): (x, y, confidence, ms) in image pixels, or
    None (no Pillow, not found in the crop, or an answer outside it). Raises
    what _ask raises."""
    z = _zoom(image, width, height, x, y)
    if z is None:
        return None
    png, x0, y0, cw, ch = z
    zw, zh = cw * REFINE_SCALE, ch * REFINE_SCALE
    ans, ms, _size = await _ask(model_id, png, zw, zh, description, op_id,
                                zoom=REFINE_SCALE)
    if ans is None:
        return None
    rx, ry, conf = ans
    zx, zy = to_pixels(rx, ry, conv, zw, zh)
    return (max(0, min(width - 1, x0 + int(round(zx / REFINE_SCALE)))),
            max(0, min(height - 1, y0 + int(round(zy / REFINE_SCALE)))), conf, ms)


def _to_raw(x: int, y: int, convention: str, width: int, height: int) -> tuple[float, float]:
    """The inverse of to_pixels (for the probe's refined answers)."""
    if convention == "k1000":
        return x * 1000.0 / width, y * 1000.0 / height
    if convention == "unit":
        return x / width, y / height
    return float(x), float(y)


async def locate(image: bytes, width: int, height: int, description: str,
                 *, op_id: str | None = None, refine: bool | None = None) -> Located | None:
    """Ground `description` on `image` (PNG/JPEG bytes, `width` x `height`).
    Returns None when the model answered but could not find it, or the call
    failed or timed out (logged); raises NotConfigured when there is no model
    to ask. A spent budget (BudgetExceeded) propagates like any model call.
    `refine` (default REFINE) asks pass one for the element's size too and
    adds the zoomed second pass for small elements; its latency is included
    in latency_ms."""
    model_id, conv = _resolve()
    from .agent import budget as budget_mod
    try:
        do_refine = REFINE if refine is None else refine
        ans, ms, size = await _ask(model_id, image, width, height, description, op_id,
                                   sized=do_refine)
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
    why = reject_reason(rx, ry, conf, conv, width, height)
    if why:
        log.info("grounding: %s answer (%s, %s, conf %s) rejected as not found: %s "
                 "(convention %s)", model_id, rx, ry, conf, why, conv)
        return None
    x, y = to_pixels(rx, ry, conv, width, height)
    if do_refine and _small(size, conv, width, height):
        try:
            r = await _refine(model_id, conv, image, width, height, x, y,
                              description, op_id)
        except budget_mod.BudgetExceeded:
            raise
        except Exception as e:  # noqa: BLE001 — incl. timeout: the first answer stands
            log.info("grounding: refine on %s failed: %s", model_id, str(e)[:200] or
                     type(e).__name__)
            r = None
        if r is not None:
            x, y, conf, ms2 = r
            ms += ms2
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
            pt = checked_pixels(ans, conv, w, h)
            if pt is None:
                continue
            hit, err = score(pt, box)
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
                     order: list[tuple[int, int]], on_step=None,
                     refine: bool = False) -> dict:
    """Ask `model_id` every target in `order`; the ranking row. `on_step`, if
    given, is called after each target with a dict {model, fixture,
    description, box, size, answer (raw rx, ry, conf or None), first (the
    first-pass answer; differs from answer only with refine), ms, error}.

    refine=True measures what locate() does with its second pass: the refined
    point is handed back in the model's stored convention (px when it has
    none), so convention detection only works on a model already probed."""
    from .agent import budget as budget_mod
    answers, boxes, sizes, lat = [], [], [], []
    errors, last_error = 0, None
    conv = DEFAULT_CONVENTION
    if refine:
        e = _entry(_load(), model_id)
        if e and e.get("convention") in CONVENTIONS:
            conv = e["convention"]
    for fi, ti in order:
        f = fixtures[fi]
        t = f["targets"][ti]
        job["current"] = f"{model_id} · {f['name']}"
        ans, first, ms, err = None, None, None, None
        try:
            ans, ms, size = await _ask(model_id, f["png"], f["w"], f["h"],
                                       t["description"], None, sized=refine)
            first = ans
            if refine and ans is not None and _small(size, conv, f["w"], f["h"]):
                x, y = to_pixels(ans[0], ans[1], conv, f["w"], f["h"])
                try:
                    r = await _refine(model_id, conv, f["png"], f["w"], f["h"], x, y,
                                      t["description"], None)
                except (asyncio.CancelledError, budget_mod.BudgetExceeded):
                    raise
                except Exception:  # noqa: BLE001 — as in locate(): the first answer stands
                    r = None
                if r is not None:
                    ans = (*_to_raw(r[0], r[1], conv, f["w"], f["h"]), r[2])
                    ms += r[3]
            lat.append(ms)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            err = f"timed out after {settings.grounding_timeout_s:.0f}s"
        except budget_mod.BudgetExceeded as e:
            err = str(e)[:200]
        except Exception as e:  # noqa: BLE001 — a refused image is a miss, not a crash
            err = str(e)[:200] or type(e).__name__
        if err is not None:
            errors += 1
            last_error = err
        answers.append(ans)
        boxes.append(t["box"])
        sizes.append((f["w"], f["h"]))
        job["done"] += 1
        if on_step is not None:
            on_step({"model": model_id, "fixture": f["name"],
                     "description": t["description"], "box": tuple(t["box"]),
                     "size": (f["w"], f["h"]), "answer": ans, "first": first,
                     "ms": ms, "error": err})
    return score_model(model_id, answers, boxes, sizes, lat, errors, last_error)


async def run_probe(models: list[str] | None = None, *, targets: int | None = None,
                    on_step=None, save: bool = True, refine: bool = False) -> list[dict]:
    """The model finder in the foreground (scripts/grounding_probe.py): probe
    `models` (default: every candidate) on the first `targets` targets
    (default settings.grounding_probe_targets), write the ranking to
    grounding.json (keeping the pin) unless save=False, and return it. Same
    scoring as start_probe; no job, no security event. ValueError for a model
    that is not a candidate, NotConfigured when there is nothing to test,
    grounding_fixtures.FixturesUnavailable without Pillow."""
    from . import grounding_fixtures as gf
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
    fixtures = gf.fixtures()
    order = _targets(fixtures, settings.grounding_probe_targets if targets is None else targets)
    job = {"done": 0, "current": None}
    rows = [await _probe_one(job, mid, fixtures, order, on_step, refine) for mid in chosen]
    ranking = rank(rows)
    if save:
        st = _load()
        st["ranking"] = ranking
        st["probed_at"] = _now()
        _save(st)
    return ranking


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
