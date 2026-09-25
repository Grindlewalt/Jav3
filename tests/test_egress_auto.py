"""Egress auto mode (backend/egress_auto.py): the deterministic scorer, the one
model question (stubbed under the real Model.complete gateway, so the budget
and key policy still run), and the guardrails that keep a guess from widening
access silently — off by default, per-project scope, cap, expiry, revoke,
promote, and the anomaly cut outranking everything."""
import pytest

from backend import db as db_mod
from backend import egress, egress_auto, security
from backend.agent import model as model_mod
from backend.config import settings
from backend.vm import egress_proxy


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    egress._stack.clear()
    egress._context.update(egress._EMPTY)
    egress._cut.clear()
    egress_auto._locks.clear()
    monkeypatch.setattr(settings, "peak_windows", [])
    yield
    egress._cut.clear()


@pytest.fixture
async def db(tmp_env):
    await db_mod.init_db()
    conn = await db_mod.get_db()
    yield conn
    await conn.close()


@pytest.fixture
def model_says(monkeypatch):
    """Make the model answer `text` — through the real gateway (budget, key
    policy) with only the network transport replaced. Returns the call log."""
    calls: list[list[dict]] = []
    answer = {"text": ""}

    async def fake_transport(messages, **kw):
        calls.append(messages)
        yield {"type": "message", "content": answer["text"], "tool_calls": None,
               "usage": {"prompt_tokens": 50, "completion_tokens": 10}}

    monkeypatch.setattr(model_mod.model, "api_key", "test-key")
    monkeypatch.setattr(model_mod.model.transport, "complete", fake_transport)

    def set_answer(text: str):
        answer["text"] = text
        return calls
    return set_answer


async def _on(db, slug=None):
    await egress_auto.set_mode(db, slug, "on")


async def _events(db, kind="egress_auto"):
    return [e for e in await security.list_events(db) if e["kind"] == kind]


# --- the scorer ----------------------------------------------------------------

@pytest.mark.parametrize("host", [
    "pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "api.github.com",
    "codeload.github.com", "objects.githubusercontent.com", "gitlab.com",
    "static.crates.io", "deb.debian.org", "archive.ubuntu.com", "huggingface.co",
    "cdn-lfs.huggingface.co", "api.openai.com", "api.anthropic.com",
    "api.deepseek.com", "docs.python.org", "developer.mozilla.org",
    "stackoverflow.com", "en.wikipedia.org", "cdn.jsdelivr.net", "unpkg.com",
    "cdnjs.cloudflare.com", "PyPI.org."])
def test_known_hosts_allow(host):
    v, rule, _ = egress_auto.score(host, 443)
    assert (v, rule) == ("allow", "known")


@pytest.mark.parametrize("host,rule", [
    ("1.2.3.4", "ip"), ("::1", "ip"), ("[2001:db8::1]", "ip"), ("10.0.0.58", "ip"),
    ("xn--pypi-7ya.org", "punycode"), ("files.xn--80ak6aa92e.com", "punycode"),
    ("printer.local", "private"), ("localhost", "private"), ("nas.lan", "private"),
    ("db.internal", "private"),
    ("abc123.ngrok-free.app", "exfil"), ("x.ngrok.io", "exfil"),
    ("quiet-river.trycloudflare.com", "exfil"), ("serveo.net", "exfil"),
    ("webhook.site", "exfil"), ("pastebin.com", "exfil"), ("transfer.sh", "exfil"),
    ("eoabc.m.pipedream.net", "exfil"), ("a1b2.dnslog.cn", "exfil"),
    ("my-ngrok-mirror.com", "exfil"), ("abc.oast.fun", "exfil"),
    ("a3f9c2e81b7d4e6f.example.com", "entropy"), ("x7k2q9mzp4w8v3n.com", "entropy"),
    ("qwrtzxcvbnm.io", "entropy"),
    ("bad host.com", "malformed"), ("no-dot", "malformed"), ("", "malformed"),
])
def test_deny_rules(host, rule):
    v, got, reason = egress_auto.score(host, 443)
    assert (v, got) == ("deny", rule) and reason


def test_known_host_on_a_strange_port_is_still_denied():
    assert egress_auto.score("pypi.org", 8080)[:2] == ("deny", "port")
    assert egress_auto.score("pypi.org", "443")[0] == "allow"
    assert egress_auto.score("pypi.org", "80")[0] == "allow"


