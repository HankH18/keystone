"""R13/R16: the guarded, unattended proposer.

One run reads the conflict store, builds an evidence packet per conflict, picks
the committed fix template, scores it, classifies it, and writes **exactly one
proposal per eligible conflict** -- ``pending`` or ``sensitive_hold``, never
anything else. Nothing else in the database moves. The read-only mirror
(``raw_records``, ``ingest_runs``, ``stg_*``) is not touched at all, and
``recon.suite.mirror.check_mirror_unchanged`` brackets :func:`run_once` with a
content hash of every one of those tables to say so with a number rather than a
comment.

The order of operations, and why it is that order
--------------------------------------------------
Per conflict, in this sequence:

1. **evidence packet** -- assembled from durable tables only (``conflicts``,
   ``entity_link_candidates``, ``source_generations``, ``field_lineage``), so the
   packet a reviewer reads later is the packet that was scored;
2. **fix template** -- contract SS6's committed fix-target table, via
   ``reference.fix_target``. Evidence-only types (SS6: "no field write") get
   ``{"set": {}}``, explicitly and by name, rather than being skipped: R13 says
   one proposal per conflict, and a conflict a human must look at is exactly the
   kind that must not vanish because the automation had nothing to write;
3. **confidence** -- :mod:`recon.confidence`, from the committed
   ``confidence.yaml``. No LLM input, ever;
4. **sensitivity** -- :mod:`recon.sensitive`, a pure function of the target field
   path. It runs on a value computed at step 2 and it cannot see step 3's number:
   classification wins over confidence because the classifier has no confidence
   parameter to be overridden by;
5. **dedup / oscillation** -- R16, below;
6. **INSERT** one proposal, then one ``audit_log`` row through the redacting
   chokepoint ``recon.logging.insert_audit_row``.

Steps 3 and 4 are in DESIGN's order, but note that swapping them would change
nothing: that is the point. The hold does not depend on when the score is
computed, because no branch that produces a hold reads a score.

R16, stated as policy
----------------------
Two rules, and they are not the same rule:

* **fingerprint dedup.** A non-rejected proposal already carrying this
  conflict's fingerprint means the conflict is already in front of a human --
  do not re-propose. This is what makes a re-run with no source change create
  ZERO new proposals. The partial unique index
  ``uq_proposals_open_fingerprint`` (``WHERE status <> 'rejected'``) is the
  database's backstop for the same rule; the check here is the control.
* **oscillation.** If the underlying field oscillated A -> B -> A across
  generations (contract SS7), the conflict is marked ``escalated`` with
  ``escalation_reason = 'oscillation'`` -- the dashboard's committed
  ``escalated:oscillation``. The *additional* thing this buys over fingerprint
  dedup is the case fingerprint dedup deliberately allows: a proposal that a
  human **rejected**. A rejected proposal frees the fingerprint, so without this
  rule the next run would re-propose the identical fix that the human just
  refused, on a field the source keeps re-asserting. R16 forbids exactly that,
  so a prior proposal with the same fingerprint **and an identical action** is
  never re-proposed when the field oscillates.

  A conflict oscillating for the first time still gets its one proposal -- that
  is not a *re*-proposal -- carrying the model's heaviest penalty (-0.25) and the
  escalation on the conflict row.

**The honest limit, up front:** ``field_lineage`` is written for generations 1-3
by ``recon.resolve.materialize``, but the committed test path ingests generation 3
only, so in that configuration the A -> B -> A scan has no history to read and
finds nothing. The packet therefore records ``lineage_rows`` and which input
answered (``lineage_scan`` or the ``conflicts.oscillating`` column), so "scanned
and found none" is never reported as "there is nothing here". The dedup logic is
unit-tested against constructed lineage; see the ticket report for what that does
and does not prove.

The LLM is a seam, and it is empty in this ticket
--------------------------------------------------
T-7's non-goals say "no LLM calls". :func:`reconcile` therefore takes a
``rationale`` callable defaulting to :func:`no_rationale`, which returns
``None``. T-8 wires ``recon.llm.generate_rationale`` into that parameter without
touching a line of the proposer. The seam is wrapped in ``try/except`` and a
failing hook is logged and ignored, because the brief's rule is absolute: if the
rationale fails or the cap is hit, **the proposal still lands, with rationale
null**. ``tests/reconciler`` asserts that with a hook that raises.

Write boundary
--------------
Everything here runs as ``recon_writer``, which can INSERT ``proposals``,
``audit_log`` and ``conflicts`` and UPDATE ``conflicts`` -- and holds no UPDATE on
``proposals``, no write of any kind on ``entities``, and no INSERT/UPDATE on
``budget_ledger``. It therefore cannot decide, cannot apply, and cannot approve
its own work. That is a grant, not a convention.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import Connection, text

from recon.confidence import (
    ConfidenceModel,
    Score,
    Signals,
    disagreeing_row_count,
    load_model,
    observed_value_is_null,
    partial_evidence_reasons,
    score,
)
from recon.db import ROLE_RECON_WRITER, role_connection
from recon.logging import get_logger, insert_audit_row
from recon.privacy import canonical_json
from recon.reference import (
    COMPARED_FIELD_BY_PATH,
    CONFLICT_TYPES,
    fix_target,
    person_key,
)
from recon.sensitive import Classification, classify

__all__ = [
    "AUDIT_ACTOR",
    "CURRENT_GENERATION",
    "ESCALATION_OSCILLATION",
    "SKIP_FINGERPRINT",
    "SKIP_OSCILLATION",
    "ConflictRow",
    "EvidencePacket",
    "FixAction",
    "ProposalOutcome",
    "ReconcileReport",
    "build_packet",
    "fix_action",
    "no_rationale",
    "reconcile",
    "run_once",
]

log = get_logger("recon.reconciler")

#: SS7: current state is generation 3, and that is the snapshot conflicts describe.
CURRENT_GENERATION: Final = 3

#: SQLSTATE ``KS003`` requires ``^system:`` for ``recon_writer``. This is the one
#: actor string this module writes; nothing here may look like a human.
AUDIT_ACTOR: Final = "system:reconciler"

#: ``conflicts.escalation_reason`` for SS7's oscillation. Paired with
#: ``conflicts.status = 'escalated'`` it is the dashboard's ``escalated:oscillation``,
#: which the committed dashboard contract admits alongside ``open`` and nothing else.
ESCALATION_OSCILLATION: Final = "oscillation"

SKIP_FINGERPRINT: Final = "fingerprint_dedup"
SKIP_OSCILLATION: Final = "oscillation_identical_fix"

#: The three ``entity_link_candidates`` key classes, in ``normalize.KEY_CLASSES``
#: order. Read from the model file, not restated -- see :meth:`_identity_agreement`.
_CONTACT_PREFIX: Final = "crm:contact:"
_STUDENT_PREFIX: Final = "appdb:student:"


# =====================================================================================
# rows and packets
# =====================================================================================
@dataclass(frozen=True, slots=True)
class ConflictRow:
    """One row of the ``conflicts`` table, as the reconciler reads it."""

    id: int
    fingerprint: str
    type: str
    rule_id: str | None
    entity_refs: tuple[str, ...]
    sources_involved: tuple[str, ...]
    disagreeing_fields: tuple[str, ...]
    observed_values: Mapping[str, Any]
    oscillating: bool
    status: str
    escalation_reason: str | None

    @property
    def contact_refs(self) -> tuple[str, ...]:
        return tuple(ref for ref in self.entity_refs if ref.startswith(_CONTACT_PREFIX))

    @property
    def student_refs(self) -> tuple[str, ...]:
        return tuple(ref for ref in self.entity_refs if ref.startswith(_STUDENT_PREFIX))


@dataclass(frozen=True, slots=True)
class FixAction:
    """The committed fix template, resolved to a concrete ``action`` payload.

    ``action`` is always ``{"set": {...}}`` -- migration 0007's
    ``ck_proposals_action_vocabulary`` admits exactly that shape and nothing else,
    and ``{"set": {}}`` is the contract's evidence-only proposal.
    """

    conflict_type: str
    target_path: str | None
    value: Any
    derivable: bool
    derivation: str
    action: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "conflict_type": self.conflict_type,
            "target_path": self.target_path,
            "value": self.value,
            "value_derivable": self.derivable,
            "derivation": self.derivation,
            "action": dict(self.action),
        }


@dataclass(frozen=True, slots=True)
class EvidencePacket:
    """Everything the score saw, persisted with the proposal.

    R14 calls the signals "inspectable". A score whose inputs are not recorded is
    not inspectable by a reviewer -- only by whoever can re-run the pipeline over
    the same snapshot -- so the packet travels into ``proposals.evidence`` rather
    than being reconstructible in principle.
    """

    conflict: ConflictRow
    person_key: str
    generation: int
    incomplete_sources: tuple[str, ...]
    key_class_matches: Mapping[str, tuple[str, ...]]
    #: Contact refs whose match-key classes resolve to DIFFERENT students -- the
    #: strongest evidence against an identity, recorded so a reviewer can see why
    #: the identity signals are absent rather than merely that they are.
    key_class_contradictions: tuple[str, ...]
    null_observed_values: tuple[str, ...]
    oscillation_source: str
    lineage_rows: int
    signals: Signals
    score: Score
    classification: Classification
    fix: FixAction
    run_id: str

    def as_dict(self) -> dict[str, Any]:
        """The JSON written to ``proposals.evidence``.

        ``observed_values`` is included verbatim: it is the contract's pinned
        per-type key set (SS5.4) and it is the substance of "the evidence used"
        that R13 requires the proposal to carry.
        """
        return {
            "schema": "keystone.evidence.v1",
            "run_id": self.run_id,
            "generation": self.generation,
            "conflict": {
                "id": self.conflict.id,
                "type": self.conflict.type,
                "rule_id": self.conflict.rule_id,
                "fingerprint": self.conflict.fingerprint,
                "entity_refs": list(self.conflict.entity_refs),
                "sources_involved": list(self.conflict.sources_involved),
                "disagreeing_fields": list(self.conflict.disagreeing_fields),
                "observed_values": dict(self.conflict.observed_values),
                "person_key": self.person_key,
            },
            "identity": {
                "key_class_matches": {
                    key_class: list(refs)
                    for key_class, refs in sorted(self.key_class_matches.items())
                },
                "contradictions": list(self.key_class_contradictions),
                "contact_refs": list(self.conflict.contact_refs),
                "student_refs": list(self.conflict.student_refs),
            },
            "completeness": {
                "incomplete_sources": list(self.incomplete_sources),
                "null_observed_values": list(self.null_observed_values),
            },
            "oscillation": {
                "observed": self.signals.oscillation_observed,
                "decided_by": self.oscillation_source,
                "lineage_rows": self.lineage_rows,
                "conflict_row_flag": self.conflict.oscillating,
            },
            "fix": self.fix.as_dict(),
            "classification": self.classification.as_dict(),
            "confidence": self.score.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class ProposalOutcome:
    """What the run did about one conflict. Every conflict gets one of these."""

    fingerprint: str
    conflict_type: str
    proposed: bool
    status: str | None
    confidence: Decimal | None
    skip_reason: str | None = None
    proposal_id: int | None = None
    escalated: bool = False
    rationale_attached: bool = False


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """The result of one run, in the shape a scorecard row wants."""

    run_id: str
    generation: int
    conflicts_seen: int
    proposed: int
    pending: int
    sensitive_hold: int
    evidence_only: int
    skipped_fingerprint: int
    skipped_oscillation: int
    escalated_oscillation: int
    rationale_attached: int
    lineage_rows: int
    model_version: int
    by_type: Mapping[str, int]
    outcomes: tuple[ProposalOutcome, ...] = field(repr=False, default=())
    elapsed_ms: float = 0.0

    def confidence_vector(self) -> tuple[tuple[str, str], ...]:
        """``(fingerprint, confidence)`` for every proposal this run wrote, sorted.

        DESIGN's determinism check compares this between two seeded runs; keeping
        it a method of the report means the thing compared is the thing produced.
        """
        return tuple(
            sorted(
                (outcome.fingerprint, str(outcome.confidence))
                for outcome in self.outcomes
                if outcome.proposed and outcome.confidence is not None
            )
        )

    def confidence_digest(self) -> str:
        """One sha256 over :meth:`confidence_vector` -- comparable across processes."""
        payload = "\n".join(f"{fp}={value}" for fp, value in self.confidence_vector())
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        """The report as a caller reads it -- structured logs, the scorecard, tests."""
        return {
            "run_id": self.run_id,
            "generation": self.generation,
            "conflicts_seen": self.conflicts_seen,
            "proposed": self.proposed,
            "pending": self.pending,
            "sensitive_hold": self.sensitive_hold,
            "evidence_only": self.evidence_only,
            "skipped_fingerprint": self.skipped_fingerprint,
            "skipped_oscillation": self.skipped_oscillation,
            "escalated_oscillation": self.escalated_oscillation,
            "rationale_attached": self.rationale_attached,
            "lineage_rows": self.lineage_rows,
            "model_version": self.model_version,
            "by_type": dict(self.by_type),
            "confidence_digest": self.confidence_digest(),
            "elapsed_ms": round(self.elapsed_ms, 3),
        }

    def audit_body(self) -> dict[str, Any]:
        """The same run, spelled in ``recon.privacy.SAFE_KEYS``.

        The redacting chokepoint is default-deny on mapping **keys**, so
        ``as_dict()``'s names -- ``conflicts_seen``, ``by_type``,
        ``skipped_fingerprint`` -- are tokenised in the default ``safe`` log
        mode, and so are ``by_type``'s ``C1``..``C14`` keys. Every count below is
        therefore renamed onto the committed vocabulary (the ``_count`` and
        ``_ms`` suffixes are allow-listed patterns), and the per-type breakdown
        travels as a ``label`` string rather than as a map whose keys would be
        replaced one by one. Same numbers, spelled so the audit row is readable
        by the reviewer it exists for.
        """
        return {
            "run_id": self.run_id,
            "generation": self.generation,
            "version": self.model_version,
            "conflicts_count": self.conflicts_seen,
            "proposed_count": self.proposed,
            "pending_count": self.pending,
            "sensitive_count": self.sensitive_hold,
            "evidence_only_count": self.evidence_only,
            "skipped_fingerprint_count": self.skipped_fingerprint,
            "skipped_oscillation_count": self.skipped_oscillation,
            "escalated_count": self.escalated_oscillation,
            "rationale_count": self.rationale_attached,
            "rows": self.lineage_rows,
            "label": " ".join(f"{key}={value}" for key, value in sorted(self.by_type.items())),
            "fingerprint": self.confidence_digest(),
            "elapsed_ms": round(self.elapsed_ms, 3),
        }


# =====================================================================================
# the committed fix templates (contract SS6)
# =====================================================================================
def fix_action(
    conflict_type: str,
    *,
    disagreeing_fields: Sequence[str] = (),
    observed_values: Mapping[str, Any] | None = None,
) -> FixAction:
    """Resolve SS6's fix target to a concrete ``{"set": {...}}`` action.

    SS6 pins the target **path** per type; it does not pin the **value**, so the
    value derivation is committed here, one rule per type, and each rule either
    produces a value from evidence or says plainly that it cannot:

    ``evidence-only types`` (C1, C3, C5, C7, C8, C10, C11, C12, C13)
        SS6: "no field write". ``{"set": {}}``. Named explicitly rather than
        skipped -- R13 wants one proposal per conflict, and these are the ones a
        human most needs to see.

    ``C6`` and ``C14``
        The target is the CRM-side path (SS6 ruling 8). The value is the **other
        endpoint of the same comparison row** -- the app-DB side -- because SS4.6
        survivorship makes the app DB authoritative for identity fields. Both
        endpoints are in ``observed_values`` by SS5.4's C6/C14 row ("one entry per
        disagreeing comparison, keyed by the source-qualified path ... value =
        that side's normalized value"), so the fix is read off the evidence and is
        never re-derived from a source.

    ``C4``
        Target ``crm.contact.email``; the value is the student's guardian email --
        derivable only when ``student_guardian_email_norms`` holds **exactly one**
        address. Two guardian addresses is a genuine ambiguity and the proposal
        carries an empty ``set`` and says so. (Held either way: SS6 classifies
        every C4 ``sensitive_hold``, and SS6 also forbids re-targeting a C4 at
        ``crm.contact.external_id`` to escape that.)

    ``C9``
        Target ``appdb.enrollment.crm_deal_id``; the value is ``None`` -- clear
        the stale pointer. Nothing in the evidence names a correct deal, and SS5.5
        states a null ``crm_deal_id`` is not a conflict, so the cleared state is
        one the committed rule set already calls clean. Conservative and always
        derivable.

    ``C2``
        Target ``payments.payment.external_ref``, and **no value is derivable**:
        SS5.6 makes the C2 population the only payments omitting both
        ``external_ref`` and the metadata name pair, with a ``payer_email`` used by
        no student and no contact. There is no candidate person anywhere in the
        data. The proposal names the target (which is what makes SS6's
        classification decidable) and carries ``{"set": {}}``.
    """
    observed = dict(observed_values or {})
    paths = tuple(disagreeing_fields)
    target = fix_target(conflict_type, paths)
    path = target.field_path

    if path is None:
        return _fix(conflict_type, None, None, False, "SS6: no field write for this type", {})

    if conflict_type in {"C6", "C14"}:
        row = COMPARED_FIELD_BY_PATH.get(path)
        if row is None:  # pragma: no cover - every SS6 C6/C14 target is a compared path
            return _fix(
                conflict_type, path, None, False, f"{path} is not a COMPARED_FIELDS path", {}
            )
        counterpart = row.right_path if path == row.left_path else row.left_path
        if counterpart not in observed:
            return _fix(
                conflict_type,
                path,
                None,
                False,
                f"authoritative endpoint {counterpart} absent from observed_values",
                {},
            )
        value = observed[counterpart]
        if observed_value_is_null(value):
            return _fix(
                conflict_type,
                path,
                None,
                False,
                f"authoritative endpoint {counterpart} was not observed",
                {},
            )
        return _fix(
            conflict_type,
            path,
            value,
            True,
            f"SS4.6 survivorship: write the authoritative {counterpart} value onto {path}",
            {path: value},
        )

    if conflict_type == "C4":
        guardians = observed.get("student_guardian_email_norms") or []
        if isinstance(guardians, str):
            guardians = [guardians]
        unique = sorted({str(value) for value in guardians if value})
        if len(unique) != 1:
            return _fix(
                conflict_type,
                path,
                None,
                False,
                (
                    f"{len(unique)} guardian addresses on the student -- which one the "
                    "contact should carry is a human decision, not a derivation"
                ),
                {},
            )
        return _fix(
            conflict_type,
            path,
            unique[0],
            True,
            "the student's single normalized guardian address is the address the "
            "contact should carry",
            {path: unique[0]},
        )

    if conflict_type == "C9":
        return _fix(
            conflict_type,
            path,
            None,
            True,
            (
                "clear the stale pointer: no evidence names a correct deal, and SS5.5 "
                "makes a null crm_deal_id a clean state rather than a conflict"
            ),
            {path: None},
        )

    if conflict_type == "C2":
        return _fix(
            conflict_type,
            path,
            None,
            False,
            (
                "no candidate person exists: SS5.6 makes the C2 population the only "
                "payments lacking both external_ref and the metadata name pair, with a "
                "payer_email used by no student and no contact"
            ),
            {},
        )

    # pragma: no cover -- SS6's table is total over CONFLICT_TYPES and every branch
    # above covers a row of it; this is the fail-loud arm for a new type.
    raise ValueError(f"no committed fix-value derivation for conflict type {conflict_type!r}")


def _fix(
    conflict_type: str,
    path: str | None,
    value: Any,
    derivable: bool,
    derivation: str,
    assignments: Mapping[str, Any],
) -> FixAction:
    return FixAction(
        conflict_type=conflict_type,
        target_path=path,
        value=value,
        derivable=derivable,
        derivation=derivation,
        action={"set": dict(assignments)},
    )


# =====================================================================================
# reading the durable evidence
# =====================================================================================
_SELECT_CONFLICTS = text(
    """
    SELECT id, fingerprint, type, rule_id, entity_refs, sources, disagreeing_fields,
           observed_values, oscillating, status::text, escalation_reason
      FROM conflicts
     ORDER BY fingerprint
    """
)

#: Every prior proposal, rejected ones included. The rejected rows are exactly what
#: R16's oscillation rule needs: a rejected proposal frees the fingerprint for the
#: partial unique index, so without them a re-proposal of a refused fix is invisible.
_SELECT_PROPOSALS = text("SELECT fingerprint, status::text, action FROM proposals")

_SELECT_CANDIDATES = text(
    """
    SELECT source_ref, key_class, resolved_ref
      FROM entity_link_candidates
     WHERE generation = :generation
    """
)

_INSERT_PROPOSAL = text(
    """
    INSERT INTO proposals
        (conflict_id, fingerprint, action, confidence, evidence, rationale,
         status, sensitive, created_run, target_canonical_id)
    VALUES (:conflict_id, :fingerprint, CAST(:action AS jsonb), :confidence,
            CAST(:evidence AS jsonb), :rationale,
            CAST(:status AS proposal_status), :sensitive, :created_run,
            CAST(:target_canonical_id AS uuid))
    RETURNING id
    """
)

_ESCALATE_CONFLICT = text(
    """
    UPDATE conflicts
       SET status = 'escalated'::conflict_status,
           escalation_reason = :reason
     WHERE id = :id
       AND (status <> 'escalated'::conflict_status
            OR escalation_reason IS DISTINCT FROM :reason)
    """
)


def _driver_connection(conn: Connection) -> Any:
    """The raw psycopg connection under a SQLAlchemy one.

    ``recon.invariants.oscillation.scan_field_lineage`` and
    ``recon.invariants.context.read_completeness`` are the committed readers for
    two of this module's inputs and both speak psycopg. Reaching through to the
    same underlying connection reuses them **inside this run's transaction**
    rather than re-implementing either query here -- which is the rule that keeps
    the A -> B -> A scan a single definition.
    """
    return conn.connection.driver_connection


def _load_conflicts(conn: Connection) -> list[ConflictRow]:
    rows: list[ConflictRow] = []
    for row in conn.execute(_SELECT_CONFLICTS):
        rows.append(
            ConflictRow(
                id=row.id,
                fingerprint=row.fingerprint,
                type=row.type,
                rule_id=row.rule_id,
                entity_refs=tuple(row.entity_refs or ()),
                sources_involved=tuple(row.sources or ()),
                disagreeing_fields=tuple(row.disagreeing_fields or ()),
                observed_values=dict(row.observed_values or {}),
                oscillating=bool(row.oscillating),
                status=row.status,
                escalation_reason=row.escalation_reason,
            )
        )
    return rows


def _load_candidate_index(
    conn: Connection, generation: int
) -> dict[str, dict[str, frozenset[str]]]:
    """``source_ref -> key_class -> {resolved_ref}`` for one generation.

    One query for the whole run rather than one per conflict: the identity
    signals are read for every conflict, and 14 round trips per conflict over a
    2,600-conflict run is the difference between a scheduled job and a stalled
    one.
    """
    index: dict[str, dict[str, set[str]]] = {}
    for source_ref, key_class, resolved_ref in conn.execute(
        _SELECT_CANDIDATES, {"generation": generation}
    ):
        index.setdefault(source_ref, {}).setdefault(key_class, set()).add(resolved_ref)
    return {
        source_ref: {key_class: frozenset(refs) for key_class, refs in classes.items()}
        for source_ref, classes in index.items()
    }


def _load_prior_proposals(conn: Connection) -> tuple[frozenset[str], dict[str, list[str]]]:
    """``(fingerprints with an open proposal, fingerprint -> prior action JSONs)``.

    The first set implements fingerprint dedup; the second implements R16's
    "never the identical fix", which has to be able to see the rejected rows the
    first set deliberately excludes.
    """
    open_fingerprints: set[str] = set()
    prior_actions: dict[str, list[str]] = {}
    for fingerprint, status, action in conn.execute(_SELECT_PROPOSALS):
        if status != "rejected":
            open_fingerprints.add(fingerprint)
        prior_actions.setdefault(fingerprint, []).append(canonical_json(action))
    return frozenset(open_fingerprints), prior_actions


# =====================================================================================
# the packet
# =====================================================================================
def _identity_agreement(
    conflict: ConflictRow,
    candidates: Mapping[str, Mapping[str, frozenset[str]]],
    key_classes: Iterable[str],
) -> tuple[dict[str, tuple[str, ...]], tuple[str, ...]]:
    """Which match-key classes AGREE WITH EACH OTHER that these records are one person.

    Returns ``(matches, contradictions)``: the classes that corroborate the
    identity, and the contact refs whose classes contradict one another.

    Per contact ref, each committed key class is resolved through
    ``entity_link_candidates`` -- which contract SS4.7 requires to hold **every**
    match-key resolution, accepted or not -- and intersected with the conflict's
    app-DB student refs. A class counts as agreement only when **every** class
    that resolved at all lands on a common student. If two classes reach two
    different students, none of them is agreement and the contact is recorded as
    a contradiction.

    **Why the mutual-consistency test, and not "does this class reach any of the
    students":** the weaker test was the first implementation and the real data
    falsified it. C10 -- merge-collapsed record -- is *defined* by one contact
    whose ``ext`` key and ``namedob`` key resolve to two **different** students
    (SS5.5), and its ``entity_refs`` carry both. Under the weaker test all three
    classes "matched", so all 50 C10s scored ``0.30 + 0.35 + 0.25 + 0.20 = 1.10``
    -> clamped to **1.0000** -- maximum confidence for the one conflict type whose
    defining property is that the automation cannot tell which student this is,
    and whose base is the lowest in the model precisely to say so. Two keys
    pointing at two different people is the strongest possible evidence
    *against* an identity, and reading it as three independent agreements
    inverted the signal. With the consistency test C10 scores its base, less the
    conflicting-evidence penalty.

    Reading candidates rather than ``entity_links`` is also deliberate.
    ``entity_links`` keeps only the winning link (one row per source record per
    generation, ``method`` = the first cascade rule that fired), so a person
    linked by ``L1`` shows no evidence that email and name+dob also agree -- the
    three signals would collapse into "which rule happened to fire first" and
    could never add. The candidates table is where the independence lives.
    """
    matches: dict[str, set[str]] = {}
    contradictions: list[str] = []
    students = set(conflict.student_refs)
    if not students:
        return {}, ()

    classes = tuple(key_classes)
    for contact_ref in conflict.contact_refs:
        resolved = {
            key_class: candidates.get(contact_ref, {}).get(key_class, frozenset()) & students
            for key_class in classes
        }
        answering = {key_class: refs for key_class, refs in resolved.items() if refs}
        if not answering:
            continue
        consensus = set.intersection(*(set(refs) for refs in answering.values()))
        if not consensus:
            contradictions.append(contact_ref)
            continue
        for key_class in answering:
            matches.setdefault(key_class, set()).update(consensus)

    return (
        {key_class: tuple(sorted(refs)) for key_class, refs in matches.items()},
        tuple(sorted(contradictions)),
    )


def build_packet(
    conflict: ConflictRow,
    *,
    run_id: str,
    generation: int,
    model: ConfidenceModel,
    candidates: Mapping[str, Mapping[str, frozenset[str]]],
    incomplete_sources: Sequence[str],
    oscillating: bool,
    oscillation_source: str,
    lineage_rows: int,
) -> EvidencePacket:
    """Assemble the evidence packet, score it, and classify it.

    Kept as one function taking plain values so a test can build a packet with no
    database at all -- the arithmetic and the classification are then checkable
    without the ingest pipeline, and the pipeline test checks that the real
    inputs reach it.
    """
    key_class_by_signal = model.key_class_signals()
    matches, contradictions = _identity_agreement(conflict, candidates, key_class_by_signal)

    observed = conflict.observed_values
    corroborating = model.corroborating_keys(conflict.type)
    corroborated = bool(corroborating) and all(
        key in observed and not observed_value_is_null(observed[key]) for key in corroborating
    )

    reasons = partial_evidence_reasons(
        incomplete_sources=list(incomplete_sources),
        observed_values=conflict.observed_values,
        sources_involved=list(conflict.sources_involved),
        contradictory_match_keys=contradictions,
    )

    signal_kwargs: dict[str, Any] = {
        "conflict_type": conflict.type,
        "amount_date_corroboration": corroborated,
        "disagreeing_field": disagreeing_row_count(conflict.disagreeing_fields),
        "partial_evidence": bool(reasons),
        "partial_evidence_reasons": reasons,
        "oscillation_observed": oscillating,
    }
    for key_class, signal_name in key_class_by_signal.items():
        signal_kwargs[signal_name] = key_class in matches

    signals = Signals(**signal_kwargs)
    computed = score(signals, model=model)

    classification = classify(conflict.type, conflict.disagreeing_fields)
    action = fix_action(
        conflict.type,
        disagreeing_fields=conflict.disagreeing_fields,
        observed_values=conflict.observed_values,
    )

    # The two must name the same path or one of them is wrong about what this
    # proposal touches -- and the classifier's path is the one R15 is written in
    # terms of. Raising here beats writing a proposal whose action edits a field
    # its classification never considered.
    if action.target_path != classification.target_path:
        raise ValueError(
            f"{conflict.type} {conflict.fingerprint}: fix action targets "
            f"{action.target_path!r} but the classifier ruled on "
            f"{classification.target_path!r}"
        )

    nulls = tuple(sorted(key for key, value in observed.items() if observed_value_is_null(value)))

    return EvidencePacket(
        conflict=conflict,
        person_key=str(person_key(conflict.entity_refs)),
        generation=generation,
        incomplete_sources=tuple(sorted(incomplete_sources)),
        key_class_matches=matches,
        key_class_contradictions=contradictions,
        null_observed_values=nulls,
        oscillation_source=oscillation_source,
        lineage_rows=lineage_rows,
        signals=signals,
        score=computed,
        classification=classification,
        fix=action,
        run_id=run_id,
    )


# =====================================================================================
# the rationale seam (empty in T-7 -- see the module docstring)
# =====================================================================================
RationaleHook = Callable[[EvidencePacket], str | None]


def no_rationale(packet: EvidencePacket) -> None:
    """The default hook: no LLM call, rationale ``NULL``.

    T-7's non-goals are "no LLM calls". T-8 passes
    ``recon.llm.generate_rationale`` in here; nothing else in this module changes,
    and the proposal is identical either way except for one nullable text column.
    """
    del packet
    return None


# =====================================================================================
# the run
# =====================================================================================
#: Which input decided ``oscillation_observed``. Recorded in every packet.
OSCILLATION_FROM_SCAN: Final = "lineage_scan"
OSCILLATION_FROM_ROW: Final = "conflict_row"
OSCILLATION_NO_INPUT: Final = "no_lineage_and_no_flag"


def oscillation_state(conflict: ConflictRow, scan: Any) -> tuple[bool, str]:
    """Did this conflict's field oscillate, and which input said so?

    Two inputs, in a fixed order:

    1. the live ``field_lineage`` A -> B -> A scan, but **only when it has rows to
       read**. ``LineageScan.had_input`` is what distinguishes "scanned and found
       none" from "there was nothing to scan", and treating the second as the
       first is exactly the confident ``false`` contract SS7 warns about;
    2. otherwise the stored ``conflicts.oscillating`` column, stamped by the
       invariant run that detected the conflict.

    The returned label goes into the packet, so a reviewer always knows which
    answer they are reading -- and ``OSCILLATION_NO_INPUT`` says plainly that
    neither input had anything to say.
    """
    if scan is not None and getattr(scan, "had_input", False):
        from recon.invariants.oscillation import OSCILLATION_TYPES

        if conflict.type in OSCILLATION_TYPES and conflict.disagreeing_fields:
            key = str(person_key(conflict.entity_refs))
            if any(scan.oscillates(key, path) for path in conflict.disagreeing_fields):
                return True, OSCILLATION_FROM_SCAN
        if conflict.oscillating:
            return True, OSCILLATION_FROM_ROW
        return False, OSCILLATION_FROM_SCAN
    if conflict.oscillating:
        return True, OSCILLATION_FROM_ROW
    return False, OSCILLATION_NO_INPUT


def reconcile(
    *,
    conn: Connection | None = None,
    run_id: str | None = None,
    generation: int = CURRENT_GENERATION,
    rationale: RationaleHook = no_rationale,
    model: ConfidenceModel | None = None,
) -> ReconcileReport:
    """One reconciler pass. Writes proposals, audit rows, and nothing else.

    ``conn`` is injectable so a test can drive the run inside its own
    transaction; the default opens a ``recon_writer`` connection, which is the
    principal the whole write boundary is built around.
    """
    if conn is None:
        with role_connection(ROLE_RECON_WRITER) as owned:
            return reconcile(
                conn=owned,
                run_id=run_id,
                generation=generation,
                rationale=rationale,
                model=model,
            )

    started = time.perf_counter()
    active = model or load_model()

    conflicts = _load_conflicts(conn)
    open_fingerprints, prior_actions = _load_prior_proposals(conn)
    candidates = _load_candidate_index(conn, generation)

    from recon.invariants.context import read_completeness
    from recon.invariants.oscillation import scan_field_lineage

    driver = _driver_connection(conn)
    incomplete = tuple(sorted({source for source, _ in read_completeness(driver, generation)}))
    scan = scan_field_lineage(driver)

    resolved_run_id = run_id or _derive_run_id(conflicts)

    outcomes: list[ProposalOutcome] = []
    by_type: dict[str, int] = {}
    escalated = 0
    evidence_only = 0

    for conflict in conflicts:
        oscillating, source = oscillation_state(conflict, scan)

        if oscillating:
            escalated += _escalate(conn, conflict)

        packet = build_packet(
            conflict,
            run_id=resolved_run_id,
            generation=generation,
            model=active,
            candidates=candidates,
            incomplete_sources=incomplete,
            oscillating=oscillating,
            oscillation_source=source,
            lineage_rows=scan.rows,
        )

        skip = _skip_reason(packet, oscillating, open_fingerprints, prior_actions)
        if skip is not None:
            outcomes.append(
                ProposalOutcome(
                    fingerprint=conflict.fingerprint,
                    conflict_type=conflict.type,
                    proposed=False,
                    status=None,
                    confidence=None,
                    skip_reason=skip,
                    escalated=oscillating,
                )
            )
            continue

        text_rationale = _rationale(rationale, packet)
        proposal_id = _insert_proposal(conn, packet, rationale=text_rationale)
        _audit_proposal(conn, packet, proposal_id=proposal_id, rationale=text_rationale)

        by_type[conflict.type] = by_type.get(conflict.type, 0) + 1
        if packet.classification.evidence_only:
            evidence_only += 1
        # Keep the in-process view of "already proposed" in step with the table,
        # so a duplicate fingerprint inside ONE run is caught by the control and
        # not only by the unique index.
        open_fingerprints = open_fingerprints | {conflict.fingerprint}
        prior_actions.setdefault(conflict.fingerprint, []).append(canonical_json(packet.fix.action))
        outcomes.append(
            ProposalOutcome(
                fingerprint=conflict.fingerprint,
                conflict_type=conflict.type,
                proposed=True,
                status=packet.classification.status,
                confidence=packet.score.value,
                proposal_id=proposal_id,
                escalated=oscillating,
                rationale_attached=text_rationale is not None,
            )
        )

    report = ReconcileReport(
        run_id=resolved_run_id,
        generation=generation,
        conflicts_seen=len(conflicts),
        proposed=sum(1 for outcome in outcomes if outcome.proposed),
        pending=sum(1 for outcome in outcomes if outcome.status == "pending"),
        sensitive_hold=sum(1 for outcome in outcomes if outcome.status == "sensitive_hold"),
        evidence_only=evidence_only,
        skipped_fingerprint=sum(1 for o in outcomes if o.skip_reason == SKIP_FINGERPRINT),
        skipped_oscillation=sum(1 for o in outcomes if o.skip_reason == SKIP_OSCILLATION),
        escalated_oscillation=escalated,
        rationale_attached=sum(1 for outcome in outcomes if outcome.rationale_attached),
        lineage_rows=scan.rows,
        model_version=active.version,
        by_type=dict(sorted(by_type.items())),
        outcomes=tuple(outcomes),
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
    )

    insert_audit_row(
        conn,
        actor=AUDIT_ACTOR,
        action="reconcile.run",
        subject=resolved_run_id,
        body=report.audit_body(),
    )
    # `audit_body()` here too, for the same reason: `recon.logging`'s redaction
    # processor is the same default-deny redactor, so logging `as_dict()` emits an
    # operational line whose count keys are all tokens.
    log.info("reconciler.run", **report.audit_body())
    return report


def _skip_reason(
    packet: EvidencePacket,
    oscillating: bool,
    open_fingerprints: frozenset[str] | set[str],
    prior_actions: Mapping[str, Sequence[str]],
) -> str | None:
    """R16's two rules, in the order they apply. ``None`` means propose."""
    fingerprint = packet.conflict.fingerprint
    if fingerprint in open_fingerprints:
        return SKIP_FINGERPRINT
    if oscillating and canonical_json(packet.fix.action) in set(prior_actions.get(fingerprint, ())):
        return SKIP_OSCILLATION
    return None


