"""`python -m recon.privacy` -- the retention sweep's entry point (R21, R26).

`docs/retention-policy.md` §3.2 spelled the sweep's body out and §6 recorded that
nothing ran it: `run_purge` was a callable with no caller, so the whole schedule
was reachable only from a Python prompt or from `tests/privacy/test_purge.py`.
That test proves the *function* removes and redacts the right rows. This one
proves the **entry point** does -- argparse, the engine, the transaction, the
principal check, the exit status and the report -- because a policy whose sweep
has no way to be run is a document, not a control.

Nothing here is simulated:

* the rows are **committed** into the migrated database, because the entry point
  opens its own connection and would not see an uncommitted transaction. Each one
  is tagged, and the fixture deletes exactly what it wrote (plus every `audit_log`
  row written after its watermark) whether the test passes or fails;
* `main()` is called for real; it acquires the real engine from `DATABASE_URL`,
  runs the real schedule and commits;
* the refusal test **logs in as `recon_writer`**, so `assert_purge_principal`'s
  `SELECT current_user` sees the role Postgres actually authenticated.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text

from recon.config import get_settings
from recon.db import ROLE_RECON_WRITER, get_engine, role_url
from recon.logging import ENTRY_POINTS
from recon.privacy import RETENTION, canonical_json, is_token, main, retention_rule

TAG = "privacy-cli-test"

#: Synthetic, from the generated dataset's shape -- never a real address (SS3).
EMAIL = "brenmar-fairbank-mead@gmail.com"
NAME = "Fairbank-Mead"

PII = {"guardian_email": EMAIL, "last_name": NAME, "grade": "6"}


def ago(days: int) -> datetime:
    """`days` before now. The entry point takes no clock override, so this is the
    real one -- which is the point: the CLI must age rows against the wall clock."""
    return datetime.now(UTC) - timedelta(days=days)


@pytest.fixture(autouse=True)
def _database_url_must_be_named() -> Iterator[None]:
    """Refuse to run a test in this module unless `DATABASE_URL` is in the environment.

    This is the only test module that runs a sweep which really **commits**, and
    `recon.config.Settings` resolves its `env_file` chain at import time to
    absolute paths. So a process whose `DATABASE_URL` variable is *absent* is not
    unconfigured: it silently inherits the repository's `.env`, which names the
    shared development database. A test that deleted the variable to simulate a
    misconfiguration therefore purged that database for real -- 37,498
    `field_lineage` rows, committed, in this project's own local `keystone`
    database. This guard is that incident's fix: presence is required, and a test
    that wants "not configured" sets the variable to the empty string instead,
    which `database_url()` rejects before any engine can be built.
    """
    assert "DATABASE_URL" in os.environ, (
        "tests/privacy/test_retention_cli.py commits a real retention sweep and "
        "must never fall back to the repository .env. Export DATABASE_URL (a "
        "scratch database), or set it to '' to simulate an unconfigured process."
    )
    yield


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
def committed(owner_engine: Engine) -> Iterator[dict[str, Any]]:
    """Backdated rows COMMITTED into the database, and removed again afterwards.

    Every other test in this package works inside a rolled-back transaction. That
    is not available here: `main()` opens its own connection, so anything it must
    see has to be committed first. The teardown deletes by tag, in dependency
    order, and takes the `audit_log` watermark with it so the sweep's own row goes
    too -- a failing assertion must not leave the database dirty for the next run.
    """
    ids: dict[str, Any] = {}
    with owner_engine.begin() as conn:
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
        with owner_engine.begin() as conn:
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
# the sweep, run through the entry point
# ---------------------------------------------------------------------------


def test_the_entry_point_removes_and_redacts_exactly_what_the_policy_says(
    owner_engine: Engine, committed: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """`python -m recon.privacy` with no arguments: the whole schedule, committed.

    Past retention: the landing record and its staging child are gone, the
    800-day-old audit row is gone, and the 200-day-old `invariant_results` row is
    still there with its `detail` tokenised -- purge where the policy says purge,
    anonymize where it says anonymize.

    Inside retention: every counterpart is untouched, and the recent
    `invariant_results` row still holds the raw value, which is what makes the
    first half evidence of a *window* rather than of a table being emptied.
    """
    assert main([]) == 0

    # -- past retention
    assert not _exists(owner_engine, "stg_student", committed["stg_old"]), (
        "the 100-day-old staging row is outside its 30-day window and survived"
    )
    assert not _exists(owner_engine, "raw_records", committed["rr_old"]), (
        "the 200-day-old landing record is outside its 90-day window and survived"
    )
    assert not _exists(owner_engine, "audit_log", committed["al_old"]), (
        "the 800-day-old audit row is outside its 730-day window and survived"
    )
    assert _exists(owner_engine, "invariant_results", committed["ir_old"]), (
        "invariant_results is anonymize, not purge: the verdict row must survive"
    )
    old_detail = _detail(owner_engine, committed["ir_old"])
    assert is_token(old_detail["guardian_email"]), old_detail
    assert is_token(old_detail["last_name"]), old_detail
    assert EMAIL not in canonical_json(old_detail)
    assert NAME not in canonical_json(old_detail)

    # -- inside retention
    assert _exists(owner_engine, "raw_records", committed["rr_new"])
    assert _exists(owner_engine, "audit_log", committed["al_new"])
    fresh = _detail(owner_engine, committed["ir_new"])
    assert fresh == PII, f"a row INSIDE its window was redacted anyway: {fresh}"

    # -- retained tables have no clock at all
    with owner_engine.connect() as conn:
        after = int(conn.execute(text("SELECT count(*) FROM budget_model_prices")).scalar_one())
    assert after == committed["prices_before"]

    report = capsys.readouterr().out
    assert "retention swept" in report
    assert "raw_records" in report and "invariant_results" in report
    assert EMAIL not in report and NAME not in report


def test_the_entry_point_writes_its_own_audit_row(
    owner_engine: Engine, committed: dict[str, Any]
) -> None:
    """R21: the sweep is itself an auditable action, and the CLI commits that row."""
    assert main([]) == 0
    with owner_engine.connect() as conn:
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
    owner_engine: Engine, committed: dict[str, Any], capsys: pytest.CaptureFixture[str]
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
        assert _exists(owner_engine, table, committed[key]), f"--dry-run deleted {table}.{key}"
    assert _detail(owner_engine, committed["ir_old"]) == PII, "--dry-run redacted a row"

    with owner_engine.connect() as conn:
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
    owner_engine: Engine, committed: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The report is not decorative: the counted rows are the rows that then go.

    Asserted on a leaf table, where policy §3.2 says the dry run is *exact*
    (a parent with dependents is only a lower bound, deliberately).
    """
    assert main(["--dry-run"]) == 0
    counted = _rows_for(capsys.readouterr().out, "invariant_results")
    assert counted >= 1
    assert main([]) == 0
    swept = _rows_for(capsys.readouterr().out, "invariant_results")
    assert swept == counted


