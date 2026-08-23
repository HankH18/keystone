"""Fixtures for the privacy tests.

Two sources of truth, and neither of them is a hand-written example:

* **A real dev-profile dataset.** ``python -m recon.seed --profile dev --out
  <tmp>`` is run once per session into a temporary tree (never without
  ``--out``, which would rewrite the committed ``golden/`` set), and the tests
  enumerate the PII field paths that dataset *actually* contains. If the
  generator grows a field, the table-driven tests pick it up without being
  edited -- and if the redactor does not cover it, they go red.
* **The live Postgres.** The purge tests exercise real backdated rows through
  real DELETE/UPDATE statements against the migrated schema, because the whole
  question the purge job answers -- *which principal is allowed to do this, and
  what does the foreign-key graph permit* -- is a property of the database and
  cannot be proved against a fake.

The database skip follows ``tests/schema/conftest.py``: skipping keeps a laptop
without docker usable, and ``KEYSTONE_REQUIRE_DB=1`` turns the skip into a hard
failure so a database-less run can never report a green that proves nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, create_engine, text

from recon.db import DatabaseNotConfigured, database_url
from recon.privacy import PII_KEYS

SERVICE_ROOT = Path(__file__).resolve().parents[2]

#: Committed canonical seed, the same one `tests/seed` uses.
DEV_SEED = 20260822

#: The generated files, source by source, exactly as `recon.seed` lays them out.
FIXTURE_FILES: tuple[tuple[str, str], ...] = (
    ("crm.contact", "crm/gen1/contact.jsonl"),
    ("crm.deal", "crm/gen1/deal.jsonl"),
    ("appdb.student", "appdb/gen1/student.jsonl"),
    ("appdb.enrollment", "appdb/gen1/enrollment.jsonl"),
    ("payments.payment", "payments/gen1/payment.jsonl"),
)

REQUIRE_DB_ENV = "KEYSTONE_REQUIRE_DB"

SKIP_REASON = (
    "DATABASE_URL is not set: the purge tests need the live Postgres from "
    "infra/docker-compose.yml (host port 55432). Export DATABASE_URL and run "
    "`uv run alembic upgrade head` first."
)

REQUIRE_DB_REASON = (
    f"{REQUIRE_DB_ENV} is set, so the purge tests must actually run -- but "
    "DATABASE_URL is not configured, so every one of them would have skipped and "
    "the run would have reported a green that proves nothing."
)


def database_is_required() -> bool:
    raw = os.environ.get(REQUIRE_DB_ENV, "")
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


# ---------------------------------------------------------------------------
# the generated dataset
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def dev_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real `--profile dev` tree in a temp directory. Never touches golden/."""
    root = tmp_path_factory.mktemp("privacy-dev")
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONPATH"] = str(SERVICE_ROOT)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "recon.seed",
            "--seed",
            str(DEV_SEED),
            "--profile",
            "dev",
            "--out",
            str(root),
            "--quiet",
        ],
        cwd=SERVICE_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return root


@pytest.fixture(scope="session")
def dev_records(dev_tree: Path) -> dict[str, list[dict[str, Any]]]:
    """The first 200 records of every generated entity, keyed `source.entity`."""
    records: dict[str, list[dict[str, Any]]] = {}
    for label, relative in FIXTURE_FILES:
        path = dev_tree / "fixtures" / relative
        assert path.exists(), f"seed run produced no {relative}"
        with path.open() as handle:
            records[label] = [json.loads(line) for _, line in zip(range(200), handle, strict=False)]
        assert records[label], f"{relative} is empty"
    return records


def _walk(obj: Any, prefix: str) -> Iterator[tuple[str, Any]]:
    if isinstance(obj, dict):
        for name, value in obj.items():
            yield from _walk(value, f"{prefix}.{name}" if prefix else str(name))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from _walk(value, f"{prefix}[{index}]")
    else:
        yield prefix, obj


@pytest.fixture(scope="session")
def pii_field_paths(dev_records: dict[str, list[dict[str, Any]]]) -> tuple[str, ...]:
    """Every dotted path in the real dataset whose leaf key is a known PII key.

    Derived from the data, not from a list in a test: the assertion "the
    redactor covers every PII field the generator emits" is only worth anything
    if the field set comes from the generator.
    """
    found: set[str] = set()
    for label, records in dev_records.items():
        for record in records:
            for path, value in _walk(record, ""):
                if value is None:
                    continue
                leaf = path.rsplit(".", 1)[-1]
                if leaf in PII_KEYS:
                    found.add(f"{label}.{path}")
    return tuple(sorted(found))


@pytest.fixture(scope="session")
def raw_pii_values(dev_records: dict[str, list[dict[str, Any]]]) -> tuple[str, ...]:
    """Every distinct raw PII string in the sampled dataset, for the leak hunt."""
    values: set[str] = set()
    for records in dev_records.values():
        for record in records:
            for path, value in _walk(record, ""):
                leaf = path.rsplit(".", 1)[-1]
                if leaf in PII_KEYS and isinstance(value, str) and len(value) >= 4:
                    values.add(value)
    return tuple(sorted(values))


# ---------------------------------------------------------------------------
# the live database
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def configured_url() -> str:
    try:
        return database_url().render_as_string(hide_password=False)
    except DatabaseNotConfigured:
        if database_is_required():
            pytest.fail(REQUIRE_DB_REASON, pytrace=False)
        pytest.skip(SKIP_REASON)


@pytest.fixture(scope="session")
def owner_engine(configured_url: str) -> Iterator[Engine]:
    """Engine for the ops/migration principal in ``DATABASE_URL``."""
    engine = create_engine(configured_url, future=True)
    with engine.connect() as conn:
        migrated = conn.execute(text("SELECT to_regclass('public.audit_log')")).scalar()
    if migrated is None:
        pytest.fail(
            "DATABASE_URL points at a database with no Keystone schema. "
            "Run `uv run alembic upgrade head` in service/ first."
        )
    yield engine
    engine.dispose()


@pytest.fixture
def owner_conn(owner_engine: Engine) -> Iterator[Connection]:
    """An owner transaction that is ALWAYS rolled back.

    Every purge test seeds its own backdated rows and sweeps them inside this
    transaction, so the shared development database is left byte-identical.
    """
    with owner_engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


# ---------------------------------------------------------------------------
# module-level structlog loggers
# ---------------------------------------------------------------------------


def uncache_logger(proxy: Any) -> None:
    """Forget the bound logger structlog cached on a module-level lazy proxy.

    `configure_logging(cache=True)` -- the production setting -- makes
    `structlog.get_logger(...)` memoise the assembled bound logger on the proxy
    the first time it is used, by rebinding `proxy.bind` in the instance dict.
    A module-level `log = structlog.get_logger("recon.ingest")` is therefore
    pinned to whatever stream the FIRST test in the session to touch it
    configured, and a later test driving that same object would assert against a
    stream that is closed by then.

    Clearing the cache is what makes those tests order-independent. It is not
    what makes them pass: the assertion is still that the configuration
    `create_app()` installed redacts.
    """
    proxy.__dict__.pop("bind", None)
