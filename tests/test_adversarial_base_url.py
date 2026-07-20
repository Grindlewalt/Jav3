"""Adversarial audit of backend/agent/model.py base_url_allowed /
_is_deepseek_endpoint — the guest-supplied base_url is a key-exfil seam.

No gap found: every classic host-confusion trick fails CLOSED. These
test_CONTROL_* pin that the guard holds (regression guards).
Run: .venv/bin/python -m pytest tests/test_adversarial_base_url.py -q
"""
import pytest

from backend.agent import model
from backend.config import settings


# Each of these must NOT be honoured as the DeepSeek endpoint (would route the
# real Bearer key to an attacker).
@pytest.mark.parametrize("url", [
    "https://api.deepseek.com@evil.com",       # userinfo confusion
    "https://api.deepseek.com.evil.com",       # suffix lookalike
    "https://evil.com#api.deepseek.com",        # fragment
    "https://evil.com/?x=api.deepseek.com",     # query
    "https://api.deepseek.com:443@evil",        # port-in-userinfo
    "http://api.deepseek.com",                  # scheme downgrade (http)
    "https://api.deepseek.com\\@evil.com",      # backslash trick
])
def test_CONTROL_lookalike_base_urls_refused(url):
    assert not model.base_url_allowed(url)


# Whitespace/tab injection is NOT a bypass: urlsplit strips it, so these
# normalise to the genuine api.deepseek.com — sending the key there is correct.
@pytest.mark.parametrize("url", [
    " https://api.deepseek.com",
    "https://api.deepseek.com\n",
    "ht\ttps://api.deepseek.com",
])
def test_CONTROL_whitespace_normalises_to_real_deepseek(url):
    assert model.base_url_allowed(url) and model._is_deepseek_endpoint(url)


def test_CONTROL_real_key_only_reaches_real_deepseek():
    # The one host that IS honoured is genuinely api.deepseek.com (case-folded).
    assert model.base_url_allowed("https://API.DEEPSEEK.COM")
    assert model._is_deepseek_endpoint("https://API.DEEPSEEK.COM")
    # For every refused lookalike, even if _is_deepseek_endpoint is fooled by the
    # hostname, base_url_allowed (checked first in ModelGateway) refuses it, so
    # the key is never attached.
    for url in ("api.deepseek.com", "https://api.deepseek.com:443"):
        assert not model.base_url_allowed(url)   # gated before key attachment