@pytest.mark.parametrize("host", [
    "pypi.org.evil.com", "evilpypi.org", "github.com.attacker.net",
    "raw.githubusercontent.com", "example-startup.com", "docs.rs",
    "kubernetes-dashboard.dev", "internationalization.org"])
def test_unknown_or_lookalike_goes_to_the_model(host):
    # a lookalike is not "known" (no suffix trick) and an ordinary name is
    # not "random": both are the model's question, never an auto-allow
    assert egress_auto.score(host, 443)[0] is None


def test_parse_answer_fails_closed():
    assert egress_auto.parse_answer('{"verdict":"yes","reason":"docs"}') == ("allow", "docs")
    assert egress_auto.parse_answer('sure! {"verdict": "no", "reason": "tunnel"}')[0] == "deny"
    assert egress_auto.parse_answer('{"verdict":"unsure"}')[0] == "unsure"
    for junk in ("", "yes", '{"verdict":"allow"}', '{"verdict":', "[1,2]"):
        assert egress_auto.parse_answer(junk)[0] == "unsure"
    long = egress_auto.parse_answer('{"verdict":"no","reason":"' + "x" * 500 + '"}')[1]
    assert len(long) <= 160


# --- off by default --------------------------------------------------------------

async def test_off_by_default_does_nothing(db, model_says):
    calls = model_says('{"verdict":"yes","reason":"fine"}')
    mode = await egress_auto.get_mode(db, "proj")
    assert mode == {"global": "off", "project": None, "effective": False}
    assert await egress_auto.judge(db, "proj", "pypi-mirror.example", 443) is None
    assert await egress_auto.judge(db, "proj", "huggingface.co", 443) is None
    assert calls == []
    assert await egress.list_pending(db) == []
    assert await _events(db) == []
    assert (await egress.decide(db, "proj", "huggingface.co"))[0] == "deny"


async def test_project_setting_overrides_global(db):
    await _on(db)                                   # global on
    await egress_auto.set_mode(db, "quiet", "off")  # this project opts out
    assert (await egress_auto.get_mode(db, "quiet"))["effective"] is False
    assert (await egress_auto.get_mode(db, "other"))["effective"] is True
    assert await egress_auto.judge(db, "quiet", "huggingface.co", 443) is None
    await egress_auto.set_mode(db, "quiet", "inherit")
    assert (await egress_auto.get_mode(db, "quiet"))["project"] is None
    bad = await egress_auto.set_mode(db, "quiet", "maybe")
    assert bad["ok"] is False


# --- deterministic allow: scoped, expiring, announced -----------------------------

async def test_known_host_auto_allowed_for_that_project_only(db):
    await _on(db, "proj")
    v, reason = await egress_auto.judge(db, "proj", "huggingface.co", 443)
    assert v == "allow" and "auto-allowed" in reason
    # the project can now reach it; nobody else can, and the shared general
    # list was not widened
    assert (await egress.decide(db, "proj", "huggingface.co"))[0] == "allow"
    assert (await egress.decide(db, "other", "huggingface.co"))[0] == "deny"
    pol = await egress.get_policy(db, "other")
    assert "huggingface.co" not in pol["effective"]
    # exact host only: a subdomain is not covered by the guess
    assert (await egress.decide(db, "proj", "evil.huggingface.co"))[0] == "deny"

    ev = await _events(db)
    assert len(ev) == 1 and ev[0]["severity"] == "info"
    assert ev[0]["project_slug"] == "proj"
    d = ev[0]["detail"]
    assert d["verdict"] == "allow" and d["rule"] == "known" and d["host"] == "huggingface.co"
    assert d["auto_id"] and d["expires_at"]
    async with db.execute("SELECT verdict, reason FROM egress_events") as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert rows[0]["verdict"] == "auto_allow"

    groups = {g["project"]: g for g in await egress.allowlist(db)}
    entry = [e for e in groups["proj"]["entries"] if e["host"] == "huggingface.co"][0]
    assert entry["source"] == "auto" and entry["id"] == d["auto_id"]
    # the general list reports its seeds as seed
    gen = {e["host"]: e["source"] for e in groups[egress.GENERAL]["entries"]}
    assert gen["pypi.org"] == "seed"


