"""The canonical apply path, R24's auto-apply gate, and the rollback path.

Three things live here and they are deliberately separate functions:

:func:`apply_proposal`
    Writes one **approved** proposal's action onto the canonical layer, as
    ``apply_writer``, in one transaction, citing the proposal. This is the
    endpoint's body (``POST /api/proposals/{id}/apply``).
:func:`auto_apply`
    R24. The same write, taken **unattended**, and only when every one of the
    stretch's four conditions holds. It is a separate function because R24 says
    so and because a gate that shares a body with the manual path is a gate that
    a later edit to the manual path can widen by accident.
:func:`rollback_proposal`
    The reverse leg. Restores the entity to the state the apply's
    ``proposal_events`` row captured -- byte for byte, because the value written
    back is the stored ``before`` column itself and never a value this process
    reconstructed.

What this module does NOT do, and cannot
----------------------------------------
It never approves anything. ``apply_writer`` may move ``approved -> applied``
and ``applied -> rolled_back`` and nothing else (SQLSTATE ``KS004``), so the
automation cannot decide its own work even if this code tried. That is the
answer to the obvious question about R24: "auto-apply" here means *the machine
may press APPLY without a human pressing it*, not *the machine may approve*. The
approval is still a ``review_writer`` act by a named decider. Widening
``apply_writer``'s transition graph to make it mean the other thing is exactly
the change the three-role boundary exists to prevent, so it is not made.

It also never writes a source. The sources are files behind
:class:`recon.adapters.base.ReadOnlyAdapter`, whose Protocol has no write member
and whose classes may not carry a write-shaped attribute anywhere in their MRO.
:func:`assert_sources_are_unwritable` states that as an executable assertion
rather than as a comment -- and it now inspects the adapter OBJECTS: it used to
iterate the ``dict`` ``build_adapters`` returns, which yields its keys, so it
introspected three strings and returned ``("str", "str", "str")``.
:func:`source_tree_digest` measures the other half, that a real apply run left
the bytes on disk unchanged, and :data:`WRITABLE_TABLES` pins the one table of
the **canonical layer** this module's SQL mutates.

That is a narrower claim than it used to make, and the narrowing is a
correction, not a hedge: this sentence read ":data:`WRITABLE_TABLES` pins the one
table this module's SQL mutates", which is false. The statements below also
``INSERT INTO proposal_events`` (the before/after ledger a rollback is restored
from), ``UPDATE proposals`` (the ``approved -> applied -> rolled_back`` arc
``KS004`` permits ``apply_writer``) and write ``audit_log`` through
:func:`recon.logging.insert_audit_row`. None of those is a source and none is
canonical output, which is why R24 is satisfied -- but a reader who took the
old sentence literally would go looking for a one-statement module and would
conclude, wrongly, that the ledger write happens somewhere else.

The content of the write is not this module's choice
----------------------------------------------------
Migration 0007's ``KS010`` admits a canonical UPDATE only when::

    NEW.current = OLD.current || (action -> 'set')

so every statement below computes the new value **in SQL, from the row and the
cited proposal**, and never round-trips a value through Python. That is not
style: ``jsonb`` distinguishes ``1`` from ``1.0`` as text while comparing them
equal as jsonb, the citation trigger pins both, and ``recon.suite.mirror``
hashes ``row::text`` on a graded determinism path. A value that has been through
``json.loads``/``json.dumps`` is a value that may have changed its bytes.

The shallow-merge trap
----------------------
``entities.current`` is a **flat** object that happens to contain one nested
object, ``survived`` (``recon.resolve.SURVIVED_PATHS``). ``||`` is a shallow
merge, so ``{"set": {"survived": {"crm.contact.email": ...}}}`` does not update
one survived field -- it **replaces the whole map** and erases the other eight.
A **scalar** does the same thing more completely: ``{"set": {"survived":
"wiped"}}`` replaces all nine with a string, which the guard used to report safe
because it asked for both sides to be objects before it looked. It guards on the
SHAPE CHANGE now -- an object ceasing to be one -- not on both sides being maps.
:func:`merge_preview` reports exactly which nested siblings a merge would drop,
and :func:`apply_proposal` refuses any proposal whose merge would drop one --
placed there rather than in R24's gate on purpose, because ``apply_proposal`` is
the single statement BOTH the manual and the automatic paths go through, so a
reviewer pressing apply by hand is guarded by it too. (The gate is pure and
holds no entity value; it could not compute the merge without one.) No committed
fix template writes ``survived`` today -- they write source-qualified paths,
which land as top-level keys -- so this is a guard against the next one, not a
description of current behaviour.

The write set is a set of PATHS, and it is read off the VALUE
--------------------------------------------------------------
Because ``survived`` is nested, "the keys of ``action->'set'``" and "the paths
this statement writes" are two different sets, and the first one is judgeable
without ever naming a contract path. An action of::

    {"set": {"survived": {<all nine SURVIVED_PATHS, three of them replaced>}}}

presents ONE key, ``survived``, which is in neither ``SENSITIVE_FIELDS`` nor
``AUTO_APPLY_ELIGIBLE`` -- while replacing ``crm.contact.email``,
``appdb.student.status`` and ``appdb.enrollment.stage``. :func:`effective_write_paths`
is therefore the subject of :func:`write_set_gate`.

**And it derives the answer from the merged VALUE, not from the shape of either
side.** That distinction has now had to be made twice. ``merge_preview`` asked
for BOTH sides to be objects before it looked, so ``{"set": {"survived":
"wiped"}}`` -- a scalar erasing the whole nine-key map -- was reported *safe*.
:func:`effective_write_paths` then made the same mistake one layer up: it
descended only when the value being ASSIGNED was an object, so the same scalar
reported the single unlisted leaf ``survived`` and the erasure of six
``SENSITIVE_FIELDS`` members was invisible to the gate and to ``KS013``. The rule
is now stated over the row the merge would produce -- every leaf on which it
would differ from the row as it stands, one level deep, plus every top-level key
the action names with a non-object value -- so a list, a scalar, a JSON ``null``,
an absent key and an object are one comparison instead of four branches.
Migration 0014 rewrites the SQL to the same rule, and
``tests/apply/test_nested_write_set.py`` asserts the two equal over every shape.

What an auto-appliable write is OBSERVABLE in
----------------------------------------------
``AUTO_APPLY_ELIGIBLE`` is written in source-qualified paths and
``recon.resolve.VIEW_FIELDS`` shares no member with it.

``VIEW_FIELDS`` is **the key set the entity projection is built from**, and the
readers that actually exist are two: ``recon.api.entities._view_of``, the body of
``GET /api/entities`` and ``GET /api/entities/{key}``; and ``recon.suite.golden``,
the R10 join check, which diffs that projection against the committed
``golden/expected-views.json``. **The dashboard is not one of them** -- this
docstring used to say "every reader projects ... the object the dashboard
renders", and it does not: ``dashboard/src/lib/httpClient.ts`` calls
``/api/conflicts``, ``/api/proposals``, the three decision verbs and
``/api/scorecard``, and never the entities endpoint at all.

A ``{"set": {"crm.contact.grade": "7"}}`` write therefore lands as a NEW
top-level key of ``entities.current`` that neither reader projects: the row
moves, its digest moves, and no value either of them shows has changed. The one
eligible path that does live in the view is ``crm.contact.lifecycle_stage``,
which is a member of ``survived`` -- so the observable form of that fix is the
nested one, carrying the whole map, and it is representable only because the gate
judges paths instead of keys. ``recon.reconciler`` now EMITS that form for a
lifecycle-only C6, and ``tests/apply/test_observable_auto_apply.py`` applies a
real one, reads the value back through the reader's own projection, rolls it back
and reads it again. What it cannot demonstrate is an unattended apply of it: see
``docs/proposal-policy.md`` SS8.10 for the measured reason.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import Connection, text

from recon.db import ROLE_APPLY_WRITER, role_connection
from recon.logging import get_logger, insert_audit_row
from recon.reference import (
    AUTO_APPLY_ELIGIBLE,
    CONFLICT_TYPES,
    FIX_TARGETS,
    SENSITIVE_FIELDS,
)
from recon.sensitive import Classification, classify

__all__ = [
    "APPLY_ACTOR",
    "AUTO_APPLY_ACTOR",
    "AUTO_APPLY_CASE_TYPES",
    "AUTO_APPLY_CONFIDENCE_FLOOR",
    "EVIDENCE_SCHEMA",
    "WRITABLE_TABLES",
    "ApplyError",
    "ApplyResult",
    "AutoApplyDecision",
    "AutoApplyRefused",
    "EligibilityClearance",
    "GateCheck",
    "MergePreview",
    "ProposalRecord",
    "RollbackPath",
    "RollbackResult",
    "SensitivityVerdict",
    "WritePath",
    "WriteSetVerdict",
    "apply_proposal",
    "assert_sources_are_unwritable",
    "auto_apply",
    "auto_apply_decision",
    "effective_write_paths",
    "entity_current",
    "entity_digest",
    "evaluate_auto_apply",
    "load_proposal",
    "merge_preview",
    "rollback_path",
    "rollback_proposal",
    "sensitivity_gate",
    "source_tree_digest",
    "write_set_gate",
]

log = get_logger("recon.apply")

# ===========================================================================
# R24's four conditions, as committed constants
# ===========================================================================

#: R24: "fires only at confidence >= 0.95". A :class:`~decimal.Decimal` because
#: ``proposals.confidence`` is ``numeric(5,4)`` and ``0.95`` as a float is
#: ``0.9500000000000000111...`` -- a score of exactly 0.95 must pass, and with
#: floats whether it does depends on which side of the comparison rounds.
AUTO_APPLY_CONFIDENCE_FLOOR: Final = Decimal("0.95")

#: R24's "approved case types", **derived** from the committed contract rather
#: than restated: a case type is approved exactly when contract SS6's fix-target
#: table classifies its template ``eligible``. Restating the list here would let
#: it drift from ``FIX_TARGETS``, and the drift would be silent in the safe-
#: looking direction only until someone edited the wrong copy.
AUTO_APPLY_CASE_TYPES: Final[frozenset[str]] = frozenset(
    conflict_type
    for conflict_type in CONFLICT_TYPES
    if FIX_TARGETS[conflict_type].classification == "eligible"
)

#: The evidence packet ``recon.reconciler`` persists. "Complete evidence" is a
#: statement about *this* packet; a proposal carrying some other schema is not
#: evidence this module knows how to judge, so it is refused rather than assumed
#: complete.
EVIDENCE_SCHEMA: Final = "keystone.evidence.v1"

#: The only **canonical** table any statement in this module mutates. R24:
#: "applies only to Keystone's canonical layer -- never to sources."
#:
#: Not the only table it writes, and this comment used to say it was. An apply
#: also inserts the ``proposal_events`` before/after ledger row, moves
#: ``proposals.status`` along ``KS004``'s arc, and lands an ``audit_log`` row
#: through :func:`recon.logging.insert_audit_row`. Those are Keystone's own
#: bookkeeping, not canonical output and not a source, so R24 is untouched --
#: but the set is named for what it is rather than overstated, because a reader
#: auditing "what does apply write?" against this line would have missed three
#: tables.
WRITABLE_TABLES: Final[frozenset[str]] = frozenset({"entities"})

#: ``proposal_events.actor`` and ``audit_log.actor`` must match ``^system:`` for
#: ``apply_writer`` (SQLSTATE ``KS003``). The two actors are distinct so the
#: ledger says whether a human pressed the button.
APPLY_ACTOR: Final = "system:apply"
AUTO_APPLY_ACTOR: Final = "system:auto-apply"

#: Statuses this module may move a proposal out of, matching ``KS004``'s graph.
_APPLIABLE_STATUS: Final = "approved"
_REVERSIBLE_STATUS: Final = "applied"


class ApplyError(RuntimeError):
    """An apply or rollback that this module refuses to attempt.

    ``reason`` is a stable machine token (the API maps it onto an RFC7807
    ``type``); the message is the human sentence.
    """

    def __init__(self, reason: str, detail: str, *, proposal_id: int | None = None) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.proposal_id = proposal_id


class AutoApplyRefused(ApplyError):
    """R24's gate said no. Carries the full :class:`AutoApplyDecision`."""

    def __init__(self, decision: AutoApplyDecision) -> None:
        super().__init__(
            decision.reason,
            decision.detail,
            proposal_id=decision.proposal_id,
        )
        self.decision = decision


