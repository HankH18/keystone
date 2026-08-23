"""`/health` reports what is reachable, can say "no", and cannot hang (R3).

The interesting assertions are the negative ones. Any handler passes "returns 200
with status ok" -- including one that returns a constant. What separates a real
health check from a decorative one is whether it can come back *unhealthy* for a
real reason, so the tests here break a source in three different ways and require
the report to change:

* one entity type missing        -> `degraded` (answering, but not completely);
* nothing readable at all        -> `down`, and the endpoint answers 503;
* a source that never returns    -> `timeout`, **within its bound**, measured on
                                    the wall clock rather than trusted.

The database probe is checked the same way: it opens a connection and runs a
query, so "DATABASE_URL is set" is not mistaken for "Postgres is up".
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from recon import __version__
from recon.adapters import CrmAdapter, FaultInjectingAdapter, build_adapters, stub_records
from recon.app import create_app
from recon.health import SERVICE_NAME, health_report, probe_database, probe_source

STATUSES = {"ok", "degraded", "down", "timeout", "unconfigured"}


def test_health_reports_the_database_and_every_source(owner_engine, seed_tree) -> None:
    report = health_report(build_adapters(seed_tree.root))

    assert report["service"] == SERVICE_NAME
    assert report["version"] == __version__
    assert report["status"] == "ok"

    database = report["checks"]["database"]
    assert database["status"] == "ok"
    assert database["latency_ms"] >= 0

    adapters = build_adapters(seed_tree.root)
    sources = report["checks"]["sources"]
    assert sorted(sources) == ["appdb", "crm", "payments"]
    for source_id, result in sources.items():
        assert result["status"] == "ok", source_id
        assert result["latency_ms"] >= 0
        assert result["latest_generation"] == max(seed_tree.generations)
        assert set(result["entities"]) == set(adapters[source_id].entity_types)


def test_the_database_probe_actually_queries(owner_engine) -> None:
    result = probe_database(timeout=5.0)
    assert result["status"] == "ok"
    assert "latency_ms" in result


def test_a_source_missing_one_entity_type_reports_degraded(broken_tree: Path) -> None:
    """`crm` still answers for contacts and not for deals -- that is not `ok`."""
    adapters = build_adapters(broken_tree)
    result = probe_source(adapters["crm"])

    assert result["status"] == "degraded"
    assert result["entities"]["contact"]["status"] == "ok"
    assert result["entities"]["deal"]["status"] == "down"
    assert result["entities"]["deal"]["detail"]


def test_a_degraded_source_degrades_the_whole_report(broken_tree: Path, owner_engine) -> None:
    report = health_report(build_adapters(broken_tree))
    assert report["status"] == "degraded"
    assert report["checks"]["sources"]["crm"]["status"] == "degraded"
    assert report["checks"]["sources"]["appdb"]["status"] == "ok"


def test_an_unreachable_source_reports_down(tmp_path: Path) -> None:
    result = probe_source(CrmAdapter(tmp_path / "nothing-here"))
    assert result["status"] == "down"
    assert result["detail"]


def test_a_hanging_source_is_reported_as_timeout_within_its_bound() -> None:
    adapter = FaultInjectingAdapter(source_id="crm", mode="hang", records=stub_records(1))

    started = time.monotonic()
    result = probe_source(adapter, timeout=0.3)
    elapsed = time.monotonic() - started

    assert result["status"] == "timeout"
    assert elapsed < 2.0, f"/health's source probe was not bounded: {elapsed:.2f}s"
    assert result["latency_ms"] >= 300


def test_health_answers_even_when_every_source_hangs(owner_engine) -> None:
    """The whole report is bounded, not just one probe."""
    adapters = {
        source_id: FaultInjectingAdapter(source_id=source_id, mode="hang")
        for source_id in ("crm", "appdb", "payments")
    }

    started = time.monotonic()
    report = health_report(adapters, timeout=0.3)
    elapsed = time.monotonic() - started

    assert elapsed < 4.0, f"/health hung for {elapsed:.2f}s"
    assert report["status"] == "down"
    for result in report["checks"]["sources"].values():
        assert result["status"] == "timeout"


def test_the_endpoint_serves_the_report(owner_engine) -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["service"] == SERVICE_NAME
    assert payload["version"] == __version__
    assert payload["status"] in STATUSES
    assert payload["checks"]["database"]["status"] in STATUSES
    assert sorted(payload["checks"]["sources"]) == ["appdb", "crm", "payments"]
    for result in payload["checks"]["sources"].values():
        assert result["status"] in STATUSES
        assert "latency_ms" in result


def test_the_endpoint_says_503_when_the_sources_are_really_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, owner_engine
) -> None:
    """A health endpoint that always answers 200 is a liveness check in disguise.

    Only the *source registry* is redirected -- at an empty directory, so every
    source is genuinely unreadable. The real `health_report` then runs, reaches
    `down` on its own evidence, and the endpoint has to turn that into a 503.
    Patching `health_report` itself would have tested the patch.
    """
    import recon.health as health_module

    monkeypatch.setattr(
        health_module, "build_adapters", lambda *args, **kwargs: build_adapters(tmp_path / "gone")
    )
    with TestClient(create_app()) as client:
        response = client.get("/health")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "down"
    assert payload["checks"]["database"]["status"] == "ok", (
        "the database is fine; it is the sources that are down"
    )
    assert all(result["status"] == "down" for result in payload["checks"]["sources"].values())
