"""R15's classifier: sensitivity is decided by the TARGET FIELD PATH, before confidence.

    "Proposals touching a sensitive field (legal name, DOB, government/student id,
     billing owner, financially-consequential status, consent/compliance flags)
     SHALL be classified `sensitive_hold`, can never auto-apply at any confidence,
     and are forced to human review."  -- R15

Contract SS6 makes that decidable: *"Classification is a **pure function of the
target field path**, evaluated *before* confidence. The list **is** the whole
classifier"*, and SS6's committed fix-target table pins the target path of every
conflict type so the function is total.

Classification wins over confidence -- structurally
----------------------------------------------------
The strongest way to say "confidence can never unlock a sensitive proposal" is to
build a classifier that **cannot see confidence**. :func:`classify` takes a
conflict type and a set of disagreeing paths. There is no confidence parameter,
no score, no threshold, and no branch anywhere in this module that reads one. A
future edit that wanted to let a 0.99 through would have to change the signature
first, which is a reviewable act rather than an accident.

Two independent controls, and which is which
---------------------------------------------
The database also refuses a sensitive proposal that is not born held: migration
0005/0006's ``keystone_proposal_born_pending`` raises SQLSTATE ``KS002`` on
``sensitive = true`` with any birth status other than ``sensitive_hold``. That is
the **backstop**. This module is the **control**: it is what decides, and the
reconciler asks it rather than relying on the trigger to catch a mistake. The
distinction matters because a trigger can only reject a row that was already
built wrong -- it cannot make the automation classify correctly, and a system
whose only correct behaviour comes from its error path is one migration away from
having none.

``tests/reconciler/test_sensitive.py`` asserts both halves: that this function
holds every SS6 sensitive path and every C14, and that the trigger independently
refuses a hand-built sensitive proposal that claims ``pending``.

**What the backstop covers, and what it still does not.** ``KS002`` binds
``proposals.sensitive`` to the birth STATUS. This paragraph used to go on to say
that *nothing* in the schema binds ``proposals.sensitive`` to the field paths the
``action`` actually writes, so that a row with ``sensitive = false``,
``status = 'pending'`` and ``action = {"set": {"crm.contact.dob": ...}}`` "is
accepted by every committed constraint". **That is no longer true, and saying a
control is missing when it exists is as misleading as claiming one that is not
there.** Migrations 0012, 0013 and 0014 built it, each closing the row the one
before still accepted: 0012's ``ck_proposals_sensitive_covers_write_set`` (the
top-level key), 0013's ``keystone_effective_write_paths``/``KS013`` (the write
set is a set of PATHS, and ``entities.current`` nests ``survived``), and 0014's
reading of the write set off the VALUE the merge would produce, at every shape --
so ``{"set": {"survived": "wiped"}}``, which erases all nine members with a
scalar, is refused as well.

Two things are still worth stating plainly. First, the code path is not
redundant: ``recon.reconciler._assert_action_matches_classification`` re-derives
R15 from the ACTION's own write paths against ``recon.reference``, in a different
component from the one that classified, so the guarantee does not rest on one
implementation. Second, a CHECK and a trigger bind *rows*, not DDL: the schema
owner runs migrations and can ``DROP CONSTRAINT``, so this is defence in depth
over the three non-owner roles, which are the boundary. ``docs/proposal-policy.md``
§8.4 enumerates the remaining edges.

What "escalated" means here
----------------------------
SS6's table gives three classifications. Two of them are proposal *statuses*
(``pending``, ``sensitive_hold``); the third, ``escalated``, is not -- the
``proposal_status`` enum has no such value, and the birth trigger admits only the
first two. ``escalated`` in SS6 describes the *disposition* of an evidence-only
proposal: "no field write -- evidence-only proposal", "human merge review". Such a
proposal is therefore born ``pending`` (it is a human-review queue item like any
other) with an empty ``action``, and :class:`Classification` carries
``disposition='escalated'`` so the dashboard and the audit row can say so in
words. The conflict-row status ``escalated`` is reserved for SS7's
``escalated:oscillation``, which is the only value the committed dashboard
contract admits alongside ``open``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from recon.reference import (
    AUTO_APPLY_ELIGIBLE,
    SENSITIVE_FIELDS,
    FixTarget,
    fix_target,
    is_auto_apply_eligible,
    is_sensitive,
)

__all__ = [
    "BIRTH_STATUSES",
    "DISPOSITION_ELIGIBLE",
    "DISPOSITION_ESCALATED",
    "DISPOSITION_SENSITIVE_HOLD",
    "STATUS_PENDING",
    "STATUS_SENSITIVE_HOLD",
    "Classification",
    "classify",
    "sensitive_paths",
]

#: The two statuses a proposal may be BORN with (migration 0005/0006,
#: ``BIRTH_STATUSES``). Anything else is SQLSTATE ``KS002``.
STATUS_PENDING: Final = "pending"
STATUS_SENSITIVE_HOLD: Final = "sensitive_hold"

#: The closed birth vocabulary, as a set the CODE checks -- not only the trigger.
#: ``Classification`` refused a self-contradictory sensitive/pending pair from the
#: start but happily constructed ``status='approved'`` or ``status='applied'``,
#: leaving ``keystone_proposal_born_pending`` (SQLSTATE ``KS002``) as the ONLY
#: thing standing between a re-targeting bug and a proposal born decided. A
#: backstop that is the sole control is not a backstop.
BIRTH_STATUSES: Final = frozenset({STATUS_PENDING, STATUS_SENSITIVE_HOLD})

#: SS6's three classifications, kept under their contract spelling.
DISPOSITION_ELIGIBLE: Final = "eligible"
DISPOSITION_SENSITIVE_HOLD: Final = "sensitive_hold"
DISPOSITION_ESCALATED: Final = "escalated"

#: SS5.5: only ``R-006`` and ``R-014`` populate ``disagreeing_fields``, and SS6
#: says "**every** C14" is held -- so C14 is named here as a type-level rule that
#: does not depend on the path selection succeeding.
_ALWAYS_HELD_TYPES: Final = frozenset({"C14"})


def sensitive_paths(paths: Sequence[str]) -> tuple[str, ...]:
    """The sorted subset of ``paths`` that is in ``SENSITIVE_FIELDS`` (SS6)."""
    return tuple(sorted(path for path in paths if is_sensitive(path)))


@dataclass(frozen=True, slots=True)
class Classification:
    """The classification of one proposal, with the reason it reached it.

    ``status`` is what the proposal is BORN with and ``sensitive`` is the column
    the ``KS002`` trigger reads; the two are constructed together here precisely
    so they cannot disagree -- ``sensitive=True`` with ``status='pending'`` is the
    exact row the trigger exists to refuse, and this class makes it
    unconstructible upstream of the database.
    """

    conflict_type: str
    #: The committed SS6 target path, or ``None`` for an evidence-only type.
    target_path: str | None
    #: ``pending`` | ``sensitive_hold`` -- the birth status.
    status: str
    #: The ``proposals.sensitive`` column.
    sensitive: bool
    #: SS6's own word: ``eligible`` | ``sensitive_hold`` | ``escalated``.
    disposition: str
    #: Every sensitive path among the conflict's disagreeing paths -- recorded
    #: even when it is not the chosen target, so a reviewer sees the whole set.
    sensitive_paths: tuple[str, ...]
    #: Why this classification, in one line, for the audit row.
    reason: str
    #: R24's allowlist membership. Never sufficient on its own: auto-apply also
    #: needs confidence >= 0.95, an approved case type and complete evidence, and
    #: that decision belongs to the apply path, not here.
    auto_apply_eligible_path: bool

    def __post_init__(self) -> None:
        # A proposal is BORN pending or sensitive_hold and nothing else. The
        # database says so (KS002); this says so too, so the automation cannot
        # even construct the row that the trigger would have to refuse. The
        # ordering matters: check the vocabulary BEFORE the pairing, or
        # `status='approved', sensitive=False` passes the pairing check.
        if self.status not in BIRTH_STATUSES:
            raise ValueError(
                f"status={self.status!r} is not a birth status: a proposal is born "
                f"{sorted(BIRTH_STATUSES)} (R15, SQLSTATE KS002)"
            )
        # The class's whole job is that these two agree. Assert it rather than
        # trusting every construction site, because the failure is silent until a
        # KS002 rejection in production.
        if self.sensitive != (self.status == STATUS_SENSITIVE_HOLD):
            raise ValueError(
                f"sensitive={self.sensitive} contradicts status={self.status!r}: a "
                "sensitive proposal is born sensitive_hold (R15, SQLSTATE KS002)"
            )
        if self.sensitive and self.auto_apply_eligible_path:
            raise ValueError(
                f"{self.target_path!r} is classified sensitive and also on the "
                "auto-apply allowlist; SS6 pins those two sets disjoint"
            )

    @property
    def held(self) -> bool:
        return self.status == STATUS_SENSITIVE_HOLD

    @property
    def evidence_only(self) -> bool:
        """True when SS6's table says "no field write" for this type."""
        return self.target_path is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "conflict_type": self.conflict_type,
            "target_path": self.target_path,
            "status": self.status,
            "sensitive": self.sensitive,
            "disposition": self.disposition,
            "sensitive_paths": list(self.sensitive_paths),
            "reason": self.reason,
            "auto_apply_eligible_path": self.auto_apply_eligible_path,
        }


