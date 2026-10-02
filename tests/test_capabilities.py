"""What this host can do for a project, asked before the plan is written (benchmark-
game run, conv 500: four browser-verified items on a host whose services were
refused, whose screenshot tool needed the `desktop` image and which had no tab
open: found out one item at a time, after the plan had started).

The probe reuses the checks the tools' own refusals come from; here the underlying
state is substituted (settings, the profile row, the placement, the image table, the
browser and desk registries)."""
import json
import types

from backend import browser, capabilities, desk, runtime
from backend import plan as plan_mod
from backend.config import settings
from backend.vm import boxes, images, placement
from tests.test_operator_orchestrate import _handler
from tests.test_plan import SLUG, _put, client  # noqa: F401

PROFILE = {"allow_services": 1, "service_placement": "per_project", "allow_package_requests": 1,
           "default_verdict": "deny", "network_off": 0, "separate_box": 1, "box_runtime": "kvm",
           "box_image": "desktop", "allow_hosts": json.dumps(["pypi.org", "github.com"])}
OWN_DESKTOP = {"mode": "own", "source": "project", "box_id": "p-alpha", "runtime": "kvm",
               "image": "desktop", "mem_mb": None, "owner": "alpha"}


def _host(monkeypatch, *, boxes_on=True, prof=PROFILE, eff=OWN_DESKTOP, built=True,
          desktop_variants=("desktop",), browsers=(), grants=None, desks=()):
    """Substitute what the probe reads. `browsers`: [(name, paused)]; `grants`:
    {"read": bool, "act": bool}; `desks`: names of connected computers."""
    monkeypatch.setattr(settings, "vm_boxes_enabled", boxes_on)

    async def profile(slug):
        return dict(prof) if prof is not None else None

    async def effective(slug):
        return dict(eff)

    async def is_desktop(variant):
        return variant in desktop_variants

    monkeypatch.setattr(boxes, "project_profile", profile)
    monkeypatch.setattr(placement, "effective", effective)
    monkeypatch.setattr(capabilities, "_is_desktop_variant", is_desktop)
    monkeypatch.setattr(images, "active_version", lambda variant: 3 if built else None)
    monkeypatch.setattr(browser, "connected", lambda: [
        browser.Browser(device_id=i, name=n, ws=None, paused=p)
        for i, (n, p) in enumerate(browsers, 1)])

    async def grant_for(device_id, project):
        return grants if grants is not None else {"read": True, "act": True}

    monkeypatch.setattr(browser, "grant_for", grant_for)
    monkeypatch.setattr(desk, "offered", lambda: bool(desks))
    monkeypatch.setattr(desk, "connected", lambda: [types.SimpleNamespace(name=n) for n in desks])


def _no(caps, name):
    assert caps[name]["ok"] is False, caps[name]
    return caps[name]["note"]


# --- the probe ---------------------------------------------------------------------

async def test_a_fully_equipped_host_can_do_everything(monkeypatch):
    _host(monkeypatch, browsers=[("Grant's Mac", False)], desks=["Mac"])
    caps = await capabilities.for_project(SLUG)
    assert {k: v["ok"] for k, v in caps.items()} == dict.fromkeys(capabilities.CAPS, True)
    assert "Grant's Mac (read and act)" in caps["browser"]["note"]
    assert "2 hosts allowed" in caps["internet"]["note"]


async def test_the_host_of_run_500_cannot_run_services_a_desktop_or_a_browser(monkeypatch):
    prof = {**PROFILE, "allow_services": 0, "allow_package_requests": 0, "box_image": "main"}
    _host(monkeypatch, boxes_on=False, prof=prof)
    caps = await capabilities.for_project(SLUG)
    assert "vm_boxes_enabled" in _no(caps, "services")
    assert "vm_boxes_enabled" in _no(caps, "desktop") and "no desktop" in caps["desktop"]["note"]
    assert "vm_boxes_enabled" in _no(caps, "packages")
    assert "no browser extension" in _no(caps, "browser")
    assert "no computer" in _no(caps, "desk")
    assert caps["internet"]["ok"] is True


async def test_a_profile_that_does_not_allow_services_or_packages_is_the_reason(monkeypatch):
    _host(monkeypatch, prof={**PROFILE, "allow_services": 0, "allow_package_requests": 0})
    caps = await capabilities.for_project(SLUG)
    assert "profile does not allow services" in _no(caps, "services")
    assert "profile does not allow package requests" in _no(caps, "packages")
    _host(monkeypatch, prof={**PROFILE, "service_placement": None})
    assert "no service placement" in _no(await capabilities.for_project(SLUG), "services")
    _host(monkeypatch, prof=None)
    caps = await capabilities.for_project(SLUG)
    assert caps["services"]["ok"] is False and caps["internet"]["ok"] is None


