"""`python -m recon.privacy` -- the retention sweep's entry point (R21, R26).

`docs/retention-policy.md` §3.2 spelled the sweep's body out and §6 recorded that
nothing ran it: `run_purge` was a callable with no caller, so the whole schedule
was reachable only from a Python prompt or from `tests/privacy/test_purge.py`.
That test proves the *function* removes and redacts the right rows. This one
proves the **entry point** does -- argparse, the engine, the transaction, the
principal check, the exit status and the report -- because a policy whose sweep
has no way to be run is a document, not a control.

Nothing here is simulated:

* the rows are **committed**, because the entry point opens its own connection
  and would not see an uncommitted transaction;
* `main()` is called for real; it acquires the real engine from `DATABASE_URL`,
  runs the real schedule and commits;
* the refusal test **logs in as `recon_writer`**, so `assert_purge_principal`'s
  `SELECT current_user` sees the role Postgres actually authenticated.

**Where those rows are committed is the whole safety story, and the first version
of this module got it wrong.** It committed them into the database `DATABASE_URL`
named and then ran a real, committing sweep against it -- so `make test`, which
loads the repository `.env`, ran the retention schedule against the *shared
development database* and deleted every row in it older than the committed
windows: rows the test had never seen, let alone created. It is not a
hypothetical. It destroyed 37,498 `field_lineage` rows in this project's own
`keystone` database during development, and an adversarial verifier reproduced it
afterwards by planting one marked row and watching a green run remove it.

So this module **creates a database of its own** (`tests.er.scratchdb`, the same
helper `tests/integration` and `tests/suite` use) and points the process at it for
the duration. `DATABASE_URL` supplies the server coordinates and nothing else. Two
tests below hold that line from opposite directions: one asserts the process is
not on the configured database at all, and one plants a row *in* the configured
database, older than every window, and asserts a full `--apply` sweep leaves it
alone. Neither can pass if the sweep is ever pointed back at `DATABASE_URL`.

The scratch database is dropped in teardown whether the module passes or fails,
and a leftover from a run that was killed before teardown is dropped by the next
run (`_drop_orphaned_scratch_databases`) -- orphaned scratch databases have filled
this machine's disk before.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from recon.config import get_settings
from recon.db import ROLE_RECON_WRITER, database_url, get_engine, role_url
from recon.logging import ENTRY_POINTS
from recon.privacy import RETENTION, canonical_json, is_token, main, retention_rule
from tests.er.scratchdb import (
    MAINTENANCE_DATABASE,
    create_scratch_database,
    drop_database,
    use_database,
)

TAG = "privacy-cli-test"

#: `tests.er.scratchdb` names a database `keystone_<label>_<pid>_<token>`.
SCRATCH_LABEL = "ret"

#: The driver SQLAlchemy must be told to use. `recon.db._ASYNC_SAFE_DRIVER` pins the
#: same string for connections built from the environment; the DSNs in this module
#: come from `tests.er.scratchdb` instead, which speaks libpq, so they need the same
#: normalisation applied by hand.
_SQLALCHEMY_DRIVER = "postgresql+psycopg"

#: Synthetic, from the generated dataset's shape -- never a real address (SS3).
EMAIL = "brenmar-fairbank-mead@gmail.com"
NAME = "Fairbank-Mead"

PII = {"guardian_email": EMAIL, "last_name": NAME, "grade": "6"}

#: The verifier's marker: a `field_lineage` row older than that table's 180-day
#: window, planted in the database `DATABASE_URL` names -- which this module does
#: not own and must never be able to delete.
ADVERSARY_FIELD = "adversary_marker"
ADVERSARY_ID = "11111111-1111-1111-1111-111111111111"


def ago(days: int) -> datetime:
    """`days` before now. The entry point takes no clock override, so this is the
    real one -- which is the point: the CLI must age rows against the wall clock."""
    return datetime.now(UTC) - timedelta(days=days)


@pytest.fixture(autouse=True)
def _database_url_must_be_named() -> Iterator[None]:
    """Refuse to run a test in this module unless `DATABASE_URL` is in the environment.

    `recon.config.Settings` resolves its `env_file` chain at import time to
    absolute paths. So a process whose `DATABASE_URL` variable is *absent* is not
    unconfigured: it silently inherits the repository's `.env`, which names the
    shared development database. A test that deleted the variable to simulate a
    misconfiguration would therefore be configured after all, and pointed at the
    one database this module must never touch.

    The scratch database below is what makes that harmless rather than fatal, and
    this guard is still worth keeping: `create_scratch_database` needs a
    `DATABASE_URL` for the *server* coordinates, and "not configured" must be
    spelled as the empty string, which `database_url()` rejects before any engine
    can be built.
    """
    assert "DATABASE_URL" in os.environ, (
        "tests/privacy/test_retention_cli.py needs DATABASE_URL for the Postgres "
        "server coordinates it creates its scratch database on. Export it, or set "
        "it to '' to simulate an unconfigured process."
    )
    yield


# ---------------------------------------------------------------------------
# a database of this module's own
# ---------------------------------------------------------------------------


def _drop_orphaned_scratch_databases(server: str) -> None:
    """Drop this module's leftovers from runs that never reached their teardown.

    A `finally` covers a failing test, an error and a `KeyboardInterrupt`; it does
    not cover a `SIGKILL`, and a scratch database that outlives its run is a few
    hundred megabytes nobody will ever look at again. `create_scratch_database`
    puts the creating pid in the name, so a leftover is identifiable -- and a
    database whose pid is **still alive** belongs to a concurrent pytest process
    and is left strictly alone.
    """
    prefix = f"keystone_{SCRATCH_LABEL}_"
    admin = make_url(server).set(drivername="postgresql", database=MAINTENANCE_DATABASE)
    with psycopg.connect(admin.render_as_string(hide_password=False), autocommit=True) as conn:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE %s", (f"{prefix}%",)
            ).fetchall()
        ]
        for name in names:
            parts = name[len(prefix) :].split("_")
            if len(parts) != 2 or not parts[0].isdigit():
                continue
            try:
                os.kill(int(parts[0]), 0)
            except ProcessLookupError:
                conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            except OSError:
                # Alive but owned by another user, or otherwise not ours to judge.
                continue


@pytest.fixture(scope="module")
def scratch_database(configured_url: str) -> Iterator[str]:
    """A migrated database this module creates, owns, and drops.

    `configured_url` is requested first on purpose: it is the session-scoped
    fixture the rest of this package resolves `DATABASE_URL` through, and taking
    it here pins it to the *real* value before this fixture moves the process --
    so a module that runs after this one is not handed a scratch DSN, or a dropped
    one.
    """
    _drop_orphaned_scratch_databases(configured_url)
    previous = os.environ.get("DATABASE_URL")
    dsn = create_scratch_database(SCRATCH_LABEL)
    use_database(dsn)
    try:
        yield dsn
    finally:
        if previous:
            use_database(previous)
        get_settings.cache_clear()
        get_engine.cache_clear()
        drop_database(dsn)


@pytest.fixture(scope="module")
def sweep_engine(scratch_database: str) -> Iterator[Engine]:
    """Owner engine on the scratch database -- the one the sweep will really run on."""
    # `create_scratch_database` returns a **libpq** DSN (`postgresql://...`), which
    # is what psycopg.connect wants. Handed to SQLAlchemy unchanged it selects the
    # default `postgresql` dialect -- psycopg2, which this project does not install
    # -- and every test in the module errors with ModuleNotFoundError before it
    # reaches an assertion. `recon.db` pins the same driver for the same reason
    # (`_ASYNC_SAFE_DRIVER`); this is that normalisation applied to a DSN that did
    # not come from the environment.
    engine = create_engine(
        make_url(scratch_database).set(drivername=_SQLALCHEMY_DRIVER), future=True
    )
    with engine.connect() as conn:
        migrated = conn.execute(text("SELECT to_regclass('public.audit_log')")).scalar()
    assert migrated is not None, "the scratch database was not migrated"
    yield engine
    engine.dispose()


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def test_the_sweep_is_a_declared_entry_point() -> None:
    """`recon/privacy.py` is a way a Keystone process starts, so it is enumerated.

    `tests/privacy/test_logging_installed.py` parametrises over `ENTRY_POINTS` and
    asserts each listed file installs the redaction chain; being on the list is
    what puts the sweep under that rule instead of outside it.
    """
    assert "recon/privacy.py" in ENTRY_POINTS


# ---------------------------------------------------------------------------
# committed rows on both sides of the windows
# ---------------------------------------------------------------------------


@pytest.fixture
def committed(sweep_engine: Engine) -> Iterator[dict[str, Any]]:
    """Backdated rows COMMITTED into the scratch database, and removed again afterwards.

    Every other test in this package works inside a rolled-back transaction. That
    is not available here: `main()` opens its own connection, so anything it must
    see has to be committed first. The teardown deletes by tag, in dependency
    order, and takes the `audit_log` watermark with it so the sweep's own row goes
    too -- the database is dropped at the end of the module anyway, but a test that
    left rows behind would change what the *next* test in this module counts.
    """
    ids: dict[str, Any] = {}
    with sweep_engine.begin() as conn:
        watermark = int(
            conn.execute(text("SELECT coalesce(max(id), 0) FROM audit_log")).scalar_one()
        )
        ids["audit_watermark"] = watermark
        ids["prices_before"] = int(
            conn.execute(text("SELECT count(*) FROM budget_model_prices")).scalar_one()
        )

        def scalar(sql: str, **params: Any) -> Any:
            return conn.execute(text(sql), params).scalar_one()

        # landing (90d): rr_old is outside, rr_new is inside
        for key, days in (("rr_old", 200), ("rr_new", 10)):
            ids[key] = scalar(
                "INSERT INTO raw_records (source_id, entity_type, natural_key, generation,"
                " payload, row_hash, load_id, run_id, ingest_ts)"
                " VALUES ('appdb','student',:nk,1,CAST(:p AS jsonb),:rh,:ld,:run,:ts)"
                " RETURNING id",
                nk=f"{TAG}-{key}",
                p=canonical_json(PII),
                rh=f"{TAG}-{key}",
                ld=TAG,
                run=TAG,
                ts=ago(days),
            )
        # staging (30d): the landing row's child, itself outside its window, so
        # the NOT EXISTS guard on raw_records is satisfied by this same sweep.
        ids["stg_old"] = scalar(
            "INSERT INTO stg_student (generation, source_id, source_ref, raw_record_id, row_hash,"
            " materialized_at, student_id, guardian_email, last_name)"
            " VALUES (1,'appdb',:ref,:rr,:rh,:ts,:sid,:em,:ln) RETURNING id",
            ref=f"appdb:student:{TAG}-stg_old",
            rr=ids["rr_old"],
            rh=f"{TAG}-stg_old",
            ts=ago(100),
            sid=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{TAG}/stg_old")),
            em=EMAIL,
            ln=NAME,
        )
        # invariant_results (180d): ANONYMIZE. The row survives; `detail` does not.
        for key, days in (("ir_old", 200), ("ir_new", 10)):
            ids[key] = scalar(
                "INSERT INTO invariant_results (run_id, rule_id, rule_version, record_ref,"
                " entity_type, verdict, detail, created_at)"
                " VALUES (:run,'C4','v1',:ref,'student','fail',CAST(:d AS jsonb),:ts)"
                " RETURNING id",
                run=f"{TAG}-{key}",
                ref=f"appdb:student:{TAG}-{key}",
                d=canonical_json(PII),
                ts=ago(days),
            )
        # audit_log (730d): the longest window in the schedule.
        for key, days in (("al_old", 800), ("al_new", 10)):
            ids[key] = scalar(
                "INSERT INTO audit_log (actor, action, subject, detail, ts)"
                " VALUES ('system:test',:a,:s,CAST(:d AS jsonb),:ts) RETURNING id",
                a=f"{TAG}.{key}",
                s=TAG,
                d=canonical_json({"body": "already redacted"}),
                ts=ago(days),
            )
    try:
        yield ids
    finally:
        with sweep_engine.begin() as conn:
            conn.execute(text("DELETE FROM audit_log WHERE id > :w"), {"w": watermark})
            conn.execute(
                text("DELETE FROM invariant_results WHERE run_id LIKE :t"), {"t": f"{TAG}-%"}
            )
            conn.execute(text("DELETE FROM stg_student WHERE row_hash LIKE :t"), {"t": f"{TAG}-%"})
            conn.execute(text("DELETE FROM raw_records WHERE load_id = :t"), {"t": TAG})


def _exists(engine: Engine, table: str, row_id: Any, column: str = "id") -> bool:
    with engine.connect() as conn:
        return bool(
            conn.execute(text(f"SELECT 1 FROM {table} WHERE {column} = :i"), {"i": row_id}).scalar()
        )


def _detail(engine: Engine, row_id: int) -> dict[str, Any]:
    with engine.connect() as conn:
        value = conn.execute(
            text("SELECT detail FROM invariant_results WHERE id = :i"), {"i": row_id}
        ).scalar_one()
    return dict(value)


# ---------------------------------------------------------------------------
# the suite cannot reach the database DATABASE_URL names
# ---------------------------------------------------------------------------


def test_the_sweep_runs_against_a_database_this_module_created(
    scratch_database: str, configured_url: str
) -> None:
    """The process is on a scratch database, and `DATABASE_URL` supplies only the server.

    This is the structural half of the fix: there is no argument, no flag and no
    environment in which a test in this module reaches the configured database,
    because by the time any of them runs the process is not pointed at it.
    """
    configured = make_url(configured_url)
    scratch = make_url(scratch_database)
    assert scratch.database != configured.database
    assert scratch.database is not None
    assert scratch.database.startswith(f"keystone_{SCRATCH_LABEL}_")
    # the server is the same one, which is what makes it a real Postgres test
    assert (scratch.host, scratch.port) == (configured.host, configured.port)
    # and `main()` will resolve to the scratch database, not to the configured one
    assert database_url().database == scratch.database


def test_a_row_in_the_configured_database_survives_a_full_apply_sweep(
    configured_url: str, committed: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The adversarial verifier's own experiment, kept as a test.

    A `field_lineage` row is planted in the database `DATABASE_URL` names -- 400
    days old, so far outside that table's 180-day window that any sweep pointed at
    that database removes it -- and then the real, committing entry point is run.
    The marker must still be there.

    This is the empirical half of the fix, and it fails loudly against the
    original module: that one committed its rows into the configured database and
    swept it, so the marker went, along with every other row in it older than the
    windows.
    """
    engine = create_engine(make_url(configured_url).set(drivername=_SQLALCHEMY_DRIVER), future=True)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO field_lineage (canonical_id, field, value_text, source_id,"
                    " generation, observed_ts)"
                    " VALUES (CAST(:cid AS uuid), :f, 'DO-NOT-DELETE', 'appdb', 1, :ts)"
                ),
                {"cid": ADVERSARY_ID, "f": ADVERSARY_FIELD, "ts": ago(400)},
            )
        try:
            assert main(["--apply"]) == 0
            capsys.readouterr()
            with engine.connect() as conn:
                surviving = int(
                    conn.execute(
                        text("SELECT count(*) FROM field_lineage WHERE field = :f"),
                        {"f": ADVERSARY_FIELD},
                    ).scalar_one()
                )
            assert surviving == 1, (
                "a committing retention sweep run by this suite deleted a row in the "
                "database DATABASE_URL names -- a row this suite did not create. That "
                "is the defect this module exists to prevent."
            )
        finally:
            with engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM field_lineage WHERE field = :f"), {"f": ADVERSARY_FIELD}
                )
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# counting is the default; deleting is opt-in
# ---------------------------------------------------------------------------


