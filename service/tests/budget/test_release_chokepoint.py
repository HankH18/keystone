"""Spend goes DOWN only where the DATABASE says it may, and only against evidence.

The previous version of this module counted release sites in the source and
asserted that the one function issuing them took no amount. Both claims were
true and neither was a boundary, which a fourth red-team pass proved twice over:

* it settled open reservations at ``actual = 0`` as ``recon_writer`` using
  ``UPDATE public.budget_reservations``, ``UPDATE "budget_reservations"`` and
  ``UPDATE ONLY budget_reservations`` -- three spellings the regex below does not
  match and, far more to the point, three statements the GRANT permitted. A
  Python chokepoint cannot be the boundary while the grant still allows the
  write;
* it called the chokepoint with ``reserve_microusd=0``. The docstring said the
  function had no amount parameter. It had one, caller-supplied and never
  checked against the row, and :class:`OutcomeUnknown` -- the value documented as
  charging the full reservation -- returned that zero and released 15,850
  microusd.

So this module now tests three separate things, in increasing order of how much
they are worth:

1. **the database refuses the release** unless the amount is one it derives from
   the row being closed (migration 0010). Run as the real ``recon_writer``,
   against real Postgres, in the spellings that worked;
2. **no evidence value can compute money at all**, and the chokepoint has no
   parameter that could carry one;
3. the source-level count, kept as defence in depth and labelled as such.
"""

from __future__ import annotations

import ast
import inspect
import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from recon.budget import (
    KS_RESERVATION_LIFECYCLE,
    NEVER_SENT_WINDOW_SECONDS,
    CostExceededReservation,
    DegenerateUsage,
    NeverSent,
    OutcomeUnknown,
    PreSendProof,
    ProviderReportedUsage,
    SpendEvidence,
    Usage,
    _close_reservation,
    cost_microusd,
)
from recon.db import ROLE_RECON_WRITER, role_connection
from recon.suite.burst import HARNESS_MODULE, release_sites
from tests.budget.support import ScopeFactory, spent

#: A release is an ``UPDATE`` against ``budget_reservations`` -- the settle
#: trigger is what hands ``reserve - actual`` back to the ledger -- or a direct
#: call to the privileged ``keystone_budget_release`` helper. Anchored to the
#: start of a line so prose about the table is not counted as a statement.
RELEASE_SQL = re.compile(r"(?im)^\s*update\s+budget_reservations\b|keystone_budget_release\s*\(")

PACKAGE = Path(__file__).resolve().parents[2] / "recon"

MODEL = "mock-rationale-v1"
#: 100 input x 6.25 (the dearest input-side rate) + 384 output x 25 = 10,225.
RESERVE_INPUT_TOKENS = 100
RESERVE_OUTPUT_TOKENS = 384
RESERVE_AMOUNT = 10_225

_RESERVE = text(
    "INSERT INTO budget_reservations "
    "(scope, idempotency_key, reserve_microusd, model, max_input_tokens, max_output_tokens) "
    "VALUES (:scope, :key, :reserve, :model, :max_in, :max_out) RETURNING id"
)


def _price_bound_reservation(scope: str) -> str:
    """One reservation on ``scope``, inserted as the capped party. Returns its key."""
    key = f"chokepoint-{uuid.uuid4()}"
    with role_connection(ROLE_RECON_WRITER) as conn:
        conn.execute(
            _RESERVE,
            {
                "scope": scope,
                "key": key,
                "reserve": RESERVE_AMOUNT,
                "model": MODEL,
                "max_in": RESERVE_INPUT_TOKENS,
                "max_out": RESERVE_OUTPUT_TOKENS,
            },
        )
    return key


def _sqlstate(error: DBAPIError) -> str | None:
    return getattr(getattr(error, "orig", None), "sqlstate", None)


