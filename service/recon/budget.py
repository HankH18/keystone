"""Spend cap: reserve worst-case, call, settle actuals (SPEC R17, DESIGN §Budget ledger).

**This module wraps a cap that lives in the database. It does not implement one.**

That distinction is the whole design. An earlier red-team pass broke a
Python-side cap by simply zeroing ``budget_ledger.spent_microusd``; migration
0005's answer was to delete the writable spend column rather than to guard it.
``recon_writer`` today holds **no INSERT and no UPDATE on ``budget_ledger`` at
all**. Spend moves only under the triggers on the append-only
``budget_reservations`` table:

* **RESERVE** is one atomic ``INSERT``. Its ``BEFORE INSERT`` trigger takes the
  ledger row lock (``SELECT ... FOR UPDATE``), checks ``spent + reserve <=
  cap``, and either increments spend or raises SQLSTATE ``KS006``. A raise
  means: **the spend stops**. What else stops -- and what deliberately does not
  -- is spelled out under `What "stop on cap" actually stops`_ below, because
  three documents used to claim a run-level halt that no caller performed.
* **SETTLE** is ``UPDATE(actual_microusd, state, settled_at, settle_evidence,
  settle_proof, usage_*)``, ``open -> settled``, exactly once, and -- since
  migration 0010 -- at an amount the settle trigger **derives from the row
  itself** rather than one the caller names. It releases the difference.
* ``open -> reclaimed`` — the release-in-full transition — is refused to
  ``recon_writer`` and belongs to the **ops principal**, because a capped party
  that can reclaim a reservation it actually consumed has re-invented "zero the
  spend". Nothing in this module performs it any more: the TTL sweeper closes a
  dead lease by ``open -> settled`` at the **full reservation**, so the
  ``reclaimed`` state survives only as the schema's refusal.

So there is deliberately **no Python-side cap check anywhere in this file**. A
second check could disagree with the database, and the moment it disagrees the
database is right and the Python is a bug that reads like a safeguard. What
Python adds is the four things SQL cannot do: pricing a call from the committed
table, spanning both scopes atomically, turning ``KS006`` into a logged refusal
with a ``cap_hit`` audit row and an alert, and -- the part no trigger can see --
deciding *which* evidence a call produced. What the release is worth, given that
evidence, is the database's arithmetic and no longer this module's.

The daily cap is a DAY, and something has to roll it
----------------------------------------------------
R17 mandates a *daily* cap, so the ledger row that carries it is keyed on the
**UTC date**: ``daily:2026-08-25`` (:func:`daily_scope`, :func:`daily_scope_for`).
It used to be the fixed string ``daily``, and nothing anywhere rolled it -- which
made the "daily" cap a **lifetime** cap. Measured, in
``recon.incidents._daily_cap_for``'s own note: one bare ``python -m
recon.incidents`` pass over the golden set costs 56,487 microusd of it, so the
seeded 5 USD budget was spent for good after ~88 hand runs and every metered call
in the service was refused from then on, with no date on which that recovered.

A date-keyed scope rolls by **naming a different row**, which is the only form of
rolling this schema can express honestly: ``spent_microusd`` is writable by
nobody (migration 0005 deleted the write grants rather than guarding them), so
"reset the day" cannot mean zeroing a counter -- zeroing the spend is the exact
red-team move the schema exists to refuse. Yesterday's row keeps yesterday's
reservations and yesterday's spend for ever, and today's cap is a fresh row.

The new day's row is therefore **provisioned by ops**, like every other ledger
row (:func:`provision_scope`; ``recon_writer`` holds no INSERT on
``budget_ledger`` at all).

**And ops opens it by itself, the first time a reservation looks for it**
(:func:`_open_todays_daily_scope`). That is not a convenience. Keying the scope
on the date means the row the deployment had -- the bare string ``daily`` --
stops being the row the code asks for the moment the change ships, so a version
that waited for a cron would refuse **every** metered call in the live service,
hourly reconcile included, from the deploy until a human added the cron *and* ran
it once by hand. A change that takes the service down until somebody notices is
not a fix, whatever its own tests say. So :func:`reserve` catches
:class:`LedgerScopeMissing` for exactly one scope name -- ``daily:<today in
UTC>``, which it computed rather than accepted -- opens it on the ops principal
at the deployment's configured cap, and retries once.

What that deliberately is *not* is a way to obtain budget. The name is not a
parameter, an override (:data:`DAILY_SCOPE_ENV`) is never opened this way, the
cap is :data:`DAILY_CAP_USD_ENV` parsed exactly as migration 0005 parses it,
``ON CONFLICT DO NOTHING`` means an existing day is never widened or re-zeroed,
and the INSERT still runs on :func:`ops_engine` -- so a process configured *as*
the capped party gets a permission error, no row, and the original refusal. Every
other scope, including yesterday's and a stand-in an override names, still raises
:class:`LedgerScopeMissing`: a loud configuration fault, and never a quiet charge
to somebody else's budget.

``python -m recon.budget roll`` stays, because opening a day *ahead* of time, at
a stated cap, or re-opening one the deployment missed is a real ops action. It is
no longer a cron the deployment depends on to serve traffic.

.. _What "stop on cap" actually stops:

What "stop on cap" actually stops
---------------------------------
``KS006`` stops the SPEND, for every caller, with no way past it: the trigger
refuses the reservation, nothing is charged, no provider call happens without a
live reservation, and a retry is a fresh trip through the same trigger rather
than a second use of a refused one. Every refusal logs, writes a ``cap_hit``
audit row and fires the alert (:func:`record_cap_hit`).

What happens to the *run* is the caller's decision, and the two callers differ.
Both are stated here rather than in one word, because "``KS006`` ⇒ halt run" was
written in this docstring, in :mod:`recon.llm` and in ``docs/DESIGN.md`` while
no caller performed a run-level halt at all:

* **the metered batch job halts.** ``python -m recon.incidents`` lets
  :class:`BudgetCapExceeded` propagate out of its embedding pass
  (:func:`recon.incidents.embed_descriptors`, whose ``except BudgetError: raise``
  is there precisely so it does) and exits ``EXIT_REFUSED`` from
  :func:`recon.incidents.main`, whose ``except (IncidentError, BudgetError,
  ValueError)`` catches **every** class in this module's hierarchy. Stop, logged,
  alerted, non-zero exit;
* **the reconcile path degrades, and does not halt.**
  :func:`recon.llm.generate_rationale` returns ``status="cap_hit"`` with
  ``text=None``; ``recon.reconciler``'s rationale hook turns that into ``None``
  and the proposal lands with ``rationale NULL``. The run continues. Every later
  conflict makes its own reservation and meets the same refusal, so the audit log
  carries one ``cap_hit`` row and one alert per refused attempt, not one per run.

The degradation is deliberate, and it is not a gap left open. The LLM is
rationale *text* and nothing else -- it never detects, never scores, never writes
-- so ending a detection run because its nicety budget is gone would drop
conflicts that the cap has nothing to do with. And the obvious "fix" (a
process-side latch that stops attempting once the cap has spoken) is exactly the
Python-side cap check this module refuses to have: it answers "is there budget?"
without asking the database. The committed ``spend-cap-burst`` scorecard row is
the measurement that the answer always comes from the trigger -- 120 contenders,
6 granted, 114 refused, **every** refusal carrying ``KS006``, 124 ``cap_hit``
rows and 124 alerts -- and a latch would replace 114 database refusals with 113
Python guesses.

Both scopes, one transaction, and no way to ask for fewer
----------------------------------------------------------
R17 mandates a per-run cap **and** a hard daily cap. :func:`reserve` inserts one
reservation per scope inside a **single transaction**, in sorted scope order
(``daily`` before ``run:*``).

**It has no ``scopes`` parameter.** It used to, guarded by a type whose
constructor walked the stack and matched ``frame.f_code.co_filename`` by path
suffix; a red team built one with ``exec(compile(src,
"/anywhere/service/tests/x.py", "exec"))`` and no file edits, and every caller
was then one keyword away from a real billed call that never touched the daily
cap. Stack inspection is not a security boundary. Applying the mandated scope is
this module's job now (:func:`daily_scope`), and *which ledger row* carries it is
deployment configuration in the same class as ``DATABASE_URL``.

Two consequences of the single transaction, both load-bearing:

* the run that is inside its own run cap but over the daily cap is refused, and
  refused *atomically* — the ``daily`` insert raising ``KS006`` aborts the
  transaction, so the ``run:`` reservation that may already have incremented its
  ledger row is rolled back with it. There is no window in which a run is
  charged for a call the daily cap refused;
* a fixed lock order across every caller means the burst serialises rather than
  deadlocking.

No retry bypasses the cap
-------------------------
Every attempt reserves. :func:`recon.llm.generate_rationale` retries by calling
:func:`reserve` again with a fresh idempotency key, so a retry is a new trip
through the same trigger, not a second use of a reservation that was already
refused. There is no code path in this module that performs a provider call
without a reservation, and none that reuses one.

The release is a DATABASE rule, not a Python one
------------------------------------------------
This is the governing principle of the whole module, and it is the one a red
team broke three times: **the cap can be defeated by making the application
refund money it actually spent.**

The first fix wrote the principle into docstrings. The second made it
structural in Python -- one function, no amount parameter, a typed evidence
value -- and a third red team walked straight past it, because *Python cannot be
the boundary while the grant still permits the write*. As ``recon_writer`` it
settled live reservations at ``actual = 0`` with

    UPDATE public.budget_reservations SET actual_microusd = 0, state = 'settled' ...
    UPDATE "budget_reservations"      SET actual_microusd = 0, state = 'settled' ...
    UPDATE ONLY budget_reservations   SET actual_microusd = 0, state = 'settled' ...

-- three spellings the project's AST counter does not match and, far more to the
point, three statements the grant allowed however they were written. It also
called the chokepoint with ``reserve_microusd=0``: a caller-supplied amount that
the "fail-closed" evidence value echoed straight back, releasing 15,850 microusd
through the function whose docstring said it took no amount.

So the boundary moved to where every other boundary in this project already is.
**Migration 0010 makes the settle a rule of the database:**

* a reservation is **price-bound**. It names its ``model`` and the token bounds
  it was sized against, and the reserve trigger refuses it unless
  ``reserve_microusd`` is exactly the worst case that the ops-owned rates in
  ``budget_model_prices`` give for those bounds. The capped party cannot write a
  rate, and deflating the binding deflates the reservation it is trying to keep;
* a price-bound reservation settles **only against a ``settle_evidence`` value**,
  and each value fixes the amount *from the row*: ``provider_reported_usage``
  must equal what the committed rates price the recorded usage at (non-degenerate
  and within the reserved bounds), ``cost_exceeded_reservation`` and
  ``outcome_unknown`` must equal the reservation exactly, and ``never_sent`` must
  be zero and carry a proof from a closed vocabulary. A settlement that names an
  amount and no reason is ``KS007``, whatever statement carried it.

What stays on this side is the *choice of evidence*, which no trigger can make:

* :class:`ProviderReportedUsage` -- the provider reported usage that is
  **present and non-degenerate**. A usage block that is absent, zeroed, or
  reports no output tokens for a call that returned text is not evidence and
  cannot be constructed (:class:`DegenerateUsage`);
* :class:`NeverSent` -- the request **provably** never left this process. It
  takes a :class:`PreSendProof` member, classified from the transport's own
  exception, and never a sentence: ``NeverSent("trust me bro")`` used to release
  a whole reservation. The database refuses the operator-grade proof
  (``ops_attested_outage``) to ``recon_writer`` and refuses any pre-send claim
  made more than :data:`NEVER_SENT_WINDOW_SECONDS` after the reservation was
  created;
* :class:`OutcomeUnknown` -- everything else. A timeout, a read error, a 5xx
  after send, a cancelled stream, an unrecognised error class, a holder whose
  lease expired, a successful call whose usage was degenerate. This is the
  *absence* of evidence, so it charges the **FULL RESERVATION** and releases
  nothing. There is no guessed middle number: guessing low is the leak.

:func:`_close_reservation` is still the only ``UPDATE`` against
``budget_reservations`` in the package, still takes no amount -- it no longer
takes ``reserve_microusd`` either -- and the amount is now read out of the ROW
inside the closing statement. ``tests/budget/test_release_chokepoint.py`` and
:func:`recon.suite.burst.release_sites` still count the release sites, and both
now say plainly what that count is: **defence in depth, and not the boundary.**

At the boundary, be conservative: a failure that cannot be *proved* pre-send is
:class:`OutcomeUnknown`.

Two consequences follow, both correct, both stated here rather than discovered:

* **a retry after a post-send failure pays the worst case twice.** That is the
  honest price of not knowing where a failure happened, and it is why a failure
  storm now walks into the cap and is **refused** there -- ``KS006``, every
  attempt, until the day rolls -- instead of refunding itself forever against a
  ledger that never moves. What the refusal does to the *run* is the caller's,
  and both answers are above;
* **an abandoned reservation consumes its budget permanently.** A dead lease is
  evidence the *holder* died. It is not evidence the *call* did not happen -- a
  child process that completed a paid call and was then ``SIGKILL``ed used to
  get a 100% refund. So :func:`sweep_expired_reservations` closes a dead lease by
  **charging it in full**, and that budget is gone until ops re-provisions the
  scope. Losing budget to a crash is the correct direction; refunding a call that
  may have happened is not.

An overspend HALTS the scope, in the ledger, not in a return value
------------------------------------------------------------------
An actual above the reservation is a cap-relevant event, not a rounding detail:
the ledger is now known to under-count real spend, so every later reservation is
checked against a number that is wrong in the dangerous direction.
:func:`settle_capped` therefore settles at the reservation (the database refuses
more, correctly), audits and alerts the shortfall, **halts every scope the
reservation touched**, and raises :class:`BudgetOverspend`.

The halt is durable and lives in the ``audit_log`` the dashboard already
reconciles against (R18), not in a status string somebody has to remember to
read: :func:`reserve` refuses a halted scope with :class:`BudgetScopeHalted`
*before* it inserts anything. An earlier version raised ``BudgetOverspend``,
turned it into ``status="overspend"``, and nothing consumed that value -- 20
consecutive calls each overspending by ~30,000,000 microusd all proceeded.
Clearing a halt is an ops action (:func:`resume_scope`), never the capped
party's.

Money
-----
Integer microusd everywhere, parsed through :class:`~decimal.Decimal`, never a
float. Every derived amount is rounded **up**, so rounding can only over-charge
the ledger — an under-charge is a slow leak past the cap.
"""

from __future__ import annotations

import math
import os
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar, Final