def _target_is_in_dispute(field_path: str, paths: Sequence[str]) -> bool:
    """Is the selected target actually one of the paths this conflict disputes?

    Only asked of a conflict that HAS disagreeing paths. A type that carries none
    (SS2.4: only ``R-006`` and ``R-014`` populate ``disagreeing_fields``) is
    answered ``True`` -- its target comes from SS6's per-type table and there is
    no set for it to be in.

    **The fail-open this closes.** ``reference.fix_target``'s C6 branch looks for
    a wholly-sensitive comparison row, then for an ``AUTO_APPLY_ELIGIBLE`` path,
    and if it finds neither returns the default row ``FIX_TARGETS["C6"]`` =
    ``crm.contact.grade`` -- which is *eligible*, and which is not in the
    disagreeing set at all. So ``classify("C6", ["appdb.student.status"])``
    returned target ``crm.contact.grade``, status ``pending``,
    ``auto_apply_eligible=True``: a SENSITIVE path in the conflict, a proposal
    not held, and a target naming a field nobody disagreed about. Contract SS6
    pins the opposite direction ("eligibility is an allowlist, not the complement
    of ``SENSITIVE_FIELDS``").

    That shape is **not reachable from the committed engine today** -- SS2.4 puts
    BOTH endpoints of every disagreeing row into ``disagreeing_fields``, so a
    single-endpoint or unknown-path C6 is never produced, and the real run
    confirms it (measured: 0 proposals write a sensitive path, every action key
    on the allowlist). It is a latent fail-open in the module the classifier
    trusts, and the safe arm of the classifier was unreachable *because* of it.
    ``reference.py`` is authoritative and shared, so it is not edited here; the
    hole is closed on this side instead and flagged for its owner.
    """
    return not paths or field_path in tuple(paths)


