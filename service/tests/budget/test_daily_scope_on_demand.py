"""The day's ledger row opens ITSELF, so a deploy does not need a human first.

Date-keying the daily cap (``daily:<YYYY-MM-DD>``) fixed a real defect -- a cap
that never rolled was a *lifetime* cap -- and introduced a deployment one. The
production row was the fixed string ``daily``, so the first request after the
change asked for a scope that did not exist and got
:class:`~recon.budget.LedgerScopeMissing`; **every** metered call in the live
service would have been refused, hourly reconcile included, until somebody added
a cron *and* ran it once by hand. A change that takes the service down until an
operator notices is not deployable, whatever its own tests say.

So today's row is opened **on demand**, by the principal that was always allowed
to open it, the first time a reservation looks for it and does not find it. Three
properties have to hold together, and each has a test here:

* **a fresh deploy on a new UTC day meters correctly with nobody touching
  anything** -- proved end to end, in a subprocess with ``PYTEST_CURRENT_TEST``
  removed, against a database that has no row for the day
  (:func:`test_a_fresh_process_on_an_unopened_day_meters_without_a_human`). That
  subprocess is the only honest witness available: ``recon.budget`` refuses a
  *test* process the real daily row outright (:class:`RealDailyScopeRefused`),
  and that refusal is a property this file must not weaken to observe another;
* **the capped party still cannot conjure a scope with a cap of its choosing.**
  Exactly one scope name is ever opened this way -- ``daily:<today in UTC>``,
  computed and never supplied -- so a harness override, yesterday's row, or a
  ``run:`` scope is refused
  (:func:`test_only_todays_own_row_is_opened_on_demand`), and an existing cap is
  never widened (:func:`test_opening_a_day_twice_never_widens_its_cap`);
* **it fails closed at the grant.** Opening the row is an INSERT on
  ``budget_ledger``, which ``recon_writer`` does not hold (migration 0005). A
  process configured as the capped party gets no row and the original refusal
  (:func:`test_the_capped_party_cannot_open_the_day_itself`) -- the boundary is
  the grant, not this function's good manners.

The days used here are in **1999**, deliberately, and the clock is injected to
reach them: no deployment will ever name a row in that family, so nothing here
can spend a real day's budget or be rescued by one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy import Engine, text

import recon.budget as budget
from recon.budget import (
    DAILY_CAP_USD_ENV,
    DAILY_SCOPE,
    DAILY_SCOPE_ENV,
    OPS_DATABASE_URL_ENV,
    daily_scope_for,
    ledger_row,
)
from recon.db import ROLE_RECON_WRITER, reset_engine_cache, role_url
from tests.budget.support import unique

#: The private day family this module opens rows in. See the module docstring.
TEST_DAY_FAMILY = "daily:1999-06-"

#: One UTC day per behaviour, spaced so no test's "yesterday" is another's
#: "today": a failure in one must not be maskable by a row another one left.
DEPLOY_DAY = date(1999, 6, 5)
ONLY_TODAY_DAY = date(1999, 6, 11)
YESTERDAY_OF_ONLY_TODAY = date(1999, 6, 10)
WIDEN_DAY = date(1999, 6, 17)
CAPPED_PARTY_DAY = date(1999, 6, 23)

#: Midday, so a test can never straddle the boundary it is not testing.
NOON = 12


def _noon(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, NOON, tzinfo=UTC)


SERVICE_ROOT = Path(__file__).resolve().parents[2]

#: The deployed shape of one metered call, driven from a process that is not a
#: test: reserve on both mandated scopes, call the (mock) provider, settle.
#: ``recon.llm.generate_rationale`` is what ``recon.reconciler`` calls per
#: conflict, so this is the live service's own path and not a stand-in for it.
#:
#: The clock is injected here rather than in the parent, because the whole point
#: of the subprocess is that ``PYTEST_CURRENT_TEST`` is absent from it -- which
#: is also what makes ``daily_scope()`` resolve the real, un-overridden row.
_DRIVER = """
import json, sys
from datetime import datetime
from unittest import mock

import recon.budget as budget
from recon.budget import daily_scope, provision_run_scope
from recon.llm import RationaleRequest, generate_rationale

moment = datetime.fromisoformat(sys.argv[1])
run_id, run_cap, key = sys.argv[2], int(sys.argv[3]), sys.argv[4]

with mock.patch.object(budget, "_utc_now", return_value=moment):
    provision_run_scope(run_id, run_cap)
    outcome = generate_rationale(
        RationaleRequest(subject="conflict-on-an-unopened-day", prompt="two sources disagree"),
        run_id=run_id,
        idempotency_key=key,
    )
    resolved = daily_scope()