async def test_auto_allow_expires(db, monkeypatch):
    monkeypatch.setattr(settings, "egress_auto_ttl_days", 7)
    await _on(db, "proj")
    await egress_auto.judge(db, "proj", "huggingface.co", 443)
    async with db.execute("SELECT expires_at, created_at FROM egress_auto_allow") as cur:
        r = await cur.fetchone()
    async with db.execute("SELECT julianday(?) - julianday(?) AS d",
                          (r["expires_at"], r["created_at"])) as cur:
        assert round((await cur.fetchone())["d"]) == 7
    await db.execute("UPDATE egress_auto_allow SET expires_at = datetime('now', '-1 minute')")
    await db.commit()
    assert (await egress.decide(db, "proj", "huggingface.co"))[0] == "deny"
    groups = {g["project"]: g for g in await egress.allowlist(db)}
    assert "proj" not in groups or not groups["proj"]["entries"]


async def test_daily_cap_falls_back_to_pending(db, monkeypatch):
    monkeypatch.setattr(settings, "egress_auto_daily_cap", 2)
    await _on(db, "proj")
    assert (await egress_auto.judge(db, "proj", "huggingface.co", 443))[0] == "allow"
    assert (await egress_auto.judge(db, "proj", "gitlab.com", 443))[0] == "allow"
    assert await egress_auto.judge(db, "proj", "unpkg.com", 443) is None
    assert (await egress.decide(db, "proj", "unpkg.com"))[0] == "deny"
    pend = {p["host"] for p in await egress.list_pending(db, "proj")}
    assert "unpkg.com" in pend                    # waiting for the operator
    # revoking does not refill the cap
    aid = (await egress.active_auto(db, "proj", "gitlab.com"))["id"]
    await egress.revoke_auto(db, aid)
    assert await egress_auto.judge(db, "proj", "unpkg.com", 443) is None
    # the cap is per project
    assert (await egress_auto.judge(db, "other", "unpkg.com", 443)) is None  # auto off there
    await _on(db, "other")
    assert (await egress_auto.judge(db, "other", "unpkg.com", 443))[0] == "allow"


# --- revoke / promote ------------------------------------------------------------

async def test_revoke_sticks(db):
    await _on(db, "proj")
    await egress_auto.judge(db, "proj", "huggingface.co", 443)
    aid = (await egress.active_auto(db, "proj", "huggingface.co"))["id"]
    res = await egress.revoke_auto(db, aid)
    assert res["ok"]
    assert (await egress.decide(db, "proj", "huggingface.co"))[0] == "deny"
    # the guest retries: auto mode must not re-grant what the operator revoked,
    # and the re-hit does not bounce it back into the waiting queue
    assert await egress_auto.judge(db, "proj", "huggingface.co", 443) is None
    await egress.note_denied(db, "proj", "huggingface.co")
    assert await egress.list_pending(db, "proj") == []


async def test_promote_moves_it_onto_the_real_list(db):
    await _on(db, "proj")
    await egress_auto.judge(db, "proj", "huggingface.co", 443)
    aid = (await egress.active_auto(db, "proj", "huggingface.co"))["id"]
    res = await egress.promote_auto(db, aid)
    assert res["ok"] and res["added_to"] == egress.GENERAL   # pure-default project
    assert await egress.active_auto(db, "proj", "huggingface.co") is None
    v, reason = await egress.decide(db, "proj", "huggingface.co")
    assert v == "allow" and "allowlist" in reason
    groups = {g["project"]: g for g in await egress.allowlist(db)}
    gen = {e["host"]: e["source"] for e in groups[egress.GENERAL]["entries"]}
    assert gen["huggingface.co"] == "operator"
    assert (await egress.promote_auto(db, aid))["ok"] is False


async def test_operator_revokes_a_standing_entry(db):
    res = await egress.remove_host(db, egress.GENERAL, "pypi.org")
    assert res["ok"]
    assert (await egress.decide(db, "proj", "pypi.org"))[0] == "deny"
    assert (await egress.remove_host(db, egress.GENERAL, "pypi.org"))["ok"] is False


# --- deterministic deny ----------------------------------------------------------

async def test_tunnel_auto_denied_once_and_leaves_the_queue(db):
    await _on(db, "proj")
    v, reason = await egress_auto.judge(db, "proj", "x.trycloudflare.com", 443)
    assert v == "deny" and "tunnel" in reason
    await egress.note_denied(db, "proj", "x.trycloudflare.com")
    assert await egress.list_pending(db, "proj") == []          # not "waiting for you"
    # a retry does not raise a second event
    await egress_auto.judge(db, "proj", "x.trycloudflare.com", 443)
    await egress.note_denied(db, "proj", "x.trycloudflare.com")
    ev = await _events(db)
    assert len(ev) == 1 and ev[0]["severity"] == "warn"
    assert ev[0]["detail"]["verdict"] == "deny" and ev[0]["detail"]["rule"] == "exfil"
    assert await egress.list_pending(db, "proj") == []
    # the operator can still override it explicitly
    res = await egress.allow_host(db, "proj", "x.trycloudflare.com")
    assert res["ok"]
    assert (await egress.decide(db, "proj", "x.trycloudflare.com"))[0] == "allow"


