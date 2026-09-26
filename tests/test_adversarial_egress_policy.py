"""Adversarial audit of backend/egress.py (per-project egress policy).

test_GAP_* assert secure expectations and are EXPECTED TO FAIL; test_CONTROL_*
confirm a control that holds. Run:
.venv/bin/python -m pytest tests/test_adversarial_egress_policy.py -q
"""
import pytest

from backend import db as db_mod, egress


@pytest.fixture
async def db(tmp_env):
    await db_mod.init_db()
    egress._cut.clear()
    conn = await db_mod.get_db()
    yield conn
    await conn.close()


# --- CONTRACT (since profiles, DESIGN-BOXES (c)): an approval writes the
# PROJECT's own list, always. The shared baseline is the profile's list, edited
# only by the operator; no approval for one project widens another's reach.
# (Until 2026-09-26 a pure-default project's approval trained the shared
# GENERAL list; that was the operator's earlier model and is gone.)

async def test_default_project_approval_stays_with_that_project(db):
    await egress.note_denied(db, "projA", "shared-cdn.com")
    pid = (await egress.list_pending(db, "projA"))[0]["id"]
    res = await egress.approve_host(db, pid)
    assert res["added_to"] == "projA"                   # its own list, never a shared one
    assert (await egress.decide(db, "projA", "shared-cdn.com"))[0] == "allow"
    assert (await egress.decide(db, "projB", "shared-cdn.com"))[0] == "deny"


async def test_scoped_project_is_isolated_from_general_training(db):
    # a sensitive project with its OWN non-inheriting allowlist is unaffected by
    # any default project's approvals — the real containment boundary.
    await egress.set_policy(db, "finance", mode="allowlist",
                            inherit_general=False, hosts=["internal.api"])
    await egress.note_denied(db, "projA", "exfil.attacker.xyz")
    pid = (await egress.list_pending(db, "projA"))[0]["id"]
    await egress.approve_host(db, pid)
    assert (await egress.decide(db, "finance", "exfil.attacker.xyz"))[0] == "deny"


async def test_scoped_project_approval_trains_its_own_list_not_general(db):
    # approving a host for a project that has its own allowlist stays with that
    # project — it does NOT widen the shared general list (footgun reduction).
    await egress.set_policy(db, "scoped", mode="allowlist",
                            inherit_general=True, hosts=["a.com"])
    await egress.note_denied(db, "scoped", "b.com")
    pid = (await egress.list_pending(db, "scoped"))[0]["id"]
    res = await egress.approve_host(db, pid)
    assert res["added_to"] == "scoped"                  # its own list, not general
    assert (await egress.decide(db, "other-default", "b.com"))[0] == "deny"


# --- CONTROL: host-matching does not over-permit lookalikes -------------------
@pytest.mark.parametrize("host", [
    "pypi.org.evil.com",      # suffix trick
    "evilpypi.org",           # no dot boundary
    "pythonhosted.org.evil",  # substring, wrong boundary
])
async def test_CONTROL_lookalike_hosts_denied(db, host):
    assert (await egress.decide(db, "p", host))[0] == "deny"


@pytest.mark.parametrize("host", ["PYPI.ORG", "pypi.org.", "x.pypi.org"])
async def test_CONTROL_legit_case_dot_and_subdomain_allowed(db, host):
    # case-fold, trailing dot, and real subdomains resolve to the same allowed host
    assert (await egress.decide(db, "p", host))[0] == "allow"


# --- CONTROL: cut takes precedence over every mode ----------------------------
async def test_CONTROL_cut_beats_denyall_and_denylist(db):
    await egress.set_policy(db, "sp", mode="denyall")
    egress.mark_cut("sp", "pypi.org")
    assert (await egress.decide(db, "sp", "pypi.org"))[0] == "cut"