print(json.dumps({
    "daily_scope": resolved,
    "status": outcome.status,
    "cost_microusd": outcome.cost_microusd,
    "has_text": outcome.text is not None,
    "detail": outcome.detail,
}))
"""


@pytest.fixture(autouse=True)
def _remove_the_test_day_rows(owner_engine: Engine) -> Iterator[list[str]]:
    """Delete every row this module opened, whatever happened.

    Autouse and unconditional, for the reason ``tests/budget/test_daily_roll.py``
    gives: a test that failed part way through has still charged a ledger row,
    and a leaked row makes the *next* run start from a used-up budget. Bounded to
    the 1999 family (which no deployment can name) plus whatever run scopes the
    test registers.
    """
    run_scopes: list[str] = []
    yield run_scopes
    with owner_engine.begin() as conn:
        for pattern in ({"p": f"{TEST_DAY_FAMILY}%"}, *({"p": scope} for scope in run_scopes)):
            conn.execute(text("DELETE FROM budget_reservations WHERE scope LIKE :p"), pattern)
            conn.execute(text("DELETE FROM audit_log WHERE subject LIKE :p"), pattern)
            conn.execute(text("DELETE FROM budget_ledger WHERE scope LIKE :p"), pattern)


@pytest.fixture(autouse=True)
def _no_engine_survives_this_module() -> Iterator[None]:
    """No cached engine crosses into or out of a test that repoints a principal.

    ``recon.budget.ops_engine`` re-reads ``OPS_DATABASE_URL`` on every call but
    caches an engine per DSN, and ``recon.db`` caches one per role; a test that
    points ops at the capped party must not leave either behind.
    """
    reset_engine_cache()
    yield
    reset_engine_cache()


# ===========================================================================
# the deployability property: no human, no cron, no manual first run
# ===========================================================================
def test_a_fresh_process_on_an_unopened_day_meters_without_a_human(
    owner_engine: Engine, _remove_the_test_day_rows: list[str]
) -> None:
    """A deploy onto a brand-new UTC day meters, from a cold database.

    This is the regression the date-keyed scope *was*: the production row is
    named after the day, the day the deploy lands on has no row, and before this
    every metered call in the live service was refused ``budget_error`` until an
    operator opened the row by hand. The deployed instance reconciles 3,050
    conflicts an hour on that path.

    Run as a real subprocess with ``PYTEST_CURRENT_TEST`` removed, exactly as
    ``test_the_roll_entry_point_is_wired_and_says_what_it_did`` does: an
    operator's process has no such variable, and it is the variable that makes
    ``recon.budget`` refuse a test process the real daily row.
    """
    scope = daily_scope_for(DEPLOY_DAY)
    assert ledger_row(scope) is None, "the day's row must not exist before the deploy"

    run_id = unique("fresh-deploy")
    _remove_the_test_day_rows.append(budget.run_scope(run_id))

    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    env.pop(DAILY_SCOPE_ENV, None)
    env[DAILY_CAP_USD_ENV] = "0.05"
    env["KEYSTONE_REQUIRE_DB"] = "1"

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _DRIVER,
            _noon(DEPLOY_DAY).isoformat(),
            run_id,
            "1000000",
            unique("fresh-deploy-key"),
        ],
        cwd=SERVICE_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, f"stderr:\n{completed.stderr}"
    result = json.loads(completed.stdout.strip().splitlines()[-1])

    assert result["daily_scope"] == scope, (
        "the process did not resolve the day-keyed production row, so this proves nothing"
    )
    assert result["status"] == "ok", (
        "a metered call on a day nobody had opened was refused; a deploy that needs "
        f"a human to run a cron once by hand is a live outage. detail: {result['detail']}"
    )
    assert result["has_text"], "status was ok but no rationale came back"

    row = ledger_row(scope)
    assert row is not None, "the day's ledger row was never opened"
    assert row.cap_microusd == 50_000, (
        "the day opened on demand did not carry the deployment's configured cap"
    )
    assert row.spent_microusd == result["cost_microusd"] > 0, (
        "the call was not charged to the row that was opened for it"
    )
    assert row.spent_microusd < row.cap_microusd

    with owner_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT state::text AS state FROM budget_reservations WHERE scope = :s"),
            {"s": scope},
        ).fetchall()
    states = [row.state for row in rows]
    assert states == ["settled"], f"the day's row holds {states!r}, not one settled reservation"


# ===========================================================================
# and it is still exactly one row, with exactly one cap
# ===========================================================================
def test_only_todays_own_row_is_opened_on_demand(owner_engine: Engine) -> None:
    """One computed name is openable this way. Nothing a caller or an env var names.

    The harness override (``KEYSTONE_DAILY_SCOPE``) documents that redirecting
    the mandated cap "cannot buy budget that was not already provisioned". If
    on-demand opening followed the *resolved* scope instead of today's own row,
    that sentence would stop being true: a stand-in name would mint a fresh
    ledger row with a fresh day's cap. So the condition is an equality against
    ``daily_scope_for(utc_today())`` and nothing else.
    """
    day = ONLY_TODAY_DAY
    refused = (
        daily_scope_for(YESTERDAY_OF_ONLY_TODAY),  # yesterday's row
        "run:a-harness-stand-in",  # what an override would name
        DAILY_SCOPE,  # the bare family migration 0005 seeded
        f"{daily_scope_for(day)}T12:00:00+00:00",  # a datetime that got this far
    )
    # Snapshotted rather than asserted absent, because one of these -- the bare
    # family -- is a row migration 0005 really does seed. What must not change is
    # that this path did nothing to any of them.
    before = {scope: ledger_row(scope) for scope in refused}

    with mock.patch.object(budget, "_utc_now", return_value=_noon(day)):
        for scope in refused:
            assert budget._open_todays_daily_scope(scope) is False, (
                f"{scope!r} was opened on demand; only today's own row may be"
            )
        assert budget._open_todays_daily_scope(daily_scope_for(day)) is True

    assert {scope: ledger_row(scope) for scope in refused} == before, (
        "opening today's row on demand touched a row that is not today's"
    )
    opened = ledger_row(daily_scope_for(day))
    assert opened is not None and opened.spent_microusd == 0


def test_opening_a_day_twice_never_widens_its_cap(owner_engine: Engine) -> None:
    """Re-entering the on-demand path is not a way to raise a cap or clear a spend.

    Every concurrent request on a cold morning takes this path at once, and a
    ``LedgerScopeMissing`` is retried by the next request whatever caused it. So
    "open the day" has to be exactly ``ON CONFLICT DO NOTHING`` -- raising a cap
    stays a deliberate ops action against the row, as it is for the cron.
    """
    day = WIDEN_DAY
    scope = daily_scope_for(day)
    with (
        mock.patch.dict(os.environ, {DAILY_CAP_USD_ENV: "0.02"}),
        mock.patch.object(budget, "_utc_now", return_value=_noon(day)),
    ):
        assert budget._open_todays_daily_scope(scope) is True
        first = ledger_row(scope)
        assert first is not None and first.cap_microusd == 20_000

    with (
        mock.patch.dict(os.environ, {DAILY_CAP_USD_ENV: "999.00"}),
        mock.patch.object(budget, "_utc_now", return_value=_noon(day)),
    ):
        assert budget._open_todays_daily_scope(scope) is True

    again = ledger_row(scope)
    assert again is not None
    assert again.cap_microusd == 20_000, (
        "re-entering the on-demand path widened the day's cap; that is 'give me a "
        "bigger budget' with a friendlier name"
    )


# ===========================================================================
# the boundary is the grant, not this function's good manners
# ===========================================================================
def test_the_capped_party_cannot_open_the_day_itself(
    owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configured as ``recon_writer``, opening the day fails and stays failed.

    ``recon_writer`` holds no INSERT on ``budget_ledger`` at all (migration
    0005), which is what closes "insert a brand new scope with a cap of my
    choosing". On-demand opening runs on :func:`recon.budget.ops_engine`, so it
    inherits that boundary rather than working around it: point ops at the capped
    party and no row appears, the helper reports failure, and the caller's
    original :class:`LedgerScopeMissing` is what survives.
    """
    day = CAPPED_PARTY_DAY
    scope = daily_scope_for(day)
    monkeypatch.setenv(
        OPS_DATABASE_URL_ENV, role_url(ROLE_RECON_WRITER).render_as_string(hide_password=False)
    )
    reset_engine_cache()

    with mock.patch.object(budget, "_utc_now", return_value=_noon(day)):
        assert budget._open_todays_daily_scope(scope) is False, (
            "the capped party opened its own day's ledger row"
        )

    # Read back through the OWNER, not through `ledger_row`: ops is pointed at
    # the capped party for the length of this test, and the question is what is
    # in the table, not what that principal can see of it.
    with owner_engine.connect() as conn:
        present = conn.execute(
            text("SELECT count(*) FROM budget_ledger WHERE scope = :s"), {"s": scope}
        ).scalar_one()
    assert present == 0, "a row appeared for a principal with no INSERT on the table"
