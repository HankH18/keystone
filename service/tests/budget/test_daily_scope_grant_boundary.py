"""The grant is what refuses the capped party -- proved against a positive control.

``tests/budget/test_daily_scope_on_demand.py`` already asserts that a process
configured as ``recon_writer`` cannot open the day's ledger row
(``test_the_capped_party_cannot_open_the_day_itself``): the helper answers
``False`` and no row appears. Both halves of that are also true of a build in
which on-demand opening **does not exist at all** -- a
:func:`recon.budget._open_todays_daily_scope` whose body is ``return False``
passes it unchanged. Measured, not reasoned: stubbing the body out turns three
tests in that module red and leaves that one green.

A negative with no positive control cannot tell "the grant refused this INSERT"
from "nothing attempted an INSERT", and it is the first of those two that R17's
boundary rests on. So this file pins the pair together, in one test, on one
function:

* **the same call opens a day** when ops is the ops principal, at the
  deployment's own cap -- so a build that opens nothing fails here rather than
  passing quietly;
* **the same call opens nothing** when ops is pointed at the capped party, and
* **the reason is ``42501``**, asserted on the raw INSERT that
  :func:`recon.budget.provision_scope` issues. "Permission denied" and "some
  exception happened" are different claims, and only the first one is the
  boundary migration 0005 built.

Days here are in **1999-07**, one family clear of the ``1999-06`` days the
sibling module opens and deletes, so neither module's cleanup can mask the
other's failure and no deployment will ever name a row in either.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from unittest import mock

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

import recon.budget as budget
from recon.budget import DAILY_CAP_USD_ENV, OPS_DATABASE_URL_ENV, daily_scope_for
from recon.db import ROLE_RECON_WRITER, engine_for_role, reset_engine_cache, role_url
from tests.schema.conftest import assert_insufficient_privilege

#: The private day family this module opens rows in. See the module docstring.
TEST_DAY_FAMILY = "daily:1999-07-"

#: One day for the positive control, one for the refusal. Distinct, so a row the
#: first half opened can never be what the second half reads back as "absent".
OPS_DAY = date(1999, 7, 6)
CAPPED_PARTY_DAY = date(1999, 7, 14)

#: The cap this test configures, in USD and in the microusd it must become.
CAP_USD = "0.07"
CAP_MICROUSD = 70_000

#: Midday, so no assertion here can straddle a UTC boundary it is not about.
NOON = 12


def _noon(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, NOON, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _remove_the_test_day_rows(owner_engine: Engine) -> Iterator[None]:
    """Delete every row this module opened, whatever happened.

    Unconditional for the reason the sibling modules give: a test that failed
    part way through has still opened a ledger row, and a leaked row makes the
    next run start from a budget something already spent.
    """
    yield
    with owner_engine.begin() as conn:
        pattern = {"p": f"{TEST_DAY_FAMILY}%"}
        conn.execute(text("DELETE FROM budget_reservations WHERE scope LIKE :p"), pattern)
        conn.execute(text("DELETE FROM audit_log WHERE subject LIKE :p"), pattern)
        conn.execute(text("DELETE FROM budget_ledger WHERE scope LIKE :p"), pattern)


@pytest.fixture(autouse=True)
def _no_engine_survives_this_module() -> Iterator[None]:
    """No cached engine crosses into or out of a test that repoints a principal.

    :func:`recon.budget.ops_engine` re-reads ``OPS_DATABASE_URL`` on every call
    but caches an engine per DSN, and :mod:`recon.db` caches one per role.
    """
    reset_engine_cache()
    yield
    reset_engine_cache()


def _row_count(engine: Engine, scope: str) -> int:
    """How many ledger rows exist for ``scope``, read through the OWNER.

    Never through :func:`recon.budget.ledger_row`: ops is pointed at the capped
    party for half of this test, and the question is what is in the table, not
    what that principal can see of it.
    """
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM budget_ledger WHERE scope = :s"), {"s": scope}
        ).scalar_one()


def test_the_day_opens_for_ops_and_the_grant_is_what_refuses_the_capped_party(
    owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One function, two principals: it opens for ops and is denied for the capped party.

    The two halves have to be the same call for this to mean anything. A build
    that cannot open a day for anybody satisfies the second half on its own,
    which is exactly the hole this test exists to close.
    """
    monkeypatch.setenv(DAILY_CAP_USD_ENV, CAP_USD)

    # ---- the positive control: ops opens the day, at the deployment's cap ----
    ops_scope = daily_scope_for(OPS_DAY)
    assert _row_count(owner_engine, ops_scope) == 0, "the day's row existed before the test"

    with mock.patch.object(budget, "_utc_now", return_value=_noon(OPS_DAY)):
        assert budget._open_todays_daily_scope(ops_scope) is True, (
            "the ops principal could not open the day on demand; with this false the "
            "refusal asserted below proves nothing about any grant"
        )

    assert _row_count(owner_engine, ops_scope) == 1, "ops reported success and opened no row"
    with owner_engine.connect() as conn:
        cap = conn.execute(
            text("SELECT cap_microusd FROM budget_ledger WHERE scope = :s"), {"s": ops_scope}
        ).scalar_one()
    assert cap == CAP_MICROUSD, (
        f"the day opened on demand carries {cap} microusd, not the deployment's "
        f"configured {DAILY_CAP_USD_ENV}={CAP_USD}"
    )

    # ---- the same call, as the capped party: no row, and 42501 is why ----
    capped_scope = daily_scope_for(CAPPED_PARTY_DAY)
    monkeypatch.setenv(
        OPS_DATABASE_URL_ENV, role_url(ROLE_RECON_WRITER).render_as_string(hide_password=False)
    )
    reset_engine_cache()

    with mock.patch.object(budget, "_utc_now", return_value=_noon(CAPPED_PARTY_DAY)):
        assert budget._open_todays_daily_scope(capped_scope) is False, (
            "the capped party opened its own day's ledger row"
        )

    assert _row_count(owner_engine, capped_scope) == 0, (
        "a row appeared for a principal that holds no INSERT on budget_ledger"
    )

    # The refusal above is a *permission denial* and not a typo, a dead
    # connection, or a table that moved: this is the statement
    # `recon.budget.provision_scope` issues, run as the same role.
    with (
        pytest.raises(DBAPIError) as excinfo,
        engine_for_role(ROLE_RECON_WRITER).begin() as conn,
    ):
        conn.execute(
            text(
                "INSERT INTO budget_ledger (scope, cap_microusd, spent_microusd) "
                "VALUES (:scope, :cap, 0) ON CONFLICT (scope) DO NOTHING RETURNING scope"
            ),
            {"scope": capped_scope, "cap": CAP_MICROUSD},
        )
    assert_insufficient_privilege(excinfo.value)
