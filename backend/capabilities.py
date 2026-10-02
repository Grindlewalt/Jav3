"""What this host can do for a project right now, as the planner needs to know it.

A plan once held four items whose proof was a browser check on a host that could
not do it: services refused (boxes off, then a profile that does not allow them),
the screenshot tool needed the `desktop` image variant, and no browser tab was
open. The items found out one at a time, after the plan had started. This module
asks the same checks the tools themselves make, once, before the plan is written:

  services   boxes on (vm_boxes_enabled) + the profile allows services
  desktop    the project's box can run the `desktop` image: a KVM box (Docker has
             no image of it), on that variant or one built from it, built here
  packages   boxes on + the profile allows package requests
  browser    a browser extension connected, not paused, granted to the project
             (whether a Jav3 tab is open is only known to browser_list_tabs, which
             is a call into the operator's browser: not made from here)
  desk       a computer (or the box's desktop) connected for computer use
  internet   the profile's network: none / deny-by-default (new sites queue for
             the operator) / allow-by-default

It invents no policy: every verdict is the one the tool's own refusal comes from
(vm/services.file_request, packages.file_request, screenshot's NEEDS_DESKTOP,
vm/display_api.unsupported, browser.act, profiles.setup_choices). A check that
cannot run says so (`ok` None) and never blocks planning.

`for_project(slug)` -> {name: {"ok": True|False|None, "note": str}};
`block(caps)` is the "this host can / cannot" text; `item_warnings(items, caps)`
the warnings for planned items whose words need something missing.
"""
import json
import re

from .config import settings

CAPS = ("services", "desktop", "packages", "browser", "desk", "internet")

LABELS = {
    "services": "services (service_request)",
    "desktop": "the desktop image: screenshots, headless chromium, GUI apps (screenshot)",
    "packages": "package requests (package_request)",
    "browser": "the operator's browser (browser_* tools)",
    "desk": "computer use (desk_* tools)",
    "internet": "internet",
}
SHORT = {"services": "services", "desktop": "the desktop image", "packages": "package requests",
         "browser": "the browser extension", "desk": "computer use", "internet": "internet"}


def _yes(note: str) -> dict:
    return {"ok": True, "note": note}


def _no(note: str) -> dict:
    return {"ok": False, "note": note}


# --- the checks ----------------------------------------------------------------

def _services(prof: dict | None) -> dict:
    from .vm import boxes
    if not settings.vm_boxes_enabled:
        return _no("service boxes are off on this host (vm_boxes_enabled)")
    if not prof or not prof.get("allow_services"):
        return _no("this project's security profile does not allow services")
    if prof.get("service_placement") not in boxes.PLACEMENTS:
        return _no("this project's security profile has no service placement set")
    return _yes("a service_request is filed and the operator approves it")


async def _is_desktop_variant(variant: str) -> bool:
    """The `desktop` variant itself, or one built from it (vm/display_api's test)."""
    if variant == "desktop":
        return True
    from .db import get_db
    from .vm import images
    db = await get_db()
    try:
        return variant in await images.descendants(db, "desktop")
    finally:
        await db.close()


async def _desktop(slug: str) -> dict:
    from .vm import boxes, images, placement
    if not boxes.enabled():
        return _no("boxes are off on this host (vm_boxes_enabled): every project runs in the "
                   "shared box, whose image has no desktop")
    eff = await placement.effective(slug)
    if eff["mode"] == "shared":
        return _no("the project runs in the shared box (image `main`), which has no desktop; "
                   "its Runs in setting needs a KVM box with the `desktop` image")
    if eff["mode"] == "join":
        b = boxes.get(eff["box_id"])
        runtime, variant = (b.runtime, b.image[0]) if b is not None else ("kvm", "main")
    else:
        runtime, variant = eff.get("runtime") or "kvm", eff.get("image") or "main"
    if runtime == "docker":
        return _no("the project runs in a Docker box, and the desktop image is a KVM layer "
                   "(no Docker image of it): its Runs in setting needs a KVM box")
    if not await _is_desktop_variant(variant):
        return _no(f"the project's box runs the `{variant}` image, which has no chromium or "
                   "Xvfb: it needs the `desktop` image (Runs in)")
    if images.active_version(variant) is None:
        return _no(f"the `{variant}` image is not built on this host")
    return _yes(f"the project's KVM box runs the `{variant}` image")


def _packages(prof: dict | None) -> dict:
    if not settings.vm_boxes_enabled:
        return _no("package requests need boxes, which are off on this host (vm_boxes_enabled)")
    if not prof or not prof.get("allow_package_requests"):
        return _no("this project's security profile does not allow package requests")
    return _yes("the operator approves each request; it lands with a later image version, "
                "not in the same run")


async def _browser(slug: str) -> dict:
    from . import browser
    bs = browser.connected()
    if not bs:
        return _no("no browser extension is connected")
    names = []
    for b in bs:
        if b.paused:
            continue
        g = await browser.grant_for(b.device_id, slug)
        if g["read"]:
            names.append(f"{b.name} ({'read and act' if g['act'] else 'read only'})")
    if not names:
        return _no("a browser is connected but paused, or not granted to this project "
                   "(Settings, Browser use)")
    return _yes(", ".join(names) + "; an open Jav3 tab is needed, browser_list_tabs shows them")


def _desk() -> dict:
    from . import desk
    if not desk.offered():
        return _no("no computer is connected for computer use")
    names = ", ".join(d.name for d in desk.connected()[:3])
    return _yes(f"connected: {names}")