def _rows_for(report: str, table: str) -> int:
    """Parse `rows=N` off the report line for `table`."""
    line = next(part for part in report.splitlines() if part.strip().startswith(f"{table} "))
    return int(line.split("rows=")[1].split()[0])


# ---------------------------------------------------------------------------
# which principal may run it
# ---------------------------------------------------------------------------


def test_the_entry_point_refuses_an_application_writer_role(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Policy §3.3: the sweep is an ops job, and refuses to be a service job.

    `DATABASE_URL` is pointed at a real `recon_writer` login, so Postgres
    authenticates the role and `assert_purge_principal`'s `SELECT current_user`
    sees it. The CLI exits 1 rather than issuing DELETEs that the grants would
    reject partway through the schedule.
    """
    writer = role_url(ROLE_RECON_WRITER).render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", writer)
    get_settings.cache_clear()
    get_engine.cache_clear()
    try:
        assert main([]) == 1
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
    database, and `main([])` would then sweep it for real. An empty value is a
    value: environment beats dotenv, `database_url()` sees a falsy DSN and raises
    `DatabaseNotConfigured` before an engine can exist. See the module-level
    `_database_url_must_be_named` guard.
    """
    monkeypatch.setenv("DATABASE_URL", "")
    get_settings.cache_clear()
    get_engine.cache_clear()
    try:
        with pytest.raises(SystemExit) as excinfo:
            main([])
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
    """
    assert main(["--dry-run"]) == 0
    lines = [line.strip() for line in capsys.readouterr().out.splitlines()]
    named = [line.split()[0] for line in lines[1:-1]]
    assert named == [rule.table for rule in RETENTION]
    assert named.count("conflicts") == 2
    assert retention_rule("conflicts", "anonymize").window_days == 365
