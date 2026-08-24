"""Fixtures for the incident-clustering tests (R25, stretch #8).

Three things here are worth reading before trusting anything in this package.

**1. The database is real, and required.** Everything about the write side --
the `recon_writer` grants on `incidents`/`conflict_incidents`, migration 0010's
reserve and settle triggers, the ledger arithmetic -- lives in Postgres. A
mocked connection would assert that the Python issued the right SQL, which is
the claim that is *not* in doubt. `KEYSTONE_REQUIRE_DB` behaves exactly as it
does for `tests/schema` and `tests/budget`; the gate helpers are imported from
`tests.schema.conftest` rather than re-implemented so the three packages cannot
drift into disagreeing about what "the database is required" means.

**2. The stand-in that used to be here is GONE, and this is what replaced it.**
Until 2026-08-24 the embedding models were unpriced in the committed
`prices.yaml`, and :func:`embedding_prices` supplied both the parsed rates and
the `budget_model_prices` rows itself -- the one place in this package where the
green was evidence of something the deployed service did not have.
`prices.yaml` version 2 and migration `0016_price_embedding_models` closed that,
so the fixture now returns the **committed** table and *asserts* that the
database it is about to reserve against really holds those rates. It supplies
nothing. `test_provider.py::test_the_price_gap_is_closed` pins the closure from
the other side.

Everything downstream was already real and still is: `recon.budget.reserve`
really inserts a price-bound reservation, the reserve trigger really re-derives
the worst case from `budget_model_prices`, the settle trigger really refuses
anything but a full charge, the ledger really moves.

**3. Golden conflicts are loaded from the committed grading contract**, not
invented here. `golden/conflicts.json` is the 3,050-conflict set every gate runs
against, and its fingerprints are computed with `recon.reference.fingerprint` --
the one callable that defines them -- so the rows these tests cluster are the
rows the reconciler would have written.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Iterator, Sequence
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text

from recon.budget import DAILY_SCOPE_ENV, PriceTable, load_price_table
from recon.db import DatabaseNotConfigured, database_url
from recon.reference import fingerprint
from tests.schema.conftest import (
    REQUIRE_DB_REASON,
    SKIP_REASON,
    database_is_required,
)

#: Marker embedded in every row this package commits, so teardown finds them all.
TEST_TAG = "incidents-test"

#: `<repo>/golden/conflicts.json` -- this file is
#: `<repo>/service/tests/incidents/conftest.py`.
GOLDEN_CONFLICTS = Path(__file__).resolve().parents[3] / "golden" / "conflicts.json"

#: The rates `prices.yaml` version 2 commits and migration
#: `0016_price_embedding_models` seeds, in microusd per token. **This is not a
#: stand-in and nothing inserts it** -- it is the literal the fixture below
#: checks the database against, so a database migrated past 0016 with different
#: numbers in it fails here rather than three layers down inside a trigger.
#:
#: All four rates per model are the same number because an embeddings endpoint
#: has one rate, emits no output tokens and has no prompt cache;
#: `ck_model_price_positive` forbids a zero on the first two, and
#: `recon.budget.worst_case_microusd` prices the input side at the *cache-write*
#: rate. `prices.yaml` states all three reasons at the block.
COMMITTED_EMBEDDING_RATES: dict[str, str] = {
    "mock-embedding-v1": "0.06",
    "text-embedding-3-small": "0.02",
    "voyage-3.5": "0.06",
}

_RATE_COLUMNS = ("input_rate", "output_rate", "cache_read_rate", "cache_write_rate")


def unique(prefix: str) -> str:
    """A collision-proof identifier for one test's rows."""
    return f"{prefix}-{TEST_TAG}-{uuid.uuid4()}"


@pytest.fixture(scope="session")
def configured_url() -> str:
    """The configured DSN, or skip/fail per `KEYSTONE_REQUIRE_DB`."""
    try:
        return database_url().render_as_string(hide_password=False)
    except DatabaseNotConfigured:
        if database_is_required():
            pytest.fail(REQUIRE_DB_REASON, pytrace=False)
        pytest.skip(SKIP_REASON)