# ===========================================================================
# the row this module reasons about
# ===========================================================================


@dataclass(frozen=True)
class ProposalRecord:
    """One ``proposals`` row joined to its ``conflicts`` row.

    Deliberately **not** frozen-with-slots and deliberately a plain dataclass:
    ``tests/apply/test_structural_order.py`` subclasses it with a ``confidence``
    that raises on access, which is how "a sensitive proposal never reaches the
    confidence gate" is proved structurally instead of by a threshold test.
    """

    id: int
    conflict_id: int
    conflict_type: str
    fingerprint: str
    disagreeing_fields: tuple[str, ...]
    action: Mapping[str, Any]
    confidence: Decimal
    evidence: Mapping[str, Any]
    status: str
    sensitive: bool
    target_canonical_id: str | None
    rationale: str | None = None

    @property
    def assignments(self) -> dict[str, Any]:
        """``action['set']`` -- the field paths this proposal would write."""
        return dict(self.action.get("set") or {})

    @property
    def evidence_only(self) -> bool:
        """True when the proposal writes no field (contract SS6's third class)."""
        return not self.assignments


_SELECT_PROPOSAL = text(
    """
    SELECT p.id,
           p.conflict_id,
           p.fingerprint,
           p.action,
           p.confidence,
           p.evidence,
           p.status::text        AS status,
           p.sensitive,
           p.target_canonical_id::text AS target_canonical_id,
           p.rationale,
           c.type                AS conflict_type,
           c.disagreeing_fields  AS disagreeing_fields
      FROM proposals p
      JOIN conflicts c ON c.id = p.conflict_id
     WHERE p.id = :proposal_id
    """
)


def load_proposal(conn: Connection, proposal_id: int) -> ProposalRecord | None:
    """The proposal and the conflict it belongs to, or ``None``.

    The JOIN is not a convenience: ``proposals`` carries neither ``type`` nor
    the disagreeing path set, and both are inputs to the R15 classifier. A gate
    that read only the ``proposals`` row would have to trust the stored
    ``sensitive`` flag, which is the thing it is meant to re-derive.
    """
    row = conn.execute(_SELECT_PROPOSAL, {"proposal_id": proposal_id}).fetchone()
    if row is None:
        return None
    return ProposalRecord(
        id=row.id,
        conflict_id=row.conflict_id,
        conflict_type=row.conflict_type,
        fingerprint=row.fingerprint,
        disagreeing_fields=tuple(row.disagreeing_fields or ()),
        action=dict(row.action or {}),
        confidence=Decimal(str(row.confidence)),
        evidence=dict(row.evidence or {}),
        status=row.status,
        sensitive=bool(row.sensitive),
        target_canonical_id=row.target_canonical_id,
        rationale=row.rationale,
    )


# ===========================================================================
# gate 1 -- sensitivity. Runs FIRST and CANNOT SEE CONFIDENCE.
# ===========================================================================


@dataclass(frozen=True, slots=True)
class SensitivityVerdict:
    """The R15 gate's answer. ``cleared`` is the only way past it."""

    cleared: bool
    classification: Classification
    reason: str
    detail: str


def sensitivity_gate(
    *,
    conflict_type: str,
    disagreeing_fields: Sequence[str] = (),
    sensitive: bool = False,
    status: str = "",
) -> SensitivityVerdict:
    """R15, evaluated before anything else. **No confidence parameter.**

    This signature is the control. :func:`recon.sensitive.classify` already
    refuses to take a score; this refuses to take one too, so the whole
    sensitivity arm of the auto-apply decision is written in a vocabulary that
    cannot express "unless the score is high enough". A future edit that wanted
    to let a 0.99 through would have to add a parameter here first, which is a
    reviewable act rather than an accident.

    Three independent facts are consulted and **any** of them holds the
    proposal, so no single corrupted column can unlock one:

    * the committed classifier's verdict, re-derived here from the conflict type
      and its disagreeing paths (``recon.sensitive.classify``);
    * the stored ``proposals.sensitive`` column -- the one the ``KS002`` birth
      trigger reads;
    * the stored status: ``sensitive_hold`` is a hold whatever else says.

    They should never disagree. If they ever do, this returns *held* and says
    which one dissented, because the safe direction of a disagreement about
    sensitivity is not the one that writes.
    """
    classification = classify(conflict_type, disagreeing_fields)
    dissenting: list[str] = []
    if classification.sensitive:
        dissenting.append(f"classifier: {classification.reason}")
    if sensitive:
        dissenting.append("proposals.sensitive is true (the column KS002 reads)")
    if status == "sensitive_hold":
        dissenting.append("proposals.status is sensitive_hold")

    if dissenting:
        return SensitivityVerdict(
            cleared=False,
            classification=classification,
            reason="sensitive_hold",
            detail=(
                f"proposal for conflict type {conflict_type} touches a sensitive field and "
                "can never auto-apply, at any confidence including 1.0 (R15, contract SS6). "
                "It is forced to human review. Held because -- " + "; ".join(dissenting)
            ),
        )
    return SensitivityVerdict(
        cleared=True,
        classification=classification,
        reason="not_sensitive",
        detail=(
            f"{conflict_type} target {classification.target_path!r} is not in "
            "SENSITIVE_FIELDS; eligibility is considered next"
        ),
    )


# ===========================================================================
# gate 1b -- THE WRITE SET. What will this statement actually WRITE?
# ===========================================================================


@dataclass(frozen=True, slots=True)
class WritePath:
    """One path a statement **effectively writes**.

    ``leaf`` is the contract path the committed sets are written in -- the
    source-qualified string contract SS6 lists in ``SENSITIVE_FIELDS`` and in
    ``AUTO_APPLY_ELIGIBLE``. ``container`` is the top-level key of
    ``entities.current`` the write reaches that leaf THROUGH, or ``None`` when
    the leaf is itself a top-level key.

    The two exist separately because ``entities.current`` is **not flat**: it
    holds one nested object, ``survived`` (``recon.resolve.SURVIVED_PATHS``),
    whose nine members are themselves source-qualified contract paths and six of
    which are in ``SENSITIVE_FIELDS``. So::

        {"set": {"survived": {<nine paths, three of them replaced>}}}

    has exactly ONE top-level key -- ``survived`` -- which is in neither
    committed set, and a gate that reads *top-level keys* judges that key rather
    than the paths the statement lands on. That row was accepted with
    ``sensitive = false``, ``status = 'pending'``, and it replaces
    ``crm.contact.email``, ``appdb.student.status`` and
    ``appdb.enrollment.stage``. Judging the top-level key is the same shape of
    mistake as judging the conflict's classification instead of the write.
    """

    leaf: str
    container: str | None = None

    @property
    def display(self) -> str:
        """``leaf``, or ``container->leaf`` when it is reached through one."""
        return self.leaf if self.container is None else f"{self.container}->{self.leaf}"