async def test_port_deny_does_not_poison_the_host(db):
    await _on(db, "proj")
    assert (await egress_auto.judge(db, "proj", "huggingface.co", 8443))[0] == "deny"
    assert (await egress_auto.judge(db, "proj", "huggingface.co", 443))[0] == "allow"


# --- the model path --------------------------------------------------------------

async def test_model_yes_allows(db, model_says):
    calls = model_says('{"verdict": "yes", "reason": "well-known docs site"}')
    await _on(db, "proj")
    v, reason = await egress_auto.judge(db, "proj", "docs.rs", 443)
    assert v == "allow" and "well-known docs site" in reason
    assert len(calls) == 1
    sys, user = calls[0]
    assert sys["role"] == "system" and "docs.rs" in user["content"]
    ev = await _events(db)
    assert ev[0]["detail"]["rule"] == "model" and ev[0]["severity"] == "info"


async def test_model_no_denies_and_is_not_reasked(db, model_says):
    calls = model_says('{"verdict": "no", "reason": "unknown file host"}')
    await _on(db, "proj")
    v, reason = await egress_auto.judge(db, "proj", "files-share.example", 443)
    assert v == "deny" and "unknown file host" in reason
    await egress.note_denied(db, "proj", "files-share.example")
    v2, _ = await egress_auto.judge(db, "proj", "files-share.example", 443)
    assert v2 == "deny" and len(calls) == 1
    ev = await _events(db)
    assert len(ev) == 1 and ev[0]["severity"] == "warn"


async def test_model_unsure_leaves_it_waiting(db, model_says):
    calls = model_says('{"verdict": "unsure", "reason": "never heard of it"}')
    await _on(db, "proj")
    assert await egress_auto.judge(db, "proj", "odd-site.example", 443) is None
    await egress.note_denied(db, "proj", "odd-site.example")
    pend = await egress.list_pending(db, "proj")
    assert [p["host"] for p in pend] == ["odd-site.example"]
    assert pend[0]["auto_verdict"] == "unsure"
    assert await _events(db) == []
    assert await egress_auto.judge(db, "proj", "odd-site.example", 443) is None
    assert len(calls) == 1                                       # memoized


async def test_model_garbage_fails_closed(db, model_says):
    model_says("Sure, go ahead and allow it!")
    await _on(db, "proj")
    assert await egress_auto.judge(db, "proj", "odd-site.example", 443) is None
    assert (await egress.decide(db, "proj", "odd-site.example"))[0] == "deny"


async def test_no_key_falls_back_to_the_operator(db, monkeypatch):
    monkeypatch.setattr(model_mod.model, "api_key", "")
    await _on(db, "proj")
    assert await egress_auto.judge(db, "proj", "odd-site.example", 443) is None
    pend = await egress.list_pending(db, "proj")
    assert pend and pend[0]["auto_verdict"] is None     # it can try again later
    assert await _events(db) == []


async def test_peak_window_skips_the_model(db, model_says, monkeypatch):
    calls = model_says('{"verdict": "yes", "reason": "x"}')
    monkeypatch.setattr(settings, "peak_windows", ["00:00-23:59"])
    await _on(db, "proj")
    assert await egress_auto.judge(db, "proj", "odd-site.example", 443) is None
    assert calls == []
    # deterministic rules still work in a peak window
    assert (await egress_auto.judge(db, "proj", "huggingface.co", 443))[0] == "allow"


async def test_model_call_is_budget_metered(db, model_says):
    from backend.agent import budget as budget_mod
    seen = []

    async def spy(messages, **kw):
        seen.append(budget_mod.current())
        yield {"type": "message", "content": '{"verdict":"yes","reason":"ok"}',
               "tool_calls": None, "usage": {"prompt_tokens": 50, "completion_tokens": 10}}
    model_says("")
    import backend.agent.model as m
    m.model.transport.complete = spy
    await _on(db, "proj")
    await egress_auto.judge(db, "proj", "odd-site.example", 443)
    assert seen and seen[0] is not None
    assert seen[0].max_input == settings.egress_auto_budget_input
    assert budget_mod.current() is None                  # reset afterwards