@pytest.fixture(scope="session")
def owner_engine(configured_url: str) -> Iterator[Engine]:
    """Engine for the principal in `DATABASE_URL` -- the schema owner locally."""
    engine = create_engine(configured_url, future=True)
    with engine.connect() as conn:
        migrated = conn.execute(text("SELECT to_regclass('public.incidents')")).scalar()
    if migrated is None:
        pytest.fail(
            "DATABASE_URL points at a database with no Keystone schema. "
            "Run `uv run alembic upgrade head` in service/ first."
        )
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def embedding_prices(owner_engine: Engine) -> PriceTable:
    """The **committed** price table, after checking the database agrees with it.

    This fixture used to be a stand-in: it inserted the embedding rates into
    `budget_model_prices` itself, because `prices.yaml` did not carry them and no
    migration seeded them. That gap is closed -- `prices.yaml` version 2 commits
    the rates and `0016_price_embedding_models` seeds them -- so the fixture now
    supplies **nothing** and verifies instead.

    It fails rather than skips on a database that is behind, because a skip here
    is the outcome this package cannot afford: the whole point of these tests is
    that a *deployed* Keystone can run the clustering, and a suite that quietly
    passed against an unmigrated database would be asserting the opposite of
    what it claims. The failure names the migration.

    Both halves are checked -- the parsed file a test passes as `table=`, and the
    ops-owned rows the reserve trigger actually reads -- because two sources for
    one rate is exactly the drift `tests/budget/test_prices.py
    ::test_the_seeded_database_rates_are_the_committed_price_table` exists to
    catch, and this package reserves against both of them.
    """
    committed = load_price_table()
    missing = sorted(set(COMMITTED_EMBEDDING_RATES) - set(committed.models))
    if missing:
        pytest.fail(
            f"prices.yaml does not price {missing}. recon.incidents cannot build any "
            "provider without them -- see prices.yaml's embedding block.",
            pytrace=False,
        )

    columns = ", ".join(_RATE_COLUMNS)
    with owner_engine.connect() as conn:
        rows = {
            row.model: row
            for row in conn.execute(
                text(
                    f"SELECT model, {columns} FROM budget_model_prices WHERE model = ANY(:models)"
                ),
                {"models": sorted(COMMITTED_EMBEDDING_RATES)},
            )
        }

    absent = sorted(set(COMMITTED_EMBEDDING_RATES) - set(rows))
    if absent:
        pytest.fail(
            f"budget_model_prices has no row for {absent}, so migration 0010's reserve "
            "trigger would refuse every embedding reservation. Run "
            "`uv run alembic upgrade head` in service/ -- "
            "0016_price_embedding_models seeds them.",
            pytrace=False,
        )
    for model, rate in sorted(COMMITTED_EMBEDDING_RATES.items()):
        expected = Decimal(rate)
        assert committed.price(model).input == expected, f"prices.yaml drifted on {model}"
        for column in _RATE_COLUMNS:
            assert Decimal(getattr(rows[model], column)) == expected, (
                f"budget_model_prices.{column} for {model} is "
                f"{getattr(rows[model], column)}, not the committed {expected}"
            )
    return committed


ScopeFactory = Callable[[], str]


@pytest.fixture
def make_ledger_scope(owner_engine: Engine) -> Iterator[ScopeFactory]:
    """Factory for throwaway `run:` ledger rows, provisioned by **ops**.

    `recon.budget.reserve` has no `scopes` parameter: it always reserves on the
    run scope *and* the mandated daily scope, and a test process is refused the
    real `daily` row outright (`RealDailyScopeRefused`). So a test that reserves
    needs a stand-in daily row, and pointing `KEYSTONE_DAILY_SCOPE` at the run
    row it just made is how it gets **one** number to assert on without anything
    being able to drop a cap. Same arrangement as
    `tests/budget/support.make_scope`, and for the same reason: the capped party
    holds no INSERT on `budget_ledger` outside its own `run:` scope, so a fixture
    that provisioned one through `recon_writer` could not exist.

    A **factory** rather than one scope, because the reservation idempotency key
    is `embed:<run_id>:<batch>:<digest>` and the run scope is `run:<run_id>`: a
    second clustering pass needs a second run id, and a second run id needs its
    own ledger row. Suffixing one scope's run id would name a scope with no
    ledger row at all (`LedgerScopeMissing`), which is the correct refusal.

    The cap is 10 USD in microusd: comfortably above the whole golden set at the
    embedding rate, so a cap hit in this package is a real defect and not a
    fixture that was sized too small.
    """
    created: list[str] = []
    previous = os.environ.get(DAILY_SCOPE_ENV)

    def _make() -> str:
        scope = f"run:{unique('incidents')}"
        with owner_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO budget_ledger (scope, cap_microusd, spent_microusd) "
                    "VALUES (:scope, :cap, 0)"
                ),
                {"scope": scope, "cap": 10_000_000},
            )
        # The newest scope carries the mandated daily cap too, so both scopes
        # `reserve` resolves collapse onto the row this test is watching.
        os.environ[DAILY_SCOPE_ENV] = scope
        created.append(scope)
        return scope

    yield _make

    if previous is None:
        os.environ.pop(DAILY_SCOPE_ENV, None)
    else:
        os.environ[DAILY_SCOPE_ENV] = previous
    with owner_engine.begin() as conn:
        for scope in created:
            conn.execute(text("DELETE FROM budget_reservations WHERE scope = :s"), {"s": scope})
            conn.execute(text("DELETE FROM audit_log WHERE subject LIKE :p"), {"p": f"%{scope}%"})
            conn.execute(text("DELETE FROM budget_ledger WHERE scope = :s"), {"s": scope})