def _row_census(engine: Engine) -> dict[str, int]:
    """`count(*)` for every table the schedule would delete from or rewrite."""
    tables = sorted({rule.table for rule in RETENTION if rule.disposition != "retain"})
    with engine.connect() as conn:
        return {
            table: int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())
            for table in tables
        }


def test_a_no_argument_run_counts_and_deletes_nothing(
    sweep_engine: Engine, committed: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """`python -m recon.privacy` with no arguments must never remove a row.

    The rows the `committed` fixture plants are deliberately outside their
    windows, so a destructive default has plenty to delete: every count below
    would move. This is the property the whole ticket turns on -- "I ran the
    module with no arguments" has to be a safe thing to have done.
    """
    before = _row_census(sweep_engine)
    assert main([]) == 0
    after = _row_census(sweep_engine)
    assert after == before, "a no-argument run deleted rows"

    # nothing was rewritten either, and no audit row was written
    assert _detail(sweep_engine, committed["ir_old"]) == PII
    with sweep_engine.connect() as conn:
        swept = conn.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'retention.purge' AND id > :w"),
            {"w": committed["audit_watermark"]},
        ).scalar_one()
    assert swept == 0

    report = capsys.readouterr().out
    assert "would sweep (dry run)" in report
    assert "Re-run with --apply" in report
    # and it counted for real: it found the rows it declined to delete
    assert _rows_for(report, "stg_student") >= 1
    assert _rows_for(report, "audit_log") >= 1


