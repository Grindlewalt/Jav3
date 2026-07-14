#!/usr/bin/env python3
"""End-to-end smoke test against a LIVE Jarvis instance (run on the Pi).

Exercises the real HTTP surfaces an autonomous agent depends on: auth,
chat (SSE messaging with a real model turn), memory, projects, the tool
registry, the git gate (request -> approve -> commit), and the jobs view.

Pure-logic features (crawl/search indexing, context_exclude, research
auto-approve, staging) are covered by the pytest suite; this drives the
live wire.

Usage:  .venv/bin/python scripts/e2e_smoke.py --password PW
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

# so the git-gate step can import backend.gitgate when run as scripts/e2e_smoke.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "http://localhost:8000"
COOKIE = None
PASS = FAIL = SKIP = 0


def _req(method, path, body=None, timeout=180):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if COOKIE:
        req.add_header("Cookie", COOKIE)
    return urllib.request.urlopen(req, timeout=timeout)


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")
    return cond


def skip(name, why):
    global SKIP
    SKIP += 1
    print(f"  SKIP  {name}  ({why})")


def login(user, pw):
    global COOKIE
    resp = _req("POST", "/api/auth/login", {"username": user, "password": pw})
    sc = resp.headers.get("Set-Cookie", "")
    COOKIE = sc.split(";", 1)[0] if sc else None
    check("login", resp.status == 200 and bool(COOKIE), f"status={resp.status}")


def sse_chat(message):
    """POST /api/chat, drain the SSE stream, return (final_text, n_events)."""
    resp = _req("POST", "/api/chat", {"message": message})
    final, events = "", 0
    for raw in resp:
        line = raw.decode(errors="replace").strip()
        if not line.startswith("data:"):
            continue
        events += 1
        try:
            ev = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "final":
            final = ev.get("content", "")
    return final, events


async def _make_request(slug):
    """Create a commit request exactly as the git_commit_request tool does."""
    from backend import gitgate
    await gitgate.ensure_repo(slug)
    row = await gitgate.create_request(slug, "e2e commit", None)
    return row.get("id") if isinstance(row, dict) else row


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--user", default="operator")
    ap.add_argument("--password", required=True)
    args = ap.parse_args()
    BASE = args.base.rstrip("/")

    print("== auth ==")
    login(args.user, args.password)

    print("== health ==")
    check("health ok", json.load(_req("GET", "/api/health")).get("ok") is True)

    slug = "e2e-smoke"
    print("== project ==")
    try:
        _req("POST", "/api/projects", {"name": "E2E Smoke"})
    except urllib.error.HTTPError as e:
        if e.code != 409:
            raise
    _req("POST", f"/api/projects/{slug}/load")
    projects = json.load(_req("GET", "/api/projects")).get("projects", [])
    check("project exists", any(p["slug"] == slug for p in projects))

    print("== chat (SSE messaging, real model turn) ==")
    t0 = time.time()
    text, events = sse_chat("Reply with exactly the word PONG and nothing else.")
    check("chat streamed events", events > 0, f"events={events}")
    check("chat produced a reply", len(text.strip()) > 0, f"got={text[:60]!r}")
    print(f"        ({events} events, {time.time()-t0:.1f}s, reply={text.strip()[:40]!r})")

    print("== conversations list ==")
    convs = json.load(_req("GET", f"/api/conversations?project={slug}")).get("conversations", [])
    check("chat conversation listed", len(convs) >= 1)

    print("== memory ==")
    mem = json.load(_req("GET", "/api/memory")).get("files", [])
    check("memory files present", len(mem) > 0)

    print("== tool registry ==")
    tools = json.load(_req("GET", "/api/tools")).get("tools", [])
    names = {t["name"] for t in tools}
    for want in ("git_status", "git_commit_request",
                 "crawl_codebase", "search_codebase", "dashboard"):
        check(f"tool registered: {want}", want in names)

    print("== git gate (request -> approve -> commit) ==")
    gs = json.load(_req("GET", f"/api/projects/{slug}/git/status"))
    check("git status returned", "status" in gs)
    _req("PUT", f"/api/projects/{slug}/file",
         {"path": "code/hello.py", "content": f"print('e2e {int(time.time())}')\n"})
    # Request creation is agent-only (the git_commit_request tool); drive it via
    # the same function the tool calls, then approve over the operator HTTP API.
    try:
        import asyncio
        rid = asyncio.run(_make_request(slug))
        if check("commit request created (tool path)", rid is not None, str(rid)):
            ap_ = json.load(_req("POST",
                  f"/api/projects/{slug}/git/requests/{rid}/approve"))
            check("commit approved -> sha", bool(ap_.get("commit_sha")), str(ap_)[:100])
            after = json.load(_req("GET", f"/api/projects/{slug}/git/status"))
            s = after["status"].lower()
            check("tree clean after commit",
                  "clean" in s or "nothing to commit" in s, after["status"][:80])
    except Exception as e:  # noqa: BLE001 - smoke test, surface anything
        check("git commit flow", False, repr(e)[:160])

    print("== jobs view ==")
    try:
        jobs = json.load(_req("GET", "/api/jobs")).get("jobs")
        check("jobs endpoint returns list", isinstance(jobs, list))
    except urllib.error.HTTPError as e:
        check("jobs endpoint", False, f"HTTP {e.code}")

    print(f"\n== RESULT ==  {PASS} passed, {FAIL} failed, {SKIP} skipped")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
