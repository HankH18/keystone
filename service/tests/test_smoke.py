"""Smoke tests: the app serves /health and the suite harness runs."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from recon import __version__
from recon.config import Settings

#: The closed status vocabulary `recon.health` reports (see its module docstring).
HEALTH_STATUSES = {"ok", "degraded", "down", "timeout", "unconfigured"}


def test_health_reports_real_per_source_and_database_checks(client: TestClient) -> None:
    """`/health` carries real probe results, one per source plus the database.

    This assertion replaces ``response.json() == {..., "checks": {}}``, which was
    an assertion that the checks map is EMPTY. That was T-0's scaffolding
    contract (docs/TASKS.md T-0 acceptance clause 1: *"FastAPI hello + `/health`
    stub"*), and ``recon/app.py`` carried the matching note in its own docstring:
    *"Per-source adapter and database reachability checks land in T-4 and
    populate the currently empty `checks` map."* T-4 is the ticket that does it
    -- TASKS.md T-4 acceptance clause 7, *"`/health` reports per-source + DB
    reachability"*, and DESIGN.md pins ``GET /health`` as "service + per-source
    adapter + DB reachability". An assertion that the map stays empty cannot
    survive the map being populated, so keeping it would have meant either a red
    suite forever or gutting the endpoint to satisfy the stub.

    What is asserted instead is strictly stronger than what it replaces: the
    envelope is still pinned exactly, and every check is now required to be
    present, named, and carrying a status from the closed vocabulary plus a
    measured latency. The *values* are deliberately not pinned here -- whether
    the database is reachable is a property of the environment, not of the code,
    and `tests/ingest/test_health.py` is where degraded/down/timeout are proved
    by actually breaking a source.
    """
    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"status", "service", "version", "checks"}
    assert payload["service"] == "keystone"
    assert payload["version"] == __version__
    assert payload["status"] in HEALTH_STATUSES

    checks = payload["checks"]
    assert set(checks) == {"database", "sources"}
    assert checks["database"]["status"] in HEALTH_STATUSES
    assert checks["database"]["latency_ms"] >= 0

    assert sorted(checks["sources"]) == ["appdb", "crm", "payments"]
    for source_id, result in checks["sources"].items():
        assert result["status"] in HEALTH_STATUSES, source_id
        assert result["latency_ms"] >= 0, source_id


def _run_suite(service_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "recon.suite", *args],
        cwd=service_root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_suite_module_runs_its_checks_and_exits_non_zero(service_root: Path) -> None:
    """The harness runs its registered checks and fails loudly on the unfinished one.

    This assertion replaces ``returncode == 0`` plus ``"no checks yet"``, which
    was an assertion that the check registry is EMPTY. That was T-0's scaffolding
    contract (docs/TASKS.md, T-0 acceptance clause 4: *"``python -m recon.suite``
    exists as a stub that exits 0 with 'no checks yet'"*) and it was replaced
    when the first real check landed: ``recon.suite.mirror``'s
    ``mirror-unchanged`` is registered in ``CHECKS``, and DESIGN.md pins the
    harness as one that *"prints the scorecard and exits non-zero on any
    failure"*. An assertion that the registry is empty cannot survive the
    registry being populated, and keeping it would have meant either a red suite
    forever or deleting the check.

    What is asserted instead is the contract that holds today, and it is
    deliberately not "exits with some code": exit status exactly 1, the
    ``mirror-unchanged`` row present and FAILing, the reason naming the module
    that does not exist yet rather than an infrastructure problem, the tally
    line, and ``no checks yet`` gone. If the check were quietly unregistered to
    make the suite green, every one of those goes red.
    """
    result = _run_suite(service_root)

    assert result.returncode == 1, result.stderr
    assert "scorecard" in result.stdout.lower()
    assert "mirror-unchanged" in result.stdout
    assert "FAIL" in result.stdout
    assert "not yet implemented" in result.stdout
    assert "recon.reconciler" in result.stdout
    # The mirror-unchanged row must be the failing one. Deliberately NOT a
    # suite-wide tally: "0/1 passed" asserted that the registry holds exactly
    # one check, which T-8c ended by registering the graded spend-cap-burst
    # row that SPEC gate 1 requires. Pin the row, not the count.
    mirror_row = next(
        line for line in result.stdout.splitlines() if line.startswith("mirror-unchanged")
    )
    assert "FAIL" in mirror_row, result.stdout
    assert "no checks yet" not in result.stdout


def test_suite_module_accepts_only_flag(service_root: Path) -> None:
    result = _run_suite(service_root, "--only", "not-a-real-check-yet")

    assert result.returncode == 0, result.stderr
    assert "no checks yet" in result.stdout


def test_settings_defaults_are_env_driven_and_secret_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defaults must carry no DSN and no secret.

    The environment is cleared first on purpose: the claim under test is about the
    *code's* defaults, so reading an inherited DATABASE_URL (as CI legitimately
    sets) would test the environment instead of the code.
    """
    for var in (
        "DATABASE_URL",
        "TRIGGER_SECRET",
        "ANTHROPIC_API_KEY",
        "LOG_MODE",
        "LLM_PROVIDER",
        "SEED",
    ):
        monkeypatch.delenv(var, raising=False)

    settings = Settings(_env_file=None)

    assert settings.log_mode == "safe"
    assert settings.llm_provider == "mock"
    assert settings.seed == 20260822
    assert settings.database_url is None
    assert settings.trigger_secret is None
    assert settings.anthropic_api_key is None


def test_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same fields must actually be env-driven, not merely absent."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h:5432/d")
    monkeypatch.setenv("LOG_MODE", "full")

    settings = Settings(_env_file=None)

    assert settings.database_url == "postgresql+psycopg://u:p@h:5432/d"
    assert settings.log_mode == "full"