# ===========================================================================
# 1. the boundary: the DATABASE refuses a release it cannot derive
# ===========================================================================
#: Every one of these is a statement ``recon_writer`` holds the grant for. The
#: first three are the exact spellings a red team used to zero a live
#: reservation while the source-level count below reported exactly one release
#: site; the rest name an amount the row does not justify.
_REFUSED_RELEASES = (
    pytest.param(
        "UPDATE public.budget_reservations SET actual_microusd = 0, state = 'settled' "
        "WHERE idempotency_key = :key",
        id="schema-qualified",
    ),
    pytest.param(
        "UPDATE \"budget_reservations\" SET actual_microusd = 0, state = 'settled' "
        "WHERE idempotency_key = :key",
        id="quoted",
    ),
    pytest.param(
        "UPDATE ONLY budget_reservations SET actual_microusd = 0, state = 'settled' "
        "WHERE idempotency_key = :key",
        id="only",
    ),
    pytest.param(
        "UPDATE budget_reservations SET actual_microusd = 1, state = 'settled', "
        "settle_evidence = 'provider_reported_usage', usage_input_tokens = 1, "
        "usage_output_tokens = 1, usage_cache_read_tokens = 0, usage_cache_write_tokens = 0 "
        "WHERE idempotency_key = :key",
        id="priced-at-a-number-of-its-own",
    ),
    pytest.param(
        "UPDATE budget_reservations SET actual_microusd = 0, state = 'settled', "
        "settle_evidence = 'outcome_unknown' WHERE idempotency_key = :key",
        id="unknown-outcome-refunded",
    ),
    pytest.param(
        "UPDATE budget_reservations SET actual_microusd = 0, state = 'settled', "
        "settle_evidence = 'never_sent', settle_proof = 'ops_attested_outage' "
        "WHERE idempotency_key = :key",
        id="self-attested-outage",
    ),
    pytest.param(
        "UPDATE budget_reservations SET actual_microusd = 0, state = 'settled', "
        "settle_evidence = 'never_sent' WHERE idempotency_key = :key",
        id="full-release-with-no-proof",
    ),
    pytest.param(
        "UPDATE budget_reservations SET actual_microusd = 0, state = 'settled', "
        "settle_evidence = 'provider_reported_usage', usage_input_tokens = 0, "
        "usage_output_tokens = 0, usage_cache_read_tokens = 0, usage_cache_write_tokens = 0 "
        "WHERE idempotency_key = :key",
        id="degenerate-usage-priced-at-zero",
    ),
)


@pytest.mark.parametrize("statement", _REFUSED_RELEASES)
def test_the_database_refuses_a_release_it_cannot_derive_from_the_row(
    owner_engine: Engine, make_scope: ScopeFactory, statement: str
) -> None:
    """THE blocker, executed literally, as the role that holds the grant.

    Each of these is a legal statement for ``recon_writer`` -- the grant names
    ``actual_microusd``, ``state`` and ``settled_at``, and since 0010 the
    evidence columns too. What refuses them is the settle trigger, and it refuses
    them by the same rule every time: the amount is not one the row justifies.
    """
    scope = make_scope(RESERVE_AMOUNT * 2)
    key = _price_bound_reservation(scope)
    assert spent(owner_engine, scope) == RESERVE_AMOUNT

    with pytest.raises(DBAPIError) as excinfo, role_connection(ROLE_RECON_WRITER) as conn:
        conn.execute(text(statement), {"key": key})

    assert _sqlstate(excinfo.value) == KS_RESERVATION_LIFECYCLE, (
        f"the refusal must be the settle trigger's, not an unrelated failure: {excinfo.value}"
    )
    assert spent(owner_engine, scope) == RESERVE_AMOUNT, "the ledger moved anyway"