def test_dry_run_and_apply_are_mutually_exclusive() -> None:
    """Asking for both is a misconfiguration, not a silent choice of one."""
    with pytest.raises(SystemExit) as excinfo:
        main(["--dry-run", "--apply"])
    assert excinfo.value.code == 2


def test_every_run_names_the_database_it_is_about_to_sweep(
    scratch_database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Dry or not, the first line names the target -- and never the password.

    A destructive tool that does not say what it is pointed at is how a sweep
    emptied the shared development database while printing a report that named
    tables and row counts and no database at all.
    """
    url = make_url(scratch_database)
    for argv, mode in (([], "dry run"), (["--apply", "--yes"], "APPLY")):
        assert main(argv) == 0
        first = capsys.readouterr().out.splitlines()[0]
        assert first.startswith("retention target:")
        assert f"database={url.database}" in first
        assert f"host={url.host}" in first
        assert f"port={url.port}" in first
        assert f"user={url.username}" in first
        assert mode in first
        # The password must not leak. A bare `url.password not in first` cannot
        # express that here: the local password IS "keystone", which is also the
        # username and a substring of every scratch database name, so that check
        # is unsatisfiable for this credential no matter how correct the output
        # is -- it fails on a line that leaks nothing. So assert the two things
        # that actually distinguish a leak:
        #   1. the line is not a DSN (no `://`, no `user:pass@host` userinfo), and
        #   2. the password does not appear anywhere OUTSIDE the four fields that
        #      legitimately carry it as a substring.
        assert "://" not in first and "@" not in first, f"the target line is a DSN: {first}"
        assert url.password
        residue = first
        for legitimate in (
            f"database={url.database}",
            f"host={url.host}",
            f"port={url.port}",
            f"user={url.username}",
        ):
            residue = residue.replace(legitimate, "")
        assert url.password not in residue, f"password leaked outside the named fields: {first}"


def test_an_apply_reports_what_it_will_remove_before_removing_it(
    committed: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The preview comes first, and it agrees with what the sweep then does.

    Asserted on a leaf table, where policy §3.2 says the count is *exact* (a parent
    with dependents is only a lower bound, deliberately).
    """
    assert main(["--apply"]) == 0
    out = capsys.readouterr().out
    assert out.index("would sweep (dry run)") < out.index("retention swept"), (
        "the apply removed rows before saying what it would remove"
    )
    preview, swept = out.split("retention swept", 1)
    assert _rows_for(preview, "invariant_results") >= 1
    assert _rows_for(swept, "invariant_results") == _rows_for(preview, "invariant_results")


# ---------------------------------------------------------------------------
# the sweep itself, run through the entry point
# ---------------------------------------------------------------------------


def test_the_entry_point_removes_and_redacts_exactly_what_the_policy_says(
    sweep_engine: Engine, committed: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """`python -m recon.privacy --apply`: the whole schedule, committed.

    Past retention: the landing record and its staging child are gone, the
    800-day-old audit row is gone, and the 200-day-old `invariant_results` row is
    still there with its `detail` tokenised -- purge where the policy says purge,
    anonymize where it says anonymize.

    Inside retention: every counterpart is untouched, and the recent
    `invariant_results` row still holds the raw value, which is what makes the
    first half evidence of a *window* rather than of a table being emptied.
    """
    assert main(["--apply"]) == 0

    # -- past retention
    assert not _exists(sweep_engine, "stg_student", committed["stg_old"]), (
        "the 100-day-old staging row is outside its 30-day window and survived"
    )
    assert not _exists(sweep_engine, "raw_records", committed["rr_old"]), (
        "the 200-day-old landing record is outside its 90-day window and survived"
    )
    assert not _exists(sweep_engine, "audit_log", committed["al_old"]), (
        "the 800-day-old audit row is outside its 730-day window and survived"
    )
    assert _exists(sweep_engine, "invariant_results", committed["ir_old"]), (
        "invariant_results is anonymize, not purge: the verdict row must survive"
    )
    old_detail = _detail(sweep_engine, committed["ir_old"])
    assert is_token(old_detail["guardian_email"]), old_detail
    assert is_token(old_detail["last_name"]), old_detail
    assert EMAIL not in canonical_json(old_detail)
    assert NAME not in canonical_json(old_detail)

    # -- inside retention
    assert _exists(sweep_engine, "raw_records", committed["rr_new"])
    assert _exists(sweep_engine, "audit_log", committed["al_new"])
    fresh = _detail(sweep_engine, committed["ir_new"])
    assert fresh == PII, f"a row INSIDE its window was redacted anyway: {fresh}"

    # -- retained tables have no clock at all
    with sweep_engine.connect() as conn:
        after = int(conn.execute(text("SELECT count(*) FROM budget_model_prices")).scalar_one())
    assert after == committed["prices_before"]

    report = capsys.readouterr().out
    assert "retention swept" in report
    assert "raw_records" in report and "invariant_results" in report
    assert EMAIL not in report and NAME not in report


def test_the_entry_point_writes_its_own_audit_row(
    sweep_engine: Engine, committed: dict[str, Any]
) -> None:
    """R21: the sweep is itself an auditable action, and the CLI commits that row."""
    assert main(["--apply"]) == 0
    with sweep_engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT actor, subject, detail FROM audit_log"
                    " WHERE action = 'retention.purge' AND id > :w ORDER BY id DESC LIMIT 1"
                ),
                {"w": committed["audit_watermark"]},
            )
            .mappings()
            .one()
        )
    assert row["actor"] == "system:retention"
    assert row["subject"].startswith("principal:")
    body = canonical_json(row["detail"])
    assert EMAIL not in body and NAME not in body
    tables = {entry["table"] for entry in row["detail"]["body"]["tables"]}
    assert {rule.table for rule in RETENTION if rule.disposition != "retain"} <= tables