import yaml
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from recon.db import ROLE_RECON_WRITER, get_engine, role_connection
from recon.logging import get_logger, insert_audit_row

__all__ = [
    "ALERT_CAP_HIT",
    "ALERT_SCOPE_HALTED",
    "ALERT_SETTLE_OVERFLOW",
    "AUDIT_ACTOR",
    "AUDIT_ACTOR_OPS",
    "AUDIT_CAP_HIT",
    "AUDIT_LLM_CALL",
    "AUDIT_LLM_CALL_FAILED",
    "AUDIT_SCOPE_HALTED",
    "AUDIT_SCOPE_RESUMED",
    "AUDIT_SETTLE_OVERFLOW",
    "AUDIT_SWEEP_CHARGED",
    "DAILY_CAP_USD_ENV",
    "DAILY_SCOPE",
    "DAILY_SCOPE_ENV",
    "DAILY_SCOPE_SEPARATOR",
    "DEFAULT_DAILY_CAP_USD",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_SWEEP_GRACE_SECONDS",
    "KS_CAP_EXCEEDED",
    "KS_RESERVATION_LIFECYCLE",
    "MAX_LEASE_SECONDS",
    "MICROUSD_PER_USD",
    "NEVER_SENT_WINDOW_SECONDS",
    "OPS_DATABASE_URL_ENV",
    "SQLSTATE_UNIQUE_VIOLATION",
    "BudgetCapExceeded",
    "BudgetError",
    "BudgetOverspend",
    "BudgetScopeHalted",
    "CostExceededReservation",
    "DegenerateUsage",
    "LedgerRow",
    "LedgerScopeMissing",
    "ModelPrice",
    "NeverSent",
    "OutcomeUnknown",
    "PreSendProof",
    "PriceTable",
    "ProviderReportedUsage",
    "RealDailyScopeRefused",
    "Reservation",
    "Settlement",
    "SettlementRefused",
    "SpendEvidence",
    "SweptReservation",
    "UnknownModelError",
    "Usage",
    "ZeroReservationRefused",
    "cap_microusd_from_env",
    "cost_microusd",
    "daily_scope",
    "daily_scope_for",
    "degenerate_usage_reason",
    "fire_alert",
    "halt_scope",
    "halted_scopes",
    "lease_deadline",
    "lease_seconds_from_key",
    "ledger_row",
    "load_price_table",
    "main",
    "ops_engine",
    "price_table",
    "provision_run_scope",
    "provision_scope",
    "record_cap_hit",
    "record_settle_overflow",
    "register_alert_sink",
    "reserve",
    "resume_scope",
    "roll_daily_scope",
    "run_scope",
    "scope_key",
    "settle",
    "settle_capped",
    "settle_failed_call",
    "sweep_expired_reservations",
    "unregister_alert_sink",
    "utc_today",
    "worst_case_input_tokens",
    "worst_case_microusd",
]

log = get_logger("recon.budget")

#: Project SQLSTATEs raised by the reservation triggers (migration 0005).
#: Outside every built-in Postgres error class, so a test asserting one of them
#: cannot pass on an unrelated failure -- a dropped connection, a typo'd table
#: or a deadlock produces a *different* code and fails.
KS_CAP_EXCEEDED: Final = "KS006"
KS_RESERVATION_LIFECYCLE: Final = "KS007"

#: Postgres' own ``unique_violation``. A reservation key is UNIQUE across the
#: whole table, so a replayed idempotency key arrives as this and **not** as a
#: project SQLSTATE. It is a documented outcome, not an error: see
#: :func:`reserve`.
SQLSTATE_UNIQUE_VIOLATION: Final = "23505"

MICROUSD_PER_USD: Final = 1_000_000

#: The **family** the hard daily cap's ledger rows belong to. One row per UTC
#: day, named ``daily:<YYYY-MM-DD>`` by :func:`daily_scope_for`; this bare string
#: is the row migration 0005 seeds and is no longer charged by anything, because
#: a cap that never rolls is a lifetime cap. See :func:`daily_scope`.
DAILY_SCOPE: Final = "daily"

#: Separates the family from the UTC day in a daily scope name.
DAILY_SCOPE_SEPARATOR: Final = ":"

#: Points the mandated daily cap at a stand-in ledger row. Deployment
#: configuration, never a caller's argument -- see :func:`daily_scope`.
DAILY_SCOPE_ENV: Final = "KEYSTONE_DAILY_SCOPE"

#: The day's cap, read exactly as migration 0005 reads it (same variable, same
#: default, same :func:`cap_microusd_from_env` parse), so the row ``roll``
#: provisions for a new day carries the cap the migration would have seeded.
DAILY_CAP_USD_ENV: Final = "DAILY_CAP_USD"
DEFAULT_DAILY_CAP_USD: Final = "5.00"

#: The **ops** principal's DSN, when the process's own ``DATABASE_URL`` is not
#: it. See :func:`ops_engine`.
OPS_DATABASE_URL_ENV: Final = "OPS_DATABASE_URL"

#: `audit_log.actor` must match `^system:` for `recon_writer` (SQLSTATE KS003).
AUDIT_ACTOR: Final = "system:budget"

#: The ops principal's actor. Distinct from :data:`AUDIT_ACTOR` so "ops lifted
#: this halt" and "the capped party halted itself" are different rows (R18).
AUDIT_ACTOR_OPS: Final = "system:budget-ops"

#: The audit `action` R17 names. The dashboard and `recon.suite` look for it.
AUDIT_CAP_HIT: Final = "cap_hit"
AUDIT_SETTLE_OVERFLOW: Final = "budget_settle_overflow"

#: A call that SUCCEEDED. R18 has the dashboard reconciling against these rows,
#: so a call that failed must never be written under this action: 1,000 timeouts
#: recorded as `llm_call` with cost 0 read as 1,000 free successful calls.
AUDIT_LLM_CALL: Final = "llm_call"

#: A call that FAILED, with the failure reason and the amount it was charged.
#: Its own action precisely so the two are distinguishable in the audit log.
AUDIT_LLM_CALL_FAILED: Final = "llm_call_failed"

#: A reservation the sweeper closed because its lease died. Charged in FULL: an
#: expired lease is evidence the holder is dead, never that the call did not go
#: out. Its own action so "the sweeper charged this" is legible in the audit log.
AUDIT_SWEEP_CHARGED: Final = "budget_lease_expired"

#: The durable overspend halt (R17/R18). ``subject`` is the SCOPE, so
#: :func:`reserve` can answer "is this scope halted?" with one indexed read of
#: the same audit log the dashboard reconciles against.
AUDIT_SCOPE_HALTED: Final = "budget_scope_halted"
#: The ops-only counterpart that lifts a halt. Never written by the capped party.
AUDIT_SCOPE_RESUMED: Final = "budget_scope_resumed"

#: Alert event names for the stubbed alerting hook.
ALERT_CAP_HIT: Final = "budget.cap_hit"
#: An actual above the reservation is cap-relevant: same halt, same alert.
ALERT_SETTLE_OVERFLOW: Final = "budget.settle_overflow"
#: A scope that will refuse every further reservation until ops resumes it.
ALERT_SCOPE_HALTED: Final = "budget.scope_halted"

#: How long a reservation's holder claims it may stay in flight. Stamped into
#: the reservation key at insert, and the ONLY thing that entitles the sweeper
#: to reclaim it. Generous relative to any provider timeout, because reclaiming
#: early is worse than reclaiming late: an early reclaim hands a live call's
#: budget to someone else and then loses that call's cost entirely (its settle
#: is refused ``KS007``), while a late reclaim only holds budget for longer.
DEFAULT_LEASE_SECONDS: Final = 300

#: Hard ceiling on a lease. A holder chooses its own lease *duration*, so this
#: bounds how long a dead process can pin budget before the sweeper closes it.
MAX_LEASE_SECONDS: Final = 3_600

#: Extra slack the sweeper adds on top of an expired lease before closing it.
#: Covers clock skew between the reserving process and the sweeper.
DEFAULT_SWEEP_GRACE_SECONDS: Final = 60

#: Separator introducing the lease **duration** inside a reservation key.
#:
#: A *duration*, deliberately, and not a deadline. The deadline used to be
#: ``int(lease_expires_at.timestamp())``, which baked a wall clock into the
#: UNIQUE key: the same logical idempotency key replayed 1.2 seconds later
#: produced a *different* key, missed the constraint, and MADE THE PAID CALL
#: AGAIN. A duration is a property of the caller's identity for the work -- the
#: provider's own timeout plus a fixed margin -- so a replay of the same logical
#: call collides on the UNIQUE constraint however long it took to arrive.
#:
#: The deadline is then ``created_at + duration``. Both halves are immutable
#: after insert (migration 0005's settle trigger freezes ``idempotency_key`` and
#: ``created_at``), and ``created_at`` is the *database's* clock, so a holder can
#: neither extend its own lease nor backdate its birth.
_LEASE_MARK: Final = "#lease"

#: Tokens of message/system framing the provider adds around a prompt. Added to
#: every worst-case input bound so the bound stays a bound.
FRAMING_TOKEN_OVERHEAD: Final = 64

#: Substring of the trigger's own diagnostic when a scope has no ledger row.
#: The trigger reuses ``KS006`` for that case, so the message is the only thing
#: that separates "the cap refused this" from "nobody provisioned this scope".
_MISSING_LEDGER_ROW: Final = "no budget_ledger row for scope"

_PRICES_FILENAME: Final = "prices.yaml"
_PRICE_FIELDS: Final = ("input", "output", "cache_read", "cache_write")


# ===========================================================================
# errors
# ===========================================================================
class BudgetError(RuntimeError):
    """Base class for every refusal this module raises."""


class UnknownModelError(BudgetError):
    """A model id that the committed price table does not price.

    Deliberately fatal. The alternative -- treating an unpriced model as free --
    reserves nothing for it, so the cap is never reached and spend is unbounded
    precisely for the model nobody reviewed.
    """


class BudgetCapExceeded(BudgetError):
    """The database refused a reservation with ``KS006``. **The spend stops here.**

    What stops is the spend, in the trigger, for every caller and with no way
    past it: nothing was charged, no provider call happens without a live
    reservation, and a retry is a fresh trip through the same trigger rather than
    a second use of a refused one.

    **What stops besides the spend depends on the caller, and this docstring used
    to name the wrong one.** It said "halt the run", which was true of neither
    caller at the time it was written -- the same claim the module docstring and
    ``docs/DESIGN.md`` carried while no code performed a run-level halt at all.
    The two real behaviours are:

    * ``python -m recon.incidents`` **does** halt. It lets this propagate out of
      :func:`recon.incidents.embed_descriptors` and exits ``EXIT_REFUSED`` from
      :func:`recon.incidents.main`, which catches :class:`BudgetError`. Non-zero
      exit, nothing further attempted;
    * the **reconcile path degrades and keeps going**.
      :func:`recon.llm.generate_rationale` turns this into ``status="cap_hit"``
      with ``text=None``, the proposal lands with ``rationale NULL``, and the run
      continues -- so every later conflict makes its own reservation, meets the
      same refusal, and writes its own ``cap_hit`` row and alert.

    Both are deliberate and the module docstring's `What "stop on cap" actually
    stops`_ has the reasoning: the LLM is rationale text only, so ending a
    detection run because its nicety budget is gone would drop conflicts the cap
    has nothing to do with, and a process-side latch to make "the run halts" true
    would be the Python cap check this module refuses to have.

    Carries the scope that refused, so a caller can tell "this run is done" from
    "the whole day is done" without re-reading the ledger.
    """

    def __init__(self, scope: str, reserve_microusd: int, detail: str) -> None:
        super().__init__(f"budget cap reached for scope {scope!r}: {detail}")
        self.scope = scope
        self.reserve_microusd = reserve_microusd
        self.detail = detail
        self.sqlstate = KS_CAP_EXCEEDED


class LedgerScopeMissing(BudgetError):
    """The scope has no ``budget_ledger`` row: nobody provisioned it.

    A configuration fault wearing ``KS006``'s clothes. It is deliberately *not*
    a :class:`BudgetCapExceeded`: recording it as one would write false
    ``cap_hit`` rows into the audit log the dashboard reconciles against (R18)
    and page someone about a budget that was never reached.

    **One scope is exempt and only one**: ``daily:<today in UTC>``, which
    :func:`reserve` opens on the ops principal and retries once
    (:func:`_open_todays_daily_scope`), because a date-keyed daily cap whose row
    nothing opened would refuse the whole live service on the day it shipped.
    Reaching this for any *other* scope -- a ``run:`` scope, a stand-in an
    override names, a past day -- still means exactly what it says, and reaching
    it for today's own row means the ops principal could not open it either.
    """

    def __init__(self, scope: str, detail: str) -> None:
        super().__init__(
            f"budget scope {scope!r} has no ledger row; ledger rows are provisioned "
            "by ops (recon.budget.provision_scope), never by the capped party"
        )
        self.scope = scope
        self.detail = detail
        self.sqlstate = KS_CAP_EXCEEDED


class ZeroReservationRefused(BudgetError):
    """A reservation of zero microusd: a live reservation that reserves nothing.

    Refused rather than granted. A zero reservation is a call the cap cannot
    see: it is admitted whatever the ledger says, and -- combined with a
    settlement that absorbed overflow -- it was an unmetered call. There is no
    legitimate caller: a real call always has a worst case above zero, so a zero
    here means either a zero-priced model or zero token bounds, and both are
    configuration faults.
    """

    def __init__(self, model: str, detail: str) -> None:
        super().__init__(
            f"refusing a zero reservation for model {model!r}: {detail}. A "
            "reservation of 0 is a live reservation that reserves nothing, so the "
            "call it covers is invisible to the cap."
        )
        self.model = model
        self.detail = detail


