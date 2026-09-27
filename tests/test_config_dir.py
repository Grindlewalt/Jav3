"""A second instance with its own JARVIS_CONFIG_DIR reads its OWN env file and
secrets (not the live ~/.config/jarvis ones), and JARVIS_DEEPSEEK_BASE_URL
routes the default model (e2e BUG-2)."""
import json
import os
import subprocess
import sys

from backend import providers
from backend.config import settings


def test_config_dir_moves_env_file_and_secrets(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "env").write_text("JARVIS_DEEPSEEK_API_KEY=sk-from-instance\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("JARVIS_")}
    env["JARVIS_CONFIG_DIR"] = str(cfg)
    code = ("import json; from backend.config import settings, ENV_FILE; "
            "print(json.dumps([str(ENV_FILE), str(settings.secrets_path), "
            "settings.deepseek_api_key]))")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                         text=True, cwd=str(settings.base_dir), check=True).stdout
    env_file, secrets, key = json.loads(out.strip().splitlines()[-1])
    assert env_file == str(cfg / "env")
    assert secrets == str(cfg / "secrets.json")
    assert key == "sk-from-instance"


def test_deepseek_base_url_routes_default_model(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "deepseek_base_url", "http://127.0.0.1:8799")
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-x")
    r = providers.resolve(None)
    assert r.provider == "deepseek"
    assert r.base_url == "http://127.0.0.1:8799"
