"""Smoke tests: the app serves /health and the suite harness runs."""

from __future__ import annotations

import os
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
    """Run the harness with NO database and NO writable scorecard directory.

    `DATABASE_URL` is removed deliberately. These are smoke tests: what they
    assert is that the module runs, prints a scorecard and exits non-zero when a
    row is red -- not what the graded pass finds. With a database configured the
    same argv builds the real 100k pipeline, which takes minutes and belongs in
    `tests/suite/`, not here. `KEYSTONE_SCORECARD_DIR` is redirected so a smoke
    test can never overwrite the committed `docs/scorecard.*`.
    """
    env = dict(os.environ)
    env.pop("DATABASE_URL", None)
    env.pop("KEYSTONE_REQUIRE_DB", None)
    env["KEYSTONE_SCORECARD_DIR"] = str(service_root / ".pytest_cache" / "smoke-scorecard")
    return subprocess.run(
        [sys.executable, "-m", "recon.suite", *args],
        cwd=service_root,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        env=env,
    )


def test_suite_module_prints_a_scorecard_and_exits_non_zero_on_a_red_row(
    service_root: Path,
) -> None:
    """The harness prints its scorecard and exits 1 when any row is red.

    **This replaces two assertions that no longer describe the repository, and
    the evidence is in the repository rather than in a preference.** The previous
    version asserted ``"not yet implemented" in stdout`` and ``"recon.reconciler"
    in stdout`` -- i.e. that ``recon.reconciler`` DOES NOT EXIST, which was
    ``recon.suite.mirror.reconciler_entrypoint``'s documented state until T-9
    landed it. ``recon/reconciler.py`` is 1,400 lines with its own test package,
    so that assertion was already failing before T-14 touched anything, and it
    could only be made green again by deleting the reconciler.

    It also asserted ``returncode == 1`` for a full ``python -m recon.suite`` run
    with no arguments. After T-14 registered the remaining fourteen rows, that is
    an assertion that the whole graded harness is permanently red -- and a
    120-second timeout on a run whose graded pass takes minutes.

    What is asserted instead is the durable half of the original claim, and it is
    still specific: the module runs, it prints a scorecard, a check that cannot
    run is a **FAIL** carrying its reason (never a skip), and the process exits
    non-zero. `tests/suite/` covers the registry and the individual rows.
    """
    result = _run_suite(service_root, "--only", "golden-diff", "--no-write")

    assert result.returncode == 1, result.stdout[-2000:]
    assert "scorecard" in result.stdout.lower()
    assert "SKIP" not in result.stdout
    row = next(line for line in result.stdout.splitlines() if line.startswith("golden-diff"))
    assert "FAIL" in row, result.stdout[-2000:]
    assert "DATABASE_URL" in result.stdout, "the row must name the real cause"
    assert "no checks yet" not in result.stdout


def test_suite_module_refuses_an_unknown_only_name(service_root: Path) -> None:
    """A typo in ``--only`` must not select the empty set and report success.

    **This replaces an assertion that a typo exits 0 with "no checks yet".** That
    was T-0's scaffolding tolerance -- the flag's own help read "Unknown names
    are ignored for now" -- and it is the harness's own failure mode: a run that
    executed ZERO checks and returned success. T-14's contract is "exits non-zero
    on any failure" over a registry of sixteen rows, and a misspelled ``--only``
    is the cheapest way to get a green out of it. The name is echoed so the
    operator can see which spelling was refused.
    """
    result = _run_suite(service_root, "--only", "not-a-real-check-yet")

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "not-a-real-check-yet" in combined, combined[-1000:]
    assert "no checks yet" not in result.stdout


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