def test_the_settlements_the_database_DOES_derive_still_work(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """The other side, so the rule is not "refuse every settlement".

    A boundary that refused everything would pass every test above and would
    have broken the product. Each of these is an amount the database derives for
    itself from the row being closed.
    """
    scope = make_scope(RESERVE_AMOUNT * 10)

    # Priced from the committed rates, and ONLY at that number.
    usage = Usage(input_tokens=40, output_tokens=30)
    priced_key = _price_bound_reservation(scope)
    before = spent(owner_engine, scope)
    with role_connection(ROLE_RECON_WRITER) as conn:
        conn.execute(
            text(
                "UPDATE budget_reservations SET actual_microusd = :actual, state = 'settled', "
                "settle_evidence = 'provider_reported_usage', usage_input_tokens = :ui, "
                "usage_output_tokens = :uo, usage_cache_read_tokens = 0, "
                "usage_cache_write_tokens = 0 WHERE idempotency_key = :key"
            ),
            {
                "actual": cost_microusd(MODEL, usage),
                "ui": usage.input_tokens,
                "uo": usage.output_tokens,
                "key": priced_key,
            },
        )
    charged = before - spent(owner_engine, scope)
    assert charged == RESERVE_AMOUNT - cost_microusd(MODEL, usage), (
        "a priced settlement must release exactly reserve - the committed cost"
    )

    # An unknown outcome charges the reservation in full and releases nothing.
    unknown_key = _price_bound_reservation(scope)
    before = spent(owner_engine, scope)
    with role_connection(ROLE_RECON_WRITER) as conn:
        conn.execute(
            text(
                "UPDATE budget_reservations SET actual_microusd = reserve_microusd, "
                "state = 'settled', settle_evidence = 'outcome_unknown' "
                "WHERE idempotency_key = :key"
            ),
            {"key": unknown_key},
        )
    assert spent(owner_engine, scope) == before, "an unknown outcome releases nothing"

    # A pre-send failure, on a proof the capped party is allowed to claim.
    never_key = _price_bound_reservation(scope)
    before = spent(owner_engine, scope)
    with role_connection(ROLE_RECON_WRITER) as conn:
        conn.execute(
            text(
                "UPDATE budget_reservations SET actual_microusd = 0, state = 'settled', "
                "settle_evidence = 'never_sent', settle_proof = 'connection_refused' "
                "WHERE idempotency_key = :key"
            ),
            {"key": never_key},
        )
    assert before - spent(owner_engine, scope) == RESERVE_AMOUNT, (
        "a request that provably never left is released in full"
    )


def test_a_reservation_must_reserve_the_worst_case_its_own_rates_give(
    make_scope: ScopeFactory,
) -> None:
    """The other half of the loop: the rates that release also SIZE the reservation.

    Without this the price binding would be decorative -- a caller could name
    tiny token bounds against a large reservation and then settle for a
    microusd. The reserve trigger refuses a reservation whose own arithmetic
    does not hold, so deflating the binding deflates the reservation the caller
    is trying to keep.
    """
    scope = make_scope(RESERVE_AMOUNT * 10)
    with pytest.raises(DBAPIError) as excinfo, role_connection(ROLE_RECON_WRITER) as conn:
        conn.execute(
            _RESERVE,
            {
                "scope": scope,
                "key": f"chokepoint-inflated-{uuid.uuid4()}",
                "reserve": RESERVE_AMOUNT,
                "model": MODEL,
                "max_in": 1,
                "max_out": 1,
            },
        )
    assert _sqlstate(excinfo.value) == KS_RESERVATION_LIFECYCLE


def test_the_never_sent_proof_is_a_closed_vocabulary_the_database_holds() -> None:
    """MINOR 6: symmetric bars on both sides of the one 100% refund.

    ``NeverSent("trust me bro")`` released 15,850 microusd. The Python type now
    takes a :class:`PreSendProof` member and the database column is an enum of
    the same values, so a proof cannot be invented on either side.
    """
    with pytest.raises(TypeError):
        NeverSent("trust me bro")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        NeverSent(None)  # type: ignore[arg-type]

    honest = NeverSent(PreSendProof.CONNECTION_REFUSED, "ConnectionRefusedError: [Errno 61]")
    assert honest.settlement_proof() is PreSendProof.CONNECTION_REFUSED
    assert honest.settlement_usage() is None


def test_the_database_enum_and_the_python_vocabulary_are_the_same_set(
    owner_engine: Engine,
) -> None:
    """A value on one side and not the other is a boundary with a gap in it."""
    with owner_engine.connect() as conn:
        proofs = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT enumlabel FROM pg_enum JOIN pg_type ON pg_type.oid = enumtypid "
                    "WHERE typname = 'budget_never_sent_proof'"
                )
            )
        }
        evidence = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT enumlabel FROM pg_enum JOIN pg_type ON pg_type.oid = enumtypid "
                    "WHERE typname = 'budget_settle_evidence'"
                )
            )
        }
    assert proofs == {member.value for member in PreSendProof}
    assert evidence == {klass.kind for klass in SpendEvidence.__subclasses__()}