def _same_json(left: Any, right: Any) -> bool:
    """Is this the same JSON value? ``jsonb``'s answer, not Python's.

    Exists for one divergence, and it is the kind that makes two implementations
    of one rule disagree exactly where nobody looks: Python considers ``True ==
    1`` and ``False == 0``, ``jsonb`` considers a boolean and a number different
    types and ``IS DISTINCT FROM`` says so. Without this, an action assigning
    ``true`` over a stored ``1`` is "no change" to :func:`effective_write_paths`
    and a change to ``keystone_effective_write_paths``, and the more permissive
    of the two is the hole. Numbers still compare numerically (``1`` and ``1.0``
    are one value in both), which is what ``jsonb`` does too.
    """
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return bool(left == right)


def effective_write_paths(
    assignments: Mapping[str, Any],
    current: Mapping[str, Any] | None = None,
) -> tuple[WritePath, ...]:
    """The paths ``OLD.current || assignments`` would EFFECTIVELY write.

    **One rule, and it is a rule about the VALUE, not about the statement:**

        the write set is the difference between the row the merge would PRODUCE
        and the row as it stands -- every leaf path whose value would differ,
        one level deep -- plus every top-level key the action NAMES with a
        non-object value, which is written whether or not it changes.

    That is the whole definition. It is written over the merged value rather
    than over the shapes of the two sides on purpose: guarding on shape is the
    mistake this function made twice. It descended only when the new value was a
    Mapping, so ``{"set": {"survived": "wiped"}}`` -- a scalar replacing the
    nine-key map, six of whose members are in ``SENSITIVE_FIELDS`` -- reported
    the single leaf ``survived`` and nothing else, and the erasure of six
    sensitive paths was judged as one unlisted key. ``merge_preview`` had made
    the identical mistake one round earlier by requiring BOTH sides to be
    objects. A list, a scalar, a JSON ``null``, an absent key and an object are
    not four special cases plus a default; they are five values, and asking
    what the merged row would DIFFER on answers all of them at once.

    Mechanically, per top-level key the action names -- and only those, because
    ``||`` is shallow and leaves every other key exactly as it was. The two
    clauses are independent and a key can satisfy both:

    * **the action assigns a non-object there**: the top-level key is a leaf its
      author chose to name, and it is written. Full stop -- no comparison, no
      look at the row. See the asymmetry below.
    * **either side is an object**: the members of the two sides are compared,
      and a member the merged row would give a different value, one it ADDS, and
      one it DROPS (``||`` replaces the map wholesale, so an omitted member is
      an erasure) are each written as ``container->leaf``. A member carried
      through unchanged is not. A non-object side contributes no members, which
      is exactly what makes ``{"survived": "wiped"}`` report the key AND all
      nine erasures, and ``{"survived": {...}}`` over a scalar, a list, a JSON
      ``null`` or an absent key report every member it introduces -- out of one
      comparison instead of four branches.
    * an assigned OBJECT the merge would change nothing about reports the
      container key itself, so the refusal names something real rather than
      reporting an empty write set the ``writes_a_field`` check would then wave
      through.

    **The asymmetry, which is deliberate and is not a shape guard.** A top-level
    key is written whenever it is *named*: its author could simply have omitted
    it, so naming it is a write even when the value assigned equals the value
    already there. A nested member is written only when the merge would change
    it, because contract SS5's shallow-merge rule gives its author no such
    choice -- a fix that writes one member of a map must carry the WHOLE map, so
    counting carried siblings would make every possible write to ``survived`` a
    sensitive write and no member of it could ever be fixed. The asymmetry is
    between *naming* and *carrying*, not between one shape and another.

    ``current`` is the canonical row's stored value, or ``None`` when it is not
    available (a pure-gate call with no entity in hand). ``None`` is the
    **conservative** answer everywhere: with no row to diff against, every named
    key and every member it carries counts as written, so a caller who forgets
    the row can only widen the set, never narrow it.

    **One level, deliberately, and here is what happens below it.**
    ``entities.current`` has exactly one nested level -- ``survived`` -- so this
    descends exactly one. A doubly-nested action
    (``{"set": {"survived": {"x": {"crm.contact.email": ...}}}}``) presents the
    leaf ``x``, which is on neither committed list and is refused by the
    allow-list arm, and ``merge_preview`` refuses it again on both apply paths
    -- for erasing the other eight members, and for INTRODUCING a member the
    nested map did not have. So the deeper case is *refused*, not *inspected*:
    it is covered by the allow-list being an allow-list rather than by this
    function understanding it, which is the honest way round given no committed
    shape reaches there.
    """
    written: list[WritePath] = []
    for key in sorted(assignments):
        new_value = assignments[key]
        old_value = None if current is None else current.get(key)
        after: Mapping[str, Any] | None = new_value if isinstance(new_value, Mapping) else None
        before: Mapping[str, Any] | None = old_value if isinstance(old_value, Mapping) else None
        if after is None:
            # The action assigns a NON-object here, so this key is a leaf its
            # author chose to name -- and naming it is writing it, whatever the
            # value is worth today and whatever shape the row holds. Emitted
            # BEFORE the members, so a scalar landing on an object reports both
            # the key it re-points and every member that assignment erases.
            written.append(WritePath(key))
        if after is None and before is None:
            continue
        members = [
            sub
            for sub in sorted(set(after or {}) | set(before or {}))
            if sub not in (after or {})
            or sub not in (before or {})
            or not _same_json((after or {})[sub], (before or {})[sub])
        ]
        if members:
            written.extend(WritePath(sub, container=key) for sub in members)
        elif after is not None:
            # The action assigns an object and the merge would change nothing
            # inside it: report the container, so the refusal names something
            # real instead of an empty write set `writes_a_field` waves through.
            written.append(WritePath(key))
    return tuple(sorted(written, key=lambda path: path.display))


@dataclass(frozen=True, slots=True)
class WriteSetVerdict:
    """R15/R24 evaluated against what the ACTION WRITES. No confidence.

    Why this exists as a gate of its own, and why the classification is not it
    ------------------------------------------------------------------------
    :func:`sensitivity_gate` answers **"what kind of conflict is this?"** -- it
    consults ``conflicts.type`` and ``conflicts.disagreeing_fields`` and hands
    them to :func:`recon.sensitive.classify`, which is a pure function of the
    committed fix target for that type. That is a real question and its answer is
    a real control, but it is not the question R15 asks. R15 forbids a *write* to
    a sensitive field, and the only thing in the row that says what will be
    written is ``action->'set'``.

    The two questions came apart in exactly the way they can: a proposal on a
    ``C2`` conflict -- an approved case type whose committed template writes
    ``payments.payment.external_ref`` -- carrying
    ``action = {"set": {"crm.contact.email": "..."}}`` at confidence 0.99 was
    classified *not sensitive* (because C2's template is eligible) and
    **auto-applied**, writing a ``SENSITIVE_FIELDS`` path (contract SS12 D-7
    classifies the billing email sensitive precisely so C4 cannot be re-targeted
    to escape). Every condition R24 evaluated was satisfied; none of them had
    looked at what the statement writes.

    They then came apart a second time, one level down. Reading the **top-level
    keys** of ``action->'set'`` is not reading the write set either:
    ``{"set": {"survived": {...}}}`` names one key, ``survived``, and lands on
    nine source-qualified paths inside it. So the subject of this verdict is
    :func:`effective_write_paths` -- every leaf on which the MERGED row would
    differ from the row as it stands, one level deep, plus every top-level key
    the action names with a non-object value -- and never the key list.

    It is an **allow-list**: every effective path's leaf must be in
    ``AUTO_APPLY_ELIGIBLE`` and none may be in ``SENSITIVE_FIELDS``. A path in
    neither set is refused -- contract SS6: *"A field path in neither set is not
    auto-applyable: eligibility is an allowlist, not the complement of
    ``SENSITIVE_FIELDS``."* Defaulting an unrecognised path to admitted is how
    the next field silently becomes auto-appliable.

    **No confidence parameter**, for the same reason :func:`sensitivity_gate` has
    none: R15 holds at every score including 1.0, so the arm of the decision that
    enforces it is written in a vocabulary that cannot express a threshold.

    An **empty** write set clears this gate and is refused downstream by the
    ``writes_a_field`` check: an evidence-only proposal writes no path, so there
    is no path for this gate to object to, and naming the right refusal reason is
    the point of having named checks at all.
    """

    cleared: bool
    #: Every effective write path, sorted, rendered as ``container->leaf`` where
    #: one was traversed. The whole subject of the verdict.
    paths: tuple[str, ...]
    #: The distinct contract paths those writes land on, sorted -- the strings
    #: contract SS6's sets and ``FIX_TARGETS`` are written in.
    leaves: tuple[str, ...]
    #: The subset in ``SENSITIVE_FIELDS`` -- what R15 forbids writing.
    sensitive_paths: tuple[str, ...]
    #: The subset on neither committed list -- refused by the allow-list rule.
    unlisted_paths: tuple[str, ...]
    reason: str
    detail: str


