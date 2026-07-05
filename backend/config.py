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
    frontend_dist: Path = BASE_DIR / "frontend" / "dist"

    db_path: Path = BASE_DIR / "data" / "jarvis.db"

    jwt_secret: str = ""
    jwt_ttl_hours: int = 24 * 7

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    model_name: str = "deepseek-v4-flash"
    model_max_tokens: int = 4096

    # Peak-pricing windows, local time, "HH:MM-HH:MM". May cross midnight.
    peak_windows: list[str] = ["18:00-21:00", "23:00-03:00"]
    # How long a user's "yes, use the API" answer stays valid.
    peak_confirm_ttl_minutes: int = 60

    max_react_iterations: int = 10
    recent_message_limit: int = 40

    # Workspace runner (light host-side sandbox; the QEMU VM is pass 2)
    run_python: str = "python3"
    run_timeout_seconds: int = 60
    run_max_mem_mb: int = 768

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
              settings.projects_dir, settings.skills_dir, settings.agents_dir):
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
