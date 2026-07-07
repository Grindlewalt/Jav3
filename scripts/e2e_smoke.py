#!/usr/bin/env python3
"""End-to-end smoke test against a LIVE Jarvis instance (run on the Pi).

Exercises the real HTTP surfaces an autonomous agent depends on: auth,
chat (SSE messaging with a real model turn), memory, projects, the tool
registry, the git gate (request -> approve -> commit), the jobs view, and —
if the VM is up — a monitored gate run with egress-lock verification.

Pure-logic features (crawl/search indexing, context_exclude, research
auto-approve, staging) are covered by the pytest suite; this drives the
live wire.

Usage:  .venv/bin/python scripts/e2e_smoke.py --password PW [--vm]
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

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


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--user", default="operator")
    ap.add_argument("--password", required=True)
    ap.add_argument("--vm", action="store_true", help="require the gated VM run")
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
    for want in ("run_command", "run_gated", "git_status", "git_commit_request",
                 "crawl_codebase", "search_codebase", "dashboard"):
        check(f"tool registered: {want}", want in names)

    print("== git gate (request -> approve -> commit) ==")
    gs = json.load(_req("GET", f"/api/projects/{slug}/git/status"))
    check("git status returned", "status" in gs)
    _req("PUT", f"/api/projects/{slug}/file",
         {"path": "code/hello.py", "content": f"print('e2e {int(time.time())}')\n"})
    try:
        cr = json.load(_req("POST", f"/api/projects/{slug}/git/requests",
                            {"message": "e2e commit"}))
        rid = cr.get("id")
        if check("commit request created", rid is not None, str(cr)[:100]):
            ap_ = json.load(_req("POST",
                  f"/api/projects/{slug}/git/requests/{rid}/approve"))
            check("commit approved -> sha", bool(ap_.get("commit_sha")), str(ap_)[:100])
            after = json.load(_req("GET", f"/api/projects/{slug}/git/status"))
            check("tree clean after commit", "clean" in after["status"].lower()
                  or "nothing to commit" in after["status"].lower(),
                  after["status"][:80])
    except urllib.error.HTTPError as e:
        check("git commit flow", False, f"HTTP {e.code}: {e.read()[:120]}")

    print("== jobs view ==")
    try:
        jobs = json.load(_req("GET", "/api/jobs")).get("jobs")
        check("jobs endpoint returns list", isinstance(jobs, list))
    except urllib.error.HTTPError as e:
        check("jobs endpoint", False, f"HTTP {e.code}")

    print("== VM monitored gate run ==")
    try:
        st = json.load(_req("GET", "/api/vm/status"))
        if not st.get("ssh_ready"):
            (check("VM ssh ready", False, "VM not reachable")
             if args.vm else skip("VM gate run", "VM ssh not ready"))
        else:
            g = json.load(_req("POST", "/api/vm/gate/run",
                  {"project": slug, "command": "python3 -c \"print('gate-ok')\"",
                   "fresh": False}, timeout=300))
            check("gate run exit 0", g.get("exit_status") == 0, str(g)[:120])
            check("gate report staged", str(g.get("report", "")).endswith("report.md"))
            check("egress lock verified", g.get("egress_locked") is True,
                  "nftables deny-by-default not detected")
            print(f"        (dns={g.get('dns_lookups')} "
                  f"blocked={g.get('blocked_attempts')} execs={g.get('execs_logged')})")
    except urllib.error.HTTPError as e:
        check("VM gate run", False, f"HTTP {e.code}: {e.read()[:120]}")

    print(f"\n== RESULT ==  {PASS} passed, {FAIL} failed, {SKIP} skipped")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
