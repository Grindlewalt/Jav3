"""Run the grounding model finder from a terminal, outside the web server.

    python -m scripts.grounding_probe [--models provider/id ...] [--targets N]
                                      [--state-dir DIR] [--dry-run]

Loads the same settings, provider state and secrets store the server would
(backend.config: JARVIS_CONFIG_DIR / ~/.config/jarvis/{env,secrets.json},
state under JARVIS_STATE_DIR / ~/.local/share/jarvis), lists the image-capable
candidates, asks each one to find every sampled target on the labelled
fixtures (backend/grounding_fixtures.py), then prints the ranking, the misses
and where grounding.json went.

--state-dir DIR writes grounding.json AND the model-call ledger (a fresh
ledger.db) under DIR instead of the live state dir, so a trial run leaves the
running server's ranking and ledger alone. Provider settings and keys are
only ever read.

--dry-run lists candidates and fixtures and calls no model.

Never prints a key: every line goes through a filter that masks any
configured provider key.
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

from backend import grounding, grounding_fixtures as gf, providers
from backend.config import ENV_FILE, settings


def _keys() -> list[str]:
    out = []
    try:
        for p in providers.list_providers(include_models=False):
            k = providers.api_key(p["id"])
            if k and len(k) >= 6:
                out.append(k)
    except Exception:  # noqa: BLE001
        pass
    if settings.deepseek_api_key and len(settings.deepseek_api_key) >= 6:
        out.append(settings.deepseek_api_key)
    return out


_SECRETS: list[str] = []


def say(*parts) -> None:
    line = " ".join(str(p) for p in parts)
    for k in _SECRETS:
        line = line.replace(k, "***")
    print(line, flush=True)


def _why_not(p: dict, m: dict) -> str | None:
    """Why an image-capable model is not a candidate (None = it is)."""
    if not p.get("enabled"):
        return "provider disabled"
    if p.get("needs_base_url"):
        return "provider needs a base_url"
    if p.get("needs_key") and not p.get("key_set"):
        return "no API key"
    if not m.get("enabled"):
        return "model disabled"
    return None


def show_candidates() -> list[dict]:
    cands = grounding.candidates()
    say(f"config dir: {ENV_FILE.parent}  (env file {'present' if ENV_FILE.exists() else 'absent'})")
    say(f"secrets store: {settings.secrets_path}  "
        f"({'present' if settings.secrets_path.exists() else 'absent'})")
    say(f"provider state: {providers._state_path()}")
    say(f"grounding.json: {grounding._path()}")
    say("")
    say(f"candidates ({len(cands)}):")
    for c in cands:
        say(f"  {c['id']:<40} in ${c['price_in']}/M  out ${c['price_out']}/M")
    skipped, off = [], 0
    for p in providers.list_providers(include_models=True):
        for m in p.get("models") or []:
            if not m.get("vision"):
                continue
            why = _why_not(p, m)
            if why == "provider disabled":
                off += 1          # thousands in the catalogue; count, don't list
            elif why:
                skipped.append(f"{p['id']}/{m['id']} ({why})")
    if skipped:
        say(f"image-capable on enabled providers but not candidates ({len(skipped)}):")
        for s in skipped[:30]:
            say("  " + s)
        if len(skipped) > 30:
            say(f"  ... {len(skipped) - 30} more")
    say(f"({off} more image-capable models sit on disabled providers)")
    return cands


def show_fixtures(targets: int) -> None:
    if not gf.HAVE_PIL:
        say("fixtures: Pillow not installed")
        return
    fx = gf.fixtures()
    order = grounding._targets(fx, targets)
    say(f"fixtures: {len(fx)} screens, {sum(len(f['targets']) for f in fx)} targets; "
        f"--targets {targets} samples {len(order)} per model")
    for i, f in enumerate(fx):
        n = sum(1 for fi, _ in order if fi == i)
        say(f"  {i:>2} {f['name']:<22} {f['w']}x{f['h']}  {len(f['targets'])} targets, {n} sampled")


def _table(ranking: list[dict]) -> None:
    say("")
    say(f"{'model':<40} {'hit':>6} {'med px':>7} {'p95 ms':>7} {'$/1k':>7} "
        f"{'conv':>6} {'n':>4} {'err':>4}")
    for r in ranking:
        med = "-" if r["median_px"] is None else f"{r['median_px']:.1f}"
        cost = "-" if r["cost_per_1k"] is None else f"{r['cost_per_1k']:.3f}"
        p95 = "-" if r["p95_ms"] is None else str(r["p95_ms"])
        flag = "  UNUSABLE" if r.get("unusable") else ""
        say(f"{r['model']:<40} {r['hit_rate']:>6.3f} {med:>7} {p95:>7} {cost:>7} "
            f"{r['convention']:>6} {r['n']:>4} {r['errors']:>4}{flag}")
        if r.get("last_error"):
            say(f"    last error: {r['last_error'][:160]}")


def _misses(steps: list[dict], ranking: list[dict], limit: int) -> None:
    conv = {r["model"]: r["convention"] for r in ranking}
    for mid in conv:
        rows = [s for s in steps if s["model"] == mid]
        miss = []
        for s in rows:
            w, h = s["size"]
            pt = None
            if s["answer"] is not None:
                pt = grounding.to_pixels(s["answer"][0], s["answer"][1], conv[mid], w, h)
            hit, err = gf.score(pt, s["box"])
            if not hit:
                miss.append((s, pt, err))
        say("")
        say(f"misses for {mid} ({len(miss)}/{len(rows)}, convention {conv[mid]}):")
        for s, pt, err in miss[:limit]:
            x, y, bw, bh = s["box"]
            where = (f"pointed ({pt[0]},{pt[1]}), {err:.0f}px off" if pt else
                     f"no point ({s['error'][:80]})" if s["error"] else "no point (not found)")
            say(f"  {s['fixture']:<20} '{s['description'][:60]}'  box ({x},{y} {bw}x{bh})  {where}")


def _refine_effect(steps: list[dict], ranking: list[dict]) -> None:
    """With --refine: pass one alone vs with the second pass, per model."""
    conv = {r["model"]: r["convention"] for r in ranking}
    say("")
    for mid, c in conv.items():
        rows = [s for s in steps if s["model"] == mid]
        first = final = fixed = broken = refined = 0
        for s in rows:
            w, h = s["size"]
            hits = []
            for ans in (s.get("first"), s["answer"]):
                pt = None if ans is None else grounding.to_pixels(ans[0], ans[1], c, w, h)
                hits.append(gf.score(pt, s["box"])[0])
            first += hits[0]
            final += hits[1]
            refined += s.get("first") is not s["answer"]
            fixed += hits[1] and not hits[0]
            broken += hits[0] and not hits[1]
        say(f"refine on {mid}: pass one {first}/{len(rows)}, with refine {final}/{len(rows)} "
            f"({refined} refined: {fixed} fixed, {broken} broken)")


async def _ledger_cost(db_path: Path) -> str:
    import aiosqlite
    try:
        async with aiosqlite.connect(db_path) as db:
            async with db.execute("SELECT model, COUNT(*), SUM(input_tokens), "
                                  "SUM(output_tokens) FROM model_calls GROUP BY model") as cur:
                rows = await cur.fetchall()
    except Exception as e:  # noqa: BLE001
        return f"ledger unreadable: {e}"
    out = []
    for mid, n, tin, tout in rows:
        _k, price = providers.price_for(mid)
        usd = ((tin or 0) * price["in"] + (tout or 0) * price["out"]) / 1e6 if price else None
        out.append(f"{mid}: {n} calls, {tin} in / {tout} out tokens"
                   + (f", ${usd:.4f}" if usd is not None else ""))
    return "; ".join(out) or "no calls recorded"


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.grounding_probe")
    ap.add_argument("--models", nargs="+", metavar="provider/id")
    ap.add_argument("--targets", type=int, default=settings.grounding_probe_targets)
    ap.add_argument("--state-dir", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--misses", type=int, default=8, help="misses to show per model")
    ap.add_argument("--refine", action="store_true",
                    help="measure locate()'s zoomed second pass too (twice the calls)")
    a = ap.parse_args(argv)

    _SECRETS[:] = _keys()
    ledger = None
    if a.state_dir:
        a.state_dir.mkdir(parents=True, exist_ok=True)
        grounding.STATE_DIR = a.state_dir
        ledger = a.state_dir / "ledger.db"
        settings.db_path = ledger          # the model-call ledger goes here too
    cands = show_candidates()
    say("")
    show_fixtures(a.targets)
    if a.dry_run:
        return 0
    if not cands:
        say("\nnothing to probe: no enabled image-capable model with a key")
        return 2
    if ledger is not None:
        from backend.db import init_db
        await init_db()

    steps: list[dict] = []
    t0 = time.monotonic()

    def on_step(s):
        steps.append(s)
        n = sum(1 for x in steps if x["model"] == s["model"])
        ans = s["answer"]
        raw = ("err: " + s["error"][:60]) if s["error"] else \
            ("none" if ans is None else f"({ans[0]:g},{ans[1]:g})")
        first = s.get("first")
        if first is not None and first is not ans:
            raw = f"({first[0]:g},{first[1]:g}) refined {raw}"
        say(f"[{len(steps)}] {s['model']} {n}/{a.targets} {s['fixture']}: "
            f"{s['description'][:40]!r} -> {raw} {s['ms'] or '-'}ms")

    say("")
    try:
        ranking = await grounding.run_probe(a.models, targets=a.targets, on_step=on_step,
                                            refine=a.refine)
    except (ValueError, grounding.NotConfigured, gf.FixturesUnavailable) as e:
        say(f"probe refused: {e}")
        return 2
    say(f"\nprobe took {time.monotonic() - t0:.0f}s")
    _table(ranking)
    _misses(steps, ranking, a.misses)
    if a.refine:
        _refine_effect(steps, ranking)
    lat = [s["ms"] for s in steps if s["ms"]]
    if lat:
        say(f"\nlatency: median {statistics.median(lat):.0f} ms over {len(lat)} answers")
    if ledger is not None:
        say("cost (ledger " + str(ledger) + "): " + await _ledger_cost(ledger))
    say(f"grounding.json written: {grounding._path()}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