@pytest.fixture
def ledger_scope(make_ledger_scope: ScopeFactory) -> str:
    """One throwaway ledger scope, for a test that only needs one run."""
    return make_ledger_scope()


def run_id_for(scope: str) -> str:
    """The `run_id` whose per-run ledger scope IS `scope`."""
    return scope.removeprefix("run:")


def golden_records(step: int = 1) -> list[dict[str, object]]:
    """The committed golden conflicts, every `step`-th one, in file order.

    `step` exists so a test can cluster a deterministic *subset* without
    inventing data: `golden/conflicts.json` is written in a pinned order by the
    seed generator, so `[::5]` is as reproducible as the whole file.
    """
    records = json.loads(GOLDEN_CONFLICTS.read_text(encoding="utf-8"))
    return records[::step]


def insert_golden_conflicts(
    engine: Engine, records: Sequence[dict[str, object]], *, run_tag: str
) -> list[int]:
    """Commit `records` as real `conflicts` rows and return their ids.

    Fingerprints come from `recon.reference.fingerprint`, the one callable that
    defines them, so these are the rows the reconciler itself would have written
    -- not rows shaped to suit the clusterer.

    Inserted as the **owner**, deliberately: this is data setup, not the property
    under test. The write that *is* under test (`incidents` /
    `conflict_incidents`) goes through `recon_writer` inside
    `recon.incidents.cluster_conflicts`, where the grant is real.

    **`ON CONFLICT DO NOTHING`, never `DO UPDATE`.** A database that already
    holds the golden conflicts -- which is what a real reconcile run leaves
    behind -- holds them with *its* `first_seen_run`/`last_seen_run`, and those
    are the columns re-detection advances. An upsert here would rewrite
    `last_seen_run` on 3,050 graded rows as a side effect of a stretch feature's
    fixture. So an existing row is read, not touched, and the teardown that
    matches on `first_seen_run = run_tag` then deletes only rows this fixture
    actually created.
    """
    ids: list[int] = []
    with engine.begin() as conn:
        for record in records:
            digest = fingerprint(
                str(record["type"]),
                [str(ref) for ref in record["entity_refs"]],  # type: ignore[union-attr]
                [str(path) for path in record["disagreeing_fields"]],  # type: ignore[union-attr]
                record["observed_values"],  # type: ignore[arg-type]
            )
            row = conn.execute(
                text(
                    """
                    INSERT INTO conflicts
                        (fingerprint, type, rule_id, entity_refs, sources,
                         disagreeing_fields, observed_values, oscillating,
                         first_seen_run, last_seen_run)
                    VALUES (:fingerprint, :type, :rule_id, CAST(:entity_refs AS jsonb),
                            CAST(:sources AS jsonb), CAST(:fields AS jsonb),
                            CAST(:observed AS jsonb), :oscillating, :run, :run)
                    ON CONFLICT (fingerprint) DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "fingerprint": digest,
                    "type": record["type"],
                    "rule_id": record["rule_id"],
                    "entity_refs": json.dumps(record["entity_refs"]),
                    "sources": json.dumps(record["sources_involved"]),
                    "fields": json.dumps(record["disagreeing_fields"]),
                    "observed": json.dumps(record["observed_values"]),
                    "oscillating": bool(record["oscillating"]),
                    "run": run_tag,
                },
            ).fetchone()
            if row is None:
                row = conn.execute(
                    text("SELECT id FROM conflicts WHERE fingerprint = :f"), {"f": digest}
                ).one()
            ids.append(int(row.id))
    return ids


@pytest.fixture(scope="session")
def golden_conflict_ids(owner_engine: Engine) -> Iterator[list[int]]:
    """Every committed golden conflict, in the database, cleaned up afterwards.

    Session-scoped because inserting 3,050 rows per test would dominate the
    package's runtime, and because every test here reads the same immutable set.

    The teardown is deliberately narrow in both directions. Conflicts are
    deleted only where `first_seen_run` is *this session's* tag, so rows that
    were already there survive; incidents are deleted only above the id this
    fixture saw before it started, so a batch some other suite wrote is never
    swept up. A teardown broad enough to wipe a table it did not fill is how one
    agent's suite corrupts another's.
    """
    run_tag = unique("run")
    with owner_engine.connect() as conn:
        baseline = int(
            conn.execute(text("SELECT coalesce(max(id), 0) FROM incidents")).scalar_one()
        )
    ids = insert_golden_conflicts(owner_engine, golden_records(), run_tag=run_tag)
    yield ids
    with owner_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM conflict_incidents WHERE incident_id > :baseline"),
            {"baseline": baseline},
        )
        conn.execute(text("DELETE FROM incidents WHERE id > :baseline"), {"baseline": baseline})
        conn.execute(
            text("DELETE FROM conflict_incidents WHERE conflict_id = ANY(:ids)"), {"ids": ids}
        )
        conn.execute(text("DELETE FROM conflicts WHERE first_seen_run = :run"), {"run": run_tag})