class BudgetOverspend(BudgetError):
    """The provider reported more spend than the reservation could hold.

    A cap-relevant event, not a rounding detail. The reservation settles at its
    full reserved amount (the database refuses more, correctly), the shortfall
    -- the part of the reported cost the ledger structurally cannot hold -- is
    audited and alerted, and **every scope the reservation touched is halted**
    before this is raised. Absorbing the difference and reporting success is how
    29,986,075 microusd went uncharged in a red-team run.

    The halt this guarantees is the **scope's**, and it is durable: it lives in
    ``audit_log``, :func:`reserve` refuses a halted scope with
    :class:`BudgetScopeHalted` from then on, and only ops lifts it
    (:func:`resume_scope`). Whether the *run* also ends is the caller's, exactly
    as it is for :class:`BudgetCapExceeded`: ``python -m recon.incidents`` exits
    ``EXIT_REFUSED``, while :func:`recon.llm.generate_rationale` reports
    ``status="overspend"`` and the reconcile run carries on -- meeting
    :class:`BudgetScopeHalted` on its very next reservation, which is why the
    halt was moved into the ledger and out of a return value nobody read.
    """

    def __init__(self, settlement: Settlement) -> None:
        super().__init__(
            f"settlement for {settlement.idempotency_key!r} reported "
            f"{settlement.reported_microusd} microusd against a reservation of "
            f"{settlement.reserve_microusd}: {settlement.shortfall_microusd} microusd "
            "cannot be charged to the ledger. Every scope this reservation touched "
            "is HALTED and will refuse further reservations until ops reconciles "
            "the ledger and calls recon.budget.resume_scope."
        )
        self.settlement = settlement
        self.idempotency_key = settlement.idempotency_key
        self.shortfall_microusd = settlement.shortfall_microusd


class BudgetScopeHalted(BudgetError):
    """The scope overspent and refuses every further reservation.

    Not a cap hit and deliberately not spelled like one: the cap is intact, the
    *arithmetic* is not. A scope reaches this state when a settlement reported
    more than its reservation could hold, which means the ledger is known to
    under-count real spend -- so continuing to reserve against it is reserving
    against a number that is wrong in the dangerous direction.

    Durable, because it lives in ``audit_log`` and not in a process. Lifting it
    is an ops action (:func:`resume_scope`) after the ledger is reconciled.
    """

    def __init__(self, scope: str, detail: str) -> None:
        super().__init__(
            f"budget scope {scope!r} is HALTED after an overspend: {detail}. "
            "No further reservation will be granted on this scope until ops "
            "reconciles the ledger and calls recon.budget.resume_scope."
        )
        self.scope = scope
        self.detail = detail


class DegenerateUsage(BudgetError):
    """A usage block that is not evidence of anything.

    Absent, zeroed, or reporting no output tokens for a call that returned text.
    ``cost_microusd(model, Usage())`` is 0 and a settlement used to accept it, so
    100 successful, text-returning, billed calls were charged nothing. A provider
    that bills you and reports no usage has told you the actual is UNKNOWN, and
    unknown charges the full reservation -- see :class:`OutcomeUnknown`.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(
            f"provider-reported usage is not evidence of a cost: {reason}. "
            "Settle this call as OutcomeUnknown, which charges the full reservation."
        )
        self.reason = reason


class RealDailyScopeRefused(BudgetError):
    """A test process tried to reserve against the real ``daily`` scope.

    Two reclaimed reservations from an earlier run were found sitting on the
    production daily scope because a fixture cleaned up by scope and never
    provisioned ``daily``. A test that charges the day's real budget both spends
    it and makes its own result depend on whatever else ran today.
    """

    def __init__(self, scopes: Sequence[str]) -> None:
        super().__init__(
            f"refusing to reserve on the real {DAILY_SCOPE!r} scope from a test "
            f"process (resolved scopes: {list(scopes)}). Every reservation carries "
            "the daily cap and no caller can drop it, so a test that needs one "
            f"points {DAILY_SCOPE_ENV} at its own throwaway ledger row."
        )
        self.scopes = tuple(scopes)


class SettlementRefused(BudgetError):
    """The database refused a settlement with ``KS007``.

    Double settle, a settle of a reclaimed reservation, or an actual above the
    reserved amount. Every one of those refusals is the trigger's, not this
    module's -- the message it carries is the trigger's own diagnostic.
    """

    def __init__(self, idempotency_key: str, detail: str) -> None:
        super().__init__(f"settlement refused for {idempotency_key!r}: {detail}")
        self.idempotency_key = idempotency_key
        self.detail = detail
        self.sqlstate = KS_RESERVATION_LIFECYCLE


# ===========================================================================
# the committed price table
# ===========================================================================
@dataclass(frozen=True)
class ModelPrice:
    """Per-token rates for one model, in microusd, as exact decimals."""

    model: str
    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal

    def rate(self, field: str) -> Decimal:
        return getattr(self, field)  # type: ignore[no-any-return]


@dataclass(frozen=True)
class PriceTable:
    """The parsed ``prices.yaml``. Immutable, versioned, fails loud."""

    version: int
    units: str
    models: Mapping[str, ModelPrice]

    def price(self, model: str) -> ModelPrice:
        """Rates for ``model``, or :class:`UnknownModelError`.

        There is no default branch on purpose. An unpriced model must stop the
        call, not be waved through at zero cost.
        """
        try:
            return self.models[model]
        except KeyError:
            known = ", ".join(sorted(self.models))
            raise UnknownModelError(
                f"model {model!r} is not in the committed price table (version "
                f"{self.version}); known models: {known}. Add the rate to "
                f"{_PRICES_FILENAME} before calling it -- an unpriced model would "
                "reserve nothing and spend without a ceiling."
            ) from None


def _repo_root() -> Path:
    """``…/keystone`` -- this file is ``…/keystone/service/recon/budget.py``."""
    return Path(__file__).resolve().parents[2]


def _decimal(raw: Any, *, model: str, field: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise UnknownModelError(
            f"price {field!r} for model {model!r} is not a number: {raw!r}"
        ) from exc
    if value < 0:
        raise UnknownModelError(f"price {field!r} for model {model!r} is negative: {raw!r}")
    return value


def load_price_table(path: str | Path | None = None) -> PriceTable:
    """Parse the committed price table. ``path`` defaults to ``<repo>/prices.yaml``."""
    resolved = Path(path) if path is not None else _repo_root() / _PRICES_FILENAME
    if not resolved.is_file():
        raise UnknownModelError(
            f"the committed price table is missing at {resolved}. Cost is computed "
            "from it, never estimated, so there is no usable fallback."
        )
    raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    models_raw = raw.get("models") or {}
    if not isinstance(models_raw, dict) or not models_raw:
        raise UnknownModelError(f"{resolved} declares no models")

    models: dict[str, ModelPrice] = {}
    for model, rates in sorted(models_raw.items()):
        if not isinstance(rates, dict):
            raise UnknownModelError(f"{resolved}: model {model!r} has no rate mapping")
        missing = [field for field in _PRICE_FIELDS if field not in rates]
        if missing:
            raise UnknownModelError(
                f"{resolved}: model {model!r} is missing rate(s) {', '.join(missing)}"
            )
        name = str(model)
        models[name] = ModelPrice(
            model=name,
            **{field: _decimal(rates[field], model=name, field=field) for field in _PRICE_FIELDS},
        )
    return PriceTable(
        version=int(raw.get("version", 0)),
        units=str(raw.get("units", "")),
        models=models,
    )


@lru_cache(maxsize=1)
def price_table() -> PriceTable:
    """Process-wide committed price table, parsed once."""
    return load_price_table()


# ===========================================================================
# usage -> money
# ===========================================================================
@dataclass(frozen=True)
class Usage:
    """Provider-**reported** token usage. Never an estimate.

    Field names mirror the Anthropic ``usage`` object one-for-one so the mapping
    from response to money is a rename and not an interpretation.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
        }


def _ceil_microusd(amount: Decimal) -> int:
    """Round a decimal microusd amount UP to a whole microusd.

    Up, always: a rounded-down fraction on every call is a slow leak past a cap
    that is otherwise exact.
    """
    return math.ceil(amount)


def cost_microusd(model: str, usage: Usage, *, table: PriceTable | None = None) -> int:
    """Cost of ``usage`` for ``model``, from the committed table. Fails on unknowns."""
    price = (table or price_table()).price(model)
    total = (
        price.input * usage.input_tokens
        + price.output * usage.output_tokens
        + price.cache_read * usage.cache_read_tokens
        + price.cache_write * usage.cache_write_tokens
    )
    return _ceil_microusd(total)


# ===========================================================================
# evidence -- the only thing that can lower `budget_ledger.spent_microusd`
# ===========================================================================
def degenerate_usage_reason(usage: Usage | None) -> str | None:
    """Why ``usage`` is not evidence of a cost, or ``None`` when it is.

    A usage block is evidence only when it is **present and non-degenerate**.
    Three ways it is not:

    * it is absent entirely -- the provider returned text and no usage object;
    * it reports no input tokens. Every real call bills for the prompt it was
      given, so zero input is a usage block that was never populated;
    * it reports no output tokens. A call that returned text generated tokens,
      and a provider reporting none of them has not told us what it billed.

    ``cost_microusd(model, Usage())`` is 0 and settlement used to accept it: 100
    successful, text-returning, billed calls charged 0. The absent number is not
    a zero; it is an UNKNOWN, and unknown charges the full reservation.
    """
    if usage is None:
        return "the provider returned no usage block at all"
    if usage.total_input_tokens <= 0:
        return f"input tokens are {usage.total_input_tokens}: a billed call reads its prompt"
    if usage.output_tokens <= 0:
        return "output tokens are 0: a call that returned text generated tokens"
    return None


class PreSendProof(StrEnum):
    """The closed vocabulary of pre-send proofs, mirroring the database enum.

    :class:`NeverSent` is the one evidence value that hands a whole reservation
    back, so it is the one worth forging -- and it used to accept any string at
    all: ``NeverSent("trust me bro")`` released 15,850 microusd. A proof is now a
    member of this enum, produced by *classifying the transport's own exception*
    in :func:`recon.llm._failure_evidence`, and migration 0010's settle trigger
    holds the same closed set as ``budget_never_sent_proof``. A caller cannot
    invent one on either side of the boundary.

    :attr:`OPS_ATTESTED_OUTAGE` is the operator's attestation -- a provider
    incident report, an outbound-connection log showing nothing left the host --
    and the database **refuses it to** ``recon_writer``: the capped party does
    not attest to its own outage.
    """

    CONNECTION_REFUSED = "connection_refused"
    DNS_FAILURE = "dns_failure"
    TLS_HANDSHAKE_FAILED = "tls_handshake_failed"
    CLIENT_REJECTED_REQUEST = "client_rejected_request"
    AUTH_REJECTED_AT_EDGE = "auth_rejected_at_edge"
    OPS_ATTESTED_OUTAGE = "ops_attested_outage"


#: How long after a reservation's ``created_at`` the capped party may still
#: claim its request never left. Mirrors ``NEVER_SENT_WINDOW_SECONDS`` in
#: migration 0010, which is where it is *enforced*; the copy here exists so the
#: ops CLI and the tests can name the same number, and a test asserts they agree.
NEVER_SENT_WINDOW_SECONDS: Final = 60


class SpendEvidence(ABC):
    """Why a reservation is being closed. **It never carries an amount.**

    The previous version asked each value ``charge_microusd(reserve_microusd=...)``
    -- a caller-supplied number the evidence was free to echo back. A red team
    passed ``reserve_microusd=0`` to a settlement documented as fail-closed and
    :class:`OutcomeUnknown` dutifully returned 0, releasing a genuine 15,850
    microusd reservation in full.

    So an evidence value no longer computes money at all. It says which of four
    things happened, and carries the provider-reported usage (when there was
    any) and the pre-send proof (when it claims one). :func:`_close_reservation`
    puts those into the closing ``UPDATE`` and the **database** derives the
    amount from the row being closed, using the ops-owned rates in
    ``budget_model_prices``; migration 0010's settle trigger then re-derives it
    and refuses any statement that names a different number.

    :attr:`kind` is the database's ``budget_settle_evidence`` value, one for one.
    """

    #: Short, stable name for logs, audit rows and the ``budget_settle_evidence``
    #: enum. The two vocabularies are asserted equal in ``tests/budget``.
    kind: ClassVar[str]
    #: Does this value permit charging less than the full reservation?
    releases: ClassVar[bool]

    def settlement_usage(self) -> Usage | None:
        """The provider-reported usage the settlement is priced on, if any.

        ``None`` means "no provider report", which the settle trigger requires
        for the two unpriced outcomes and refuses for the two priced ones.
        """
        return None

    def settlement_proof(self) -> PreSendProof | None:
        """The pre-send proof this evidence claims, if any."""
        return None

    @abstractmethod
    def reason(self) -> str:
        """Human-readable justification, recorded on the audit row."""

    def reported_microusd(
        self, *, model: str | None, table: PriceTable | None = None
    ) -> int | None:
        """What the provider said the call cost, when it said anything.

        Used for the audit row and for :func:`settle_capped`'s overspend
        decision. Deliberately **not** the settled amount: that one is the
        database's.
        """
        return None

    def detail(self, *, reserve_microusd: int, model: str | None) -> dict[str, Any]:
        """The evidence, as it lands in ``audit_log.detail`` (R18)."""
        proof = self.settlement_proof()
        return {
            "evidence": self.kind,
            "evidence_reason": self.reason(),
            "releases": self.releases,
            "settle_proof": proof.value if proof is not None else None,
        }


@dataclass(frozen=True)
class ProviderReportedUsage(SpendEvidence):
    """The provider reported what it billed. The only *priced* evidence.

    Refuses construction on a degenerate usage block, so "the provider returned
    text and reported nothing" cannot be spelled as a cost of zero anywhere in
    the codebase. Callers that meet one settle :class:`OutcomeUnknown` instead --
    and the settle trigger refuses a degenerate usage block too, so the rule
    holds for a statement this module never issued.
    """

    kind: ClassVar[str] = "provider_reported_usage"
    releases: ClassVar[bool] = True

    usage: Usage

    def __post_init__(self) -> None:
        degenerate = degenerate_usage_reason(self.usage)
        if degenerate is not None:
            raise DegenerateUsage(degenerate)

    def settlement_usage(self) -> Usage:
        return self.usage

    def reported_microusd(self, *, model: str | None, table: PriceTable | None = None) -> int:
        if model is None:
            raise ValueError("pricing provider-reported usage needs the model it was billed on")
        return cost_microusd(model, self.usage, table=table)

    def reason(self) -> str:
        return "provider-reported usage, priced from the committed table"