def classify(conflict_type: str, disagreeing_fields: Sequence[str] = ()) -> Classification:
    """Classify one conflict's proposal. **Takes no confidence, by construction.**

    The order is SS6's order, and it is the order because the first step is
    allowed to be sufficient:

    1. **C14 is held on the strength of its type.** SS6: "C6 mixed, and **every**
       C14" write the disagreeing sensitive path. C14's own predicate (SS5.5) is
       "disagreeing-path set non-empty and wholly within ``SENSITIVE_FIELDS``", so
       a C14 whose target selection somehow produced a non-sensitive path would be
       a bug -- and this step means such a bug still ends in a hold rather than in
       an auto-appliable proposal.
    2. **Otherwise the target path decides**, via the committed selector
       ``reference.fix_target`` (SS6 ruling 8: partition by comparison ROW, the
       sensitive half of a mixed set wins, CRM side on a tie). ``None`` -> the
       type is evidence-only and the disposition is ``escalated``; a path in
       ``SENSITIVE_FIELDS`` -> held; a path on the auto-apply allowlist ->
       ``eligible``.
    3. **A path in neither set is held, not passed.** SS6: "eligibility is an
       allowlist, not the complement of ``SENSITIVE_FIELDS``". Defaulting an
       unknown path to eligible is how a new field silently becomes
       auto-appliable, so the default is the safe direction.
    4. **A target the selector fell back to, rather than found, is held.** See
       :func:`_target_is_in_dispute` -- this is the arm that closes
       ``reference.fix_target``'s C6 fail-open.
    """
    paths = tuple(disagreeing_fields)
    held_paths = sensitive_paths(paths)

    target: FixTarget = fix_target(conflict_type, paths)

    if conflict_type in _ALWAYS_HELD_TYPES:
        return Classification(
            conflict_type=conflict_type,
            target_path=target.field_path,
            status=STATUS_SENSITIVE_HOLD,
            sensitive=True,
            disposition=DISPOSITION_SENSITIVE_HOLD,
            sensitive_paths=held_paths,
            reason=(
                f"{conflict_type}: contract SS6 holds every C14 -- its disagreeing "
                "paths are wholly within SENSITIVE_FIELDS by SS5.5's own predicate"
            ),
            auto_apply_eligible_path=False,
        )

    if target.field_path is None:
        return Classification(
            conflict_type=conflict_type,
            target_path=None,
            status=STATUS_PENDING,
            sensitive=False,
            disposition=DISPOSITION_ESCALATED,
            sensitive_paths=held_paths,
            reason=(
                f"{conflict_type}: contract SS6 commits no field write for this type "
                "-- the proposal is evidence-only and escalated for human review"
            ),
            auto_apply_eligible_path=False,
        )

    if is_sensitive(target.field_path):
        return Classification(
            conflict_type=conflict_type,
            target_path=target.field_path,
            status=STATUS_SENSITIVE_HOLD,
            sensitive=True,
            disposition=DISPOSITION_SENSITIVE_HOLD,
            sensitive_paths=held_paths,
            reason=(
                f"{conflict_type}: target {target.field_path} is in SENSITIVE_FIELDS "
                "-- held at every confidence, including 1.0 (R15)"
            ),
            auto_apply_eligible_path=False,
        )

    if not _target_is_in_dispute(target.field_path, paths):
        return Classification(
            conflict_type=conflict_type,
            target_path=target.field_path,
            status=STATUS_SENSITIVE_HOLD,
            sensitive=True,
            disposition=DISPOSITION_SENSITIVE_HOLD,
            sensitive_paths=held_paths,
            reason=(
                f"{conflict_type}: the conflict disputes {sorted(paths)} but the "
                f"committed selector returned {target.field_path}, which is not "
                "among them -- a fallback row, not a path in dispute. SS6 makes "
                "eligibility an allowlist, so this is held for human review"
            ),
            auto_apply_eligible_path=False,
        )

    if is_auto_apply_eligible(target.field_path):
        return Classification(
            conflict_type=conflict_type,
            target_path=target.field_path,
            status=STATUS_PENDING,
            sensitive=False,
            disposition=DISPOSITION_ELIGIBLE,
            sensitive_paths=held_paths,
            reason=(
                f"{conflict_type}: target {target.field_path} is on SS6's "
                "AUTO_APPLY_ELIGIBLE allowlist; auto-apply still requires "
                "confidence >= 0.95, an approved case type and complete evidence"
            ),
            auto_apply_eligible_path=True,
        )

    return Classification(  # pragma: no cover - no committed template reaches it
        conflict_type=conflict_type,
        target_path=target.field_path,
        status=STATUS_SENSITIVE_HOLD,
        sensitive=True,
        disposition=DISPOSITION_SENSITIVE_HOLD,
        sensitive_paths=held_paths,
        reason=(
            f"{conflict_type}: target {target.field_path} is on neither committed "
            "list; SS6 makes eligibility an allowlist, so an unlisted path is held"
        ),
        auto_apply_eligible_path=False,
    )


# Import-time guard. The two sets being disjoint is what makes `classify`'s
# branches mutually exclusive rather than order-dependent, and `reference.py`
# already asserts it -- restated here so this module fails on import if the
# assumption its branch order rests on is ever broken elsewhere.
if SENSITIVE_FIELDS & AUTO_APPLY_ELIGIBLE:  # pragma: no cover - import guard
    raise ValueError("SENSITIVE_FIELDS and AUTO_APPLY_ELIGIBLE must be disjoint (SS6)")
