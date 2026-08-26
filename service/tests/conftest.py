"""Shared pytest fixtures for the service test suite."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from recon.app import create_app
from recon.config import env_file_chain_disabled

SERVICE_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
def _no_developer_env_file() -> Iterator[None]:
    """The suite reads the process environment; never the developer's `.env`.

    `README.md` step 2 is ``cp .env.example .env``, and following it used to
    turn four tests red -- the ones asserting that an unconfigured trigger
    secret **fails closed** with a 401. They clear the variable with
    ``monkeypatch.delenv``, which empties `os.environ` and leaves
    `recon.config`'s repo-root `.env` still answering underneath, so the
    unconfigured state they set up never existed and the safety property went
    untested on exactly the machines that were set up correctly.

    Suite-wide rather than four `_env_file=None` constructions, because the
    problem is not those four tests: any test on any path may reach
    `get_settings()`, and `.env` is gitignored, so a value it supplies is a
    value no other machine and no CI runner has. A verdict that moves with an
    untracked file is not a verdict. The repo-root `.env` currently carries a
    `DATABASE_URL` pointing at the graded `keystone` database, which is the same
    hazard wearing its worst costume.

    Nothing ambient is lost. `env_file_chain_disabled` removes only the file
    source; the real process environment still wins over everything, so
    ``DATABASE_URL=... uv run pytest``, `make test` (which exports `.env` into
    each recipe's environment), every `monkeypatch.setenv`, and every explicit
    `Settings(...)` argument all behave exactly as before. What changes is that
    an unset variable is now genuinely unset.

    Session-scoped and autouse: it is entered before any other fixture builds a
    `Settings`, and `get_settings`'s cache is cleared on both edges so an
    instance built at import time cannot survive into the session.
    """
    with env_file_chain_disabled():
        yield


@pytest.fixture(scope="session")
def service_root() -> Path:
    """Absolute path to `service/` (the uv project root)."""
    return SERVICE_ROOT


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A TestClient bound to a freshly built application."""
    with TestClient(create_app()) as test_client:
        yield test_client
