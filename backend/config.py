import os
import secrets
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JARVIS_",
        env_file=os.path.expanduser("~/.config/jarvis/env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    base_dir: Path = BASE_DIR
    data_dir: Path = BASE_DIR / "data"
    memory_dir: Path = BASE_DIR / "memory"
    projects_dir: Path = BASE_DIR / "projects"
    skills_dir: Path = BASE_DIR / "skills"
    agents_dir: Path = BASE_DIR / "agents"
    tools_dir: Path = BASE_DIR / "tools"
    frontend_dist: Path = BASE_DIR / "frontend" / "dist"

    db_path: Path = BASE_DIR / "data" / "jarvis.db"

    jwt_secret: str = ""
    jwt_ttl_hours: int = 24 * 7

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    model_name: str = "deepseek-v4-flash"
    model_max_tokens: int = 4096
    # Main generation temperature. 0.7 keeps personality and fluency; the
    # no-tools self-check pass (which runs at 0.0) is what enforces rules, so
    # the main turn doesn't need to run cold. Tunable via JARVIS_MODEL_TEMPERATURE.
    model_temperature: float = 0.7

    # Peak-pricing windows, local time, "HH:MM-HH:MM". May cross midnight.
    peak_windows: list[str] = ["18:00-21:00", "23:00-03:00"]
    # How long a user's "yes, use the API" answer stays valid.
    peak_confirm_ttl_minutes: int = 60

    # Backstop for the main/chat loop. Subagents get a much tighter cap below:
    # a research subagent reading 1-3 sources needs a handful of rounds, not 60
    # — leaving it high let subagents read 40-85 pages and burn millions of
    # tokens re-sending the pile each iteration.
    max_react_iterations: int = 40
    subagent_max_iterations: int = 8
    recent_message_limit: int = 40

    # Per-operation token budget (shared across every agent in a chat turn or a
    # research job). DeepSeek caches prompt prefixes automatically, so the input
    # cap is generous. ~5M in / ~1M out is roughly a cent (cached) to a dime.
    max_op_input_tokens: int = 5_000_000
    max_op_output_tokens: int = 1_000_000

    # Workspace runner (light host-side sandbox; the QEMU VM is pass 2)
    run_python: str = "python3"
    run_timeout_seconds: int = 60
    run_max_mem_mb: int = 768

    # Web access (secure + inert). The agent never touches the raw internet:
    # host-side tools query SearXNG and fetch pages, strip them to plain text,
    # and refuse internal/private targets (SSRF guard).
    searxng_url: str = "http://10.0.0.58:8080"
    web_search_results: int = 8
    web_fetch_timeout: int = 15
    web_max_bytes: int = 2_000_000      # stop reading a page past this
    # cap the inert text handed to the model. Was 20k (~5k tokens/page); a
    # research subagent reads a few pages and re-sends them each loop, so a
    # smaller slice cuts token throughput hard while keeping the useful content.
    web_max_chars: int = 6_000

    # Sandbox VM (M3). Persistent: boots once and stays up; nuke is a
    # recovery action (recreate the overlay), not a per-run ritual.
    vm_dir: Path = BASE_DIR / "data" / "vm"
    vm_ssh_host: str = "127.0.0.1"
    vm_ssh_port: int = 2222
    vm_ssh_user: str = "agent"
    vm_unit: str = "jarvis-vm.service"
    vm_workspace: str = "/workspace"
    vm_run_timeout_seconds: int = 300
    vm_boot_timeout_seconds: int = 120
    vm_push_max_mb: int = 64


settings = Settings()


def ensure_dirs() -> None:
    for d in (settings.data_dir, settings.memory_dir, settings.memory_dir / "notes",
              settings.projects_dir, settings.skills_dir, settings.agents_dir,
              settings.tools_dir):
        d.mkdir(parents=True, exist_ok=True)


def get_jwt_secret() -> str:
    """Env-provided secret wins; otherwise generate once and persist under data/."""
    if settings.jwt_secret:
        return settings.jwt_secret
    ensure_dirs()
    secret_file = settings.data_dir / "jwt_secret"
    if not secret_file.exists():
        secret_file.write_text(secrets.token_urlsafe(48))
        secret_file.chmod(0o600)
    return secret_file.read_text().strip()
