"""The one materialized dataset both T-5 suites read.

Building it is expensive and it is a pure function of the committed fixtures, so
it is built **once per process** and memoized here rather than in a pytest
fixture: a session-scoped fixture imported into two `conftest.py` files is two
fixture definitions and would run twice. `tests/er/conftest.py` and
`tests/api/conftest.py` both call :func:`ensure_dataset`.

What it contains, and why that is the honest amount of work:

* the **committed full-profile fixtures** (`fixtures/`, ~120,000 generation-3
  records), not a dev-profile stand-in -- `golden/expected-views.json` is the
  join contract of *that* dataset, and a suite that checks it against a smaller
  one proves nothing about the file it claims to verify;
* **generation 3 only**, following `tests/invariants/conftest.py`: SS7 defines
  current state as generation 3 and the identity layer is built from it.
  Generations 1 and 2 exist for `field_lineage`'s A->B->A history, which
  :func:`ensure_history_dataset` covers on a dev-profile tree -- the shape of a
  three-generation lineage does not need 120,000 records to be true, and paying
  the provenance trigger three times over would put six minutes into every run;
* the identity layer materialized by `recon.resolve.materialize` as
  `recon_writer`, through the real privilege boundary and the real deferred
  provenance triggers (`KS008`, `KS009`).

The database is created, migrated and dropped by this process (`scratchdb`);
`DATABASE_URL` supplies only the server coordinates.
"""

from __future__ import annotations

import atexit
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.er.scratchdb import create_scratch_database, drop_database, use_database

SERVICE_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = SERVICE_ROOT.parent
FIXTURES = REPO_ROOT / "fixtures"
GOLDEN = REPO_ROOT / "golden"

REQUIRE_DB_ENV = "KEYSTONE_REQUIRE_DB"

SKIP_REASON = (
    "DATABASE_URL is not set. The T-5 suites need a Postgres server "
    "(infra/docker-compose.yml, host port 55432); they create and drop their own "
    "database on that server and never touch the one DATABASE_URL names."
)

REQUIRE_DB_REASON = (
    f"{REQUIRE_DB_ENV} is set, so the T-5 suites must actually run -- but DATABASE_URL "
    "is not configured, so every test would have skipped and the run would have "
    "reported a green that proves nothing."
)


@dataclass(frozen=True)
class Dataset:
    """A migrated database holding the ingested fixtures and the identity layer."""

    dsn: str
    generation: int
    ingest_seconds: float
    materialize_seconds: float
    report: Any

    def summary(self) -> str:
        return (
            f"generation {self.generation}: {self.report.persons} persons, "
            f"{self.report.links} links, {self.report.candidates} candidates, "
            f"{self.report.lineage} lineage rows "
            f"(ingest {self.ingest_seconds:.1f}s, materialize {self.materialize_seconds:.1f}s)"
        )


_STATE: Dataset | None = None


def _require_server() -> None:
    if os.environ.get("DATABASE_URL"):
        return
    if os.environ.get(REQUIRE_DB_ENV, "").strip().lower() not in {"", "0", "false", "no", "off"}:
        pytest.fail(REQUIRE_DB_REASON)
    pytest.skip(SKIP_REASON)


def ensure_dataset() -> Dataset:
    """Build (once) and return the materialized dataset."""
    global _STATE
    if _STATE is not None:
        return _STATE

    _require_server()
    if not (FIXTURES / "manifest.json").is_file():
        pytest.fail(
            f"no committed fixture tree at {FIXTURES}: run `make seed` (or "
            "`uv run python -m recon.seed --profile full`) before the T-5 suites."
        )

    dsn = create_scratch_database("t5")
    atexit.register(drop_database, dsn)
    use_database(dsn)

    from recon.adapters import build_adapters
    from recon.ingest import expected_counts_from_manifest, ingest_generation
    from recon.resolve import CURRENT_GENERATION, materialize

    started = time.monotonic()
    report = ingest_generation(
        build_adapters(FIXTURES),
        CURRENT_GENERATION,
        run_id="t5-dataset-gen3",
        expected=expected_counts_from_manifest(FIXTURES),
    )
    failed = [result for result in report.sources if result.status != "ok"]
    if failed:  # pragma: no cover - a broken fixture tree is a hard stop
        raise RuntimeError(f"fixture ingest did not complete: {failed}")
    ingested = time.monotonic()

    materialized = materialize(lineage_generations=(CURRENT_GENERATION,))
    finished = time.monotonic()

    _STATE = Dataset(
        dsn=dsn,
        generation=CURRENT_GENERATION,
        ingest_seconds=ingested - started,
        materialize_seconds=finished - ingested,
        report=materialized,
    )
    return _STATE


