"""The guest-supplied model base_url is a key-exfil seam: the host attaches the
API key, so an unchecked base_url could route the real key to an attacker. Only
allowlisted endpoints are honoured, and only the DeepSeek endpoint gets the key."""
import pytest

from backend.agent import model
from backend.agent.model import ModelError
from backend.config import settings


def test_deepseek_default_allowed():
    assert model.base_url_allowed(settings.deepseek_base_url)
    assert model._is_deepseek_endpoint(settings.deepseek_base_url)


def test_local_ollama_allowed_but_not_deepseek():
    assert model.base_url_allowed("http://127.0.0.1:11434")
    assert not model._is_deepseek_endpoint("http://127.0.0.1:11434")


def test_attacker_endpoint_refused():
    assert not model.base_url_allowed("https://evil.attacker.xyz/v1")
    assert not model.base_url_allowed("http://api.deepseek.com.evil.xyz")  # lookalike host


async def test_complete_raises_on_bad_base_url():
    gen = model.model.complete([{"role": "user", "content": "hi"}],
                               base_url="https://evil.attacker.xyz")
    with pytest.raises(ModelError, match="refused model base_url"):
        async for _ in gen:
            pass
