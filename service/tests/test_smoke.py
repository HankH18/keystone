"""Smoke tests: the app serves /health and the suite harness runs."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from recon import __version__
from recon.config import Settings


def test_health_returns_ok_payload(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "keystone",
        "version": __version__,
        "checks": {},
    }


def _run_suite(service_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "recon.suite", *args],
        cwd=service_root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_suite_module_exits_zero(service_root: Path) -> None:
    result = _run_suite(service_root)

    assert result.returncode == 0, result.stderr
    assert "scorecard" in result.stdout.lower()
    assert "no checks yet" in result.stdout


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