@dataclass(frozen=True)
class NeverSent(SpendEvidence):
    """The request PROVABLY never left this process, so nothing was billed.

    The only evidence that releases a reservation in full, which is why its
    proof is a :class:`PreSendProof` member and not a sentence. The bar is
    symmetric on both sides of the boundary: this type refuses anything that is
    not a member, and migration 0010's settle trigger refuses anything outside
    ``budget_never_sent_proof``, refuses ``ops_attested_outage`` from the capped
    party, and refuses a claim made more than
    :data:`NEVER_SENT_WINDOW_SECONDS` after the reservation was created -- a
    pre-send failure is a connect-time failure, not something discovered later.

    ``detail`` is free text for the audit row and buys nothing: it is never what
    the release is granted on.
    """

    kind: ClassVar[str] = "never_sent"
    releases: ClassVar[bool] = True

    proof: PreSendProof
    detail_text: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.proof, PreSendProof):
            raise TypeError(
                f"NeverSent needs a PreSendProof member, got {self.proof!r}. It releases "
                "the whole reservation, so the justification is a closed vocabulary the "
                "database also holds -- never a string a caller writes. Classify the "
                "transport's own failure, or settle OutcomeUnknown."
            )

    def settlement_proof(self) -> PreSendProof:
        return self.proof

    def reason(self) -> str:
        suffix = f" ({self.detail_text})" if self.detail_text else ""
        return f"the request provably never reached the provider: {self.proof.value}{suffix}"


@dataclass(frozen=True)
class OutcomeUnknown(SpendEvidence):
    """The absence of evidence. Charges the FULL reservation.

    A timeout, a read error, a 5xx after send, a cancelled stream, an
    unrecognised error class, a holder whose lease expired, or a call that
    returned text with a usage block that was not evidence. The provider may have
    done the work and will bill for it, so the worst case is charged and nothing
    is released.

    This is a *value*, not a fallback branch: it exists so that "we do not know"
    has to be written down at the call site and travels into the audit row. The
    settle trigger enforces the arithmetic -- ``actual = reserve``, exactly -- so
    it is no longer possible to spell this outcome as a refund.
    """

    kind: ClassVar[str] = "outcome_unknown"
    releases: ClassVar[bool] = False

    why: str

    def reason(self) -> str:
        return f"the outcome of a paid call is unknown: {self.why}"


@dataclass(frozen=True)
class CostExceededReservation(SpendEvidence):
    """The provider reported MORE than the reservation could hold.

    The outcome is known; the ledger is what cannot hold it. ``actual <=
    reserve`` is a trigger, and settling at the reported figure is refused
    outright -- which would leave the row ``open`` and the whole worst case
    charged while under-reporting a call that cost more than expected. So this
    charges the reservation exactly, releases nothing, and carries the reported
    figure into the audit row and the alert so the shortfall is recorded rather
    than dropped. :func:`settle_capped` is its only constructor, and it halts the
    scope immediately afterwards.

    The database agrees independently: for this evidence the settle trigger
    requires ``actual = reserve`` *and* that the recorded usage genuinely
    exceeded what the reservation was sized for.
    """

    kind: ClassVar[str] = "cost_exceeded_reservation"
    releases: ClassVar[bool] = False

    reported_cost_microusd: int
    usage: Usage = field(default_factory=Usage)

    def settlement_usage(self) -> Usage:
        return self.usage

    def reported_microusd(self, *, model: str | None, table: PriceTable | None = None) -> int:
        return self.reported_cost_microusd

    def reason(self) -> str:
        return (
            f"the provider reported {self.reported_cost_microusd} microusd, more than "
            "the reservation could hold; the ledger under-counts this call"
        )


def worst_case_input_tokens(prompt: str, *, framing: int = FRAMING_TOKEN_OVERHEAD) -> int:
    """A genuine **upper bound** on the prompt's token count.

    One token per UTF-8 *byte*, plus framing. A byte-level BPE tokenizer merges
    bytes into tokens and never splits one, so it cannot emit more tokens than
    the input has bytes -- which makes this a hard ceiling rather than the usual
    "≈4 characters per token" guess. It over-estimates by roughly 4x, and that
    is the correct direction: DESIGN pins *"size demo caps against worst-case
    reservations, not expected spend"*, and a reservation that the settlement
    can exceed would be refused by the database (``actual <= reserve``).
    """
    return len(prompt.encode("utf-8")) + framing


def worst_case_microusd(
    model: str,
    *,
    max_output_tokens: int,
    max_input_tokens: int,
    table: PriceTable | None = None,
) -> int:
    """The amount to reserve BEFORE a call: the most it could possibly cost.

    Input is priced at the **cache-write** rate (1.25x input) rather than the
    input rate, because a prompt-cached call bills its first pass at that rate
    and the reservation must bound the worst case, not the common one. Output is
    priced for the full ``max_tokens`` -- the provider may return every one of
    them, and thinking tokens are billed as output.
    """
    if max_output_tokens < 0 or max_input_tokens < 0:
        raise ValueError("token bounds must be non-negative")
    price = (table or price_table()).price(model)
    dearest_input = max(price.input, price.cache_read, price.cache_write)
    return _ceil_microusd(dearest_input * max_input_tokens + price.output * max_output_tokens)


def cap_microusd_from_env(env_var: str, default_usd: str) -> int:
    """Integer microusd cap from ``env_var``, mirroring migration 0005 exactly.

    Same parse, same fallback, same ``Decimal``: the migration provisions the
    seeded scopes and this provisions per-run scopes, and the two must not drift.
    """
    raw = (os.environ.get(env_var) or "").strip() or default_usd
    try:
        usd = Decimal(raw)
    except (InvalidOperation, ValueError):
        usd = Decimal(default_usd)
    if usd < 0:
        usd = Decimal(default_usd)
    return int((usd * MICROUSD_PER_USD).to_integral_value())


# ===========================================================================
# the stubbed alert (R17: "at cap -> stop + log + alert (stubbed)")
# ===========================================================================
AlertSink = Callable[[Mapping[str, Any]], None]

_ALERT_SINKS: list[AlertSink] = []


def register_alert_sink(sink: AlertSink) -> None:
    """Register a stubbed alert receiver (pager, webhook, test recorder)."""
    _ALERT_SINKS.append(sink)


def unregister_alert_sink(sink: AlertSink) -> None:
    """Remove a previously registered sink; silent if it is not registered."""
    if sink in _ALERT_SINKS:
        _ALERT_SINKS.remove(sink)


def fire_alert(event: str, payload: Mapping[str, Any]) -> int:
    """Fire the stubbed alert. Returns how many sinks received it.

    Always logs, sinks or not: an alert nobody subscribed to must still leave a
    trace. A sink that raises is logged and skipped -- a broken pager must not
    turn "the cap held" into "the process died".
    """
    body = {"event": event, **dict(payload)}
    # `event` is structlog's own name for the message, so the alert's name is
    # bound as `alert=` rather than shadowing it.
    log.error("budget.alert", alert=event, **dict(payload))
    delivered = 0
    for sink in tuple(_ALERT_SINKS):
        try:
            sink(body)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("budget.alert_sink_failed", sink=repr(sink), error=str(exc))
        else:
            delivered += 1
    return delivered


# ===========================================================================
# ledger provisioning (ops principal only)
# ===========================================================================
@lru_cache(maxsize=4)
def _ops_engine_for(dsn: str) -> Any:
    """Cached engine for an explicit ops DSN, psycopg-driven like every other."""
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    url = make_url(dsn)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    return create_engine(url, pool_pre_ping=True, future=True)


def ops_engine() -> Any:
    """The engine for operations the **capped party may not perform**.

    Provisioning a ledger row, reading it back, lifting an overspend halt and
    sweeping dead leases are all ops actions: ``recon_writer`` holds no INSERT
    and no UPDATE on ``budget_ledger`` at all (migration 0005), and closing
    somebody else's reservation is a decision the party being capped does not
    get to make.

    Until now these ran on ``DATABASE_URL``, which meant the only way for the web
    service to provision its run's scope was for ``DATABASE_URL`` to name the
    database **owner** -- and ``infra/render.yaml`` duly handed the web service
    the owner's connection string, which no role-keyed trigger in this project
    binds. Six red-team rounds of boundary went out of the window on one line of
    deployment configuration.

    So the two credentials are now separate. ``DATABASE_URL`` is what the process
    serves as -- ``recon_writer`` on the web service -- and
    :data:`OPS_DATABASE_URL_ENV` names the ops principal for the handful of calls
    below. When it is unset this falls back to ``DATABASE_URL``, which is exactly
    right for the sweeper cron and for local development, where the configured
    principal *is* ops.
    """
    dsn = (os.environ.get(OPS_DATABASE_URL_ENV) or "").strip()
    if not dsn:
        return get_engine()
    return _ops_engine_for(dsn)


def run_scope(run_id: str) -> str:
    """The per-run ledger scope name for ``run_id`` (DESIGN: ``run:<id>``)."""
    if not run_id:
        raise ValueError("run_id must be a non-empty string")
    return f"run:{run_id}"


def provision_scope(scope: str, cap_microusd: int) -> bool:
    """Create ledger row ``scope`` with ``cap_microusd`` if it does not exist.

    Runs as the principal in ``DATABASE_URL`` -- **ops**, not the capped party.
    ``recon_writer`` holds no INSERT on ``budget_ledger`` at all, which is what
    closes "insert a brand new scope with a cap of my choosing"; this function
    is therefore not callable by the code it caps, by construction.

    ``ON CONFLICT DO NOTHING``: an existing cap is never widened here. Raising a
    cap is a deliberate ops action, not a side effect of starting a run.

    Note on the daily cap: it lives in one row **per UTC day**
    (``daily:<YYYY-MM-DD>``, :func:`daily_scope_for`), so rolling the day means
    creating the next day's row -- :func:`roll_daily_scope`, which is this
    function under a name that says what it opens. Nothing in the schema does it
    at midnight and nothing can: ``spent_microusd`` is writable by nobody, so a
    day is rolled by naming a new row and never by resetting a counter. It is an
    ops action against this function's principal; the capped party structurally
    cannot perform it.

    Which is why the day's row opens **itself** on first use rather than waiting
    for an operator: :func:`_open_todays_daily_scope` reaches this function, on
    this principal, for exactly one computed scope name. Nothing about the
    boundary moves -- the caller of ``reserve`` still cannot reach it, and cannot
    name a scope or a cap if it did.
    """
    if cap_microusd < 0:
        raise ValueError("a cap cannot be negative")
    with ops_engine().begin() as conn:
        result = conn.execute(
            text(
                "INSERT INTO budget_ledger (scope, cap_microusd, spent_microusd) "
                "VALUES (:scope, :cap, 0) ON CONFLICT (scope) DO NOTHING RETURNING scope"
            ),
            {"scope": scope, "cap": cap_microusd},
        )
        return result.scalar() is not None


def provision_run_scope(run_id: str, cap_microusd: int | None = None) -> bool:
    """Provision ``run:<run_id>``, defaulting its cap to ``PER_RUN_CAP_USD``."""
    cap = (
        cap_microusd
        if cap_microusd is not None
        else cap_microusd_from_env("PER_RUN_CAP_USD", "1.00")
    )
    return provision_scope(run_scope(run_id), cap)


def roll_daily_scope(
    day: date | None = None, cap_microusd: int | None = None
) -> tuple[str, int, bool]:
    """Open the hard daily cap's ledger row for ``day``. Returns ``(scope, cap, created)``.

    **This is what makes the daily cap daily.** R17's cap is a *day's* budget;
    the scope that carried it was a fixed string that nothing rolled, so it was a
    lifetime budget that ~88 hand runs exhausted for good.

    Two callers, one of which is not a person. :func:`_open_todays_daily_scope`
    calls this the first time a reservation finds today's row missing, which is
    what lets the deployment serve traffic on a day nobody opened; ``python -m
    recon.budget roll`` calls it when an operator wants a day opened *ahead* of
    time, at a stated cap, or wants to re-open one. The second is an ops
    convenience now, not something the service waits for.

    A day is rolled by **naming the next row**, not by resetting a counter --
    ``spent_microusd`` is writable by nobody, and an ops principal that zeroed it
    would be re-inventing the red-team move migration 0005 deleted the column's
    write grants to stop. Yesterday's row keeps yesterday's reservations and
    yesterday's spend, permanently and legibly.

    Idempotent, through :func:`provision_scope`'s ``ON CONFLICT DO NOTHING``: an
    operator running it twice, a retry, or fifty concurrent requests all opening
    the day at once do **not** widen a cap or clear a day's spend. ``created``
    says which happened, and the returned cap is the row's *actual* cap read back
    -- so a second run reports the cap that is really in force rather than the
    one it would have set.

    The default cap is :data:`DAILY_CAP_USD_ENV` parsed exactly as migration 0005
    parses it, so a rolled day and a freshly migrated database agree.
    """
    scope = daily_scope_for(day if day is not None else utc_today())
    cap = (
        cap_microusd
        if cap_microusd is not None
        else cap_microusd_from_env(DAILY_CAP_USD_ENV, DEFAULT_DAILY_CAP_USD)
    )
    created = provision_scope(scope, cap)
    row = ledger_row(scope)
    if row is None:  # pragma: no cover - the row was just provisioned or existed
        raise BudgetError(
            f"the daily scope {scope!r} is absent immediately after provisioning it; "
            "the ops principal's INSERT was rolled back or another writer removed the row"
        )
    log.info(
        "budget.daily_scope_rolled",
        scope=scope,
        cap_microusd=row.cap_microusd,
        spent_microusd=row.spent_microusd,
        # `outcome` and not `created`: every key this package logs has to be on
        # the committed vocabulary in `recon.privacy`, or default-deny emits it
        # as an opaque token and the ops line stops being readable
        # (`tests/privacy/test_logging_installed.py`). Widening that allow-list
        # is another ticket's file; naming the key from it is free.
        outcome="opened" if created else "already open",
    )
    return scope, row.cap_microusd, created


@dataclass(frozen=True)
class LedgerRow:
    """A ``budget_ledger`` row as read back."""

    scope: str
    cap_microusd: int
    spent_microusd: int

    @property
    def remaining_microusd(self) -> int:
        return self.cap_microusd - self.spent_microusd


def ledger_row(scope: str) -> LedgerRow | None:
    """Read one ledger row (ops connection). Returns ``None`` if absent."""
    with ops_engine().connect() as conn:
        row = conn.execute(
            text("SELECT scope, cap_microusd, spent_microusd FROM budget_ledger WHERE scope = :s"),
            {"s": scope},
        ).one_or_none()
    return None if row is None else LedgerRow(row.scope, row.cap_microusd, row.spent_microusd)