def _internet(prof: dict | None) -> dict:
    if not prof:
        return {"ok": None, "note": "no security profile to read"}
    from . import profiles
    net = profiles.setup_choices(prof)["network"]
    if net == "off":
        return _no("the project's profile has no network at all: nothing downloads, installs "
                   "or is fetched")
    if net == "allow":
        return _yes("open by default: new sites are allowed")
    try:
        n = len(json.loads(prof.get("allow_hosts") or "[]"))
    except (TypeError, ValueError):
        n = 0
    return _yes(f"deny by default: a new site waits for the operator's approval ({n} hosts "
                "allowed already), so work that needs a new host can stall")


async def for_project(slug: str) -> dict[str, dict]:
    """The capabilities of project `slug` on this host, now. Never raises: a check
    that cannot run is {"ok": None}."""
    prof = None
    try:
        from .vm import boxes
        prof = await boxes.project_profile(slug)
    except Exception:  # noqa: BLE001 — no profile: the profile checks say "no"
        pass
    out: dict[str, dict] = {}
    for name in CAPS:
        try:
            if name == "services":
                out[name] = _services(prof)
            elif name == "desktop":
                out[name] = await _desktop(slug)
            elif name == "packages":
                out[name] = _packages(prof)
            elif name == "browser":
                out[name] = await _browser(slug)
            elif name == "desk":
                out[name] = _desk()
            else:
                out[name] = _internet(prof)
        except Exception as e:  # noqa: BLE001 — a probe is advice, never a failure
            out[name] = {"ok": None, "note": f"not checked ({type(e).__name__})"}
    return out


# --- what the planner and the orchestrator read -----------------------------------

def block(caps: dict[str, dict]) -> str:
    """The "this host can / cannot" text. Capabilities that could not be checked
    are left out: saying nothing is better than guessing."""
    can = [f"- {LABELS[k]}: {v['note']}" for k, v in caps.items() if v.get("ok") is True]
    cannot = [f"- {LABELS[k]}: {v['note']}" for k, v in caps.items() if v.get("ok") is False]
    if not can and not cannot:
        return ""
    lines = ["# What this host can and cannot do (checked now)"]
    if can:
        lines += ["Can:", *can]
    if cannot:
        lines += ["Cannot:", *cannot]
    return "\n".join(lines)


# the words of a planned item that need a capability. Each entry: (words, needs any
# of these capabilities). Deliberately narrow: a warning is advice, and "browser" in
# a brief about a web page's CSS is not a request to drive one.
_BROWSER_WORDS = re.compile(
    r"browser[_ -](?:verif\w*|test\w*|check\w*|screenshot\w*|click\w*|read\w*|navigat\w*|tab\w*)"
    r"|\bbrowser_\w+|\b(?:in|with|using) (?:a |the |your )?(?:real |actual |live )?(?:web )?browser\b"
    r"|\b(?:open|load|view)\w* (?:it |the \w+ )?in (?:a |the )?(?:web )?browser\b"
    r"|\bscreen ?shots?\b", re.I)
_DESKTOP_WORDS = re.compile(
    r"\bxvfb\b|\bchromium\b|\bheadless (?:chrom\w*|browser)\b|\bplaywright\b|\bpuppeteer\b"
    r"|\bselenium\b", re.I)
_SERVICE_WORDS = re.compile(
    r"\bservice[_ ]request\b|\b(?:long[- ]running|persistent|background) service\b"
    r"|\bsystemd\b|\bdocker[- ]compose\b", re.I)
_DESK_WORDS = re.compile(r"\bdesk_\w+|\bcomputer[- ]use\b", re.I)
_PACKAGE_WORDS = re.compile(r"\bpackage_request\b", re.I)
_NET_WORDS = re.compile(
    r"\b(?:pip3?|npm|yarn|pnpm|cargo|gem) (?:install|i|add)\b|\bapt(?:-get)? install\b"
    r"|\bgit clone\b|\bcurl\b|\bwget\b|\bdownload\w*\b", re.I)

# (words, capabilities of which ONE is enough, what to call the need)
_NEEDS = (
    (_BROWSER_WORDS, ("browser", "desktop"), "a browser or screenshot"),
    (_DESKTOP_WORDS, ("desktop",), "the desktop image"),
    (_SERVICE_WORDS, ("services",), "a service"),
    (_DESK_WORDS, ("desk",), "computer use"),
    (_PACKAGE_WORDS, ("packages",), "a package request"),
    (_NET_WORDS, ("internet",), "the internet"),
)


def item_warnings(items: list[dict], caps: dict[str, dict]) -> list[str]:
    """One line per planned item whose title or brief clearly needs a capability this
    host lacks (every capability that would do is checked and says no). Items run on
    this host, under this project's profile, so such an item will block or fail."""
    out = []
    for it in items:
        text = f"{it.get('title') or ''}\n{it.get('brief') or ''}"
        for words, any_of, what in _NEEDS:
            m = words.search(text)
            if m is None:
                continue
            checked = [caps.get(c) or {} for c in any_of]
            if not all(c.get("ok") is False for c in checked):
                continue
            why = "; ".join(f"{SHORT[c]}: {caps[c]['note']}" for c in any_of)
            out.append(f"{it['id']} \"{(it.get('title') or '')[:60]}\" needs {what} "
                       f"(\"{m.group(0).strip()[:40]}\") and this host has none: {why}. Plan "
                       "its written work apart from the proof that needs it (the proof "
                       "becomes a command for the operator to run), or drop it.")
    return out
