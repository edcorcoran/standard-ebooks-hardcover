"""Runtime configuration, loaded from environment / .env."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven settings.

    All values can be supplied via environment variables (optionally through a
    local ``.env`` file). Only ``hardcover_api_token`` is required for commands
    that talk to Hardcover; the ``catalog`` command needs nothing.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Hardcover API token from https://hardcover.app/account/api (expires each Jan 1).
    hardcover_api_token: str = ""

    # Optional Discord webhook for notifications; disabled when empty.
    discord_webhook_url: str = ""

    # Local sqlite state (catalog cache, processed ids, review queue).
    state_db_path: Path = Path("data/se_hardcover.sqlite3")

    # Daemon poll interval, in seconds (default 1h).
    poll_interval: int = 3600

    # How often the daemon runs the reconcile audit (data accuracy + coverage),
    # in seconds. 0 means "every poll cycle" (same cadence as the new-book check).
    audit_interval: int = 0

    # Whether the reconcile audit compares cover images (extra downloads). Data,
    # format and coverage checks always run; only the perceptual cover check is
    # gated by this.
    audit_check_covers: bool = True

    # Global write guard. When true, no mutations are sent to Hardcover.
    dry_run: bool = False

    # Path the daemon touches each successful cycle (container healthcheck).
    heartbeat_path: Path = Path("data/heartbeat")

    def require_token(self) -> str:
        if not self.hardcover_api_token:
            raise RuntimeError(
                "HARDCOVER_API_TOKEN is not set. Get one at "
                "https://hardcover.app/account/api and add it to your .env."
            )
        return self.hardcover_api_token


def load_settings(**overrides) -> Settings:
    """Load settings, applying any explicit overrides (e.g. --dry-run from CLI)."""
    return Settings(**{k: v for k, v in overrides.items() if v is not None})
