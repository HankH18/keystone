"""The ``mirror-unchanged`` check: the control 0006's provenance floor cites.

Migration 0006 (RULING 11) is honest about the limit of its provenance floor:
``recon_writer`` holds INSERT on ``raw_records`` because ingestion is its job,
so fabricating a canonical entity costs three INSERTs rather than being
impossible -- and the third one lands in the landing table. That is only worth
anything if something *reads* the landing table and notices.

Until this module, nothing did. ``recon.suite``'s registry was empty, and both
the migration docstring and a test in ``test_single_use_citation`` justified the
floor by naming a check that did not exist. Citing a control that does not exist
is worse than admitting the limit, because it reads as settled.

So the control is built, and this is where it is proved. The tests split
cleanly along what is and is not implemented:

* the digest **is** implemented and is exercised here against the real
  database, including the exact scenario the floor cites -- ``recon_writer``
  appends a landing row, and the mirror digest moves;
* the "across a reconciler run" half **cannot** be built until ``recon
  .reconciler`` exists (T-9), and ``test_the_check_fails_loudly_...`` asserts
  that it FAILS with that reason. It must never report PASS by hashing an
  untouched database twice, which is the vacuous green the whole exercise is
  about.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from recon.db import ROLE_RECON_WRITER, role_connection
from recon.suite.__main__ import CHECKS, run_check
from recon.suite.checks import FAIL, PASS, CheckResult, NotYetImplemented
from recon.suite.mirror import (
    CHECK_NAME,
    LANDING_TABLES,
    MIRROR_TABLES,
    STAGING_TABLES,
    check_mirror_unchanged,
    compare,
    mirror_digest,
    reconciler_entrypoint,
)
from tests.schema.conftest import INSERT_RAW_RECORD, TEST_TAG, raw_record_params

#: This module's landing generation, distinct from every other module's.
GENERATION = 93


@pytest.fixture
def landing_rows(owner_engine: Engine) -> Iterator[str]:
    """A natural key this module owns, cleaned up whatever the test did."""
    key = f"{TEST_TAG}-mirror-{uuid.uuid4()}"
    yield key
    with owner_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM raw_records WHERE generation = :g AND natural_key = :k"),
            {"g": GENERATION, "k": key},
        )
        conn.execute(
            text("DELETE FROM ingest_runs WHERE run_id = :r"),
            {"r": key},
        )


# ===========================================================================
# The digest covers the whole mirror
# ===========================================================================
def test_the_check_watches_every_landing_and_staging_table(owner_engine: Engine) -> None:
    """Read the table list out of the catalog, not out of the module.

    A check that hashes a hand-maintained list silently stops watching the day
    someone adds a source: the new ``stg_*`` table would be unhashed and a
    reconciler that rewrote it would pass. This asserts the module's list is
    exactly what the schema actually has.
    """
    with owner_engine.connect() as conn:
        staging = set(
            conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename LIKE 'stg\\_%'"
                )
            )
            .scalars()
            .all()
        )
    assert staging == set(STAGING_TABLES), (
        "the mirror check's staging list has drifted from the schema: "
        f"{sorted(staging ^ set(STAGING_TABLES))}"
    )
    assert set(LANDING_TABLES) == {"ingest_runs", "raw_records"}
    assert set(MIRROR_TABLES) == staging | set(LANDING_TABLES)


def test_the_digest_is_stable_when_nothing_writes(owner_engine: Engine) -> None:
    """Two reads, no writes, identical digest.

    The counterpart to the change-detection test below: a digest that moved on
    its own -- because it hashed a physical row order, or a clock -- would make
    every future ``mirror-unchanged`` run a false alarm, which is how a check
    gets disabled.
    """
    with owner_engine.connect() as conn:
        first = mirror_digest(conn)
    with owner_engine.connect() as conn:
        second = mirror_digest(conn)

    assert first.combined() == second.combined()
    assert first.changed_tables(second) == ()
    assert compare(first, second).status == PASS


def test_the_digest_moves_when_recon_writer_appends_a_landing_row(
    owner_engine: Engine, landing_rows: str
) -> None:
    """THE scenario 0006's provenance floor cites, executed literally.

    The floor's honest limit is that ``recon_writer`` can still fabricate --
    the fabrication just has to leave a row in ``raw_records``. This is the
    check that sees it: take a digest, let ``recon_writer`` commit exactly the
    landing row the floor forces it to leave, take another digest, and assert
    the mirror hash changed and names ``raw_records``.

    Committed over a real role connection, not simulated: the claim is about
    what a reader of the landing table would observe after the fact.
    """
    with owner_engine.connect() as conn:
        before = mirror_digest(conn)

    with role_connection(ROLE_RECON_WRITER) as conn:
        conn.execute(INSERT_RAW_RECORD, raw_record_params("crm", landing_rows, GENERATION))

    with owner_engine.connect() as conn:
        after = mirror_digest(conn)

    assert before.changed_tables(after) == ("raw_records",)
    assert after.row_counts["raw_records"] == before.row_counts["raw_records"] + 1

    result = compare(before, after)
    assert result.status == FAIL
    assert result.name == CHECK_NAME
    assert "raw_records" in result.detail


def test_the_digest_moves_when_an_ingest_run_row_is_appended(
    owner_engine: Engine, landing_rows: str
) -> None:
    """A second table, so "the digest moves" is not a property of one query.

    ``ingest_runs`` is the record of what the adapters did. If only
    ``raw_records`` were really hashed, the check would still pass the test
    above while watching one seventh of the mirror.
    """
    with owner_engine.connect() as conn:
        before = mirror_digest(conn)

    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ingest_runs (run_id, source_id, generation, status) "
                "VALUES (:r, 'crm', :g, 'ok')"
            ),
            {"r": landing_rows, "g": GENERATION},
        )

    with owner_engine.connect() as conn:
        after = mirror_digest(conn)

    assert before.changed_tables(after) == ("ingest_runs",)
    assert compare(before, after).status == FAIL


def test_the_digest_moves_when_a_row_is_edited_in_place(
    owner_engine: Engine, landing_rows: str
) -> None:
    """Row counts alone are not the check: content is.

    A mirror whose row count is identical and whose *values* were rewritten is
    exactly the tampering that matters most, and a count-only check would call
    it unchanged.
    """
    with owner_engine.begin() as conn:
        conn.execute(INSERT_RAW_RECORD, raw_record_params("crm", landing_rows, GENERATION))

    with owner_engine.connect() as conn:
        before = mirror_digest(conn)

    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE raw_records SET payload = '{\"tampered\": true}'::jsonb "
                "WHERE generation = :g AND natural_key = :k"
            ),
            {"g": GENERATION, "k": landing_rows},
        )

    with owner_engine.connect() as conn:
        after = mirror_digest(conn)

    assert after.row_counts["raw_records"] == before.row_counts["raw_records"], (
        "the row count is deliberately unchanged here"
    )
    assert before.changed_tables(after) == ("raw_records",)


def test_mirror_digest_refuses_a_table_outside_the_mirror(owner_engine: Engine) -> None:
    """The table name is interpolated into SQL, so the allowlist is the guard."""
    with owner_engine.connect() as conn, pytest.raises(ValueError, match="not mirror tables"):
        mirror_digest(conn, ["entities"])


# ===========================================================================
# The half that is NOT implemented fails loudly
# ===========================================================================
def test_the_check_fails_loudly_because_the_reconciler_does_not_exist_yet() -> None:
    """The anti-vacuous-green assertion, and the most important one here.

    "Across a reconciler run" needs a reconciler; ``recon.reconciler`` is T-9
    and is not written. The tempting implementation -- hash, do nothing, hash
    again, PASS -- would produce a permanently green row whose greenness is
    caused by the absence of the thing under test.

    So the check FAILS, and says why. This test pins that: never PASS, and the
    detail names the missing module rather than reading like an infrastructure
    problem.
    """
    result = run_check(CHECK_NAME, check_mirror_unchanged)

    assert result.status == FAIL, "an unimplemented check must never report PASS"
    assert "not yet implemented" in result.detail
    assert "recon.reconciler" in result.detail


def test_the_reconciler_entrypoint_raises_rather_than_returning_a_no_op() -> None:
    """The failure is raised at the seam, not swallowed into a stub callable.

    Returning a do-nothing callable would make ``check_mirror_unchanged``
    report PASS -- the exact shape this whole ticket is about.
    """
    with pytest.raises(NotYetImplemented) as excinfo:
        reconciler_entrypoint()
    assert "recon.reconciler" in str(excinfo.value)


def test_the_check_is_registered_under_the_name_t14_keeps() -> None:
    """The registry is no longer empty, and the name is the one that stays."""
    assert CHECK_NAME == "mirror-unchanged"
    assert CHECKS[CHECK_NAME] is check_mirror_unchanged


def test_a_crashing_check_becomes_a_failing_row_not_a_missing_one() -> None:
    """A check that raises must not vanish from the scorecard."""

    def explode() -> CheckResult:
        raise RuntimeError("boom")

    result = run_check("exploding", explode)
    assert result.status == FAIL
    assert "RuntimeError" in result.detail and "boom" in result.detail


def test_there_is_no_skip_status() -> None:
    """``CheckResult`` admits PASS and FAIL only.

    A harness that can say "not applicable" will eventually say it about the
    one check that mattered.
    """
    with pytest.raises(ValueError, match="PASS or FAIL"):
        CheckResult(name="x", status="SKIP", detail="")


# ===========================================================================
# The CLI, as the graded command actually runs it
# ===========================================================================
def _run_suite(service_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "recon.suite", *args],
        cwd=service_root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_the_suite_lists_the_registered_check(service_root: Path) -> None:
    result = _run_suite(service_root, "--list")
    assert result.returncode == 0, result.stderr
    assert CHECK_NAME in result.stdout
    assert "no checks yet" not in result.stdout


def test_the_suite_exits_non_zero_while_a_registered_check_is_unimplemented(
    service_root: Path,
) -> None:
    """DESIGN pins "exits non-zero on any failure", and this is the first failure.

    The scorecard is meant to be read by a human and by CI. Both must see that
    ``mirror-unchanged`` is not satisfied yet, rather than a harness that
    reports success because it ran nothing.
    """
    result = _run_suite(service_root)

    assert result.returncode != 0, result.stdout
    assert CHECK_NAME in result.stdout
    assert FAIL in result.stdout
    assert "0/1 passed" in result.stdout
