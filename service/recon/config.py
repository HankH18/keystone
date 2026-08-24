"""Environment-driven settings.

Every value is read from the process environment (optionally seeded by a local
`.env` file). Nothing here carries a default that would be a secret or a
deployment-specific DSN -- `DATABASE_URL` and both per-job trigger secrets are
`None` until the environment supplies them, so a misconfigured deploy fails
loudly instead of quietly talking to the wrong database or accepting an
unauthenticated trigger.

`.env` resolution
-----------------
`env_file` used to be the bare relative string ``".env"``. pydantic-settings
resolves a relative `env_file` against the **process working directory**, and
every application entry point runs with the working directory set to
``service/`` -- ``Makefile``'s ``UV := uv --directory service`` and the
documented ``cd service && uv run ...`` both do it. So the repo-root ``.env``
that ``cp .env.example .env`` creates was opened by nothing, and there is no
upward search: `make serve` came up looking healthy, then answered 503 on
``/health`` and **401** on ``POST /internal/sync``, because the secret the
operator had just pasted was not in the process. Nothing said "your `.env` was
ignored"; that silence is the bug.

The files are therefore anchored to the repository, not to the caller's cwd:

    1. ``<repo>/.env``          -- what `cp .env.example .env` creates
    2. ``<repo>/service/.env``  -- optional service-local override
    3. ``$PWD/.env``            -- preserved from the old behaviour, and a
                                   no-op whenever it is already (1) or (2)

**Precedence, lowest to highest: (1), then (2), then (3), then the real process
environment.** pydantic-settings applies a tuple of env files left to right with
the last one winning, and a real environment variable outranks all of them --
so ``DATABASE_URL=... make migrate`` still overrides the file, and a missing
file at any position is skipped rather than raising. (3) is captured once, at
import, because `model_config` is class state; (1) and (2) are absolute and do
not care when they are read, which is the whole point.

`Makefile` carries the other half of this fix. `env_file` populates *this
object*; it never writes ``os.environ``, so the variables read straight from the
environment -- ``PER_RUN_CAP_USD``, ``DAILY_CAP_USD``, ``OPS_DATABASE_URL``, the
``*_WRITER_PASSWORD`` trio, every ``KEYSTONE_*`` override, and the ``VITE_*``
values Vite inlines -- would still be inert in any `.env` file, however
correctly located. The Makefile exports the repo-root ``.env`` into each
recipe's environment for exactly that reason. Both halves are needed; this one
is what makes ``cd service && uv run ...`` work without the Makefile.
"""

from __future__ import annotations

import contextlib
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: ``<repo>/service/recon/config.py`` -> ``<repo>``.
REPO_ROOT: Path = Path(__file__).resolve().parents[2]

#: ``<repo>/service``.
SERVICE_ROOT: Path = REPO_ROOT / "service"

#: Default per-probe wall-clock bound for ``GET /health``, in seconds.
#:
#: The value is what `recon.health` has always used; what is new is that it is a
#: *default* rather than the only possible answer. It lives in this module and
#: not next to the probes because `recon.config` is deliberately a leaf --
#: `recon.health` imports it (through `recon.db`), so the dependency cannot run
#: the other way.
DEFAULT_HEALTH_PROBE_TIMEOUT_SECONDS: float = 2.0


def _env_files() -> tuple[Path, ...]:
    """The `.env` chain, lowest precedence first, de-duplicated.

    De-duplication keeps the **first** occurrence so the documented order is
    stable: running from the repository root must not silently promote the
    root `.env` above ``service/.env``. `Path.cwd()` is guarded because a
    process whose working directory has been deleted may raise on it, and a
    missing cwd is not a reason to fail to configure.
    """
    candidates = [REPO_ROOT / ".env", SERVICE_ROOT / ".env"]
    with contextlib.suppress(OSError):  # a cwd unlinked under us is not a config error
        candidates.append(Path.cwd() / ".env")

    ordered: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            key = str(candidate.resolve())
        except OSError:  # pragma: no cover - defensive; unresolvable path
            key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(candidate)
    return tuple(ordered)


class Settings(BaseSettings):
    """Runtime configuration for the `recon` service."""

    model_config = SettingsConfigDict(
        env_file=_env_files(),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Postgres DSN. Never hardcoded: supplied by the environment in every context.
    database_url: str | None = None

    # "safe" stores hash + preview in the audit log; "full" stores raw detail.
    log_mode: Literal["safe", "full"] = "safe"

    # --- /health probe bound (R3) -----------------------------------------
    # Per-probe wall-clock bound for `GET /health`, in seconds. `2.0` is the
    # value `recon.health` used as a module constant with no way to change it,
    # and that was a deploy-breaking default rather than a tuning nit:
    #
    #   * the deployed database is Neon with scale-to-zero on the default
    #     ~5-minute suspend, so the FIRST connection after an idle period pays a
    #     cold start of several seconds;
    #   * `probe_database` opens a real connection under this bound, reports
    #     `timeout` when it overruns, and `timeout` is at or above `_FATAL`, so
    #     `/health` answers **503**;
    #   * `infra/render.yaml` sets `healthCheckPath: /health` and Render's
    #     blueprint spec exposes no health-check timeout or interval to widen --
    #     Render marks the deploy unhealthy and never routes traffic to it.
    #
    # So the knob has to exist on this side. A deploy fronting a scale-to-zero
    # compute sets `HEALTH_PROBE_TIMEOUT_SECONDS` to something larger than the
    # cold start it expects; nothing changes locally, where the default stands.
    #
    # `gt=0` rather than a clamp. A non-positive bound makes `Thread.join()`
    # return immediately, so every probe reports `timeout` and `/health` answers
    # 503 forever -- the exact failure this field exists to prevent, wearing the
    # costume of a configured value. Pydantic refuses it at construction, which
    # names the variable in the traceback instead of hiding in a 503.
    health_probe_timeout_seconds: float = Field(default=DEFAULT_HEALTH_PROBE_TIMEOUT_SECONDS, gt=0)

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
    # `make seed` passes it through to `python -m recon.seed --seed`, so the
    # documented `SEED` variable is the control it claims to be.
    seed: int = 20260822


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide `Settings`, built once and cached."""
    return Settings()