def write_set_gate(
    assignments: Mapping[str, Any],
    current: Mapping[str, Any] | None = None,
) -> WriteSetVerdict:
    """Judge what the action WRITES, nested paths included. **No confidence.**

    ``current`` is the canonical row this action would be merged onto, when the
    caller holds one. It is not a score and it cannot admit anything a call
    without it would refuse -- see :func:`effective_write_paths`, where every
    branch is conservative in its absence.

    Two refusals, reported separately because they are different failures and a
    refusal reason that cannot be attributed is a refusal that teaches nothing:

    ``sensitive_write``
        at least one effective path lands on a ``SENSITIVE_FIELDS`` path. This is
        R15 itself, read off the statement rather than off the conflict's
        classification -- and off the *paths*, not off the key list.
    ``write_off_allowlist``
        nothing sensitive is written but at least one effective path is absent
        from ``AUTO_APPLY_ELIGIBLE``. Contract SS6 makes eligibility an
        allow-list, so an unrecognised path is refused, never admitted by
        default.

    ``sensitive_write`` wins when both hold: it is the stronger statement about
    the same row, and R15 is the requirement that admits no exception.
    """
    written = effective_write_paths(assignments, current)
    paths = tuple(path.display for path in written)
    leaves = tuple(sorted({path.leaf for path in written}))
    sensitive = tuple(path.display for path in written if path.leaf in SENSITIVE_FIELDS)
    unlisted = tuple(path.display for path in written if path.leaf not in AUTO_APPLY_ELIGIBLE)

    if sensitive:
        return WriteSetVerdict(
            cleared=False,
            paths=paths,
            leaves=leaves,
            sensitive_paths=sensitive,
            unlisted_paths=unlisted,
            reason="sensitive_write",
            detail=(
                f"the action WRITES {list(sensitive)}, which contract SS6 lists in "
                "SENSITIVE_FIELDS: R15 forbids a sensitive-field write at any confidence "
                "including 1.0, whatever the conflict is classified as, and whether the "
                "path is a top-level key or reached through a nested object. The whole "
                f"write set is {list(paths)}"
            ),
        )
    if unlisted:
        return WriteSetVerdict(
            cleared=False,
            paths=paths,
            leaves=leaves,
            sensitive_paths=(),
            unlisted_paths=unlisted,
            reason="write_off_allowlist",
            detail=(
                f"the action WRITES {list(unlisted)}, which is on neither committed list. "
                "Contract SS6: eligibility is an allowlist, not the complement of "
                f"SENSITIVE_FIELDS, so an unlisted path is refused. The whole write set is "
                f"{list(paths)}"
            ),
        )
    return WriteSetVerdict(
        cleared=True,
        paths=paths,
        leaves=leaves,
        sensitive_paths=(),
        unlisted_paths=(),
        reason="write_set_eligible",
        detail=(
            f"every path the action WRITES ({list(paths)}) is on contract SS6's "
            "AUTO_APPLY_ELIGIBLE allowlist and none is in SENSITIVE_FIELDS"
            if paths
            else "the action writes no path at all; the writes_a_field check covers it"
        ),
    )


@dataclass(frozen=True, slots=True)
class EligibilityClearance:
    """Proof that BOTH R15 gates cleared this proposal.

    The eligibility gates -- confidence included -- take one of these and are
    unreachable without it. It cannot be constructed around a sensitive
    classification **or around a hostile write set**: ``__post_init__`` refuses
    both, so "skip the R15 gates and call the confidence gate directly" is not a
    thing a caller can do by forgetting a line.

    Two fields, because R15 has two subjects and the gate that judged only the
    first one auto-applied a write to ``crm.contact.email``. ``classification``
    answers *what kind of conflict is this*; ``write_set`` answers *what will
    this statement write*. Neither is the other, and the confidence floor is
    downstream of both.
    """

    classification: Classification
    write_set: WriteSetVerdict

    def __post_init__(self) -> None:
        if self.classification.sensitive:
            raise ValueError(
                f"cannot clear {self.classification.target_path!r} for eligibility: it is "
                "classified sensitive, and a sensitive proposal never reaches the "
                "confidence gate (R15)"
            )
        if not self.write_set.cleared:
            raise ValueError(
                f"cannot clear a write set of {list(self.write_set.paths)} for eligibility: "
                f"{self.write_set.detail}. A proposal whose ACTION writes a forbidden path "
                "never reaches the confidence gate (R15)"
            )


# ===========================================================================
# gate 2 -- R24's four conditions
# ===========================================================================


@dataclass(frozen=True, slots=True)
class GateCheck:
    """One named condition and whether it held, for the audit row and the API."""

    name: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"check": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class AutoApplyDecision:
    """R24's verdict on one proposal, with every condition it evaluated."""

    proposal_id: int
    allowed: bool
    reason: str
    detail: str
    checks: tuple[GateCheck, ...] = ()
    classification: Classification | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "allowed": self.allowed,
            "reason": self.reason,
            "detail": self.detail,
            "checks": [check.as_dict() for check in self.checks],
        }

    @property
    def failed(self) -> tuple[GateCheck, ...]:
        return tuple(check for check in self.checks if not check.passed)


@dataclass(frozen=True, slots=True)
class RollbackPath:
    """Whether this proposal's write could be undone if it were taken.

    R24 requires "a recorded rollback path". For this schema that is a
    *checkable* property, not a promise: the reversal leg needs the entity row
    to exist now (so an ``applied`` event can capture its ``before``) and both
    single-use citation indexes to be unspent (``uq_proposal_events_applied_once``
    and its ``rolled_back`` twin). If either is spent, the write could be made
    and never reversed.
    """

    known: bool
    detail: str
    entity_exists: bool = False
    applied_events: int = 0
    rolled_back_events: int = 0


_SELECT_ROLLBACK_PATH = text(
    """
    SELECT (SELECT count(*) FROM entities e
             WHERE e.canonical_id = CAST(:canonical_id AS uuid))        AS entity_rows,
           (SELECT count(*) FROM proposal_events pe
             WHERE pe.proposal_id = :proposal_id AND pe.event = 'applied')     AS applied_events,
           (SELECT count(*) FROM proposal_events pe
             WHERE pe.proposal_id = :proposal_id AND pe.event = 'rolled_back') AS reversed_events
    """
)


def rollback_path(conn: Connection, record: ProposalRecord) -> RollbackPath:
    """Probe the database for R24's "recorded rollback path"."""
    if not record.target_canonical_id:
        return RollbackPath(
            known=False,
            detail=(
                "the proposal names no target_canonical_id, so an apply would have no "
                "canonical row to capture a before value from"
            ),
        )
    row = conn.execute(
        _SELECT_ROLLBACK_PATH,
        {"canonical_id": record.target_canonical_id, "proposal_id": record.id},
    ).one()
    entity_exists = row.entity_rows == 1
    if not entity_exists:
        return RollbackPath(
            known=False,
            detail=(
                f"target_canonical_id {record.target_canonical_id} names no entities row "
                "(a legacy proposal backfilled with the nil UUID authorises nothing)"
            ),
        )
    if row.applied_events:
        return RollbackPath(
            known=False,
            detail=(
                "this proposal already has an applied event: the citation is spent and a "
                "second apply is refused by uq_proposal_events_applied_once"
            ),
            entity_exists=True,
            applied_events=row.applied_events,
            rolled_back_events=row.reversed_events,
        )
    if row.reversed_events:
        return RollbackPath(
            known=False,
            detail="this proposal already has a rolled_back event: the reversal leg is spent",
            entity_exists=True,
            rolled_back_events=row.reversed_events,
        )
    return RollbackPath(
        known=True,
        detail=(
            "the entity row exists and both single-use citation legs are unspent: an apply "
            "will record before/after in proposal_events and rollback_proposal() restores "
            "the stored before column verbatim"
        ),
        entity_exists=True,
    )


@dataclass(frozen=True, slots=True)
class MergePreview:
    """What ``OLD.current || action->'set'`` would do, computed in Python.

    Used for *reporting and refusing*, never for writing: the write itself is
    computed in SQL (see the module docstring). ``erased`` is the shallow-merge
    trap -- nested keys that exist today and would be gone afterwards --
    and ``collapsed`` is the SHAPE CHANGE that produced them wholesale.
    """

    assignments: Mapping[str, Any]
    erased: tuple[str, ...]
    #: Keys whose current value is a nested object and whose new value is not.
    #: The merge replaces the object outright, so the nesting is gone even when
    #: the object was empty and ``erased`` therefore has nothing to name.
    collapsed: tuple[str, ...] = ()
    #: Members the action would ADD to a nested object that already exists --
    #: the look-alike hole. ``survived``'s membership is the closed set
    #: ``recon.resolve.SURVIVED_PATHS``, and every reader projects the map
    #: WHOLE, so a member whose key differs from a genuine one only by case,
    #: by surrounding whitespace or by a unicode homoglyph (``crm.contact.ema``
    #: + U+0131 + ``l``, a dotless i) is rendered by the entity endpoints beside the
    #: real one, carrying an attacker's value under a name a human reads as the
    #: real path. R24's gate already refuses it -- the added member's leaf is on
    #: neither committed list, so it is ``write_off_allowlist`` -- but a human
    #: pressing APPLY is not behind that gate, and this is the guard that is.
    #: Reported as ``"<key>.<subkey>"``, like ``erased``.
    introduced: tuple[str, ...] = ()

    @property
    def safe(self) -> bool:
        return not self.erased and not self.collapsed and not self.introduced