async def test_the_desktop_needs_a_built_desktop_image_on_a_kvm_box(monkeypatch):
    shared = {**OWN_DESKTOP, "mode": "shared", "box_id": "shared", "runtime": None, "image": None}
    _host(monkeypatch, eff=shared)
    assert "shared box" in _no(await capabilities.for_project(SLUG), "desktop")
    _host(monkeypatch, eff={**OWN_DESKTOP, "runtime": "docker"})
    assert "KVM layer" in _no(await capabilities.for_project(SLUG), "desktop")
    _host(monkeypatch, eff={**OWN_DESKTOP, "image": "main"})
    assert "`main` image" in _no(await capabilities.for_project(SLUG), "desktop")
    _host(monkeypatch, built=False)
    assert "not built" in _no(await capabilities.for_project(SLUG), "desktop")
    # a variant built from desktop counts, as it does for the live desktop
    _host(monkeypatch, eff={**OWN_DESKTOP, "image": "dev"}, desktop_variants=("desktop", "dev"))
    assert (await capabilities.for_project(SLUG))["desktop"]["ok"] is True


async def test_a_joined_box_is_judged_by_its_own_runtime_and_image(monkeypatch):
    joined = {**OWN_DESKTOP, "mode": "join", "box_id": "p-beta"}
    _host(monkeypatch, eff=joined)
    monkeypatch.setattr(boxes, "get", lambda box_id: types.SimpleNamespace(
        runtime="docker", image=("main", None)))
    assert "KVM layer" in _no(await capabilities.for_project(SLUG), "desktop")
    monkeypatch.setattr(boxes, "get", lambda box_id: types.SimpleNamespace(
        runtime="kvm", image=("desktop", None)))
    assert (await capabilities.for_project(SLUG))["desktop"]["ok"] is True


async def test_a_browser_counts_only_when_connected_running_and_granted(monkeypatch):
    _host(monkeypatch, browsers=[("Mac", True)])
    assert "paused" in _no(await capabilities.for_project(SLUG), "browser")
    _host(monkeypatch, browsers=[("Mac", False)], grants={"read": False, "act": False})
    assert "not granted" in _no(await capabilities.for_project(SLUG), "browser")
    _host(monkeypatch, browsers=[("Mac", False)], grants={"read": True, "act": False})
    note = (await capabilities.for_project(SLUG))["browser"]["note"]
    assert "Mac (read only)" in note and "browser_list_tabs" in note


async def test_the_networks_a_profile_can_have(monkeypatch):
    _host(monkeypatch, prof={**PROFILE, "network_off": 1})
    assert "no network at all" in _no(await capabilities.for_project(SLUG), "internet")
    _host(monkeypatch, prof={**PROFILE, "default_verdict": "allow"})
    assert "open by default" in (await capabilities.for_project(SLUG))["internet"]["note"]
    _host(monkeypatch)
    assert "approval" in (await capabilities.for_project(SLUG))["internet"]["note"]


async def test_a_probe_that_fails_is_unknown_and_never_stops_the_rest(monkeypatch):
    _host(monkeypatch)

    async def boom(slug):
        raise RuntimeError("db gone")
    monkeypatch.setattr(placement, "effective", boom)
    monkeypatch.setattr(browser, "connected", lambda: 1 / 0)
    caps = await capabilities.for_project(SLUG)
    assert caps["desktop"]["ok"] is None and "RuntimeError" in caps["desktop"]["note"]
    assert caps["browser"]["ok"] is None and caps["services"]["ok"] is True
    monkeypatch.setattr(boxes, "project_profile", boom)
    caps = await capabilities.for_project(SLUG)           # no profile row to read
    assert caps["services"]["ok"] is False and caps["internet"]["ok"] is None


# --- what the planner and the orchestrator read ---------------------------------------

def test_the_block_says_what_the_host_can_and_cannot_do_and_skips_the_unknown():
    caps = {"services": {"ok": False, "note": "service boxes are off"},
            "desktop": {"ok": None, "note": "not checked (OSError)"},
            "browser": {"ok": True, "note": "Mac (read and act)"}}
    text = capabilities.block(caps)
    assert text.startswith("# What this host can and cannot do (checked now)")
    assert "Can:\n- the operator's browser (browser_* tools): Mac (read and act)" in text
    assert "Cannot:\n- services (service_request): service boxes are off" in text
    assert "desktop" not in text
    assert capabilities.block({"desktop": {"ok": None, "note": "x"}}) == ""


NONE = {k: {"ok": False, "note": f"no {k}"} for k in capabilities.CAPS}


def _items(*pairs):
    return [{"id": f"i{n}", "title": t, "brief": b} for n, (t, b) in enumerate(pairs, 1)]