# --- guardrails ------------------------------------------------------------------

async def test_cut_outranks_auto(db):
    await _on(db, "proj")
    await egress_auto.judge(db, "proj", "huggingface.co", 443)
    egress.mark_cut("proj", "huggingface.co")
    assert (await egress.decide(db, "proj", "huggingface.co"))[0] == "cut"
    egress.mark_cut("proj", "gitlab.com")
    assert await egress_auto.judge(db, "proj", "gitlab.com", 443) is None


async def test_open_anomaly_alert_blocks_a_guess(db):
    await _on(db, "proj")
    await security.raise_event(db, kind="egress_anomaly", severity="critical",
                               project="proj", summary="spike",
                               detail={"host": "huggingface.co"})
    assert await egress_auto.judge(db, "proj", "huggingface.co", 443) is None


async def test_operator_rejected_host_is_never_auto_allowed(db):
    await egress.note_denied(db, "proj", "huggingface.co")
    pid = (await egress.list_pending(db, "proj"))[0]["id"]
    await egress.reject_host(db, pid)
    await egress.note_denied(db, "proj", "huggingface.co")      # re-queued by a retry
    await _on(db, "proj")
    assert await egress_auto.judge(db, "proj", "huggingface.co", 443) is None


async def test_reviewer_flag_is_left_for_the_operator(db):
    await egress.note_denied(db, "proj", "huggingface.co")
    await db.execute("UPDATE egress_pending SET triage_verdict='flag'")
    await db.commit()
    await _on(db, "proj")
    assert await egress_auto.judge(db, "proj", "huggingface.co", 443) is None


async def test_standing_denies_are_not_second_guessed(db):
    await _on(db, "locked")
    await egress.set_policy(db, "locked", mode="denyall")
    egress.set_context("locked", "op1")
    assert (await egress_proxy._authorize("huggingface.co", "443"))[0] == "deny"
    await egress.set_policy(db, "locked", mode="denylist", hosts=["huggingface.co"])
    assert (await egress_proxy._authorize("huggingface.co", "443"))[0] == "deny"
    async with db.execute("SELECT COUNT(*) AS n FROM egress_auto_allow") as cur:
        assert (await cur.fetchone())["n"] == 0


async def test_proxy_path_denies_an_ip_literal_with_the_auto_reason(db):
    await _on(db, "proj")
    egress.set_context("proj", "op1")
    v, reason = await egress_proxy._authorize("203.0.113.9", "443")
    assert v == "deny" and "auto-denied" in reason


# --- the API the Network page drives ------------------------------------------

async def test_api_round_trip(tmp_env):
    import httpx
    from backend.auth import hash_password
    from backend.main import app

    await db_mod.init_db()
    conn = await db_mod.get_db()
    await conn.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                       ("operator", hash_password("pw")))
    await conn.commit()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.get("/api/egress/auto")).status_code == 401
        await c.post("/api/auth/login", json={"username": "operator", "password": "pw"})
        r = await c.get("/api/egress/auto", params={"project": "proj"})
        assert r.json() == {"global": "off", "project": None, "effective": False}
        r = await c.put("/api/egress/auto", json={"project": "proj", "mode": "on"})
        assert r.status_code == 200 and r.json()["effective"] is True
        assert (await c.put("/api/egress/auto", json={"mode": "inherit"})).status_code == 400

        assert (await egress_auto.judge(conn, "proj", "huggingface.co", 443))[0] == "allow"
        await egress.note_denied(conn, "proj", "odd.example")
        s = (await c.get("/api/egress/summary", params={"project": "proj"})).json()
        assert s == {"allowed": 1, "denied": 0, "waiting": 1}

        groups = {g["project"]: g for g in (await c.get("/api/egress/allowlist")).json()["groups"]}
        auto = [e for e in groups["proj"]["entries"] if e["source"] == "auto"][0]
        r = await c.post("/api/egress/allowlist/revoke", json={"id": auto["id"]})
        assert r.status_code == 200
        assert (await egress.decide(conn, "proj", "huggingface.co"))[0] == "deny"
        r = await c.post("/api/egress/allowlist/revoke",
                         json={"project": egress.GENERAL, "host": "nope.example"})
        assert r.status_code == 404
        r = await c.post("/api/egress/allow", json={"project": "proj", "host": "odd.example"})
        assert r.status_code == 200
        assert (await c.get("/api/egress/summary")).json()["waiting"] == 0
    await conn.close()