def merge_preview(current: Mapping[str, Any], assignments: Mapping[str, Any]) -> MergePreview:
    """What a shallow merge of ``assignments`` would destroy. Guards on SHAPE.

    ``jsonb ||`` replaces a key's value wholesale, so the question is not "are
    both sides objects?" but "does this key stop being the object it is?".

    Two ways it can, and the second one used to be reported *safe*:

    * **object over object.** Every old sub-key missing from the new object
      disappears -- writing one field of ``current['survived']`` erases the other
      eight unless the action carries the whole map. Reported as
      ``"<key>.<subkey>"``.
    * **anything-not-an-object over an object.** ``{"set": {"survived": "wiped"}}``
      replaces the nine-key map with a string. The old guard asked for **both**
      sides to be Mappings before it looked, so this reported no erasure at all,
      ``apply_proposal`` took the write, and a nine-key nested object became the
      string ``"wiped"``. Every sub-key is erased here, and the key itself is
      additionally reported in ``collapsed`` -- which is what makes an *empty*
      old object being replaced by a scalar unsafe too, even though it erases no
      named sub-key.

    And a third way, which destroys nothing and is why ``safe`` is not simply
    "erases nothing":

    * **a member the nested object did not have.** ``survived``'s membership is
      the closed set ``recon.resolve.SURVIVED_PATHS``, and the entity endpoints
      project the map WHOLE -- every member it happens to contain. So an action
      that carries all nine genuine members and ADDS a tenth whose key differs
      from a real one only by case (``CRM.contact.email``), by surrounding
      whitespace (``"crm.contact.email "``) or by a unicode homoglyph
      (``crm.contact.ema`` + U+0131 + ``l``, a dotless i) is admitted by every rule that judges
      *paths*: the added leaf is on neither committed list, so R24's gate refuses
      it -- but a reviewer pressing APPLY by hand is not behind R24's gate, and
      after such an apply the reader's own projection shows the attacker's value
      sitting beside the genuine one under a name a human reads as the genuine
      one. ``introduced`` names it and ``apply_proposal`` refuses it on BOTH
      paths, which is the same reason this guard lives here rather than in the
      gate.

      Bounded honestly: this is a rule about ADDING to a nested object that
      already exists. An action that turns a key which held no object into one
      is a different act -- it introduces no sibling to be confused with -- and
      is judged by the write-set gate, which sees every member such an action
      carries as a written path.

    A key whose old value is not an object is not this function's subject: a
    scalar replacing a scalar loses nothing that ``entities.current`` was holding
    nested, and R24's write-set gate is what decides whether the path may be
    written at all.
    """
    erased: list[str] = []
    collapsed: list[str] = []
    introduced: list[str] = []
    for key, new_value in assignments.items():
        old_value = current.get(key)
        if not isinstance(old_value, Mapping):
            continue
        if isinstance(new_value, Mapping):
            erased.extend(f"{key}.{sub}" for sub in old_value if sub not in new_value)
            introduced.extend(f"{key}.{sub}" for sub in new_value if sub not in old_value)
        else:
            collapsed.append(key)
            erased.extend(f"{key}.{sub}" for sub in old_value)
    return MergePreview(
        assignments=dict(assignments),
        erased=tuple(sorted(erased)),
        collapsed=tuple(sorted(collapsed)),
        introduced=tuple(sorted(introduced)),
    )


def _evidence_checks(record: ProposalRecord) -> tuple[GateCheck, ...]:
    """R24's "complete evidence", read off the packet the reconciler persisted."""
    evidence = record.evidence
    schema = evidence.get("schema")
    if schema != EVIDENCE_SCHEMA:
        return (
            GateCheck(
                "complete_evidence",
                False,
                f"evidence carries schema {schema!r}, not {EVIDENCE_SCHEMA!r}: this gate "
                "cannot judge the completeness of a packet it does not know",
            ),
        )

    completeness = evidence.get("completeness") or {}
    signals = ((evidence.get("confidence") or {}).get("signals")) or {}
    incomplete = tuple(completeness.get("incomplete_sources") or ())
    nulls = tuple(completeness.get("null_observed_values") or ())
    reasons = tuple(signals.get("partial_evidence_reasons") or ())
    partial = bool(signals.get("partial_evidence"))

    complete = not (incomplete or nulls or reasons or partial)
    detail = (
        "the packet reports no incomplete source, no null observed value and no "
        "partial-evidence reason"
        if complete
        else (
            "the packet is partial: "
            f"incomplete_sources={list(incomplete)}, null_observed_values={list(nulls)}, "
            f"partial_evidence={partial}, reasons={list(reasons)}"
        )
    )
    return (
        GateCheck("complete_evidence", complete, detail),
        GateCheck(
            "writes_a_field",
            not record.evidence_only,
            "the action assigns "
            + (
                f"{sorted(record.assignments)}"
                if not record.evidence_only
                else "nothing: contract SS6 makes this an evidence-only proposal, escalated "
                "for human review rather than applied"
            ),
        ),
    )


def auto_apply_decision(
    record: ProposalRecord,
    path: RollbackPath,
    current: Mapping[str, Any] | None = None,
) -> AutoApplyDecision:
    """R24, in R24's order. **The two R15 gates run first, and see no score.**

    The structure, which is the point:

    1. :func:`sensitivity_gate` is called with the conflict type, the disagreeing
       paths, the stored ``sensitive`` flag and the status -- and with **no
       score of any kind**. If it holds, this function returns *here*, before any
       expression in this module has read ``record.confidence``;
    2. :func:`write_set_gate` is called with the action's EFFECTIVE write paths
       -- and with no score either. It answers the different question: *what will
       this statement WRITE?* If any written path is in ``SENSITIVE_FIELDS`` or
       absent from ``AUTO_APPLY_ELIGIBLE``, this function returns *here* too,
       still without having read ``record.confidence``;
    3. only then is an :class:`EligibilityClearance` minted -- it requires the
       verdicts of BOTH gates -- and only a holder of one can call
       :func:`_eligibility_checks`, where the 0.95 floor lives.

    ``current`` is the canonical row the action would be merged onto, when the
    caller has one (:func:`evaluate_auto_apply` reads it). It is what lets step 2
    tell a nested member this statement REPLACES from one it merely carries
    through unchanged, and it is conservative when absent: without it every
    member of a nested object counts as written. It is not a score, and no branch
    below reads a score before step 2 has returned.

    Step 2 is not a duplicate of step 1. Step 1 classifies the CONFLICT, from
    ``conflicts.type`` and its disagreeing paths; step 2 inspects the ACTION.
    A gate with only step 1 auto-applied a ``C2`` proposal carrying
    ``{"set": {"crm.contact.email": ...}}`` at 0.99: an approved case type, a
    non-sensitive classification, and a write to a path contract SS12 D-7 pins
    sensitive. Classification and write set are different questions and only the
    second is the one R15 forbids.

    So "a proposal R15 forbids cannot reach the confidence gate at any score
    including 1.0" is a property of the call graph, not of a comparison. See
    ``tests/apply/test_structural_order.py``, which proves it for both gates by
    handing this function a record whose ``confidence`` attribute raises on
    access.
    """
    verdict = sensitivity_gate(
        conflict_type=record.conflict_type,
        disagreeing_fields=record.disagreeing_fields,
        sensitive=record.sensitive,
        status=record.status,
    )
    if not verdict.cleared:
        return AutoApplyDecision(
            proposal_id=record.id,
            allowed=False,
            reason=verdict.reason,
            detail=verdict.detail,
            checks=(GateCheck("not_sensitive", False, verdict.detail),),
            classification=verdict.classification,
        )

    writes = write_set_gate(record.assignments, current)
    if not writes.cleared:
        return AutoApplyDecision(
            proposal_id=record.id,
            allowed=False,
            reason=writes.reason,
            detail=writes.detail,
            checks=(
                GateCheck("not_sensitive", True, verdict.detail),
                GateCheck("write_set_eligible", False, writes.detail),
            ),
            classification=verdict.classification,
        )

    clearance = EligibilityClearance(verdict.classification, writes)
    checks = (
        GateCheck("not_sensitive", True, verdict.detail),
        GateCheck("write_set_eligible", True, writes.detail),
        *_eligibility_checks(clearance, record, path),
    )
    failed = tuple(check for check in checks if not check.passed)
    if failed:
        return AutoApplyDecision(
            proposal_id=record.id,
            allowed=False,
            reason=failed[0].name,
            detail="; ".join(check.detail for check in failed),
            checks=checks,
            classification=verdict.classification,
        )
    return AutoApplyDecision(
        proposal_id=record.id,
        allowed=True,
        reason="eligible",
        detail="every R24 condition holds",
        checks=checks,
        classification=verdict.classification,
    )


def _eligibility_checks(
    clearance: EligibilityClearance,
    record: ProposalRecord,
    path: RollbackPath,
) -> tuple[GateCheck, ...]:
    """R24's remaining conditions. Unreachable without a :class:`EligibilityClearance`.

    ``clearance`` is not decoration and it is not unused: it is the parameter
    that makes this function uncallable on a proposal the sensitivity gate has
    not cleared, and its ``__post_init__`` re-checks the classification on the
    way in. **This is the only function in the package that reads
    ``AUTO_APPLY_CONFIDENCE_FLOOR``.**

    ``write_matches_fix_target`` is where the two halves of the clearance are
    finally compared with each other. Conditions 2 and 4 each pass a ``C2``
    carrying ``{"set": {"crm.contact.grade": "7"}}`` -- an eligible,
    non-sensitive path on an approved case type -- and the row is still wrong:
    C2's committed template writes ``payments.payment.external_ref`` and nothing
    else. An eligible path the template does not write is a re-targeting, which
    is how a conflict of one type acquires another type's fix, so the write set
    must equal the classification's own target.
    """
    target = clearance.classification.target_path
    approved_type = record.conflict_type in AUTO_APPLY_CASE_TYPES
    on_allowlist = clearance.classification.auto_apply_eligible_path
    off_template = tuple(leaf for leaf in clearance.write_set.leaves if leaf != target)
    high_enough = record.confidence >= AUTO_APPLY_CONFIDENCE_FLOOR
    return (
        GateCheck(
            "approved_case_type",
            approved_type,
            f"{record.conflict_type} is "
            + ("" if approved_type else "not ")
            + f"an approved case type {sorted(AUTO_APPLY_CASE_TYPES)} (contract SS6's "
            "fix-target table classifies its template eligible)",
        ),
        GateCheck(
            "target_on_allowlist",
            on_allowlist,
            f"target {target!r} is "
            + ("on" if on_allowlist else "not on")
            + " contract SS6's AUTO_APPLY_ELIGIBLE allowlist",
        ),
        GateCheck(
            "write_matches_fix_target",
            not off_template,
            (
                f"the action writes {list(clearance.write_set.leaves)}, which is "
                f"{'exactly' if not off_template else 'NOT'} contract SS6's committed fix "
                f"target for a {record.conflict_type} of this shape ({target!r})"
                + (
                    ""
                    if not off_template
                    else f"; {list(off_template)} is off the template. An eligible path "
                    "the template does not write is still a re-targeting: it is how a "
                    "conflict of one type acquires another type's fix (SS6, SS12 D-7)"
                )
            ),
        ),
        GateCheck(
            "confidence_floor",
            high_enough,
            f"confidence {record.confidence} "
            + (">=" if high_enough else "<")
            + f" {AUTO_APPLY_CONFIDENCE_FLOOR} (R24)",
        ),
        *_evidence_checks(record),
        GateCheck("rollback_path", path.known, path.detail),
        GateCheck(
            "status_appliable",
            record.status == _APPLIABLE_STATUS,
            f"status is {record.status!r}; apply_writer may only move "
            f"{_APPLIABLE_STATUS!r} -> 'applied' (SQLSTATE KS004)",
        ),
    )