def _escalate(conn: Connection, conflict: ConflictRow) -> int:
    """Mark the conflict ``escalated:oscillation``; return 1 if the row moved."""
    result = conn.execute(_ESCALATE_CONFLICT, {"id": conflict.id, "reason": ESCALATION_OSCILLATION})
    if result.rowcount:
        insert_audit_row(
            conn,
            actor=AUDIT_ACTOR,
            action="conflict.escalated",
            subject=conflict.fingerprint,
            body={
                "conflict_id": conflict.id,
                "type": conflict.type,
                "fingerprint": conflict.fingerprint,
                "status": "escalated",
                "label": f"escalated:{ESCALATION_OSCILLATION}",
                "disagreeing_fields": list(conflict.disagreeing_fields),
                "rule": (
                    "R16/SS7: the field re-asserted a previous value across generations; "
                    "the identical fix is never re-proposed"
                ),
            },
        )
    return 1 if result.rowcount else 0


def _rationale(hook: RationaleHook, packet: EvidencePacket) -> str | None:
    """Call the rationale seam. A failure is logged and the proposal still lands."""
    try:
        return hook(packet)
    except Exception as exc:  # the brief's rule: the proposal lands regardless
        log.warning(
            "reconciler.rationale_failed",
            fingerprint=packet.conflict.fingerprint,
            error=f"{type(exc).__name__}: {exc}",
            detail="rationale is a nicety; the proposal lands with rationale NULL",
        )
        return None


