"""Environment-driven settings.

Every value is read from the process environment (optionally seeded by a local
`.env` file). Nothing here carries a default that would be a secret or a
deployment-specific DSN -- `DATABASE_URL` and both per-job trigger secrets are
`None` until the environment supplies them, so a misconfigured deploy fails
loudly instead of quietly talking to the wrong database or accepting an
unauthenticated trigger.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the `recon` service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Postgres DSN. Never hardcoded: supplied by the environment in every context.
    database_url: str | None = None

    # "safe" stores hash + preview in the audit log; "full" stores raw detail.
    log_mode: Literal["safe", "full"] = "safe"

    # --- scheduled-job trigger secrets (R19) ------------------------------
    # DESIGN pins a **per-job** shared secret ("one secret per job so they can
    # be rotated apart"), and `.env.example` has always declared two. A single
    # `trigger_secret` could not express that: rotating the sync job's secret
    # would have rotated the reconcile job's with it, and a leaked cron
    # environment would have handed over both endpoints instead of one.
    #
    # Both default to `None`, and `recon.api.auth` treats `None` as **fail
    # closed** -- an unconfigured secret returns 401, it does not disable the
    # check. A trigger endpoint that authenticates everyone when its secret is
    # missing is worse than one that authenticates nobody: the failure is
    # invisible until it is exploited.
    trigger_secret_sync: str | None = None
    trigger_secret_reconcile: str | None = None

    # DEPRECATED single shared secret. Kept only because `recon.ingest` (owned
    # by another ticket) still reads it; `recon.api.auth` never does. Remove it
    # once `/internal/ingest/*` moves onto the per-job secrets above.
    trigger_secret: str | None = None

    # Only needed when `llm_provider` is a live provider.
    anthropic_api_key: str | None = None

    # "mock" keeps every graded path deterministic and offline by default.
    llm_provider: str = "mock"

    # Model id used when `llm_provider == "anthropic"`. Ignored by the mock.
    # Must be priced in the committed `prices.yaml`, or the first call fails
    # loudly rather than spending against a zero-cost default.
    llm_model: str = "claude-opus-5"

    # Default dataset seed; determinism is graded, so this is pinned, not random.
    seed: int = 20260822


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide `Settings`, built once and cached."""
    return Settings()