def test_dry_run_counts_without_writing_anything(
    sweep_engine: Engine, committed: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """`--dry-run` reports the same work and leaves every row where it was.

    Including the audit row: a counted sweep that wrote its own `audit_log` entry
    would be a sweep that wrote, which is the one thing `--dry-run` promises not
    to do.
    """
    assert main(["--dry-run"]) == 0

    for table, key in (
        ("stg_student", "stg_old"),
        ("raw_records", "rr_old"),
        ("audit_log", "al_old"),
        ("raw_records", "rr_new"),
    ):
        assert _exists(sweep_engine, table, committed[key]), f"--dry-run deleted {table}.{key}"
    assert _detail(sweep_engine, committed["ir_old"]) == PII, "--dry-run redacted a row"

    with sweep_engine.connect() as conn:
        swept = conn.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'retention.purge' AND id > :w"),
            {"w": committed["audit_watermark"]},
        ).scalar_one()
    assert swept == 0, "--dry-run wrote the sweep's audit row"

    report = capsys.readouterr().out
    assert "would sweep (dry run)" in report
    # the counts are real: it found the rows it would have deleted
    assert _rows_for(report, "stg_student") >= 1
    assert _rows_for(report, "audit_log") >= 1


def test_the_dry_run_count_matches_what_the_real_sweep_then_removes(
    sweep_engine: Engine, committed: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The report is not decorative: the counted rows are the rows that then go.

    Asserted on a leaf table, where policy §3.2 says the dry run is *exact*
    (a parent with dependents is only a lower bound, deliberately).
    """
    assert main(["--dry-run"]) == 0
    counted = _rows_for(capsys.readouterr().out, "invariant_results")
    assert counted >= 1
    assert main(["--apply"]) == 0
    swept = _rows_for(capsys.readouterr().out.split("retention swept", 1)[1], "invariant_results")
    assert swept == counted


def _rows_for(report: str, table: str) -> int:
    """Parse `rows=N` off the report line for `table`."""
    line = next(part for part in report.splitlines() if part.strip().startswith(f"{table} "))
    return int(line.split("rows=")[1].split()[0])


# ---------------------------------------------------------------------------
# the confirmation an interactive operator gets
# ---------------------------------------------------------------------------


def test_an_interactive_apply_asks_first_and_aborts_on_anything_else(
    sweep_engine: Engine,
    committed: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """On a terminal, `--apply` names the database and waits. A typo writes nothing."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda: "yes")

    before = _row_census(sweep_engine)
    assert main(["--apply"]) == 3
    assert _row_census(sweep_engine) == before, "an aborted sweep wrote something"

    captured = capsys.readouterr()
    assert "about to APPLY the retention schedule to:" in captured.out
    assert f"database={make_url(str(sweep_engine.url)).database}" in captured.out
    assert "aborted" in captured.err


def test_an_interactive_apply_proceeds_when_the_operator_types_apply(
    sweep_engine: Engine, committed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prompt is a gate, not a wall: the documented answer runs the schedule."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda: "apply")

    assert main(["--apply"]) == 0
    assert not _exists(sweep_engine, "raw_records", committed["rr_old"])


def test_yes_skips_the_prompt(
    sweep_engine: Engine, committed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--yes` is for the operator who already decided; it must not consult stdin."""

    def _refuse() -> str:  # pragma: no cover - called only if the gate is wrong
        raise AssertionError("--yes still prompted")

    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", _refuse)

    assert main(["--apply", "--yes"]) == 0
    assert not _exists(sweep_engine, "raw_records", committed["rr_old"])


# ---------------------------------------------------------------------------
# which principal may run it
# ---------------------------------------------------------------------------


def test_the_entry_point_refuses_an_application_writer_role(
    scratch_database: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Policy §3.3: the sweep is an ops job, and refuses to be a service job.

    `DATABASE_URL` is pointed at a real `recon_writer` login on the scratch
    database, so Postgres authenticates the role and `assert_purge_principal`'s
    `SELECT current_user` sees it. The CLI exits 1 rather than issuing DELETEs
    that the grants would reject partway through the schedule.
    """
    writer = role_url(ROLE_RECON_WRITER).render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", writer)
    get_settings.cache_clear()
    get_engine.cache_clear()
    try:
        assert main(["--apply", "--yes"]) == 1
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        get_engine.cache_clear()

    captured = capsys.readouterr()
    assert "refused" in captured.err
    assert ROLE_RECON_WRITER in captured.err


def test_a_process_with_no_database_url_exits_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """Misconfiguration is argparse's exit status, not a traceback and not a sweep.

    **The variable is set to the empty string, never deleted.** `recon.config`
    resolves `env_file` at import time to absolute paths, so a *deleted*
    `DATABASE_URL` does not leave the process unconfigured -- pydantic-settings
    falls back to the repository's `.env`, which names the shared development
    database. An empty value is a value: environment beats dotenv, `database_url()`
    sees a falsy DSN and raises `DatabaseNotConfigured` before an engine can exist.
    See the module-level `_database_url_must_be_named` guard.
    """
    monkeypatch.setenv("DATABASE_URL", "")
    get_settings.cache_clear()
    get_engine.cache_clear()
    try:
        with pytest.raises(SystemExit) as excinfo:
            main(["--apply", "--yes"])
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        get_engine.cache_clear()
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# the report itself
# ---------------------------------------------------------------------------


def test_the_report_names_every_rule_in_execution_order(
    committed: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """One line per rule, in `RETENTION`'s order -- dependents before parents.

    `conflicts` appears twice on purpose (anonymize at 365d, purge at 730d), so a
    report keyed on table names alone would silently drop a rule.

    The report body is located by its own header and `total` lines rather than by a
    fixed offset, because the run now prints a line naming the target database
    above it -- and that line is a feature, so a test that broke on its presence
    would be testing the wrong thing.
    """
    assert main(["--dry-run"]) == 0
    lines = [line.strip() for line in capsys.readouterr().out.splitlines()]
    start = next(i for i, line in enumerate(lines) if line.startswith("retention would sweep")) + 1
    end = next(i for i, line in enumerate(lines) if line.startswith("total "))
    named = [line.split()[0] for line in lines[start:end]]
    assert named == [rule.table for rule in RETENTION]
    assert named.count("conflicts") == 2
    assert retention_rule("conflicts", "anonymize").window_days == 365