def _open_todays_daily_scope(scope: str) -> bool:
    """Open **today's own** daily row on demand. Says whether it now exists.

    This is what makes the date-keyed daily cap *deployable*. The row a running
    deployment has is the row the previous code asked for; the moment the scope
    became ``daily:<YYYY-MM-DD>`` the name changed under it, so without this the
    first request after a deploy -- and every request after that, on a service
    reconciling thousands of conflicts an hour -- is refused
    :class:`LedgerScopeMissing` until a human adds a cron and runs it once by
    hand. Waiting for that is a live outage with a scheduled fix.

    Every property that made ledger provisioning an ops action survives, because
    this does not add a new way to provision one -- it calls the same
    :func:`roll_daily_scope` the cron does, on the same principal:

    * **the name is computed, never accepted.** The argument is checked for
      equality against ``daily_scope_for(utc_today())`` and the function returns
      ``False`` for anything else -- yesterday's row, a ``run:`` scope, and in
      particular whatever :data:`DAILY_SCOPE_ENV` names. That last one is
      load-bearing: :func:`daily_scope` promises that redirecting the mandated
      cap "cannot buy budget that was not already provisioned", and following the
      *resolved* scope here instead of today's own row would make a stand-in name
      mint a fresh row with a fresh day's cap;
    * **the cap is the deployment's**, :data:`DAILY_CAP_USD_ENV` parsed exactly
      as migration 0005 parses it. No caller names it;
    * **an existing day is untouched.** :func:`provision_scope` is ``ON CONFLICT
      DO NOTHING``, so the concurrent requests that all take this path on a cold
      morning open one row between them, and none of them widens a cap or clears
      a spend;
    * **the grant is still the boundary.** The INSERT runs on
      :func:`ops_engine`, and ``recon_writer`` holds no INSERT on
      ``budget_ledger`` at all (migration 0005). A process configured as the
      capped party gets a permission error here, which is caught, logged and
      answered ``False`` -- so the caller raises its original
      :class:`LedgerScopeMissing` rather than proceeding as though a row existed.

    Any failure is swallowed into ``False`` for that last reason: the fallback is
    the refusal that was already on its way, and losing it behind a secondary
    exception would replace an honest "nobody opened today" with whatever the
    ops connection happened to say.
    """
    day = utc_today()
    if scope != daily_scope_for(day):
        return False
    try:
        opened, cap_microusd, created = roll_daily_scope(day)
    except Exception as exc:
        log.error(
            "budget.daily_scope_open_refused",
            scope=scope,
            outcome="refused",
            detail=(
                f"{type(exc).__name__}: {exc}. The day's ledger row could not be "
                "opened by the ops principal, so the reservation that needed it "
                "stays refused."
            ),
        )
        return False
    log.info(
        "budget.daily_scope_opened_on_demand",
        scope=opened,
        cap_microusd=cap_microusd,
        outcome="opened" if created else "already open",
    )
    return True


# ===========================================================================
# reserve
# ===========================================================================
def _utc_now() -> datetime:
    """The wall clock, in UTC. The **one** place the daily cap reads the time.

    A function rather than an inline ``datetime.now(tz=UTC)`` so a test can prove
    that the scope rolls across a UTC date boundary by moving the clock instead
    of by waiting for one -- ``tests/budget/test_daily_roll.py`` does exactly
    that. Nothing graded depends on this value: the ledger scope name is not part
    of any dataset, conflict set or confidence vector.
    """
    return datetime.now(tz=UTC)


def utc_today(now: datetime | None = None) -> date:
    """The UTC calendar day of ``now`` (default: the wall clock).

    **UTC, deliberately, and a naive datetime is refused.** A "daily" cap whose
    day depends on the host's timezone rolls at a different instant on every
    machine that runs the cron, which means two rows are live at once for one
    stretch of every day and neither of them is the day's budget. The refusal is
    the guard: a naive value is a clock whose zone nobody stated.
    """
    moment = now if now is not None else _utc_now()
    if moment.tzinfo is None:
        raise ValueError(
            "a naive datetime cannot name a UTC day: the daily cap rolls at "
            "00:00 UTC, so the moment it is asked about must say which zone it is in"
        )
    return moment.astimezone(UTC).date()


def daily_scope_for(day: date) -> str:
    """The ledger row that carries the hard daily cap for UTC ``day``.

    ``daily:2026-08-25``. One row per day is how the cap rolls: ``spent_microusd``
    is writable by nobody, so a new day is a new row and never a reset counter.
    Pure and total -- the row this names is opened by
    :func:`_open_todays_daily_scope` on first use or by ``python -m recon.budget
    roll`` ahead of time, and :func:`daily_scope` names the row for today.
    """
    if isinstance(day, datetime) or not isinstance(day, date):
        # `datetime` is a subclass of `date`, and its `isoformat()` carries a
        # time -- so accepting one would name `daily:2026-08-25T09:00:00+00:00`,
        # a row nobody provisions, once an hour. Convert through `utc_today`,
        # which is the function that has to decide the zone.
        raise TypeError(
            "a daily scope is named after a calendar day (datetime.date), not "
            f"{type(day).__name__}; call utc_today(moment) to get one"
        )
    return f"{DAILY_SCOPE}{DAILY_SCOPE_SEPARATOR}{day.isoformat()}"


def daily_scope() -> str:
    """The ledger scope that carries R17's hard daily cap. **Not a parameter.**

    The daily cap used to be droppable: ``reserve(scopes=...)`` took a scope set
    and :class:`IsolatedScopes` -- a type guarded by walking the call stack and
    matching ``frame.f_code.co_filename`` by path *suffix* -- was the way to give
    it up. A red team built one out of ``exec(compile(src,
    "/anywhere/service/tests/x.py", "exec"))`` with no file edits at all, because
    stack inspection is not a security boundary and never was.

    So the parameter is gone. :func:`reserve` reserves on this scope and the
    caller's run scope, always, and **no product path can express anything
    else**: neither :func:`reserve` nor :func:`recon.llm.generate_rationale`
    takes a scope argument any more.

    What remains is *which ledger row* the daily cap lives in, and that is
    deployment configuration -- :data:`DAILY_SCOPE_ENV`, in the same class as
    ``DATABASE_URL`` -- not a caller's argument. It exists so the verification
    harness and the test suite can point the mandated cap at a throwaway,
    ops-provisioned row instead of spending the day's real budget to prove the
    day's budget works. Production leaves it unset; ``infra/render.yaml`` does
    not define it. An override is logged loudly every time it is honoured, and
    whatever it names is still an ops-provisioned row with an ops-set cap, so
    redirecting it cannot buy budget that was not already provisioned.

    **The default rolls.** With no override this is ``daily:<today in UTC>`` --
    one row per day, opened on first use by :func:`_open_todays_daily_scope` --
    and not the fixed string ``daily``, which nothing rolled and which therefore
    capped the deployment's *lifetime* rather than its day. Yesterday's spend sits
    on yesterday's row and cannot refuse today's call; today's cap cannot be
    reached by spending it yesterday.

    On-demand opening applies to **that computed name only**, never to an
    override. Both halves of the promise above depend on it: a stand-in that
    minted its own ledger row on first use would be exactly the "buy budget
    nobody provisioned" this paragraph says it is not.

    Both spellings of the production row are refused to a test process: the bare
    family name, and today's actual row. Neither can be smuggled in through the
    override.
    """
    override = (os.environ.get(DAILY_SCOPE_ENV) or "").strip()
    today = daily_scope_for(utc_today())
    if not override or override in (DAILY_SCOPE, today):
        if _in_test_process():
            raise RealDailyScopeRefused((DAILY_SCOPE, today))
        return today
    log.warning(
        "budget.daily_scope_overridden",
        scope=override,
        env_var=DAILY_SCOPE_ENV,
        detail=(
            "the mandated daily cap is being charged to a stand-in ledger row; this "
            "is a harness/test configuration and must not appear in a deployment"
        ),
    )
    return override


@dataclass(frozen=True)
class Reservation:
    """One worst-case reservation, held across every scope R17 mandates."""

    idempotency_key: str
    model: str
    reserve_microusd: int
    scopes: tuple[str, ...]
    #: `idempotency_key` per scope; `budget_reservations.idempotency_key` is
    #: UNIQUE across the whole table, so one logical call needs one key per row.
    scope_keys: Mapping[str, str]
    max_output_tokens: int
    max_input_tokens: int
    price_version: int
    #: This holder's own estimate of when the call stops being in flight. Local
    #: and advisory: the SWEEPER reads `created_at + lease_seconds`, both of
    #: which the database owns and the settle trigger freezes.
    lease_expires_at: datetime
    #: The lease DURATION stamped into `scope_keys`. A duration and not a
    #: deadline, so replaying the same logical call collides on the UNIQUE
    #: constraint however much later it arrives (see :func:`scope_key`).
    lease_seconds: int = DEFAULT_LEASE_SECONDS
    #: True when the key was already present: the reservation was NOT charged
    #: again and the call it covers must not be repeated. See :func:`reserve`.
    replayed: bool = False


def scope_key(idempotency_key: str, scope: str, *, lease_seconds: int) -> str:
    """The per-scope reservation key: ``<key>#<scope>#lease<seconds>``.

    **No clock appears in this string.** The previous version baked
    ``int(lease_expires_at.timestamp())`` into it, so the same logical
    idempotency key replayed 1.2 seconds later produced a *different* UNIQUE key,
    missed the constraint, and made the paid call a second time. An idempotency
    key must be keyed on the caller's identity for the work; a lease *duration*
    is part of that identity (it is the provider's own timeout plus a fixed
    margin), a wall-clock deadline never is.

    The sweeper reconstructs the deadline as ``created_at + lease_seconds``. Both
    halves are immutable after insert and ``created_at`` is the database's clock,
    so a holder can neither extend its lease nor backdate its birth.
    """
    return f"{idempotency_key}#{scope}{_LEASE_MARK}{int(lease_seconds)}"


def lease_seconds_from_key(key: str) -> int | None:
    """The lease **duration** encoded in a reservation key, or ``None``.

    ``None`` means "this row carries no liveness signal at all", which the
    sweeper treats as *do not touch* rather than as *expired* -- fail closed.
    """
    marker = key.rfind(_LEASE_MARK)
    if marker < 0:
        return None
    try:
        seconds = int(key[marker + len(_LEASE_MARK) :])
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def lease_deadline(key: str, created_at: datetime) -> datetime | None:
    """When the holder of ``key``, born at ``created_at``, stops claiming liveness."""
    seconds = lease_seconds_from_key(key)
    if seconds is None:
        return None
    born = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=UTC)
    return born + timedelta(seconds=seconds)


#: The reserving statement, carrying the **price binding** migration 0010 needs.
#: ``model`` plus the token bounds are what let the database re-derive both the
#: worst case (here) and the settled amount (in :data:`_CLOSE_RESERVATION`) from
#: rates the capped party cannot write. The reserve trigger refuses this INSERT
#: unless ``reserve_microusd`` is exactly the worst case those rates give for
#: those bounds, so a caller cannot deflate the rates it will later settle
#: against: doing so deflates the reservation it is trying to keep.
_INSERT_RESERVATION = text(
    "INSERT INTO budget_reservations "
    "(scope, idempotency_key, reserve_microusd, model, max_input_tokens, max_output_tokens) "
    "VALUES (:scope, :key, :reserve, :model, :max_input_tokens, :max_output_tokens) "
    "RETURNING id"
)


def _sqlstate(error: BaseException) -> str | None:
    return getattr(getattr(error, "orig", None), "sqlstate", None)


#: The key ``audit_log.subject`` is redacted under. Named once because the WRITE
#: and every LOOKUP have to use the same one; see :func:`_audit_subject`.
_SUBJECT_KEY: Final = "subject"


def _audit(
    conn: Connection,
    *,
    action: str,
    subject: str | None,
    body: Any,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_microusd: int | None = None,
    actor: str = AUDIT_ACTOR,
) -> None:
    """Append one ``audit_log`` row through the **redacting chokepoint**.

    This used to bind ``actor``, ``action`` and ``subject`` straight into its own
    ``INSERT`` and redact only ``detail`` -- so the one field of a budget audit
    row that carries a caller-chosen string (``subject``: a ledger scope, or a
    reservation's idempotency key, both of which are built from a ``run_id`` this
    module never validated) went to the database exactly as it arrived.
    :func:`recon.logging.insert_audit_row` binds every column through the
    committed redactor instead, which is why this module no longer issues that
    statement itself -- the chokepoint owns the SQL as well as the redaction, so
    there is no second column list here to drift out of step with it.

    ``actor``/``action``/``subject`` are on the redactor's allow-list, so they are
    **scrubbed rather than tokenised**: an embedded email, student number, ISO
    date or ``key=value`` pair is removed and the reference itself survives. That
    is required, not incidental -- migration 0004's ``KS003`` matches ``actor``
    against ``^system:``, and a tokenised subject would make ``audit_log``
    unqueryable for R15/R18. Anything a lookup compares against ``subject`` must
    go through :func:`_audit_subject`.
    """
    insert_audit_row(
        conn,
        actor=actor,
        action=action,
        subject=subject,
        body=body,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_microusd=cost_microusd,
    )


def _audit_subject(scope: str) -> str:
    """What ``scope`` looks like in ``audit_log.subject`` once it has been written.

    The redaction has to be applied on **both** sides or the halt lookup silently
    stops finding halts: :func:`_halted_scopes` asks "is there a
    ``budget_scope_halted`` row for this scope?", and it is asking about a row
    whose ``subject`` went through :func:`recon.logging.audit_row`.

    Every scope name this module builds -- ``daily:2026-08-25``, ``run:<id>`` --
    is returned unchanged by the redactor today, so this is the identity for
    them. It is here for the scope name that is *not*: a ``run_id`` is an
    arbitrary caller-supplied string, and one carrying something the scrubber
    recognises would be stored scrubbed and, without this, looked up raw. The
    same function on both sides makes the two agree by construction rather than
    by luck.
    """
    from recon.privacy import redact  # local: mirrors this module's other privacy imports

    redacted = redact(scope, key=_SUBJECT_KEY)
    return redacted if isinstance(redacted, str) else scope


def record_cap_hit(
    *,
    scope: str,
    idempotency_key: str,
    reserve_microusd: int,
    model: str,
    detail: str,
) -> None:
    """Write the ``cap_hit`` audit row and fire the stubbed alert.

    Its own connection and its own transaction, because the transaction that hit
    the cap is already aborted -- ``KS006`` rolls the reservation back, and an
    audit row written inside it would roll back with it. The evidence that the
    cap fired must outlive the transaction the cap killed.
    """
    with role_connection(ROLE_RECON_WRITER) as conn:
        _audit(
            conn,
            action=AUDIT_CAP_HIT,
            subject=scope,
            body={
                "scope": scope,
                "idempotency_key": idempotency_key,
                "reserve_microusd": reserve_microusd,
                "model": model,
                "sqlstate": KS_CAP_EXCEEDED,
                "detail": detail,
            },
        )
    fire_alert(
        ALERT_CAP_HIT,
        {
            "scope": scope,
            "idempotency_key": idempotency_key,
            "reserve_microusd": reserve_microusd,
            "model": model,
            "sqlstate": KS_CAP_EXCEEDED,
        },
    )


