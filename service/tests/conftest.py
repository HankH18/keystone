"""Shared pytest fixtures for the service test suite."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from recon.app import create_app

SERVICE_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def service_root() -> Path:
    """Absolute path to `service/` (the uv project root)."""
    return SERVICE_ROOT


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A TestClient bound to a freshly built application."""
    with TestClient(create_app()) as test_client:
        yield test_client