def test_the_never_sent_window_matches_the_one_the_trigger_enforces(
    owner_engine: Engine,
) -> None:
    """The constant in Python is a copy of the enforced one, and must not drift."""
    with owner_engine.connect() as conn:
        body = conn.execute(
            text("SELECT prosrc FROM pg_proc WHERE proname = 'keystone_budget_settle'")
        ).scalar_one()
    assert f"interval '{NEVER_SENT_WINDOW_SECONDS} seconds'" in body


# ===========================================================================
# 2. no evidence value can compute money, and the chokepoint cannot be told one
# ===========================================================================
def test_the_chokepoint_takes_neither_an_amount_nor_a_reservation_size() -> None:
    """BLOCKER 2: the parameter the old docstring denied having.

    ``_close_reservation`` used to take ``reserve_microusd`` -- caller-supplied,
    never validated against the row -- and ``OutcomeUnknown.charge_microusd``
    returned exactly that number. Passing zero to the value documented as
    fail-closed released the whole reservation.
    """
    signature = inspect.signature(_close_reservation)
    assert "evidence" in signature.parameters
    evidence = signature.parameters["evidence"]
    assert evidence.default is inspect.Parameter.empty, "evidence is required, never defaulted"
    assert evidence.kind is inspect.Parameter.KEYWORD_ONLY

    for forbidden in (
        "actual",
        "actual_microusd",
        "charge",
        "charge_microusd",
        "released",
        "reserve_microusd",
        "reserve",
    ):
        assert forbidden not in signature.parameters, (
            f"{forbidden!r} would let a caller name the amount it wants released"
        )


def test_no_evidence_value_can_produce_an_amount_at_all() -> None:
    """The amount is the database's. Nothing on this side computes one.

    The subclass sweep matters more than the list: an evidence type that grew a
    money method would have to be added here, which is the review this control
    exists to force.
    """
    for klass in SpendEvidence.__subclasses__():
        for banned in ("charge_microusd", "charge", "actual_microusd"):
            assert not hasattr(klass, banned), (
                f"{klass.__name__}.{banned} computes money on the caller's side; the "
                "settled amount is derived from the row by the settle trigger"
            )

    releasing = sorted(klass.__name__ for klass in SpendEvidence.__subclasses__() if klass.releases)
    assert releasing == ["NeverSent", "ProviderReportedUsage"], (
        "only 'the provider told us what it billed' and 'the request provably "
        f"never left' can release a reservation; found {releasing}"
    )

    # And each value carries only what the settle trigger needs to check it.
    assert OutcomeUnknown("timed out").settlement_usage() is None
    assert OutcomeUnknown("timed out").settlement_proof() is None
    assert ProviderReportedUsage(Usage(input_tokens=1, output_tokens=1)).settlement_proof() is None
    assert CostExceededReservation(reported_cost_microusd=10**9).settlement_proof() is None


def test_the_chokepoint_refuses_anything_that_is_not_evidence() -> None:
    """A bare integer, a Usage, ``None``: none of them is a reason."""
    for impostor in (0, 12_345, None, Usage(input_tokens=1, output_tokens=1), "trust me"):
        with pytest.raises(TypeError):
            _close_reservation(
                None,  # type: ignore[arg-type]
                keys=["irrelevant"],
                evidence=impostor,  # type: ignore[arg-type]
            )


def test_degenerate_usage_cannot_be_dressed_up_as_evidence() -> None:
    """``Usage()`` prices at zero and must not settle -- in Python and in the DB.

    The constructor refusal is here; the trigger's own refusal of a degenerate
    usage block is in ``_REFUSED_RELEASES`` above, so the rule holds for a
    statement this module never issued.
    """
    for degenerate in (
        Usage(),
        Usage(input_tokens=1000, output_tokens=0),
        Usage(output_tokens=1000),
        Usage(cache_read_tokens=1000),
    ):
        with pytest.raises(DegenerateUsage):
            ProviderReportedUsage(degenerate)

    # And the honest case still works, so this is not "refuse everything".
    priced = ProviderReportedUsage(Usage(input_tokens=40, output_tokens=30))
    assert priced.reported_microusd(model=MODEL) == cost_microusd(
        MODEL, Usage(input_tokens=40, output_tokens=30)
    )


