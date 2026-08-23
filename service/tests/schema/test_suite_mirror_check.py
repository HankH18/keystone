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
* the "across a reconciler run" half needed ``recon.reconciler``, which T-9
  landed and T-14 wired into the scorecard as the digests taken either side of
  the graded pass. The anti-vacuous-green property is unchanged and still
  asserted: with the module hidden the seam RAISES rather than returning a
  do-nothing callable, and the runner turns that into FAIL. It must never report
  PASS by hashing an untouched database twice.
"""

from __future__ import annotations

import os
import re
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


def _check_row(stdout: str, name: str) -> str:
    """Return the status+detail of one scorecard row, by check name."""
    for line in stdout.splitlines():
        if line.startswith(name):
            return line[len(name) :].strip()
    raise AssertionError(f"no scorecard row named {name!r} in:\n{stdout}")


def _passed_count(stdout: str) -> tuple[int, int]:
    """Return (passed, total) from the scorecard's trailing tally."""
    match = re.search(r"(\d+)/(\d+) passed", stdout)
    assert match is not None, f"no tally in:\n{stdout}"
    return int(match.group(1)), int(match.group(2))


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
def test_the_reconciler_entrypoint_now_resolves_because_t9_landed() -> None:
    """The seam resolves to the real run, now that ``recon.reconciler`` exists.

    **This replaces two assertions that ``recon.reconciler`` DOES NOT EXIST.**
    They were correct when written -- the module docstring above says so in as
    many words -- and T-9 landed a 1,400-line ``recon/reconciler.py`` with its
    own test package, so both were already failing before T-14 touched anything
    and neither could be made green again except by deleting the reconciler.

    The property they existed to protect is NOT dropped: it is asserted by
    :func:`test_the_entrypoint_still_refuses_to_return_a_no_op` below, which
    hides the module and requires the seam to raise rather than hand back a
    do-nothing callable.
    """
    from recon.reconciler import run_once

    assert reconciler_entrypoint() is run_once


def test_the_entrypoint_still_refuses_to_return_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The anti-vacuous-green property, asserted without needing T-9 to be absent.

    Hide ``recon.reconciler`` and the seam must RAISE. Returning a do-nothing
    callable would make ``check_mirror_unchanged`` hash an untouched database
    twice and report PASS -- a green caused by the absence of the thing under
    test, which is the whole point of this module.
    """
    monkeypatch.setitem(sys.modules, "recon.reconciler", None)

    with pytest.raises(NotYetImplemented) as excinfo:
        reconciler_entrypoint()
    assert "recon.reconciler" in str(excinfo.value)


def test_a_missing_reconciler_is_a_failing_row_not_a_passing_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """And the runner turns that raise into FAIL, never into a skip."""
    monkeypatch.setitem(sys.modules, "recon.reconciler", None)

    result = run_check(CHECK_NAME, check_mirror_unchanged)

    assert result.status == FAIL, "an unimplemented check must never report PASS"
    assert "not yet implemented" in result.detail
    assert "recon.reconciler" in result.detail


def test_the_check_is_registered_under_the_name_t14_keeps() -> None:
    """The registry keeps the name; T-14 changed which callable answers to it.

    **This replaces ``CHECKS[CHECK_NAME] is check_mirror_unchanged``.** T-14
    registers ``recon.suite.__main__.check_mirror_unchanged``, which compares the
    digests ``recon.suite.pipeline`` took either side of the graded
    ``run_once()`` -- the pass that wrote all 3,050 proposals. The function in
    this module runs its OWN reconciler pass, and by the time the scorecard
    reaches this row every fingerprint is already open, so that pass proposes
    nothing: it would bracket an idle database. The identity assertion is
    replaced by the two properties that actually matter -- the name is
    registered, and the callable is not this module's.
    """
    from recon.suite.__main__ import check_mirror_unchanged as registered

    assert CHECK_NAME == "mirror-unchanged"
    assert CHECK_NAME in CHECKS
    assert CHECKS[CHECK_NAME] is registered
    assert registered is not check_mirror_unchanged


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
    """Run the harness with NO database and NO writable scorecard directory."""
    env = dict(os.environ)
    env.pop("DATABASE_URL", None)
    env.pop("KEYSTONE_REQUIRE_DB", None)
    env["KEYSTONE_SCORECARD_DIR"] = str(service_root / ".pytest_cache" / "mirror-scorecard")
    return subprocess.run(
        [sys.executable, "-m", "recon.suite", *args],
        cwd=service_root,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        env=env,
    )


def test_the_suite_lists_the_registered_check(service_root: Path) -> None:
    result = _run_suite(service_root, "--list")
    assert result.returncode == 0, result.stderr
    assert CHECK_NAME in result.stdout
    assert "no checks yet" not in result.stdout


def test_the_suite_exits_non_zero_when_this_row_cannot_run(service_root: Path) -> None:
    """DESIGN pins "exits non-zero on any failure", asserted on THIS row.

    **This replaces an assertion that the suite is red because
    ``mirror-unchanged`` is unimplemented.** It is implemented; T-9 landed the
    reconciler and T-14 registered the bracketed comparison. What survives is the
    contract that does not depend on any ticket's state: with no database the row
    cannot run, so it is FAIL -- never a skip, never absent -- and the process
    exits non-zero.

    ``DATABASE_URL`` is removed rather than left set, because with a database
    configured this argv builds the real 100k pipeline and would take minutes
    inside a schema test.
    """
    result = _run_suite(service_root, "--only", CHECK_NAME, "--no-write")

    assert result.returncode != 0, result.stdout
    assert "SKIP" not in result.stdout
    assert _check_row(result.stdout, CHECK_NAME).startswith(FAIL), result.stdout
    assert _passed_count(result.stdout)[0] < _passed_count(result.stdout)[1]