_SELECT_ENTITY_CURRENT = text(
    """
    SELECT e.current
      FROM entities e
     WHERE e.canonical_id = CAST(:canonical_id AS uuid)
    """
)


def entity_current(conn: Connection, canonical_id: str | None) -> Mapping[str, Any] | None:
    """The canonical row's stored value, or ``None`` when there is not one.

    Read for the write-set gate, which needs it to tell a nested member a
    statement REPLACES from one it carries through unchanged. ``None`` is the
    conservative input to that gate, never the permissive one, so a missing row
    can only widen the effective write set.
    """
    if not canonical_id:
        return None
    row = conn.execute(_SELECT_ENTITY_CURRENT, {"canonical_id": canonical_id}).fetchone()
    if row is None or not isinstance(row.current, Mapping):
        return None
    return dict(row.current)


def evaluate_auto_apply(conn: Connection, proposal_id: int) -> AutoApplyDecision:
    """Load, probe and decide. The read-only half of :func:`auto_apply`."""
    record = load_proposal(conn, proposal_id)
    if record is None:
        return AutoApplyDecision(
            proposal_id=proposal_id,
            allowed=False,
            reason="not_found",
            detail=f"no proposal {proposal_id}",
        )
    return auto_apply_decision(
        record,
        rollback_path(conn, record),
        entity_current(conn, record.target_canonical_id),
    )


# ===========================================================================
# the canonical write
# ===========================================================================


@dataclass(frozen=True)
class ApplyResult:
    """What one apply did, in the shape the audit row and the API both want."""

    proposal_id: int
    canonical_id: str
    event_id: int
    before: str
    after: str
    before_digest: str
    after_digest: str
    auto: bool
    decision: AutoApplyDecision | None = None
    assignments: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """**Digests, never values.** The canonical row carries personal data;
        this dict reaches the audit log and the HTTP response."""
        return {
            "proposal_id": self.proposal_id,
            "canonical_id": self.canonical_id,
            "event_id": self.event_id,
            "before_digest": self.before_digest,
            "after_digest": self.after_digest,
            "changed_paths": sorted(self.assignments),
            "auto": self.auto,
        }


@dataclass(frozen=True)
class RollbackResult:
    """What one reversal did. ``restored_digest == applied_before_digest`` is the
    byte-identity claim, and it is asserted before the transaction commits."""

    proposal_id: int
    canonical_id: str
    event_id: int
    applied_before_digest: str
    restored_digest: str
    pre_rollback_digest: str

    @property
    def byte_identical(self) -> bool:
        return self.restored_digest == self.applied_before_digest

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "canonical_id": self.canonical_id,
            "event_id": self.event_id,
            "applied_before_digest": self.applied_before_digest,
            "restored_digest": self.restored_digest,
            "byte_identical": self.byte_identical,
        }


def entity_digest(current_text: str) -> str:
    """``sha256`` of a canonical row's ``current::text``.

    The argument is the **text Postgres rendered from the stored jsonb**, never
    a Python re-serialization: jsonb's text output is a deterministic function of
    the stored value, so two digests are equal exactly when the two stored values
    are byte-identical. Digesting ``json.dumps(row.current)`` instead would
    compare Python's rendering of a parse of the value, which can differ from the
    stored bytes (``1`` vs ``1.0``, ``\\u00e9`` vs ``é``) in both directions.
    """
    return hashlib.sha256(current_text.encode("utf-8")).hexdigest()


_LOCK_ENTITY = text(
    """
    SELECT e.current::text AS current_text
      FROM entities e
     WHERE e.canonical_id = CAST(:canonical_id AS uuid)
       FOR UPDATE
    """
)

#: The ledger row, with ``before`` and ``after`` computed **from the row and the
#: cited proposal**. ``txid``/``ts``/``id`` are omitted so their defaults apply --
#: ``apply_writer``'s column grant does not name them (migration 0004).
_INSERT_APPLIED_EVENT = text(
    """
    INSERT INTO proposal_events (proposal_id, canonical_id, event, before, after, actor)
    SELECT p.id,
           e.canonical_id,
           'applied',
           e.current,
           e.current || coalesce(p.action -> 'set', '{}'::jsonb),
           :actor
      FROM proposals p
      JOIN entities e ON e.canonical_id = p.target_canonical_id
     WHERE p.id = :proposal_id
    RETURNING id, before::text AS before_text, after::text AS after_text
    """
)

#: The canonical write. The value is the same SQL expression the ledger row used
#: and the same one ``KS010`` requires; nothing here chooses content.
_APPLY_ENTITY = text(
    """
    UPDATE entities e
       SET current = e.current || coalesce(
               (SELECT p.action -> 'set' FROM proposals p WHERE p.id = :proposal_id),
               '{}'::jsonb),
           updated_at = now()
     WHERE e.canonical_id = CAST(:canonical_id AS uuid)
    RETURNING e.current::text AS current_text
    """
)

_MOVE_STATUS = text(
    """
    UPDATE proposals
       SET status = CAST(:next_status AS proposal_status)
     WHERE id = :proposal_id
       AND status = CAST(:from_status AS proposal_status)
    RETURNING id
    """
)

#: The reversal ledger row. ``after`` is the *stored* ``before`` column of this
#: proposal's ``applied`` event -- the bytes themselves, never a reconstruction.
_INSERT_ROLLED_BACK_EVENT = text(
    """
    INSERT INTO proposal_events (proposal_id, canonical_id, event, before, after, actor)
    SELECT p.id, e.canonical_id, 'rolled_back', e.current, ap.before, :actor
      FROM proposals p
      JOIN entities e ON e.canonical_id = p.target_canonical_id
      JOIN proposal_events ap ON ap.proposal_id = p.id AND ap.event = 'applied'
     WHERE p.id = :proposal_id
    RETURNING id, before::text AS before_text, after::text AS after_text
    """
)

_RESTORE_ENTITY = text(
    """
    UPDATE entities e
       SET current = (SELECT ap.before
                        FROM proposal_events ap
                       WHERE ap.proposal_id = :proposal_id AND ap.event = 'applied'),
           updated_at = now()
     WHERE e.canonical_id = CAST(:canonical_id AS uuid)
    RETURNING e.current::text AS current_text
    """
)


def _require_appliable(record: ProposalRecord) -> None:
    """Everything the canonical write needs that is not R24's gate.

    These hold for the manual path as well as the automatic one: a human may
    approve a ``sensitive_hold`` proposal and then apply it (R15 forces human
    review, it does not forbid the fix forever), but nobody may apply a proposal
    that is not approved, names no entity, or writes no field.
    """
    if record.status != _APPLIABLE_STATUS:
        raise ApplyError(
            "not_approved",
            f"proposal {record.id} is {record.status!r}: only an {_APPLIABLE_STATUS!r} "
            "proposal may be applied, and only review_writer may approve one "
            "(SQLSTATE KS004)",
            proposal_id=record.id,
        )
    if not record.target_canonical_id:
        raise ApplyError(
            "no_target",
            f"proposal {record.id} names no target_canonical_id, so it authorises no "
            "canonical row (migration 0005 RULING 3)",
            proposal_id=record.id,
        )
    if record.evidence_only:
        raise ApplyError(
            "evidence_only",
            f"proposal {record.id} has an empty action: contract SS6 makes it an "
            "evidence-only proposal for human review, and applying it would write a "
            "reversal record for a write that changes nothing",
            proposal_id=record.id,
        )