# ===========================================================================
# 3. the source-level count -- defence in depth, and labelled as such
# ===========================================================================
def _release_literals(*, include_harness: bool) -> list[tuple[Path, ast.Constant]]:
    """Every string constant in ``recon/`` that can release budget."""
    found: list[tuple[Path, ast.Constant]] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if not include_harness and path.relative_to(PACKAGE).as_posix() == HARNESS_MODULE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and RELEASE_SQL.search(node.value)
            ):
                found.append((path, node))
    return found


def test_there_is_exactly_one_statement_in_the_product_that_can_release_budget() -> None:
    """One release site in the product. Defence in depth, not the boundary.

    Worth keeping -- a second one appearing is worth knowing about -- and worth
    labelling honestly: the red team's three spellings all released money while
    this count read exactly one, because the count is over the source and the
    permission is over the statement.
    """
    literals = _release_literals(include_harness=False)
    where = [f"{path.relative_to(PACKAGE)}:{node.lineno}" for path, node in literals]

    assert len(literals) == 1, (
        "spend must be reducible in exactly ONE place in the product; a second "
        f"release site is a second way to refund money that was actually spent. Found: {where}"
    )
    path, _ = literals[0]
    assert path.name == "budget.py", f"the one release site must live in budget.py, not {where}"

    # And the independent enumeration in the graded check agrees with this one.
    assert len(release_sites()) == 1


def test_every_release_statement_in_the_harness_is_a_registered_refused_attack() -> None:
    """The one carve-out, bounded: burst.py's release SQL is the attack corpus.

    ``release_sites`` skips the verification harness because the harness
    deliberately contains release statements -- the spellings it issues as the
    capped party to prove the database refuses them. That exclusion is only safe
    if everything in there is one of those, so this counts them.
    """
    from recon.suite.burst import _RAW_RELEASE_SPELLINGS

    harness = [
        node
        for path, node in _release_literals(include_harness=True)
        if path.relative_to(PACKAGE).as_posix() == HARNESS_MODULE
    ]
    registered = {statement for _, statement in _RAW_RELEASE_SPELLINGS}
    unregistered = sorted({node.value for node in harness} - registered)
    assert not unregistered, (
        f"the harness contains release SQL that is not one of the registered, "
        f"database-refused attack spellings: {unregistered}. That would be a real "
        "release site hiding behind the exclusion"
    )
    # The regex matches only three of the six -- which is the whole reason a
    # source-level count is not the boundary -- so also assert, out of the AST,
    # that every registered spelling really is a literal in the excluded module.
    tree = ast.parse((PACKAGE / HARNESS_MODULE).read_text(encoding="utf-8"))
    constants = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert registered <= constants, sorted(registered - constants)


def test_the_release_statement_is_bound_to_the_chokepoint_and_used_nowhere_else() -> None:
    """The SQL is executed by ``_close_reservation`` and by nothing else."""
    source = (PACKAGE / "budget.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    names = [
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
        and any(
            isinstance(child, ast.Constant)
            and isinstance(child.value, str)
            and RELEASE_SQL.search(child.value)
            for child in ast.walk(node.value)
        )
    ]
    assert len(names) == 1, f"the release statement is bound to {names}, expected one name"
    statement = names[0]

    users = sorted(
        {
            function.name
            for function in ast.walk(tree)
            if isinstance(function, ast.FunctionDef)
            for child in ast.walk(function)
            if isinstance(child, ast.Name) and child.id == statement
        }
    )
    assert users == ["_close_reservation"], (
        f"{statement} is executed by {users}; every release must go through the "
        "one chokepoint that demands evidence"
    )


def test_no_module_outside_budget_calls_the_chokepoint() -> None:
    """It is private, and the privacy is enforced by an enumeration, not a habit."""
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if path.name == "budget.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "_close_reservation":
                offenders.append(f"{path.relative_to(PACKAGE)}:{node.lineno}")
            if isinstance(node, ast.Attribute) and node.attr == "_close_reservation":
                offenders.append(f"{path.relative_to(PACKAGE)}:{node.lineno}")
    assert offenders == [], f"the chokepoint is reached from outside budget.py: {offenders}"