_LATEST_SCOPE_STATE = text(
    "SELECT subject, action FROM ("
    "  SELECT DISTINCT ON (subject) subject, action FROM audit_log "
    "  WHERE action = ANY(:actions) AND subject = ANY(:scopes) "
    "  ORDER BY subject, id DESC"
    ") latest WHERE action = :halted"
)


def _halted_scopes(conn: Connection, scopes: Sequence[str]) -> tuple[str, ...]:
    """Which of ``scopes`` are currently halted, newest marker wins.

    One indexed read of the audit log the dashboard already reconciles against
    (R18), rather than a second piece of state that can disagree with it. A halt
    is durable across processes and deploys, which is the point: an in-memory
    flag would be cleared by the next restart of the very run that overspent.

    The scopes are matched **as an audit row stores them** -- through
    :func:`_audit_subject`, the same transform :func:`_audit` applies on the way
    in. A lookup that compared raw names against redacted subjects would answer
    "not halted" for a halted scope, and that is the direction that keeps
    spending. What comes back is the caller's own spelling, mapped back from the
    stored form, so nothing downstream has to know the transform happened.
    """
    stored = {_audit_subject(scope): scope for scope in scopes}
    rows = conn.execute(
        _LATEST_SCOPE_STATE,
        {
            "actions": [AUDIT_SCOPE_HALTED, AUDIT_SCOPE_RESUMED],
            "scopes": list(stored),
            "halted": AUDIT_SCOPE_HALTED,
        },
    ).fetchall()
    return tuple(sorted(stored.get(row.subject, row.subject) for row in rows))


def halt_scope(scope: str, *, reason: str, detail: Mapping[str, Any] | None = None) -> None:
    """Halt ``scope``: every later :func:`reserve` on it is refused.

    Its own connection and its own transaction, exactly as :func:`record_cap_hit`
    is, so the halt survives whatever happens to the transaction that discovered
    the overspend. Written by the capped party on purpose -- the capped party can
    *stop* itself; only ops can start it again (:func:`resume_scope`).
    """
    with role_connection(ROLE_RECON_WRITER) as conn:
        _audit(
            conn,
            action=AUDIT_SCOPE_HALTED,
            subject=scope,
            body={"scope": scope, "reason": reason, **dict(detail or {})},
        )
    log.error("budget.scope_halted_recorded", scope=scope, reason=reason)
    fire_alert(ALERT_SCOPE_HALTED, {"scope": scope, "reason": reason, **dict(detail or {})})


def resume_scope(scope: str, *, reason: str) -> None:
    """Lift a halt. **Ops only** -- it runs on the ops principal's engine.

    The capped party holds no way to write this action's audit row as ops, which
    is what keeps "the ledger under-counts real spend" from being cleared by the
    process that caused it. Ops reconciles the ledger first, then calls this.
    """
    if not reason.strip():
        raise ValueError("resuming a halted scope requires a stated reason")
    with ops_engine().begin() as conn:
        _audit(
            conn,
            action=AUDIT_SCOPE_RESUMED,
            subject=scope,
            body={"scope": scope, "reason": reason},
            actor=AUDIT_ACTOR_OPS,
        )
    log.warning("budget.scope_resumed", scope=scope, reason=reason)


def halted_scopes(scopes: Sequence[str]) -> tuple[str, ...]:
    """Which of ``scopes`` are halted right now (ops read)."""
    with ops_engine().connect() as conn:
        return _halted_scopes(conn, scopes)


