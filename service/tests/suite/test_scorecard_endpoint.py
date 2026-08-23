"""`GET /api/scorecard` -- mounted, scoped, and loud when it has nothing to serve.

Every assertion here is about a failure mode that would otherwise be silent in
the dashboard:

* **mounted.** The route is resolved from ``recon.app.create_app()``, not from
  the router object. Two routers in this service were built, tested and left
  unmounted because their tests imported the router directly; ``recon/app.py``'s
  own docstring records it. A test that mounts the router itself cannot see that.
* **admin scope.** The scorecard is an org-wide operational surface. A client key
  getting a 200 here would leak what a run found across every tenant.
* **503, never an empty scorecard.** Zeroes would render as an overview reporting
  that nothing is wrong.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from recon.app import create_app
from recon.suite.report import SCORECARD_JSON, SCORECARD_TXT
from tests.integration.test_route_table import api_routes

DEMO_ADMIN_API_KEY = "keystone-demo-admin-8c25e0b71a94f36d"
DEMO_CLIENT_API_KEY = "keystone-demo-client-3f7a19c4e2b84d05"
ADMIN = {"X-Api-Key": DEMO_ADMIN_API_KEY}
CLIENT = {"X-Api-Key": DEMO_CLIENT_API_KEY}

SAMPLE = {
    "generated_at": "2026-08-23T00:00:00+00:00",
    "run_id": "recon-abc123",
    "conflicts": {"total": 3050, "by_type": {"C1": 500, "C14": 50}},
    "proposals": {"total": 3050, "by_status": {"pending": 2670, "sensitive_hold": 380}},
    "checks": {"golden-diff": True, "join-check": False},
    "passed": False,
}


@pytest.fixture
def card_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KEYSTONE_SCORECARD_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def api(scratch_database: str, card_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


def _write(card_dir: Path, body: object) -> None:
    (card_dir / SCORECARD_JSON).write_text(json.dumps(body), encoding="utf-8")
    (card_dir / SCORECARD_TXT).write_text("scorecard\n", encoding="utf-8")


def test_the_route_is_mounted_on_the_real_application(api: TestClient) -> None:
    """Walked, not read flat: `app.routes` nests, and a flat read would find less."""
    paths = {route.path for route in api_routes(api.app)}

    assert "/api/scorecard" in paths, sorted(paths)


def test_it_serves_the_artifact_the_suite_wrote(api: TestClient, card_dir: Path) -> None:
    _write(card_dir, SAMPLE)

    response = api.get("/api/scorecard", headers=ADMIN)

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == "recon-abc123"
    assert body["conflicts"]["by_type"]["C14"] == 50
    assert body["checks"]["join-check"] is False
    assert body["artifact_modified_at"], "staleness must be visible, not implied"


def test_a_client_key_is_refused(api: TestClient, card_dir: Path) -> None:
    _write(card_dir, SAMPLE)

    response = api.get("/api/scorecard", headers=CLIENT)

    assert response.status_code == 403, response.text


def test_no_key_is_refused(api: TestClient, card_dir: Path) -> None:
    _write(card_dir, SAMPLE)

    assert api.get("/api/scorecard").status_code == 401


def test_a_missing_artifact_is_a_loud_503_not_an_empty_scorecard(api: TestClient) -> None:
    response = api.get("/api/scorecard", headers=ADMIN)

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["title"] == "no scorecard has been generated"
    assert "recon.suite" in body["detail"]
    assert "conflicts" not in body, "a zeroed scorecard must never be served in its place"


def test_a_truncated_artifact_is_refused(api: TestClient, card_dir: Path) -> None:
    (card_dir / SCORECARD_JSON).write_text("{not json", encoding="utf-8")

    response = api.get("/api/scorecard", headers=ADMIN)

    assert response.status_code == 503, response.text


def test_an_artifact_missing_an_a4_key_is_refused(api: TestClient, card_dir: Path) -> None:
    """A body without `conflicts` renders as Mismatch on every row; refuse it here."""
    incomplete = {key: value for key, value in SAMPLE.items() if key != "conflicts"}
    _write(card_dir, incomplete)

    response = api.get("/api/scorecard", headers=ADMIN)

    assert response.status_code == 503, response.text
    assert "conflicts" in response.json()["detail"]
