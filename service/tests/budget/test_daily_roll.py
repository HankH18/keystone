"""R17's daily cap is a DAY: the scope rolls, and yesterday cannot refuse today.

The scope that carries the hard daily cap used to be the fixed string ``daily``.
Nothing rolled it -- not the schema, not a cron, not a caller -- so the "daily"
cap was a **lifetime** cap: one ``python -m recon.incidents`` pass costs 56,487
microusd of it, the seeded 5 USD budget was gone for good after ~88 hand runs,
and every metered call in the deployment was refused from then on with no date on
which that recovered.

It is now one ledger row per UTC day (``daily:2026-08-25``), opened by
``python -m recon.budget roll``. That is the only form of rolling this schema can
express honestly: ``spent_microusd`` is writable by nobody, so a new day is a new
row and never a counter somebody reset.

Two things have to be true, and they are proved separately because a test process
is refused the production daily row outright (:class:`RealDailyScopeRefused`):

* **the NAME rolls at 00:00 UTC** -- proved by injecting the clock, never by
  sleeping (:func:`test_the_daily_scope_name_rolls_across_a_utc_midnight`);
* **two consecutive days are two independent budgets** -- proved against the real
  trigger, by exhausting one day's row and then reserving on the next
  (:func:`test_yesterdays_spend_does_not_count_against_todays_cap`).

The days used here are in **1999**, deliberately. No deployment will ever name a
row in that family, so these tests can neither spend a real day's budget nor be
rescued by one, and the teardown that removes them cannot remove a live row.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy import Engine, text

import recon.budget as budget
from recon.budget import (
    AUDIT_CAP_HIT,
    AUDIT_SCOPE_HALTED,
    DAILY_CAP_USD_ENV,
    DAILY_SCOPE,
    DAILY_SCOPE_ENV,
    KS_CAP_EXCEEDED,
    BudgetCapExceeded,
    BudgetScopeHalted,
    LedgerScopeMissing,
    RealDailyScopeRefused,
    daily_scope,
    daily_scope_for,
    halt_scope,
    halted_scopes,
    ledger_row,
    main,
    reserve,
    roll_daily_scope,
    run_scope,
    utc_today,
)
from tests.budget.support import ScopeFactory, run_id_for, spent, unique

MODEL = "mock-rationale-v1"

#: Same bounds and the same arithmetic as ``tests/budget/test_ledger.py``:
#: 100 x 6.25 + 384 x 25 = 10,225 microusd per reservation.
RESERVE_INPUT_TOKENS = 100
RESERVE_OUTPUT_TOKENS = 384
RESERVE_AMOUNT = 10_225

#: The private day family these tests open rows in. See the module docstring.
TEST_DAY_FAMILY = "daily:1999-"

YESTERDAY = date(1999, 1, 1)
TODAY = date(1999, 1, 2)

SERVICE_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _remove_the_test_day_rows(owner_engine: Engine) -> Iterator[None]:
    """Delete every ``daily:1999-*`` row this module opened, whatever happened.

    Autouse and unconditional: a test that fails part way through has still
    charged a ledger row, and a leaked row would make the *next* run of this file
    start from a used-up budget -- the exact stickiness that
    ``_keep_the_real_daily_scope_clean`` exists to clean up after on the real
    scope. Bounded to the 1999 family, which no deployment can name.
    """
    yield
    with owner_engine.begin() as conn:
        pattern = {"p": f"{TEST_DAY_FAMILY}%"}
        conn.execute(text("DELETE FROM budget_reservations WHERE scope LIKE :p"), pattern)
        conn.execute(text("DELETE FROM audit_log WHERE subject LIKE :p"), pattern)
        conn.execute(text("DELETE FROM budget_ledger WHERE scope LIKE :p"), pattern)


def _reserve(run_id: str, key: str) -> object:
    return reserve(
        idempotency_key=key,
        model=MODEL,
        max_output_tokens=RESERVE_OUTPUT_TOKENS,
        max_input_tokens=RESERVE_INPUT_TOKENS,
        run_id=run_id,
    )


def _scope_named_at(moment: datetime) -> str:
    """The scope :func:`daily_scope` names at ``moment``, with no override set.

    Read out of :class:`RealDailyScopeRefused` on purpose. The production daily
    row is refused to a test process -- that is a property this file must not
    weaken to observe another one -- and the refusal carries the resolved name,
    so the name is still observable without a single row being touched.
    """
    with mock.patch.dict(os.environ), mock.patch.object(budget, "_utc_now", return_value=moment):
        os.environ.pop(DAILY_SCOPE_ENV, None)
        with pytest.raises(RealDailyScopeRefused) as excinfo:
            daily_scope()
    family, resolved = excinfo.value.scopes
    assert family == DAILY_SCOPE, "the refusal must still name the family it is protecting"
    return resolved


# ===========================================================================
# the name rolls, and it rolls on the UTC day
# ===========================================================================
def test_the_daily_scope_name_rolls_across_a_utc_midnight() -> None:
    """One second either side of 00:00 UTC names two different ledger rows.

    The clock is **injected**, not waited for: ``recon.budget._utc_now`` exists as
    a function for exactly this reason. A test that slept until midnight would
    run once a day at best and would prove the same thing.
    """
    last_second = _scope_named_at(datetime(1999, 1, 1, 23, 59, 59, tzinfo=UTC))
    first_second = _scope_named_at(datetime(1999, 1, 2, 0, 0, 1, tzinfo=UTC))

    assert last_second == "daily:1999-01-01"
    assert first_second == "daily:1999-01-02"
    assert last_second != first_second, (
        "the daily cap is the same ledger row on both sides of midnight, so it is "
        "not a daily cap at all -- it is a lifetime one"
    )


def test_the_scope_is_stable_within_one_utc_day() -> None:
    """Rolling is the ONLY thing that changes the row: two instants, one day, one row."""
    morning = _scope_named_at(datetime(1999, 1, 1, 0, 0, 0, tzinfo=UTC))
    evening = _scope_named_at(datetime(1999, 1, 1, 23, 59, 59, tzinfo=UTC))
    assert morning == evening == daily_scope_for(date(1999, 1, 1))


def test_the_day_is_the_UTC_day_and_not_the_hosts() -> None:
    """A ``+05:00`` clock at 02:30 is still *yesterday's* budget.

    A cap whose day follows the host's timezone rolls at a different instant on
    every machine that runs the cron, so for part of every day two rows are live
    and neither is the day's budget. The zone is fixed here, and a naive
    datetime -- a clock that never said which zone it meant -- is refused rather
    than guessed at.
    """
    east = timezone(timedelta(hours=5))
    assert utc_today(datetime(1999, 1, 2, 2, 30, tzinfo=east)) == date(1999, 1, 1)
    assert _scope_named_at(datetime(1999, 1, 2, 2, 30, tzinfo=east)) == "daily:1999-01-01"

    with pytest.raises(ValueError, match="naive"):
        utc_today(datetime(1999, 1, 2, 2, 30))  # the naive clock IS the point of the test


def test_a_datetime_cannot_be_mistaken_for_a_day() -> None:
    """``datetime`` is a subclass of ``date`` and its isoformat carries a time.

    Accepting one would name ``daily:1999-01-01T09:00:00+00:00`` -- a row nobody
    provisions -- once an hour, and every call would then fail as an unprovisioned
    scope. Refused at the type instead.
    """
    with pytest.raises(TypeError, match=r"datetime\.date"):
        daily_scope_for(datetime(1999, 1, 1, tzinfo=UTC))  # type: ignore[arg-type]
    assert daily_scope_for(date(1999, 1, 1)) == "daily:1999-01-01"


def test_the_override_still_names_one_literal_row_and_never_the_real_one() -> None:
    """The harness override is unchanged, and neither spelling of production leaks.

    ``KEYSTONE_DAILY_SCOPE`` names a row **verbatim** -- it is not date-keyed,
    because the ops-provisioned throwaway row a harness points it at is one row,
    not a family. Both spellings of the production scope (the bare family and
    today's actual row) are treated as unset, so a test process cannot smuggle
    either of them in.
    """
    with mock.patch.dict(os.environ, {DAILY_SCOPE_ENV: "run:stand-in"}):
        assert daily_scope() == "run:stand-in"

    today = daily_scope_for(utc_today())
    for spelling in (DAILY_SCOPE, today):
        with (
            mock.patch.dict(os.environ, {DAILY_SCOPE_ENV: spelling}),
            pytest.raises(RealDailyScopeRefused) as excinfo,
        ):
            daily_scope()
        assert today in excinfo.value.scopes


# ===========================================================================
# `python -m recon.budget roll` -- the entry point the cron needs
# ===========================================================================
def test_roll_opens_the_days_ledger_row_with_the_migrations_cap(owner_engine: Engine) -> None:
    """``roll`` provisions the row, and its default cap is migration 0005's.

    The day comes from the injected clock when ``--day`` is absent, which is what
    the cron relies on -- and is why this can assert the default without opening
    a row for the real today.

    No ``capsys`` anywhere in this file, deliberately: ``main`` calls
    ``configure_logging_once``, and ``capsys`` swaps in a per-test ``sys.stdout``
    that it then CLOSES -- so structlog is left holding a closed file and every
    later test in the session dies inside a log call. What the CLI prints is
    asserted in :func:`test_the_roll_entry_point_is_wired_and_says_what_it_did`,
    from a real subprocess, which is a better witness anyway.
    """
    scope = daily_scope_for(YESTERDAY)
    assert ledger_row(scope) is None, "the day's row must not exist before it is rolled"

    with (
        mock.patch.dict(os.environ, {DAILY_CAP_USD_ENV: "0.03"}),
        mock.patch.object(budget, "_utc_now", return_value=datetime(1999, 1, 1, 12, tzinfo=UTC)),
    ):
        assert main(["roll"]) == 0

    row = ledger_row(scope)
    assert row is not None, "roll did not open the day's ledger row"
    assert (row.cap_microusd, row.spent_microusd) == (30_000, 0)


def test_rolling_twice_neither_widens_the_cap_nor_clears_the_days_spend(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """Idempotent, and idempotent in the direction that matters.

    A cron that fires twice, or an operator re-running it after a failure, must
    not hand the day a fresh budget -- that is "reset the spend" with a friendlier
    name, and it is the move migration 0005 deleted the write grants to stop.
    """
    scope = daily_scope_for(YESTERDAY)
    roll_daily_scope(YESTERDAY, RESERVE_AMOUNT * 2)
    run_row = make_scope(RESERVE_AMOUNT * 20)

    with mock.patch.dict(os.environ, {DAILY_SCOPE_ENV: scope}):
        _reserve(run_id_for(run_row), unique("roll-twice"))
    assert spent(owner_engine, scope) == RESERVE_AMOUNT

    assert main(["roll", "--day", YESTERDAY.isoformat(), "--cap-usd", "100.00"]) == 0

    row = ledger_row(scope)
    assert row is not None
    assert row.cap_microusd == RESERVE_AMOUNT * 2, (
        "a second roll widened an existing day's cap; raising a cap is a deliberate "
        "ops action against the row, never a side effect of the cron firing twice"
    )
    assert row.spent_microusd == RESERVE_AMOUNT, "a second roll cleared the day's spend"


def test_the_roll_entry_point_is_wired_and_says_what_it_did(
    owner_engine: Engine, configured_url: str
) -> None:
    """``python -m recon.budget roll`` as a real process -- what the cron runs.

    In-process ``main([...])`` proves the function; this proves the *entry point*,
    which is the thing ``infra/render.yaml`` schedules: the module is executable,
    the subcommand exists beside ``sweep``, it connects as the ops principal from
    ``DATABASE_URL``, and it reports the row and the cap so an operator reading
    the cron log knows whether the day is open.

    ``PYTEST_CURRENT_TEST`` is removed for the same reason
    ``tests/incidents/test_reachability`` removes it: an operator's cron has no
    such variable, and leaving it in would test a path production never takes.
    """
    day = date(1999, 1, 3)
    scope = daily_scope_for(day)
    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    env["DATABASE_URL"] = configured_url
    env["KEYSTONE_REQUIRE_DB"] = "1"
    command = [sys.executable, "-m", "recon.budget", "roll", "--day", day.isoformat()]

    opened = subprocess.run(
        [*command, "--cap-usd", "0.05"],
        cwd=SERVICE_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert opened.returncode == 0, f"stderr:\n{opened.stderr}"
    assert f"opened: {scope} cap=50000 microusd" in opened.stdout

    again = subprocess.run(
        [*command, "--cap-usd", "999.00"],
        cwd=SERVICE_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert again.returncode == 0, f"stderr:\n{again.stderr}"
    assert f"already open: {scope} cap=50000 microusd" in again.stdout, (
        "a re-run must report the cap that is really in force, not the one it would have set"
    )

    row = ledger_row(scope)
    assert row is not None and row.cap_microusd == 50_000


def test_roll_refuses_a_day_that_is_not_a_calendar_date() -> None:
    """``--day`` is parsed, not interpolated: a bad value cannot name a junk row."""
    with pytest.raises(SystemExit) as excinfo:
        main(["roll", "--day", "yesterday"])
    assert excinfo.value.code == 2


def test_an_unrolled_day_is_a_loud_configuration_fault_and_not_a_free_call(
    make_scope: ScopeFactory,
) -> None:
    """Before the cron has run, every call is refused -- and refused as what it is.

    :class:`LedgerScopeMissing` and deliberately **not** :class:`BudgetCapExceeded`:
    a day nobody opened is not a budget somebody reached, and recording it as one
    would write false ``cap_hit`` rows into the audit log the dashboard reconciles
    against (R18) and page an operator about a cap that was never hit.
    """
    make_scope(RESERVE_AMOUNT * 20)
    unopened = daily_scope_for(TODAY)
    assert ledger_row(unopened) is None

    with (
        mock.patch.dict(os.environ, {DAILY_SCOPE_ENV: unopened}),
        pytest.raises(LedgerScopeMissing) as excinfo,
    ):
        _reserve(unique("unrolled"), unique("unrolled-key"))
    assert excinfo.value.scope == unopened
    assert not isinstance(excinfo.value, BudgetCapExceeded)


# ===========================================================================
# the point of all of it: yesterday's spend is not today's problem
# ===========================================================================
def test_yesterdays_spend_does_not_count_against_todays_cap(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """Exhaust one day's row against the real trigger; the next day still grants.

    This is the regression the fixed scope name *was*: with one row for all time,
    the reservation at the end of this test is refused for ever, because the cap
    it is measured against is a lifetime total. The two rows are named by
    :func:`daily_scope_for`, and the assertions at the top bind those names to
    what :func:`daily_scope` itself resolves to on either side of the midnight
    between them -- so this is the same pair of rows the deployment would use,
    not two rows a test invented.
    """
    yesterday = daily_scope_for(YESTERDAY)
    today = daily_scope_for(TODAY)
    assert _scope_named_at(datetime(1999, 1, 1, 23, 59, 59, tzinfo=UTC)) == yesterday
    assert _scope_named_at(datetime(1999, 1, 2, 0, 0, 1, tzinfo=UTC)) == today

    day_cap = RESERVE_AMOUNT * 2
    roll_daily_scope(YESTERDAY, day_cap)
    roll_daily_scope(TODAY, day_cap)
    run_row = make_scope(RESERVE_AMOUNT * 20)
    run_id = run_id_for(run_row)

    with mock.patch.dict(os.environ, {DAILY_SCOPE_ENV: yesterday}):
        _reserve(run_id, unique("yday-1"))
        _reserve(run_id, unique("yday-2"))
        with pytest.raises(BudgetCapExceeded) as excinfo:
            _reserve(run_id, unique("yday-3"))
    assert excinfo.value.scope == yesterday
    assert excinfo.value.sqlstate == KS_CAP_EXCEEDED
    assert spent(owner_engine, yesterday) == day_cap, "the day was not actually exhausted"

    # 00:00 UTC passes and the cron rolls the day. Nothing is reset: a different
    # row is named, and the previous one keeps its spend for ever.
    with mock.patch.dict(os.environ, {DAILY_SCOPE_ENV: today}):
        _reserve(run_id, unique("today-1"))

    assert spent(owner_engine, today) == RESERVE_AMOUNT, (
        "yesterday's exhausted budget refused today's call: the cap is still a "
        "lifetime cap wearing a daily name"
    )
    assert spent(owner_engine, yesterday) == day_cap, (
        "rolling the day moved or cleared yesterday's spend; a rolled day must be a "
        "new row, never a reset counter"
    )


def test_today_gets_its_own_cap_and_not_a_share_of_a_running_total(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """The new day's row starts at zero spend against its own full cap."""
    yesterday = daily_scope_for(YESTERDAY)
    today = daily_scope_for(TODAY)
    roll_daily_scope(YESTERDAY, RESERVE_AMOUNT)
    roll_daily_scope(TODAY, RESERVE_AMOUNT)
    run_row = make_scope(RESERVE_AMOUNT * 20)
    run_id = run_id_for(run_row)

    with mock.patch.dict(os.environ, {DAILY_SCOPE_ENV: yesterday}):
        _reserve(run_id, unique("full-day"))
    assert spent(owner_engine, yesterday) == RESERVE_AMOUNT

    fresh = ledger_row(today)
    assert fresh is not None
    assert fresh.spent_microusd == 0
    assert fresh.remaining_microusd == RESERVE_AMOUNT


# ===========================================================================
# the audit row: redacted on the way in, and still findable
# ===========================================================================
def test_a_cap_hit_is_still_findable_by_the_raw_scope_name(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """Routing the row through the redactor did not make ``audit_log`` unreadable.

    ``recon.suite.burst`` counts ``cap_hit`` rows with exactly this query -- one
    equality against the raw scope name -- and the committed ``spend-cap-burst``
    scorecard row asserts 124 of them. A ledger scope survives redaction
    unchanged (it is on the allow-list, so it is scrubbed rather than tokenised,
    and there is nothing in ``daily:1999-01-01`` to scrub), and this is the test
    that says so with the reader's own query rather than with the writer's.
    """
    scope = daily_scope_for(YESTERDAY)
    roll_daily_scope(YESTERDAY, RESERVE_AMOUNT)
    run_row = make_scope(RESERVE_AMOUNT * 20)
    run_id = run_id_for(run_row)

    with mock.patch.dict(os.environ, {DAILY_SCOPE_ENV: scope}):
        _reserve(run_id, unique("cap-1"))
        with pytest.raises(BudgetCapExceeded):
            _reserve(run_id, unique("cap-2"))

    with owner_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT count(*) FROM audit_log WHERE action = :a AND subject = :s"),
            {"a": AUDIT_CAP_HIT, "s": scope},
        ).scalar_one()
    assert rows == 1, "the cap_hit row is no longer findable under the scope that hit the cap"


def test_the_halt_lookup_reads_subjects_the_same_way_the_write_wrote_them(
    owner_engine: Engine,
) -> None:
    """A scope whose name the redactor REWRITES still halts, and still lifts.

    Every field of an ``audit_log`` row now goes through the committed redactor,
    ``subject`` included -- and the overspend halt is stored as an audit row and
    read back by subject. So write and lookup have to ask the same question. A
    scope name containing ``dob=<date>`` is scrubbed on the way in; if the lookup
    compared the raw name it would answer "not halted" for a halted scope, which
    is the direction that keeps spending.

    The row is provisioned here rather than through ``make_scope`` because the
    name is the point: the fixture builds clean ones.
    """
    scope = f"run:{unique('halt')}-dob=2015-12-16"
    stored = budget._audit_subject(scope)
    assert stored != scope, (
        "this test needs a scope name the redactor actually rewrites; it no longer "
        f"rewrites {scope!r}, so it is proving nothing"
    )
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO budget_ledger (scope, cap_microusd, spent_microusd) VALUES (:s, :c, 0)"
            ),
            {"s": scope, "c": RESERVE_AMOUNT * 10},
        )
    try:
        halt_scope(scope, reason="proving the lookup and the write agree")

        with owner_engine.connect() as conn:
            raw = conn.execute(
                text("SELECT count(*) FROM audit_log WHERE action = :a AND subject = :s"),
                {"a": AUDIT_SCOPE_HALTED, "s": scope},
            ).scalar_one()
            redacted = conn.execute(
                text("SELECT count(*) FROM audit_log WHERE action = :a AND subject = :s"),
                {"a": AUDIT_SCOPE_HALTED, "s": stored},
            ).scalar_one()
        assert raw == 0, "the personal value in the subject reached the database unredacted"
        assert redacted == 1, "the halt row was not written under its redacted subject"

        assert halted_scopes([scope]) == (scope,), (
            "the halt lookup did not find a halt it had just written; the two sides "
            "of the redaction have drifted"
        )

        with (
            mock.patch.dict(os.environ, {DAILY_SCOPE_ENV: scope}),
            pytest.raises(BudgetScopeHalted) as excinfo,
        ):
            _reserve(scope.removeprefix("run:"), unique("halted"))
        assert excinfo.value.scope == scope, "the refusal must name the caller's spelling"
        assert run_scope(scope.removeprefix("run:")) == scope
    finally:
        with owner_engine.begin() as conn:
            conn.execute(text("DELETE FROM budget_reservations WHERE scope = :s"), {"s": scope})
            conn.execute(text("DELETE FROM audit_log WHERE subject = :s"), {"s": stored})
            conn.execute(text("DELETE FROM audit_log WHERE subject = :s"), {"s": scope})
            conn.execute(text("DELETE FROM budget_ledger WHERE scope = :s"), {"s": scope})


def test_the_ledger_scope_names_this_module_uses_survive_redaction_unchanged() -> None:
    """The identity case, stated once so the two tests above are not luck.

    A daily scope carries an ISO date, which is one of the four shapes the
    redactor detects on sight. It is not tokenised here because the detector
    matches a *bare* date and ``daily:1999-01-01`` is not one -- which is a fact
    about the committed redactor, so it is asserted rather than assumed. If it
    ever stops being true, the halt lookup still works (both sides redact) and
    ``recon.suite.burst``'s raw ``subject`` query is what breaks, loudly, here.
    """
    for scope in (daily_scope_for(YESTERDAY), daily_scope_for(TODAY), "run:abc-123"):
        assert budget._audit_subject(scope) == scope