def _insert_proposal(conn: Connection, packet: EvidencePacket, *, rationale: str | None) -> int:
    classification = packet.classification
    row = conn.execute(
        _INSERT_PROPOSAL,
        {
            "conflict_id": packet.conflict.id,
            "fingerprint": packet.conflict.fingerprint,
            "action": canonical_json(packet.fix.action),
            "confidence": packet.score.value,
            "evidence": canonical_json(packet.as_dict()),
            "rationale": rationale,
            "status": classification.status,
            "sensitive": classification.sensitive,
            "created_run": packet.run_id,
            "target_canonical_id": packet.person_key,
        },
    ).one()
    return int(row.id)


def _audit_proposal(
    conn: Connection, packet: EvidencePacket, *, proposal_id: int, rationale: str | None
) -> None:
    """R18: the proposal, its confidence, and the reviewer-facing facts.

    Routed through ``recon.logging.insert_audit_row``, the redacting chokepoint,
    so ``actor``/``action``/``subject`` and the body all pass the committed
    redactor -- and so SQLSTATE ``KS003`` sees a ``^system:`` actor.

    **Every key below is on ``recon.privacy.SAFE_KEYS``, and that is not a
    coincidence -- it is the constraint.** The redactor is default-deny at all
    three leaf positions, *mapping keys included*: a key outside the committed
    vocabulary is replaced by a token in ``LOG_MODE=safe``, which is the default
    and the production setting. The first draft of this body used the names that
    read best in Python -- ``conflict_type``, ``target_path``,
    ``classification_reason``, ``confidence_terms`` -- and every one of them was
    tokenised, producing an audit row that reconciled with nothing. An audit row
    whose keys are tokens is not a reviewer-facing record, so the body speaks the
    committed vocabulary instead:

    ``type`` / ``field_path`` / ``rule``
        the conflict type, the fix target path, and the classifier's reason.
    ``label``
        the confidence arithmetic on one line -- base, every non-zero term, and
        the result -- because the *derivation* is what makes the number
        reviewable and the full packet lives on the proposal.
    ``version``
        the ``confidence.yaml`` version that scored it, so a later weight change
        cannot silently reinterpret an old row.

    The unredacted, complete evidence packet is on ``proposals.evidence``; this
    row additionally carries ``detail.body_sha256``, a hash of the **raw** body,
    so the redacted preview is provably a preview of that exact content.
    """
    score = packet.score
    terms = " ".join(
        f"{term.name}={term.value}*{term.weight}" for term in score.terms if term.contribution != 0
    )
    insert_audit_row(
        conn,
        actor=AUDIT_ACTOR,
        action="proposal.created",
        subject=packet.conflict.fingerprint,
        body={
            "proposal_id": proposal_id,
            "conflict_id": packet.conflict.id,
            "fingerprint": packet.conflict.fingerprint,
            "type": packet.conflict.type,
            "rule_id": packet.conflict.rule_id,
            "status": packet.classification.status,
            "sensitive": packet.classification.sensitive,
            "disposition": packet.classification.disposition,
            "field_path": packet.classification.target_path,
            "action": dict(packet.fix.action),
            "confidence": str(score.value),
            "version": score.model_version,
            "label": f"base={score.base} {terms} => {score.value}".replace("  ", " "),
            "rule": packet.classification.reason,
            "oscillating": packet.signals.oscillation_observed,
            "disagreeing_fields": list(packet.conflict.disagreeing_fields),
            "sources": list(packet.conflict.sources_involved),
            "target_canonical_id": packet.person_key,
            "created_run": packet.run_id,
            "outcome": "rationale_attached" if rationale is not None else "rationale_null",
        },
    )