def test_items_that_need_a_missing_browser_screenshot_or_service_are_flagged():
    items = _items(
        ("Browser-verified score display", "open index.html and check it"),
        ("Check the layout", "Take a screenshot of the page at 1280 wide"),
        ("Verify in a browser", "load the page in the browser and click Start"),
        ("Run the stack", "file a service_request for the API"),
        ("Visual check", "run playwright against the dev server"),
        ("Drive the app", "use desk_click to press Play"),
        ("Install deps", "pip install pygame, then git clone the assets"),
        ("Ask for a lib", "call package_request for ffmpeg"))
    got = {w.split()[0]: w for w in capabilities.item_warnings(items, NONE)}
    assert sorted(got) == ["i1", "i2", "i3", "i4", "i5", "i6", "i7", "i8"]
    assert "needs a browser or screenshot" in got["i1"] and '"Browser-verified' in got["i1"]
    assert "the browser extension: no browser" in got["i1"] and "the desktop image: no desktop" in got["i1"]
    assert "needs a service" in got["i4"] and "needs the desktop image" in got["i5"]
    assert "needs computer use" in got["i6"] and "needs the internet" in got["i7"]
    assert "needs a package request" in got["i8"]
    assert "proof becomes a command" in got["i1"]


def test_an_item_is_not_flagged_when_any_capability_that_would_do_is_there():
    items = _items(("Verify in a browser", "open the page in the browser"),
                   ("Headless check", "run playwright"))
    ext = {**NONE, "browser": {"ok": True, "note": "Mac"}}
    assert [w.split()[0] for w in capabilities.item_warnings(items, ext)] == ["i2"], (
        "playwright runs in the box: the extension does not help it")
    desktop = {**NONE, "desktop": {"ok": True, "note": "kvm"}}
    assert capabilities.item_warnings(items, desktop) == []
    unknown = {k: {"ok": None, "note": "?"} for k in capabilities.CAPS}
    assert capabilities.item_warnings(items, unknown) == [], "an unchecked host is not a no"


def test_words_that_only_mention_a_browser_or_the_net_do_not_flag_an_item():
    items = _items(("Style the page", "must look right in all browsers; use flexbox"),
                   ("Write the loader", "reads the local level files and parses them"),
                   ("Notes", "the service layer is plain functions; a browser-sized canvas"))
    assert capabilities.item_warnings(items, NONE) == []


# --- where the planner and the orchestrator see it ------------------------------------------

async def test_the_planner_is_told_what_the_host_cannot_do(client, monkeypatch):
    seen = {}

    async def fake_complete(system, user, temperature=0.3):
        seen.update(system=system, user=user)
        return json.dumps([{"title": "walls", "brief": "w"}])
    monkeypatch.setattr(plan_mod, "complete_text", fake_complete)
    _host(monkeypatch, boxes_on=False, prof={**PROFILE, "allow_services": 0})
    await plan_mod.plan_from_dump(SLUG, "make the game")
    assert "# What this host can and cannot do" in seen["user"]
    assert "Cannot:\n- services (service_request)" in seen["user"]
    assert "This host:" in seen["system"] and 'under "Cannot"' in seen["system"]
    # nothing readable about the host: the planner is told nothing about it
    async def unknown(slug):
        return {k: {"ok": None, "note": "?"} for k in capabilities.CAPS}
    monkeypatch.setattr(capabilities, "for_project", unknown)
    await plan_mod.plan_from_dump(SLUG, "make the game")
    assert "What this host can" not in seen["user"] and "This host:" not in seen["system"]


async def test_the_orchestrate_result_carries_the_host_block_and_the_plan_warnings(
        client, monkeypatch):
    async def fake_complete(system, user, temperature=0.3):
        return json.dumps([
            *[{"title": f"part {n}", "brief": "p"} for n in range(9)],
            {"title": "Browser-verified build", "brief": "verify in a browser",
             "depends_on": list(range(9))}])
    monkeypatch.setattr(plan_mod, "complete_text", fake_complete)
    _host(monkeypatch, boxes_on=False, prof={**PROFILE, "allow_services": 0})
    tok = runtime.active_project.set(SLUG)
    try:
        out = await _handler("orchestrate").run(dump="build the game", run=False)
    finally:
        runtime.active_project.reset(tok)
    assert "# What this host can and cannot do (checked now)" in out
    assert "Plan warnings" in out
    assert 'i10 "Browser-verified build" 9 hard dependencies' in out
    assert 'i10 "Browser-verified build" needs a browser or screenshot' in out
    assert out.index("Plan warnings") > out.index("# What this host")
    assert "Not started (run=false)" in out


async def test_plan_status_and_plan_fix_warn_about_a_capability_the_host_lacks(
        client, monkeypatch):
    _host(monkeypatch, boxes_on=False, prof={**PROFILE, "allow_services": 0})
    await _put(client, [{"title": "walls", "brief": "w"},
                        {"title": "Check in the browser", "brief": "take a screenshot of it"}])
    text = await plan_mod.status(SLUG)
    assert 'i2 "Check in the browser" needs a browser or screenshot' in text
    out = await plan_mod.fix(SLUG, action="add", title="Screenshot the menu",
                             brief="screenshot it", run=False)
    assert out.startswith("added i3") and 'i3 "Screenshot the menu" needs a browser' in out
    out = await plan_mod.fix(SLUG, action="edit", item="i2", title="Write the check as a script",
                             brief="a script the operator runs", run=False)
    assert "i2 edited" in out and "needs a browser" not in out
    assert 'i2 "Write the check as a script" needs' not in await plan_mod.status(SLUG)
