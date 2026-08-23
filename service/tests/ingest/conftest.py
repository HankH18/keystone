"""Fixtures for the ingestion tests.

Two things are arranged here and both exist to stop a green that proves nothing.

**A missing database is an error, not a skip.** Same rule and same environment
variable as `tests/schema/conftest.py`: with `KEYSTONE_REQUIRE_DB=1` set, an
absent `DATABASE_URL` fails collection instead of quietly skipping every
DB-backed test in this package. A run that ingested nothing must not be able to
report success.

**The tests own a generation range nobody else uses.** Rather than truncating
shared tables -- which would silently delete whatever another test package
seeded -- the fixture tree is rebuilt with its generations shifted into the 900s
(`gen1 -> gen901`), manifest included, and teardown removes exactly the rows in
that range. The pipeline is exercised on real generated fixtures, in the real
database, without either colliding with or destroying anything else.

**The HTTP tests authenticate, because the endpoint fails closed.**
`/internal/ingest/*` writes to the append-only landing table and is a scheduled-job
endpoint (R19), so an unconfigured secret is a 401 rather than a disabled check.
`trigger_secret` configures one for the duration of a test and drops the cached
`Settings`; `TRIGGER_HEADERS` is what a caller has to present.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text

from recon.db import DatabaseNotConfigured, database_url, reset_engine_cache

SERVICE_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = SERVICE_ROOT.parent

#: Generations these tests own. Anything at or above this is theirs to delete.
TEST_GENERATION_BASE = 900

REQUIRE_DB_ENV = "KEYSTONE_REQUIRE_DB"

SKIP_REASON = (
    "DATABASE_URL is not set: the ingest tests need the live Postgres from "
    "infra/docker-compose.yml (host port 55432). Export DATABASE_URL and run "
    "`uv run alembic upgrade head` first."
)

REQUIRE_DB_REASON = (
    f"{REQUIRE_DB_ENV} is set, so the ingest tests must actually run -- but "
    "DATABASE_URL is not configured, so every DB-backed assertion here would "
    "have skipped and the run would have reported a green that proves nothing."
)

LANDING_TABLES = (
    "stg_crm_contact",
    "stg_crm_deal",
    "stg_student",
    "stg_enrollment",
    "stg_payment",
    "raw_records",
)


def database_is_required() -> bool:
    raw = os.environ.get(REQUIRE_DB_ENV, "")
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


@pytest.fixture(scope="session")
def owner_engine() -> Iterator[Engine]:
    """Engine for the `DATABASE_URL` principal (the schema owner).

    Used only for setup and teardown. Every *production* write in these tests
    goes through `recon_writer`, which is the whole point of the boundary.
    """
    reset_engine_cache()
    try:
        url = database_url()
    except DatabaseNotConfigured:
        if database_is_required():
            pytest.fail(REQUIRE_DB_REASON)
        pytest.skip(SKIP_REASON)
    engine = create_engine(url, future=True)
    with engine.connect() as connection:
        present = connection.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'source_generations'"
            )
        ).scalar()
    if not present:
        pytest.fail(
            "source_generations is missing: run `uv run alembic upgrade head` "
            "(migration 0009 creates the completeness ledger contract SS5.3 needs)."
        )
    try:
        yield engine
    finally:
        _purge(engine)
        engine.dispose()


def _purge(engine: Engine) -> None:
    """Delete every row these tests could have written, and nothing else."""
    with engine.begin() as connection:
        for table in LANDING_TABLES:
            connection.execute(
                text(f"DELETE FROM {table} WHERE generation >= :base"),
                {"base": TEST_GENERATION_BASE},
            )
        for table in ("source_generations", "ingest_runs"):
            connection.execute(
                text(f"DELETE FROM {table} WHERE generation >= :base"),
                {"base": TEST_GENERATION_BASE},
            )


@dataclass(frozen=True)
class FixtureTree:
    """A generated `fixtures/` tree with its generations shifted into the 900s."""

    root: Path
    generations: tuple[int, ...]
    manifest: dict


def _run_seed(out_dir: Path) -> None:
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONPATH"] = str(SERVICE_ROOT)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "recon.seed",
            "--profile",
            "dev",
            "--out",
            str(out_dir),
            "--quiet",
        ],
        cwd=SERVICE_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture(scope="session")
def seed_tree(tmp_path_factory: pytest.TempPathFactory) -> FixtureTree:
    """A real `--profile dev` seed run, re-labelled onto the 900s generations.

    Never the committed tree: the generator is run with `--out`, so the golden
    set in the repository is untouched.
    """
    out_dir = tmp_path_factory.mktemp("ingest-seed")
    _run_seed(out_dir)
    fixtures = out_dir / "fixtures"

    shifted: list[int] = []
    for source_dir in sorted(fixtures.iterdir()):
        if not source_dir.is_dir():
            continue
        for gen_dir in sorted(source_dir.iterdir(), reverse=True):
            if not gen_dir.is_dir() or not gen_dir.name.startswith("gen"):
                continue
            generation = int(gen_dir.name[3:])
            target = TEST_GENERATION_BASE + generation
            gen_dir.rename(source_dir / f"gen{target}")
            shifted.append(target)

    manifest_path = fixtures / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["expected_counts"] = {
        f"gen{TEST_GENERATION_BASE + int(key[3:])}": value
        for key, value in manifest["expected_counts"].items()
    }
    manifest_path.write_text(json.dumps(manifest, sort_keys=True))

    return FixtureTree(fixtures, tuple(sorted(set(shifted))), manifest)


@pytest.fixture
def broken_tree(seed_tree: FixtureTree, tmp_path: Path) -> Path:
    """A copy of the seed tree with the CRM deal snapshot removed.

    A source that answers for one entity type and not the other is `degraded`,
    which is a different fact from `down` and has to be reported as one.
    """
    target = tmp_path / "broken"
    shutil.copytree(seed_tree.root, target)
    latest = max(seed_tree.generations)
    (target / "crm" / f"gen{latest}" / "deal.jsonl").unlink()
    return target


#: The secret these tests configure. Not a real one, and never a default: the
#: endpoint has no default, which is the point of `test_trigger_auth.py`.
TRIGGER_SECRET = "ingest-trigger-secret-for-tests"
TRIGGER_HEADERS = {"X-Trigger-Secret": TRIGGER_SECRET}


@pytest.fixture
def trigger_secret(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Configure the sync job's trigger secret for one test.

    `get_settings` is `lru_cache`d, so an environment change is invisible without
    dropping the cache -- and it is dropped again on teardown so no later test
    inherits an authenticated environment it did not ask for.
    """
    from recon.config import get_settings

    monkeypatch.setenv("TRIGGER_SECRET_SYNC", TRIGGER_SECRET)
    monkeypatch.delenv("TRIGGER_SECRET", raising=False)
    get_settings.cache_clear()
    try:
        yield TRIGGER_SECRET
    finally:
        get_settings.cache_clear()


@pytest.fixture(scope="session")
def malformed_cases() -> list[dict]:
    """The committed SS7 corpus: 24 literal payloads, each with its expected 4xx."""
    path = REPO_ROOT / "fixtures" / "malformed" / "cases.jsonl"
    assert path.is_file(), f"missing committed malformed corpus at {path}"
    cases = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert len(cases) >= 20, "contract SS7 requires at least 20 malformed cases"
    return cases