def reserve(
    *,
    idempotency_key: str,
    model: str,
    max_output_tokens: int,
    max_input_tokens: int,
    run_id: str,
    table: PriceTable | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> Reservation:
    """Reserve the worst-case cost of one call, on every mandated scope.

    **There is no ``scopes`` parameter.** R17 mandates a per-run cap *and* a hard
    daily cap, and this reserves on both -- :func:`daily_scope` and
    ``run:<run_id>`` -- because a per-run cap alone lets N runs spend N times the
    day's budget. The previous version took a scope set, which meant "without the
    daily cap" was one argument away from every caller and the only thing in the
    way was a stack-walking guard a red team stepped over with
    ``exec(compile(...))``. Applying the mandated scope is now this function's
    job, not a decision a caller can express at all.

    The INSERT carries the model and the token bounds the amount was computed
    from, so the reserve trigger re-derives ``reserve_microusd`` from the
    ops-owned rates and refuses a reservation whose own arithmetic does not hold.

    One transaction, scopes in sorted order. Three outcomes besides success:

    * ``KS006`` from any scope -- the whole transaction is rolled back by
      Postgres (so no scope is charged for a call that will not happen), a
      ``cap_hit`` audit row is written on a fresh connection, the stubbed alert
      fires, and :class:`BudgetCapExceeded` is raised. **The spend stops here and
      nothing retries past it inside this function**: an attempt that wants to
      try anyway has to call this again and meets the same trigger, with the same
      refusal, and pays another ``cap_hit`` row for the privilege. Whether the
      *run* ends is the caller's -- ``recon.incidents`` lets this propagate and
      exits non-zero, ``recon.llm.generate_rationale`` reports ``cap_hit`` and
      the reconcile run continues with ``rationale NULL``. The module docstring's
      `What "stop on cap" actually stops`_ says why the second one is deliberate;
    * ``23505`` -- the key is already present. A replayed idempotency key is an
      **idempotent no-op**, which is the entire point of an idempotency key:
      nothing is charged (the duplicate INSERT aborts the transaction, so no
      scope moved) and a :class:`Reservation` with ``replayed=True`` comes back
      describing the row that already exists. It is deliberately not an
      exception -- :func:`recon.llm.generate_rationale` is documented never to
      raise, and a replay is a normal thing for a retried cron job to do. The
      caller must **not** repeat the paid call: the reservation it names has
      already been spent or is in flight elsewhere;
    * a worst case of zero -- :class:`ZeroReservationRefused`, before any
      statement runs. A reservation that reserves nothing is a call the cap
      cannot see.

    One retry, for one scope, and it is not a retry past a cap
    ---------------------------------------------------------
    :class:`LedgerScopeMissing` for ``daily:<today in UTC>`` is answered by
    opening that day's row on the ops principal and attempting **once** more
    (:func:`_open_todays_daily_scope`); everything else about that refusal is
    unchanged, and every other scope raises it as before. This is what makes the
    date-keyed daily cap survive its own deploy -- see the module docstring's
    `The daily cap is a DAY, and something has to roll it`_ -- and it is not a
    way past the cap: the row it opens starts at zero spend against the
    deployment's own cap, an existing row is never widened, and a ``KS006`` that
    is a real cap hit is not retried at all.
    """
    try:
        return _reserve_once(
            idempotency_key=idempotency_key,
            model=model,
            max_output_tokens=max_output_tokens,
            max_input_tokens=max_input_tokens,
            run_id=run_id,
            table=table,
            lease_seconds=lease_seconds,
            now=now,
        )
    except LedgerScopeMissing as missing:
        if not _open_todays_daily_scope(missing.scope):
            raise
    # Exactly one retry, outside the `except` so the second attempt is not
    # nested in the first one's context. A second `LedgerScopeMissing` -- the row
    # removed again between the two, or the run scope missing as well --
    # propagates, so this cannot loop.
    return _reserve_once(
        idempotency_key=idempotency_key,
        model=model,
        max_output_tokens=max_output_tokens,
        max_input_tokens=max_input_tokens,
        run_id=run_id,
        table=table,
        lease_seconds=lease_seconds,
        now=now,
    )


def _reserve_once(
    *,
    idempotency_key: str,
    model: str,
    max_output_tokens: int,
    max_input_tokens: int,
    run_id: str,
    table: PriceTable | None,
    lease_seconds: int,
    now: datetime | None,
) -> Reservation:
    """One attempt at :func:`reserve`. Every rule that function documents is here.

    Split out only so "open today's ledger row and try again" is a single, once,
    outside-the-transaction retry rather than a flag threaded through the body --
    a private param on :func:`reserve` would read like the ``scopes`` escape
    hatch this module spent three rounds removing.
    """
    resolved = _resolve_scopes(run_id)
    amount = worst_case_microusd(
        model,
        max_output_tokens=max_output_tokens,
        max_input_tokens=max_input_tokens,
        table=table,
    )
    if amount <= 0:
        raise ZeroReservationRefused(
            model,
            f"worst case of max_input_tokens={max_input_tokens}, "
            f"max_output_tokens={max_output_tokens} priced at {amount} microusd",
        )
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive: a reservation is born live")
    lease_seconds = min(lease_seconds, MAX_LEASE_SECONDS)
    lease_expires_at = (now or datetime.now(tz=UTC)) + timedelta(seconds=lease_seconds)
    keys = {
        scope: scope_key(idempotency_key, scope, lease_seconds=lease_seconds) for scope in resolved
    }

    try:
        with role_connection(ROLE_RECON_WRITER) as conn:
            # The overspend halt, checked inside the reserving transaction and
            # before a single INSERT: a scope whose ledger is known to
            # under-count real spend must not grant more budget against it.
            halted = _halted_scopes(conn, resolved)
            if halted:
                scope = halted[0]
                log.error("budget.scope_halted", scope=scope, idempotency_key=idempotency_key)
                raise BudgetScopeHalted(
                    scope,
                    "a settlement on this scope reported more than its reservation "
                    "could hold, so the ledger under-counts real spend",
                )
            for scope in resolved:
                conn.execute(
                    _INSERT_RESERVATION,
                    {
                        "scope": scope,
                        "key": keys[scope],
                        "reserve": amount,
                        "model": model,
                        "max_input_tokens": max_input_tokens,
                        "max_output_tokens": max_output_tokens,
                    },
                )
    except DBAPIError as exc:
        state = _sqlstate(exc)
        if state == SQLSTATE_UNIQUE_VIOLATION:
            return _replayed_reservation(
                idempotency_key=idempotency_key,
                model=model,
                scopes=resolved,
                keys=keys,
                amount=amount,
                max_output_tokens=max_output_tokens,
                max_input_tokens=max_input_tokens,
                price_version=(table or price_table()).version,
                lease_expires_at=lease_expires_at,
                lease_seconds=lease_seconds,
            )
        if state != KS_CAP_EXCEEDED:
            raise
        detail = str(getattr(exc, "orig", exc)).strip()
        refused = _scope_from_message(detail, resolved)
        if _MISSING_LEDGER_ROW in detail:
            # The trigger reuses KS006 for "this scope has no ledger row", which
            # is a *configuration* fault, not a cap hit. Recording it as one
            # would put false `cap_hit` rows in the audit log and page someone
            # about a budget that was never reached -- and R18 has the dashboard
            # reconciling against exactly those rows.
            log.error(
                "budget.scope_not_provisioned",
                scope=refused,
                idempotency_key=idempotency_key,
                sqlstate=state,
            )
            raise LedgerScopeMissing(refused, detail) from exc
        log.warning(
            "budget.cap_hit",
            scope=refused,
            idempotency_key=idempotency_key,
            reserve_microusd=amount,
            model=model,
            sqlstate=state,
        )
        record_cap_hit(
            scope=refused,
            idempotency_key=idempotency_key,
            reserve_microusd=amount,
            model=model,
            detail=detail,
        )
        raise BudgetCapExceeded(refused, amount, detail) from exc

    reservation = Reservation(
        idempotency_key=idempotency_key,
        model=model,
        reserve_microusd=amount,
        scopes=resolved,
        scope_keys=keys,
        max_output_tokens=max_output_tokens,
        max_input_tokens=max_input_tokens,
        price_version=(table or price_table()).version,
        lease_expires_at=lease_expires_at,
        lease_seconds=lease_seconds,
    )
    log.info(
        "budget.reserved",
        idempotency_key=idempotency_key,
        scopes=list(resolved),
        reserve_microusd=amount,
        model=model,
        lease_expires_at=lease_expires_at.isoformat(),
    )
    return reservation


_EXISTING_RESERVATION = text(
    "SELECT idempotency_key, scope, reserve_microusd, state::text AS state "
    "FROM budget_reservations WHERE idempotency_key = ANY(:keys)"
)


def _replayed_reservation(
    *,
    idempotency_key: str,
    model: str,
    scopes: tuple[str, ...],
    keys: Mapping[str, str],
    amount: int,
    max_output_tokens: int,
    max_input_tokens: int,
    price_version: int,
    lease_expires_at: datetime,
    lease_seconds: int,
) -> Reservation:
    """Describe the reservation a replayed key already made. Charges nothing.

    The duplicate INSERT aborted its transaction, so no scope moved -- including
    any scope whose row *did* insert before the collision. The reservation that
    comes back therefore describes rows that already exist; it is a receipt, not
    a new grant, and :attr:`Reservation.replayed` says so.
    """
    with role_connection(ROLE_RECON_WRITER, commit=False) as conn:
        rows = conn.execute(_EXISTING_RESERVATION, {"keys": list(keys.values())}).fetchall()
    existing = {row.idempotency_key: row for row in rows}
    stored = next((row.reserve_microusd for row in rows), amount)
    log.warning(
        "budget.reservation_replayed",
        idempotency_key=idempotency_key,
        scopes=list(scopes),
        model=model,
        reserve_microusd=stored,
        states=sorted({row.state for row in rows}),
        found=len(existing),
        detail=(
            "this idempotency key already reserved; nothing was charged again and "
            "the call it covers must not be repeated"
        ),
    )
    return Reservation(
        idempotency_key=idempotency_key,
        model=model,
        reserve_microusd=stored,
        scopes=scopes,
        scope_keys=dict(keys),
        max_output_tokens=max_output_tokens,
        max_input_tokens=max_input_tokens,
        price_version=price_version,
        lease_expires_at=lease_expires_at,
        lease_seconds=lease_seconds,
        replayed=True,
    )


def _resolve_scopes(run_id: str) -> tuple[str, ...]:
    """Both mandated scopes, always, sorted so the lock order is fixed.

    Sorted for a reason beyond tidiness: every caller takes the ledger row locks
    in the same order, so a burst serialises rather than deadlocking. The set
    collapses to one entry when a harness points :data:`DAILY_SCOPE_ENV` at the
    same row as the run scope, which is the only way a single-scope reservation
    can happen and needs no argument to express.
    """
    if not run_id:
        raise ValueError(
            "reserve() needs a run_id: R17 mandates a per-run cap as well as the daily one"
        )
    return tuple(sorted({daily_scope(), run_scope(run_id)}))


def _in_test_process() -> bool:
    """Is this process running a pytest test right now?

    ``PYTEST_CURRENT_TEST`` is set by pytest around every setup/call/teardown
    phase and by nothing else. It is the one signal available *inside* the
    product that a test is driving it, which is what makes "a test cannot charge
    the real daily budget" a property of the code rather than of a convention
    every future test author has to remember.
    """
    return "PYTEST_CURRENT_TEST" in os.environ


def _scope_from_message(detail: str, candidates: Iterable[str]) -> str:
    """Which scope the trigger refused, from its diagnostic.

    The message names the scope; matching the longest candidate first keeps
    ``run:x`` from being reported as a prefix of ``run:xy``.
    """
    for scope in sorted(candidates, key=len, reverse=True):
        if scope in detail:
            return scope
    return next(iter(candidates), DAILY_SCOPE)


# ===========================================================================
# settle -- THE ONE CHOKEPOINT
# ===========================================================================
@dataclass(frozen=True)
class Settlement:
    """The result of closing one reservation against one piece of evidence."""

    idempotency_key: str
    reserve_microusd: int
    actual_microusd: int
    released_microusd: int
    usage: Usage
    #: True when the provider reported more spend than the reservation could
    #: absorb. The reservation settles at its full reserved amount, the
    #: difference is audited and alerted, the scope is HALTED in the ledger, and
    #: :class:`BudgetOverspend` is raised -- never silently dropped, and never
    #: reported as success. Whether the caller's *run* ends on that exception is
    #: the caller's; the scope's refusal is not (see :class:`BudgetOverspend`).
    overflowed: bool = False
    reported_microusd: int | None = None
    #: :attr:`SpendEvidence.kind` of the value that closed it.
    evidence: str = OutcomeUnknown.kind

    @property
    def shortfall_microusd(self) -> int:
        """Reported cost the ledger structurally cannot hold. Zero when exact."""
        if self.reported_microusd is None:
            return 0
        return max(0, self.reported_microusd - self.actual_microusd)


#: **The one ``UPDATE`` against ``budget_reservations`` in this package**, and
#: therefore the only statement here that can make ``budget_ledger.spent_microusd``
#: go DOWN. Note what it does *not* contain: a bind for the amount. The charge is
#: an expression over the row's own columns and the ops-owned rates in
#: ``budget_model_prices`` -- ``never_sent`` is zero, a priced settlement is what
#: the committed rates say the reported usage cost, and everything else is the
#: reservation in full. A caller supplies the *evidence* and the *usage*; the
#: number is the database's.
#:
#: Counting this statement is a useful smell test and ``tests/budget/
#: test_release_chokepoint.py`` and :func:`recon.suite.burst.release_sites` both
#: still do it. It is **not** the boundary. The boundary is migration 0010's
#: settle trigger, which re-derives the same number and refuses any UPDATE that
#: names a different one -- including the three spellings a red team used to
#: release money past this module entirely (``UPDATE public.budget_reservations``,
#: ``UPDATE "budget_reservations"``, ``UPDATE ONLY budget_reservations``).
_CLOSE_RESERVATION = text(
    "UPDATE budget_reservations SET "
    "  actual_microusd = CASE "
    "    WHEN :evidence = 'never_sent' THEN 0 "
    "    WHEN :evidence = 'provider_reported_usage' THEN ("
    "      SELECT ceil("
    "        p.input_rate * :usage_input + p.output_rate * :usage_output "
    "        + p.cache_read_rate * :usage_cache_read "
    "        + p.cache_write_rate * :usage_cache_write) "
    "      FROM budget_model_prices AS p WHERE p.model = budget_reservations.model) "
    "    ELSE reserve_microusd END, "
    "  state = 'settled', "
    "  settle_evidence = CAST(:evidence AS budget_settle_evidence), "
    "  settle_proof = CAST(:proof AS budget_never_sent_proof), "
    "  usage_input_tokens = :usage_input_recorded, "
    "  usage_output_tokens = :usage_output_recorded, "
    "  usage_cache_read_tokens = :usage_cache_read_recorded, "
    "  usage_cache_write_tokens = :usage_cache_write_recorded "
    "WHERE idempotency_key = :key "
    "RETURNING id, actual_microusd, reserve_microusd"
)


def _close_reservation(
    conn: Connection,
    *,
    keys: Sequence[str],
    evidence: SpendEvidence,
) -> int:
    """Close one reservation's rows against ``evidence``. Returns what was charged.

    Note what this function does not have: **any** way to name an amount. It has
    no ``reserve_microusd`` parameter either -- the previous version did, the
    docstring claimed it did not, and a red team passed ``reserve_microusd=0`` to
    the value documented as fail-closed (:class:`OutcomeUnknown`, "charge the
    full reservation") and released a genuine 15,850 microusd reservation. A
    number a caller supplies is a number a caller can lie about, however
    conservative the code that consumes it looks.

    So the amount is read out of the ROW, inside the closing statement, and the
    caller's contribution is a typed :class:`SpendEvidence` value plus whatever
    the provider reported. The returned figure is what the database actually
    wrote, read back through ``RETURNING`` rather than recomputed here.

    Every refusal is left to migration 0010's settle trigger rather than
    pre-empted, so the test that proves a rule tests the enforcement point:
    settles-exactly-once, ``actual <= reserve``, "an unknown outcome charges the
    full reservation", "a priced settlement equals the committed rates", and
    every rule on a full release are all ``KS007`` from the database.
    """
    if not isinstance(evidence, SpendEvidence):
        raise TypeError(
            f"closing a reservation requires a SpendEvidence value, got "
            f"{type(evidence).__name__}. There is no way to release budget "
            "without stating why the money was not spent."
        )
    usage = evidence.settlement_usage()
    proof = evidence.settlement_proof()
    # The usage is bound twice on purpose: once for the arithmetic (where NULL
    # would poison the CASE) and once for the columns that record what the
    # provider actually said (where NULL is the correct value for an outcome
    # that carries no provider report at all).
    params: dict[str, Any] = {
        "evidence": evidence.kind,
        "proof": proof.value if proof is not None else None,
        "usage_input": usage.input_tokens if usage else 0,
        "usage_output": usage.output_tokens if usage else 0,
        "usage_cache_read": usage.cache_read_tokens if usage else 0,
        "usage_cache_write": usage.cache_write_tokens if usage else 0,
        "usage_input_recorded": usage.input_tokens if usage else None,
        "usage_output_recorded": usage.output_tokens if usage else None,
        "usage_cache_read_recorded": usage.cache_read_tokens if usage else None,
        "usage_cache_write_recorded": usage.cache_write_tokens if usage else None,
    }

    charged: int | None = None
    for key in keys:
        row = conn.execute(_CLOSE_RESERVATION, {**params, "key": key}).one_or_none()
        if row is None:
            raise SettlementRefused(key, "no such reservation row")
        if charged is not None and row.actual_microusd != charged:
            raise SettlementRefused(
                key,
                f"one logical call settled at {charged} on one scope and "
                f"{row.actual_microusd} on another; the scopes of one reservation "
                "must charge the same amount",
            )
        charged = int(row.actual_microusd)
    if charged is None:
        raise SettlementRefused("", "a settlement named no reservation rows")
    return charged


def _usage_of(evidence: SpendEvidence) -> Usage:
    """The usage an evidence value carries, or an empty one. Never a cost."""
    return evidence.settlement_usage() or Usage()


def settle(
    reservation: Reservation,
    evidence: SpendEvidence,
    *,
    table: PriceTable | None = None,
    audit_action: str = AUDIT_LLM_CALL,
    audit_extra: Mapping[str, Any] | None = None,
) -> Settlement:
    """Close a reservation against ``evidence`` and release what it justifies.

    Every scope settles in ONE transaction, so a partial settlement cannot leave
    the daily ledger holding a reservation the run ledger has already released.

    Three refusals belong to the database and are **left** there rather than
    pre-empted in Python, so the test that proves them is a test of the
    enforcement point and not of a mirror of it:

    * ``actual > reserve`` -> ``KS007``. :func:`settle_capped` is the caller that
      knows what to do about it;
    * settling twice -> ``KS007`` (the row is no longer ``open``);
    * settling a reclaimed reservation -> ``KS007``.

    Each surfaces as :class:`SettlementRefused` carrying the trigger's own
    diagnostic. ``audit_action``/``audit_extra`` exist because a **failed** call
    is audited under :data:`AUDIT_LLM_CALL_FAILED` with its reason, never as a
    cheap success (R18).
    """
    if reservation.replayed:
        raise SettlementRefused(
            reservation.idempotency_key,
            "this reservation is a replay receipt, not a grant: the key was already "
            "present, nothing was charged for it here, and settling it would release "
            "budget this call never reserved",
        )
    usage = _usage_of(evidence)
    reported = evidence.reported_microusd(model=reservation.model, table=table)
    keys = [reservation.scope_keys[scope] for scope in reservation.scopes]

    try:
        with role_connection(ROLE_RECON_WRITER) as conn:
            actual = _close_reservation(conn, keys=keys, evidence=evidence)
            overflowed = reported is not None and reported > actual
            _audit(
                conn,
                action=audit_action,
                subject=reservation.idempotency_key,
                body={
                    "model": reservation.model,
                    "price_table_version": reservation.price_version,
                    "reserve_microusd": reservation.reserve_microusd,
                    "actual_microusd": actual,
                    "usage": usage.as_dict(),
                    "scopes": list(reservation.scopes),
                    **evidence.detail(
                        reserve_microusd=reservation.reserve_microusd,
                        model=reservation.model,
                    ),
                    **dict(audit_extra or {}),
                },
                tokens_in=usage.total_input_tokens,
                tokens_out=usage.output_tokens,
                cost_microusd=actual,
            )
            if overflowed:
                _audit(
                    conn,
                    action=AUDIT_SETTLE_OVERFLOW,
                    subject=reservation.idempotency_key,
                    body={
                        "model": reservation.model,
                        "reserve_microusd": reservation.reserve_microusd,
                        "reported_microusd": reported,
                        "settled_microusd": actual,
                        "shortfall_microusd": (reported or 0) - actual,
                        "usage": usage.as_dict(),
                        "scopes": list(reservation.scopes),
                    },
                )
    except DBAPIError as exc:
        state = _sqlstate(exc)
        if state == KS_RESERVATION_LIFECYCLE:
            raise SettlementRefused(
                reservation.idempotency_key, str(getattr(exc, "orig", exc)).strip()
            ) from exc
        raise

    released = reservation.reserve_microusd - actual
    if overflowed:
        log.warning(
            "budget.settle_overflow",
            idempotency_key=reservation.idempotency_key,
            reserve_microusd=reservation.reserve_microusd,
            reported_microusd=reported,
            settled_microusd=actual,
        )
    log.info(
        "budget.settled",
        idempotency_key=reservation.idempotency_key,
        evidence=evidence.kind,
        actual_microusd=actual,
        released_microusd=released,
        scopes=list(reservation.scopes),
    )
    return Settlement(
        idempotency_key=reservation.idempotency_key,
        reserve_microusd=reservation.reserve_microusd,
        actual_microusd=actual,
        released_microusd=released,
        usage=usage,
        overflowed=overflowed,
        reported_microusd=reported if overflowed else None,
        evidence=evidence.kind,
    )


def record_settle_overflow(settlement: Settlement, *, model: str, scopes: Sequence[str]) -> None:
    """Alert on a settlement that could not hold its reported cost.

    The audit row is written inside the settling transaction (see
    :func:`settle`), so it commits atomically with the settlement it describes.
    This is the other half a cap hit gets: the stubbed alert. An overspend is
    cap-relevant -- the ledger is now known to under-report real spend by
    ``shortfall_microusd`` -- so it pages exactly as a cap hit does.
    """
    fire_alert(
        ALERT_SETTLE_OVERFLOW,
        {
            "idempotency_key": settlement.idempotency_key,
            "scopes": list(scopes),
            "model": model,
            "reserve_microusd": settlement.reserve_microusd,
            "reported_microusd": settlement.reported_microusd,
            "settled_microusd": settlement.actual_microusd,
            "shortfall_microusd": settlement.shortfall_microusd,
        },
    )


def settle_capped(
    reservation: Reservation,
    evidence: SpendEvidence,
    *,
    table: PriceTable | None = None,
) -> Settlement:
    """:func:`settle`, and **halt the scope** if the cost exceeded the reservation.

    :func:`worst_case_input_tokens` is a hard upper bound, so this should never
    fire. When it does, three things are true at once and all three matter:

    * the ledger **cannot** hold the reported cost. ``actual <= reserve`` is a
      trigger; settling at the reported figure is refused outright, which would
      leave the row ``open`` and the whole worst-case amount charged until the
      sweeper closed it. So the settlement lands at the reservation, carried by
      :class:`CostExceededReservation` -- a value that says exactly that;
    * the difference is **real money the ledger will never see**, so it is
      audited (``budget_settle_overflow``) and alerted;
    * the cap's arithmetic is now known to be an under-count. Continuing to
      reserve against it is reserving against a number that is wrong in the
      dangerous direction, so **every scope this reservation touched is halted**
      and will refuse further reservations until ops resumes it.

    The halt is the part that was missing. The previous version raised
    :class:`BudgetOverspend`, :func:`recon.llm.generate_rationale` turned it into
    ``status="overspend"``, and nothing consumed that value: 20 consecutive calls
    each overspending by ~30,000,000 microusd all proceeded.
    """
    reported = evidence.reported_microusd(model=reservation.model, table=table)
    if reported is None or reported <= reservation.reserve_microusd:
        return settle(reservation, evidence, table=table)

    settlement = settle(
        reservation,
        CostExceededReservation(reported_cost_microusd=reported, usage=_usage_of(evidence)),
        table=table,
    )
    record_settle_overflow(settlement, model=reservation.model, scopes=reservation.scopes)
    for scope in reservation.scopes:
        halt_scope(
            scope,
            reason="settlement reported more than the reservation could hold",
            detail={
                "idempotency_key": reservation.idempotency_key,
                "reported_microusd": reported,
                "settled_microusd": settlement.actual_microusd,
                "shortfall_microusd": settlement.shortfall_microusd,
            },
        )
    raise BudgetOverspend(settlement)


def settle_failed_call(
    reservation: Reservation,
    evidence: SpendEvidence,
    *,
    table: PriceTable | None = None,
) -> Settlement:
    """Settle a reservation whose provider call FAILED. Fails closed.

    This is the refund path, and it is the one a red team broke without touching
    the database: a failed call that settles at zero releases its whole
    reservation, so a timeout storm bills unbounded money against a ledger that
    reads zero.

    ``evidence`` carries the whole decision and there are exactly two admissible
    values, because a failed call reported no usage:

    * :class:`NeverSent` -- the failure is **provably pre-send** (connection
      refused, DNS, an auth rejection, a request the client rejected locally).
      Nothing arrived, so nothing is billed: charge zero, release everything;
    * :class:`OutcomeUnknown` -- everything else. A timeout, a read error, a 5xx
      after send, a cancelled stream, an unrecognised error class. The provider
      may have done the work and will bill for it, so the reservation settles at
      its **full reserved amount**.

    There is no third, cleverer number. Guessing a middle figure means guessing
    low on the cases that matter, and the reservation is the only bound anyone
    ever computed. Callers that cannot prove a failure is pre-send pass
    :class:`OutcomeUnknown`.

    The audit row is :data:`AUDIT_LLM_CALL_FAILED`, never :data:`AUDIT_LLM_CALL`
    -- R18 has the dashboard reconciling against these rows, and a thousand
    timeouts written as ``llm_call`` with cost 0 read as a thousand free
    successful calls.
    """
    if isinstance(evidence, ProviderReportedUsage):
        raise TypeError(
            "a FAILED call has no provider-reported usage. Settle it as NeverSent "
            "(provably pre-send) or OutcomeUnknown (everything else)."
        )
    settlement = settle(
        reservation,
        evidence,
        table=table,
        audit_action=AUDIT_LLM_CALL_FAILED,
        audit_extra={
            "outcome": "failed",
            # `reason` and not `failure_reason`: the committed privacy
            # allow-list keeps `reason` legible and tokenises the other, and an
            # audit row R18 reconciles against is worth nothing if the one field
            # explaining the failure is an opaque hash.
            "reason": evidence.reason(),
            "reached_provider": not evidence.releases,
            "disposition": (
                "released: the request provably never reached the provider"
                if evidence.releases
                else "charged in full: the request may have reached the provider"
            ),
        },
    )
    log.warning(
        "budget.failed_call_settled",
        idempotency_key=reservation.idempotency_key,
        evidence=evidence.kind,
        charged_microusd=settlement.actual_microusd,
        reserve_microusd=reservation.reserve_microusd,
        reason=evidence.reason(),
    )
    return settlement


# ===========================================================================
# TTL sweeper -- ops principal, never the capped party
# ===========================================================================
@dataclass(frozen=True)
class SweptReservation:
    """One reservation the sweeper closed because its lease died."""

    id: int
    scope: str
    reserve_microusd: int
    idempotency_key: str
    lease_expired_at: datetime
    #: What the sweep charged. The full reservation, unless ops presented
    #: evidence the call never went out.
    charged_microusd: int
    #: ``reserve - charged``. Zero on every ordinary sweep, by design.
    released_microusd: int


_SWEEP_CANDIDATES = text(
    "SELECT id, scope, reserve_microusd, idempotency_key, created_at "
    "FROM budget_reservations WHERE state = 'open' ORDER BY id FOR UPDATE"
)


def sweep_expired_reservations(
    *,
    grace_seconds: int = DEFAULT_SWEEP_GRACE_SECONDS,
    now: datetime | None = None,
    sweep_unleased: bool = False,
    release_evidence: NeverSent | None = None,
) -> tuple[SweptReservation, ...]:
    """Close reservations whose **lease has expired**, by CHARGING them in full.

    Runs as the principal in ``DATABASE_URL`` -- the **ops/sweeper** principal,
    and this function refuses to run as anyone else.

    **A dead lease is evidence the HOLDER is dead. It is not evidence the CALL
    did not happen.** The sweeper this replaces reclaimed an expired row, which
    released its whole reservation: a child process that completed a paid call
    and was then ``SIGKILL``ed got a 100% refund, and a crash loop was a way to
    spend without limit. So an expired lease is :class:`OutcomeUnknown` --
    "we cannot know what this call cost, and it may have cost the worst case" --
    and the row is closed at its **full reserved amount**.

    The consequence is deliberate and worth stating rather than discovering:
    **abandoned reservations consume budget permanently.** Budget lost to a crash
    is recoverable by ops (re-provision the scope, or raise the cap after
    reconciling); money refunded for a call that did happen is not recoverable at
    all. Losing budget to a crash is the correct direction.

    ``release_evidence`` is the only way a sweep releases anything, and it is
    what "only release on evidence the call never went out" looks like as an
    argument: ops passes :class:`NeverSent` carrying
    :attr:`PreSendProof.OPS_ATTESTED_OUTAGE` -- a provider incident report, an
    outbound-connection log showing nothing left the host. That proof is the
    operator's, and migration 0010's settle trigger **refuses it to**
    ``recon_writer``, so the capped party cannot reach this release even by
    issuing the statement itself. It is logged for every row it touches.

    A row whose key carries no lease at all is **not** swept -- absence of a
    liveness signal is not evidence of anything -- unless ops passes
    ``sweep_unleased``, which charges those rows in full as well.

    Candidates are locked ``FOR UPDATE`` for the whole decision, so a settle
    racing the sweep waits rather than interleaving with it.
    """
    if grace_seconds < 0:
        raise ValueError("grace_seconds must be non-negative")
    if release_evidence is not None and not isinstance(release_evidence, NeverSent):
        raise TypeError(
            "a sweep releases budget only on NeverSent evidence: an expired lease "
            "proves the holder died, never that the call did not go out"
        )
    evidence: SpendEvidence = release_evidence or OutcomeUnknown(
        "the holder's lease expired; whether its paid call reached the provider "
        "is unknowable from here"
    )
    reference = now or datetime.now(tz=UTC)
    horizon = reference - timedelta(seconds=grace_seconds)

    swept: list[SweptReservation] = []
    with ops_engine().begin() as conn:
        _refuse_capped_principal(conn)
        candidates = conn.execute(_SWEEP_CANDIDATES).fetchall()

        dead: list[tuple[Any, datetime]] = []
        unleased: list[str] = []
        for row in candidates:
            deadline = lease_deadline(row.idempotency_key, row.created_at)
            if deadline is None:
                unleased.append(row.idempotency_key)
                if sweep_unleased:
                    dead.append((row, reference))
                continue
            if deadline < horizon:
                dead.append((row, deadline))

        if unleased:
            log.warning(
                "budget.reservations_without_lease",
                count=len(unleased),
                swept=sweep_unleased,
                detail=(
                    "these reservations carry no lease duration, so the sweeper cannot "
                    "tell a dead one from a call still in flight; they are left alone "
                    "unless ops passes sweep_unleased"
                ),
            )
        if not dead:
            return ()

        for row, deadline in dead:
            charged = _close_reservation(conn, keys=[row.idempotency_key], evidence=evidence)
            _audit(
                conn,
                action=AUDIT_SWEEP_CHARGED,
                subject=row.idempotency_key,
                body={
                    "scope": row.scope,
                    "reserve_microusd": row.reserve_microusd,
                    "charged_microusd": charged,
                    "lease_expired_at": deadline.isoformat(),
                    **evidence.detail(reserve_microusd=row.reserve_microusd, model=None),
                },
                cost_microusd=charged,
                actor=AUDIT_ACTOR_OPS,
            )
            swept.append(
                SweptReservation(
                    id=row.id,
                    scope=row.scope,
                    reserve_microusd=row.reserve_microusd,
                    idempotency_key=row.idempotency_key,
                    lease_expired_at=deadline,
                    charged_microusd=charged,
                    released_microusd=row.reserve_microusd - charged,
                )
            )

    if swept:
        log.warning(
            "budget.reservations_swept",
            count=len(swept),
            grace_seconds=grace_seconds,
            evidence=evidence.kind,
            charged_microusd=sum(item.charged_microusd for item in swept),
            released_microusd=sum(item.released_microusd for item in swept),
            scopes=sorted({item.scope for item in swept}),
        )
    return tuple(swept)


def _refuse_capped_principal(conn: Connection) -> None:
    """Refuse to sweep as the capped party, before touching a single row.

    Migration 0005 already refuses ``open -> reclaimed`` to ``recon_writer``, so
    this cannot be the enforcement point and is not pretending to be one. It is
    here so the failure is a legible message at the top of the sweep instead of
    a ``KS007`` from row 400 of an ops cron -- and so a misconfigured
    ``DATABASE_URL`` is caught by the job that would otherwise be closing
    reservations under the wrong identity.

    Note that the sweep's own transition is now ``open -> settled``, which
    ``recon_writer`` *may* perform. That makes this check load-bearing in a way
    it was not before: charging a dead lease is an ops decision, and the capped
    party must not be able to settle somebody else's reservation.
    """
    principal = conn.execute(text("SELECT current_user")).scalar_one()
    if principal == ROLE_RECON_WRITER:
        raise BudgetError(
            f"the TTL sweeper is connected as {principal!r}, the capped party. "
            "Closing another holder's reservation is an ops decision; point "
            "DATABASE_URL at the ops principal."
        )


# ===========================================================================
# `python -m recon.budget sweep|roll|resume` -- the wiring for the ops cron
# ===========================================================================
def main(argv: Sequence[str] | None = None) -> int:
    """Ops entry point for the budget ledger (``infra/render.yaml``).

    ``sweep`` closes reservations whose lease died, **by charging them in full**.
    DESIGN pins the sweeper and, until this existed, nothing called it: a
    reservation whose process died stayed ``open`` for ever. It runs here rather
    than inside the web service on purpose -- it must connect as the ops
    principal, and the web service does not.

    ``roll`` opens a UTC day's ledger row (:func:`roll_daily_scope`) -- ahead of
    time, at a stated ``--cap-usd``, or after the fact for a day an operator
    wants open. It runs here rather than in the web service for the same reason
    ``sweep`` does: it provisions a ``budget_ledger`` row, and the capped party
    holds no INSERT on that table at all. Idempotent, so running it twice is
    harmless.

    **It is deliberately not a cron the deployment depends on.** Today's row
    opens itself the first time a reservation looks for it
    (:func:`_open_todays_daily_scope`, on this same ops principal), because a
    date-keyed cap that waited for a scheduled job would refuse every metered
    call in the live service between the deploy and the operator noticing. What
    ``roll`` adds is control of *which* day and *what* cap, which nothing on the
    request path can express.

    ``resume`` lifts an overspend halt after ops has reconciled the ledger. It is
    here, on the ops principal, because the capped party must not be able to
    clear the state that says its own ledger under-counts real spend.
    """
    import argparse

    from recon.logging import configure_logging_once

    configure_logging_once()
    parser = argparse.ArgumentParser(
        prog="python -m recon.budget",
        description="Keystone budget ledger operations (ops principal).",
    )
    parser.add_argument("command", choices=("sweep", "roll", "resume"), help="the operation to run")
    parser.add_argument(
        "--grace-seconds",
        type=int,
        default=DEFAULT_SWEEP_GRACE_SECONDS,
        help="extra slack after lease expiry before a reservation is closed",
    )
    parser.add_argument(
        "--sweep-unleased",
        action="store_true",
        help=(
            "also close open reservations whose key carries no lease duration, "
            "charging them in full. OFF by default: with no liveness signal the "
            "sweeper cannot tell a dead reservation from a call still in flight."
        ),
    )
    parser.add_argument(
        "--release-never-sent",
        metavar="NOTE",
        default=None,
        help=(
            "RELEASE the swept reservations instead of charging them, on the "
            "operator's attestation that their calls never left the host (a "
            "provider incident report, an outbound-connection log). NOTE is "
            "recorded on the audit row; the release itself is granted on "
            f"PreSendProof.{PreSendProof.OPS_ATTESTED_OUTAGE.name}, which the "
            "settle trigger refuses to the capped party. This is the ONLY way a "
            "sweep releases money: an expired lease proves the holder died, "
            "never that the call did not go out."
        ),
    )
    parser.add_argument("--scope", default=None, help="the ledger scope, for `resume`")
    parser.add_argument("--reason", default=None, help="why the halt is being lifted")
    parser.add_argument(
        "--day",
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            "the UTC day to roll, for `roll` (default: today in UTC). Any day is "
            "allowed -- tomorrow, to open it at a chosen cap before it starts, or "
            "a past one; it never moves spend, because a day is a row and rolling "
            "one only creates it. Today's own row needs no run of this at all: it "
            "opens on first use."
        ),
    )
    parser.add_argument(
        "--cap-usd",
        default=None,
        metavar="USD",
        help=(
            f"the new day's cap, for `roll` (default: ${DAILY_CAP_USD_ENV}, or "
            f"{DEFAULT_DAILY_CAP_USD} -- exactly what migration 0005 seeds). "
            "Ignored when the row already exists: raising a cap is a deliberate "
            "ops action against an existing row, never a side effect of rolling."
        ),
    )
    args = parser.parse_args(argv)

    if args.command == "roll":
        if args.day is not None:
            try:
                day = date.fromisoformat(args.day)
            except ValueError:
                parser.error(f"--day must be an ISO calendar date (YYYY-MM-DD), got {args.day!r}")
        else:
            day = utc_today()
        cap = None
        if args.cap_usd is not None:
            try:
                usd = Decimal(args.cap_usd)
            except (InvalidOperation, ValueError):
                parser.error(f"--cap-usd must be a decimal number of USD, got {args.cap_usd!r}")
            if usd < 0:
                parser.error("--cap-usd cannot be negative")
            cap = int((usd * MICROUSD_PER_USD).to_integral_value())
        scope, cap_microusd, created = roll_daily_scope(day, cap)
        state = "opened" if created else "already open"
        print(f"{state}: {scope} cap={cap_microusd} microusd")
        return 0

    if args.command == "resume":
        if not args.scope or not args.reason:
            parser.error("resume needs --scope and --reason")
        resume_scope(args.scope, reason=args.reason)
        print(f"resumed {args.scope}: {args.reason}")
        return 0

    swept = sweep_expired_reservations(
        grace_seconds=args.grace_seconds,
        sweep_unleased=args.sweep_unleased,
        release_evidence=(
            NeverSent(PreSendProof.OPS_ATTESTED_OUTAGE, args.release_never_sent)
            if args.release_never_sent
            else None
        ),
    )
    charged = sum(item.charged_microusd for item in swept)
    released = sum(item.released_microusd for item in swept)
    print(
        f"closed {len(swept)} expired reservation(s): charged {charged} microusd, "
        f"released {released} microusd"
    )
    for item in swept:
        print(
            f"  {item.scope} {item.idempotency_key} charged={item.charged_microusd} "
            f"released={item.released_microusd} microusd"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
