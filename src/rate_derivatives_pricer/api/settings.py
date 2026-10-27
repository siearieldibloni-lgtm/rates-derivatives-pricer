"""API configuration from environment variables (docs/CONVENTIONS.md section 7)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from rate_derivatives_pricer.api.log_buffer import DEFAULT_LOG_BUFFER_SIZE
from rate_derivatives_pricer.api.logging_config import LogFormat


class Settings(BaseSettings):
    """Service settings; each field is read from the environment variable of the same name.

    Attributes:
        log_level: ``LOG_LEVEL`` (``DEBUG``, ``INFO``...).
        log_format: ``LOG_FORMAT``: ``json`` in containers, ``text`` for local development.
        market_data_dir: ``MARKET_DATA_DIR``: folder of the snapshot files.
        curve_cache_size: ``CURVE_CACHE_SIZE``: maximum number of cached environments.
        curve_cache_ttl_seconds: ``CURVE_CACHE_TTL_SECONDS``: entry lifetime (unset = no expiry).
        git_commit: ``GIT_COMMIT``: commit of the deployed build, reported by ``/v1/version``.
        log_buffer_size: ``LOG_BUFFER_SIZE``: events kept in memory for ``/v1/logs``.
        log_buffer_level: ``LOG_BUFFER_LEVEL``: minimum level kept in memory (``INFO`` by
            default; ``DEBUG`` adds the per-pillar and per-bump details).
    """

    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    log_level: str = "INFO"
    log_format: LogFormat = "json"
    market_data_dir: Path = Path("data/market")
    curve_cache_size: int = Field(default=32, ge=1)
    curve_cache_ttl_seconds: float | None = Field(default=None, gt=0)
    git_commit: str = "unknown"
    log_buffer_size: int = Field(default=DEFAULT_LOG_BUFFER_SIZE, ge=1)
    log_buffer_level: str = "INFO"