def apply_proposal(
    proposal_id: int,
    *,
    actor: str = APPLY_ACTOR,
    conn: Connection | None = None,
    auto: bool = False,
    decision: AutoApplyDecision | None = None,
) -> ApplyResult:
    """Write one approved proposal onto the canonical layer, as ``apply_writer``.

    One transaction, three statements plus the audit row, in the order the
    deferred citation trigger will judge at COMMIT:

    1. lock the canonical row (``FOR UPDATE``) and read its ``current::text`` --
       the *before* digest, taken from the stored bytes;
    2. INSERT the ``applied`` ``proposal_events`` row, with ``before`` and
       ``after`` computed in SQL from that row and the cited proposal;
    3. move the proposal ``approved -> applied`` (``KS004``), which stamps
       ``status_txid`` so the deferred trigger can tell "being applied now" from
       "applied yesterday";
    4. UPDATE ``entities`` with the same expression ``KS010`` requires.

    ``conn`` is injectable so a test can drive this inside a transaction it rolls
    back; the default opens an ``apply_writer`` connection, which is the only
    principal the canonical write boundary admits.
    """
    if conn is None:
        with role_connection(ROLE_APPLY_WRITER) as owned:
            return apply_proposal(
                proposal_id, actor=actor, conn=owned, auto=auto, decision=decision
            )

    record = load_proposal(conn, proposal_id)
    if record is None:
        raise ApplyError("not_found", f"no proposal {proposal_id}", proposal_id=proposal_id)
    _require_appliable(record)
    canonical_id = record.target_canonical_id
    assert canonical_id is not None  # _require_appliable

    locked = conn.execute(_LOCK_ENTITY, {"canonical_id": canonical_id}).fetchone()
    if locked is None:
        raise ApplyError(
            "entity_missing",
            f"proposal {proposal_id} targets entity {canonical_id}, which does not exist",
            proposal_id=proposal_id,
        )
    before_text = locked.current_text
    before_value = json.loads(before_text)
    if auto:
        # The gate judged the write set against the row as it was read a moment
        # ago, OUTSIDE this FOR UPDATE. Between those two reads another committed
        # apply could have moved a nested member, turning a carried-unchanged
        # sibling into a replacement -- i.e. into a write the gate never saw. So
        # R15's write-set question is asked again here, against the LOCKED value,
        # and only for the unattended path: a human-approved manual apply of a
        # sensitive path is legitimate (R15 forces review, it does not forbid the
        # fix).
        under_lock = write_set_gate(record.assignments, before_value)
        if not under_lock.cleared:
            raise ApplyError(
                "write_set_refused_under_lock",
                f"proposal {proposal_id} cleared R24's write-set gate but not against the "
                f"locked canonical row: {under_lock.detail}",
                proposal_id=proposal_id,
            )
    preview = merge_preview(before_value, record.assignments)
    if preview.erased or preview.collapsed:
        raise ApplyError(
            "shallow_merge_would_erase",
            f"proposal {proposal_id}'s action replaces a nested object wholesale: the "
            f"jsonb merge KS010 requires would erase {list(preview.erased)}"
            + (
                f" and collapse {list(preview.collapsed)} from an object to a non-object"
                if preview.collapsed
                else ""
            )
            + ". A fix that writes one member of a nested map must carry the whole map",
            proposal_id=proposal_id,
        )
    if preview.introduced:
        raise ApplyError(
            "nested_member_introduced",
            f"proposal {proposal_id}'s action ADDS {list(preview.introduced)} to a nested "
            "object that does not have it. The members of `survived` are the closed set "
            "recon.resolve.SURVIVED_PATHS and every reader projects the map whole, so a "
            "new member -- a case variant, a whitespace variant or a unicode look-alike of "
            "a genuine path -- is rendered beside the genuine ones under a name a human "
            "reads as real. A fix carries the whole map; it does not extend it",
            proposal_id=proposal_id,
        )
    if {**before_value, **record.assignments} == before_value:
        # The same rule `_require_appliable` applies to an empty action, applied to
        # an action that is empty in EFFECT. Two proposals may legitimately write
        # the same path the same way; whichever lands second changes nothing, and
        # letting it through wrote an `applied` ledger event with `before == after`
        # -- indistinguishable from a write that moved a value, and enough to invert
        # the reversal stack: with a no-op event on top, `KS012`'s "not on top"
        # refusal saw an identical digest and let the earlier proposal be reversed
        # out from under it. Refused rather than skipped, because the citation is
        # single-use and silently spending one is the same lie in the other direction.
        #
        # `before_value` and `assignments` are both decoded from jsonb, and the
        # comparison is on the decoded values, so an action re-asserting the same
        # number in a different spelling (`1` against `1.0`) is refused here too
        # even though `current::text` would have changed.
        raise ApplyError(
            "no_op",
            f"proposal {proposal_id}'s action merges to the value the canonical row "
            "already holds, so applying it would spend the citation and write an "
            "'applied' event whose before and after are the same bytes",
            proposal_id=proposal_id,
        )

    event = conn.execute(
        _INSERT_APPLIED_EVENT, {"proposal_id": proposal_id, "actor": actor}
    ).fetchone()
    if event is None:
        raise ApplyError(
            "entity_missing",
            f"proposal {proposal_id} produced no ledger row: its target entity "
            f"{canonical_id} was not found by the correlated INSERT",
            proposal_id=proposal_id,
        )
    moved = conn.execute(
        _MOVE_STATUS,
        {
            "proposal_id": proposal_id,
            "from_status": _APPLIABLE_STATUS,
            "next_status": "applied",
        },
    ).fetchone()
    if moved is None:
        raise ApplyError(
            "not_approved",
            f"proposal {proposal_id} was not {_APPLIABLE_STATUS!r} when the status move ran; "
            "another transaction decided it first",
            proposal_id=proposal_id,
        )
    after_row = conn.execute(
        _APPLY_ENTITY, {"proposal_id": proposal_id, "canonical_id": canonical_id}
    ).fetchone()
    if after_row is None:  # pragma: no cover - the row is locked above
        raise ApplyError(
            "entity_missing",
            f"entity {canonical_id} vanished between the lock and the write",
            proposal_id=proposal_id,
        )

    result = ApplyResult(
        proposal_id=proposal_id,
        canonical_id=canonical_id,
        event_id=event.id,
        before=event.before_text,
        after=event.after_text,
        before_digest=entity_digest(event.before_text),
        after_digest=entity_digest(after_row.current_text),
        auto=auto,
        decision=decision,
        assignments=record.assignments,
    )
    # The ledger's `after` and the row's post-write value must be the same bytes.
    # KS010/KS001 enforce it at COMMIT; asserting it here means a violation is
    # reported against this statement instead of against the whole transaction.
    if entity_digest(event.after_text) != result.after_digest:  # pragma: no cover - KS001
        raise ApplyError(
            "ledger_mismatch",
            f"the applied event for proposal {proposal_id} records an after value that is "
            "not the bytes now in the canonical row",
            proposal_id=proposal_id,
        )

    insert_audit_row(
        conn,
        actor=actor,
        action="proposal.auto_applied" if auto else "proposal.applied",
        subject=str(proposal_id),
        body={
            **result.as_dict(),
            "conflict_type": record.conflict_type,
            "confidence": str(record.confidence),
            "gate": decision.as_dict() if decision is not None else None,
        },
    )
    log.info(
        "apply.applied",
        proposal_id=proposal_id,
        conflict_type=record.conflict_type,
        canonical_id=canonical_id,
        event_id=event.id,
        auto=auto,
        before_digest=result.before_digest,
        after_digest=result.after_digest,
    )
    return result


def _audit_refusal(actor: str, decision: AutoApplyDecision) -> None:
    """Record a refused auto-apply, in a transaction of its own.

    **Its own transaction on purpose.** :func:`auto_apply` raises after this, and
    the raise unwinds whatever transaction the caller opened -- so a refusal row
    written on the caller's connection would be rolled back with it and the
    refusal would leave no trace anywhere but the log. That is the shape of
    "documentation naming a control that does not exist": the policy says a
    refused auto-apply is audited, so it has to survive the refusal.
    """
    with role_connection(ROLE_APPLY_WRITER) as audit_conn:
        insert_audit_row(
            audit_conn,
            actor=actor,
            action="proposal.auto_apply_refused",
            subject=str(decision.proposal_id),
            body=decision.as_dict(),
        )


def auto_apply(
    proposal_id: int,
    *,
    actor: str = AUTO_APPLY_ACTOR,
    conn: Connection | None = None,
) -> ApplyResult:
    """R24. Apply **only** when every one of the stretch's conditions holds.

    A separate function from :func:`apply_proposal` on purpose: the gate is the
    deliverable, and a gate reached through an ``if auto:`` branch inside the
    manual path is a gate that a later edit to the manual path can widen without
    touching anything that looks like a gate.

    Raises :class:`AutoApplyRefused` -- which carries the whole
    :class:`AutoApplyDecision`, every condition and its verdict -- when the gate
    says no. The refusal is audited before it is raised, so a refused auto-apply
    is as visible in the ledger as an accepted one.
    """
    if conn is None:
        with role_connection(ROLE_APPLY_WRITER) as owned:
            return auto_apply(proposal_id, actor=actor, conn=owned)

    decision = evaluate_auto_apply(conn, proposal_id)
    if not decision.allowed:
        _audit_refusal(actor, decision)
        log.info(
            "apply.auto_apply_refused",
            proposal_id=proposal_id,
            reason=decision.reason,
            failed=[check.name for check in decision.failed],
        )
        raise AutoApplyRefused(decision)
    return apply_proposal(proposal_id, actor=actor, conn=conn, auto=True, decision=decision)