def _derive_run_id(conflicts: Sequence[ConflictRow]) -> str:
    """A run id derived from the conflict set, not from a clock or a uuid4.

    Two consequences, both wanted. A re-run over an unchanged conflict store
    produces the **same** run id, which is what DESIGN means by "idempotent per
    run id". And nothing on a graded path calls ``uuid4()`` or ``datetime.now()``
    -- the project's determinism rule -- so two runs from the same seed are
    comparable byte for byte, run id included.
    """
    payload = "\n".join(sorted(conflict.fingerprint for conflict in conflicts))
    return "recon-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def run_once() -> ReconcileReport:
    """One scheduled pass, with no arguments -- ``recon.suite.mirror``'s entrypoint.

    ``check_mirror_unchanged`` hashes every landing and staging table, calls this,
    and hashes them again. That check is the acceptance evidence for R13's
    "production/mirror data is unchanged by the run", and it can only be that if
    this function is the real run rather than a stub the check happens to call.
    """
    return reconcile()


# Import-time totality guard: every committed conflict type must have a fix-value
# derivation. A missing branch would otherwise surface as a ValueError mid-run, on
# whichever type happened to arrive first.
for _conflict_type in CONFLICT_TYPES:  # pragma: no cover - import guard
    fix_action(_conflict_type)
del _conflict_type
