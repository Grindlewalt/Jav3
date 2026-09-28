"""Grounding: turn "the Save button" into a point on a screenshot, with the
vision model the model finder measured best on our own labelled screens.

Contract: docs/navigation-contract.md section C. This file is the stub every
work package codes against; WP3 fills it in. The surface (names, signatures,
exceptions) is frozen by the contract commit.

    desk_click(target="Save button") -> desk.py -> locate(image, w, h, text)
                                                -> Located(x, y, ...) -> act()

Model selection: `settings.grounding_model` if set, else the winner of the
last probe (grounding.json), else NotConfigured. Every call goes through the
gateway (`backend.agent.model.model.complete`) so budgets and the ledger see
it; the key never leaves the host.
"""
from __future__ import annotations

import dataclasses

CONVENTIONS = ("px", "k1000", "unit")


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


async def locate(image: bytes, width: int, height: int, description: str,
                 *, op_id: str | None = None) -> Located | None:
    """Ground `description` on `image` (PNG/JPEG bytes, `width` x `height`).
    Returns None when the model answered but could not find it; raises
    NotConfigured when there is no model to ask."""
    raise NotConfigured("no grounding model configured")


def status() -> dict:
    """What Settings shows: {model, pinned, convention, ranking, probed_at,
    running, job}."""
    return {"model": None, "pinned": "", "convention": None, "ranking": [],
            "probed_at": None, "running": False, "job": None}


def candidates() -> list[dict]:
    """Enabled provider models with vision=true, as
    [{"id": "provider/model", "label", "price_in", "price_out"}]."""
    return []


async def start_probe(models: list[str] | None = None, *, by: str = "") -> str:
    """Run the model finder in the background over `models` (default: every
    candidate) and return a job id; status() reports progress."""
    raise NotConfigured("probe not implemented")