def rollback_proposal(
    proposal_id: int,
    *,
    actor: str = APPLY_ACTOR,
    conn: Connection | None = None,
) -> RollbackResult:
    """Restore the entity to the exact bytes the apply captured.

    The restored value is ``proposal_events.before`` of this proposal's
    ``applied`` row, copied **column to column inside the database**. Nothing is
    parsed, re-serialized or reassembled from field values, so "byte-identical"
    is a property of the statement rather than a claim about the merge being
    invertible -- and it is checked here by digest before the transaction ends,
    as well as by ``KS012`` at COMMIT.
    """
    if conn is None:
        with role_connection(ROLE_APPLY_WRITER) as owned:
            return rollback_proposal(proposal_id, actor=actor, conn=owned)

    record = load_proposal(conn, proposal_id)
    if record is None:
        raise ApplyError("not_found", f"no proposal {proposal_id}", proposal_id=proposal_id)
    if record.status != _REVERSIBLE_STATUS:
        raise ApplyError(
            "not_applied",
            f"proposal {proposal_id} is {record.status!r}: only an {_REVERSIBLE_STATUS!r} "
            "proposal has a write to reverse (SQLSTATE KS004)",
            proposal_id=proposal_id,
        )
    canonical_id = record.target_canonical_id
    if not canonical_id:  # pragma: no cover - an applied proposal always has one
        raise ApplyError(
            "no_target",
            f"proposal {proposal_id} names no entity",
            proposal_id=proposal_id,
        )

    locked = conn.execute(_LOCK_ENTITY, {"canonical_id": canonical_id}).fetchone()
    if locked is None:  # pragma: no cover - the apply proved it exists
        raise ApplyError(
            "entity_missing",
            f"entity {canonical_id} no longer exists",
            proposal_id=proposal_id,
        )

    event = conn.execute(
        _INSERT_ROLLED_BACK_EVENT, {"proposal_id": proposal_id, "actor": actor}
    ).fetchone()
    if event is None:
        raise ApplyError(
            "no_applied_event",
            f"proposal {proposal_id} has no applied event to reverse: there is no recorded "
            "before value, so there is no rollback path",
            proposal_id=proposal_id,
        )
    moved = conn.execute(
        _MOVE_STATUS,
        {
            "proposal_id": proposal_id,
            "from_status": _REVERSIBLE_STATUS,
            "next_status": "rolled_back",
        },
    ).fetchone()
    if moved is None:  # pragma: no cover - status re-read under the entity lock
        raise ApplyError(
            "not_applied",
            f"proposal {proposal_id} was not {_REVERSIBLE_STATUS!r} when the status move ran",
            proposal_id=proposal_id,
        )
    restored = conn.execute(
        _RESTORE_ENTITY, {"proposal_id": proposal_id, "canonical_id": canonical_id}
    ).fetchone()
    assert restored is not None  # the row is locked above

    result = RollbackResult(
        proposal_id=proposal_id,
        canonical_id=canonical_id,
        event_id=event.id,
        applied_before_digest=entity_digest(event.after_text),
        restored_digest=entity_digest(restored.current_text),
        pre_rollback_digest=entity_digest(locked.current_text),
    )
    if not result.byte_identical:  # pragma: no cover - KS012 refuses this at COMMIT
        raise ApplyError(
            "rollback_not_byte_identical",
            f"the reversal of proposal {proposal_id} did not restore the bytes the apply "
            f"captured: {result.applied_before_digest} != {result.restored_digest}",
            proposal_id=proposal_id,
        )

    insert_audit_row(
        conn,
        actor=actor,
        action="proposal.rolled_back",
        subject=str(proposal_id),
        body=result.as_dict(),
    )
    log.info(
        "apply.rolled_back",
        proposal_id=proposal_id,
        canonical_id=canonical_id,
        event_id=event.id,
        restored_digest=result.restored_digest,
    )
    return result


# ===========================================================================
# "never to sources" -- as an assertion, not as a sentence
# ===========================================================================


#: The three members contract R1's read-only port has, and the whole of it. An
#: object that does not carry all three is not an adapter, and a loop that walked
#: past one and reported success would be reporting on nothing.
_READ_ONLY_PORT_MEMBERS: Final[tuple[str, ...]] = ("source_id", "generations", "read")


def _adapter_objects(built: Any) -> tuple[Any, ...]:
    """The adapter INSTANCES from whatever ``build_adapters`` returned.

    ``build_adapters`` returns a ``dict`` keyed by source id, and iterating a
    dict yields its KEYS. The previous version of :func:`assert_sources_are_
    unwritable` iterated it directly, so it introspected the three strings
    ``"crm"``, ``"appdb"``, ``"payments"`` -- ``str`` carries no attribute
    containing a ``WRITE_NAME_TOKENS`` substring, so the loop passed, returned
    ``("str", "str", "str")`` and inspected no adapter at all. A function
    advertised as R24's "never to sources, executed rather than asserted in
    prose" that executes nothing is the phantom control this project has already
    shipped twice.

    Mappings are unwrapped; any other iterable is taken as-is, which is what lets
    the sabotage test hand this a list of one hostile adapter.
    """
    values = tuple(built.values()) if isinstance(built, Mapping) else tuple(built)
    if not values:
        raise AssertionError(
            "build_adapters() produced no adapter objects, so this assertion would "
            "inspect nothing and pass vacuously (R1, R24)"
        )
    for candidate in values:
        missing = [member for member in _READ_ONLY_PORT_MEMBERS if not hasattr(candidate, member)]
        if missing:
            raise AssertionError(
                f"{candidate!r} ({type(candidate).__name__}) is not a source adapter: it "
                f"is missing {missing} of the read-only port {list(_READ_ONLY_PORT_MEMBERS)}. "
                "Introspecting it would prove nothing about whether the sources are "
                "writable -- this is exactly the shape of the bug where the dict's KEYS "
                "were inspected instead of its adapters"
            )
    return values


def _instance_namespace(adapter: Any) -> Mapping[str, Any]:
    """The adapter's own instance attributes, or an empty mapping if it has none.

    ``vars(obj)`` raises ``TypeError`` on an object whose class defines
    ``__slots__`` and no ``__dict__``, so calling it directly made
    :func:`assert_sources_are_unwritable` **crash** on a slotted adapter instead
    of judging it -- a ``TypeError`` where the named ``AssertionError`` belongs,
    and an assertion that reports its own breakage as a different failure is one
    a reader cannot act on. A slotted adapter is not thereby unexamined: a slot
    is a data descriptor on the CLASS, so ``write_back`` in ``__slots__`` appears
    in ``vars(cls)`` and the MRO walk below still names it.
    """
    namespace = getattr(adapter, "__dict__", None)
    return namespace if isinstance(namespace, Mapping) else {}


def assert_sources_are_unwritable() -> tuple[str, ...]:
    """R24's "never to sources", executed rather than asserted in prose.

    Returns the adapter class names checked. Raises :class:`AssertionError` when

    * ``build_adapters`` yields anything that is not a read-only adapter object
      (see :func:`_adapter_objects` -- this is the arm that makes the rest of the
      function about adapters rather than about three strings), or
    * any adapter carries a write-shaped attribute anywhere in its MRO **or on
      the instance itself** -- the same ``WRITE_NAME_TOKENS`` list
      ``recon.adapters.base`` commits and ``tests/ingest/test_read_only_port.py``
      already enforces on the ingest side.

    **``WRITE_NAME_TOKENS`` is a substring list, not a decision procedure**, and
    this function is exactly as exhaustive as that list is. An adapter with
    ``def persist(...)``, ``def commit(...)``, ``def flush(...)`` or
    ``def sync(...)`` carries no listed token and passes here. What makes "never
    to sources" more than this check is that the port has no write member at all
    (:class:`recon.adapters.base.ReadOnlyAdapter`) and that
    :func:`source_tree_digest` measures the bytes across a real committed apply;
    this one is the cheap structural arm of three, not the guarantee.

    The instance dictionary is inspected as well as the class MRO because an
    adapter that is handed a bound writer at construction time
    (``self.write_back = sink.write``) carries no such attribute on any class,
    and a check that looked only at classes would report it clean.

    Restated here because "applies only to the canonical layer, never to a
    source" is a property of the *apply* path, and a property nothing on the
    apply path checks is a property the apply path does not have. What this
    function cannot see is whether an apply RUN touched the tree, which is a
    different measurement: :func:`source_tree_digest` is that one, and
    ``tests/apply/test_merge_shape.py`` takes it either side of a real committed
    apply and rollback.
    """
    from recon.adapters import build_adapters
    from recon.adapters.base import WRITE_NAME_TOKENS

    names: list[str] = []
    for adapter in _adapter_objects(build_adapters(None)):
        cls = type(adapter)
        names.append(cls.__name__)
        namespaces: list[tuple[str, Any]] = [(cls.__name__, _instance_namespace(adapter))]
        namespaces.extend((klass.__name__, vars(klass)) for klass in cls.__mro__)
        for where, namespace in namespaces:
            for attribute in namespace:
                if attribute.startswith("__"):
                    continue
                lowered = attribute.lower()
                offending = [token for token in WRITE_NAME_TOKENS if token in lowered]
                if offending:
                    raise AssertionError(
                        f"{where}.{attribute} looks like a write method "
                        f"({offending}); sources are read-only files and the apply path "
                        "targets the canonical layer only (R1, R24)"
                    )
    return tuple(sorted(names))


# ---------------------------------------------------------------------------
# ... and the same claim as a MEASUREMENT of the tree itself
# ---------------------------------------------------------------------------

#: How much of a fixture file is read at a time. The committed tree is ~123 MB
#: across ~18 files; streaming keeps the digest independent of file size.
_DIGEST_CHUNK: Final = 1 << 20


def source_tree_digest(root: Any = None) -> dict[str, str]:
    """``{relative path: sha256}`` for every file in the source fixture tree.

    :func:`assert_sources_are_unwritable` reasons about the adapter *classes*:
    no member of any of them is write-shaped, so no code path exists through
    which a source could be written. This function measures the other half --
    that a real apply run left the bytes on disk untouched -- and the two are not
    the same claim. A structural argument about the port says nothing about a
    stray ``open(..., "w")`` somewhere else, and R24's "never to sources" is a
    statement about the run, not only about the type.

    Sorted by relative path so the mapping is comparable between two calls, and
    the per-file digests are kept rather than folded into one number so a
    difference names the file that changed instead of merely reporting that
    something did.
    """
    from pathlib import Path

    from recon.adapters import default_fixtures_root

    base = Path(root) if root is not None else default_fixtures_root()
    if not base.is_dir():
        raise AssertionError(
            f"the source fixture tree {base} does not exist, so digesting it would "
            "compare nothing to nothing (run `python -m recon.seed`)"
        )
    digests: dict[str, str] = {}
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(_DIGEST_CHUNK):
                digest.update(chunk)
        digests[str(path.relative_to(base))] = digest.hexdigest()
    if not digests:
        raise AssertionError(
            f"the source fixture tree {base} holds no files, so a before/after "
            "comparison of it would pass vacuously"
        )
    return digests