_HISTORY: Dataset | None = None


def _seed_dev_tree() -> Path:
    """A real `--profile dev` seed run in a temporary directory.

    Never the committed tree: the generator is run with `--out`, so `fixtures/`
    and `golden/` in the repository are untouched.
    """
    import subprocess
    import sys
    import tempfile

    out_dir = Path(tempfile.mkdtemp(prefix="keystone-t5-history-"))
    completed = subprocess.run(
        [sys.executable, "-m", "recon.seed", "--profile", "dev", "--out", str(out_dir), "--quiet"],
        cwd=SERVICE_ROOT,
        env={**os.environ, "PYTHONHASHSEED": "0", "PYTHONPATH": str(SERVICE_ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:  # pragma: no cover - a broken generator is a hard stop
        raise RuntimeError(f"recon.seed --profile dev failed:\n{completed.stderr}")
    return out_dir / "fixtures"


def ensure_history_dataset() -> Dataset:
    """A three-generation dev-profile dataset, materialized with lineage 1-3.

    `field_lineage` is the only table that retains generations 1 and 2, and
    `materialize`'s **default** covers all three -- so without this the default
    path of the driver would never run in the suite, and "lineage across
    generations" would be a claim rather than a check. Dev profile because the
    shape of a three-generation history is not a function of record count.

    It never touches `DATABASE_URL`: every write goes through an explicitly
    constructed `recon_writer` engine, so the main dataset stays the process's
    configured database and the two cannot be confused for one another.
    """
    global _HISTORY
    if _HISTORY is not None:
        return _HISTORY

    _require_server()
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    from recon.adapters import build_adapters
    from recon.db import ROLE_RECON_WRITER, role_password
    from recon.ingest import expected_counts_from_manifest, ingest_generation
    from recon.resolve import CURRENT_GENERATION, materialize

    dsn = create_scratch_database("t5hist")
    atexit.register(drop_database, dsn)
    fixtures = _seed_dev_tree()

    url = make_url(dsn).set(
        drivername="postgresql+psycopg",
        username=ROLE_RECON_WRITER,
        password=role_password(ROLE_RECON_WRITER),
    )
    engine = create_engine(url, future=True)

    started = time.monotonic()
    adapters = build_adapters(fixtures)
    expected = expected_counts_from_manifest(fixtures)
    generations = sorted({gen for adapter in adapters.values() for gen in adapter.generations()})
    for generation in generations:
        with engine.begin() as conn:
            report = ingest_generation(
                adapters,
                generation,
                run_id=f"t5-history-gen{generation}",
                expected=expected,
                conn=conn,
            )
        failed = [result for result in report.sources if result.status != "ok"]
        if failed:  # pragma: no cover
            raise RuntimeError(f"dev-profile ingest of generation {generation} failed: {failed}")
    ingested = time.monotonic()

    with engine.begin() as conn:
        materialized = materialize(
            generation=CURRENT_GENERATION, lineage_generations=generations, conn=conn
        )
    finished = time.monotonic()

    _HISTORY = Dataset(
        dsn=dsn,
        generation=CURRENT_GENERATION,
        ingest_seconds=ingested - started,
        materialize_seconds=finished - ingested,
        report=materialized,
    )
    return _HISTORY
