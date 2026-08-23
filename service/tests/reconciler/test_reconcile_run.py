"""R13/R15/R16/R18 against the real conflict store, as ``recon_writer``.

Every test here runs over the conflicts the committed invariant engine actually
detected on generation 3 of the committed fixtures -- not a hand-built row. The
counts asserted are therefore counts of real work.

The connection is authenticated **as** ``recon_writer`` and its transaction is
rolled back afterwards. Both halves matter: the schema owner bypasses its own
grants, so a suite that connected as the owner would leave the entire
holds-before-writes boundary untested while every assertion still passed; and the
rollback is what lets each test see the same pristine store.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from recon.confidence import load_model
from recon.db import ROLE_RECON_WRITER, role_connection
from recon.privacy import canonical_json
from recon.reconciler import AUDIT_ACTOR, ReconcileReport, reconcile
from recon.suite.mirror import MIRROR_TABLES, mirror_digest

pytestmark = pytest.mark.usefixtures("conflict_store")


def _scalar(conn: Connection, sql: str, **params: object) -> int:
    return int(conn.execute(text(sql), params).scalar_one())


def _table_counts(conn: Connection) -> dict[str, int]:
    """Row counts for every table in the schema, so "nothing else moved" is checkable."""
    names = [
        row[0]
        for row in conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                "ORDER BY table_name"
            )
        )
    ]
    return {name: _scalar(conn, f'SELECT count(*) FROM "{name}"') for name in names}


@pytest.fixture
def run(writer: Connection) -> ReconcileReport:
    """One real reconciler pass inside the test's rolled-back transaction."""
    return reconcile(conn=writer, run_id="t7-test-run")


# =====================================================================================
# R13: N conflicts -> N pending proposals
# =====================================================================================
def test_n_eligible_conflicts_produce_exactly_n_proposals(
    writer: Connection, run: ReconcileReport
) -> None:
    conflicts = _scalar(writer, "SELECT count(*) FROM conflicts")
    proposals = _scalar(writer, "SELECT count(*) FROM proposals")
    assert conflicts > 0, "the store held no conflicts; this test would prove nothing"
    assert run.conflicts_seen == conflicts
    assert run.proposed == conflicts
    assert proposals == conflicts
    assert run.skipped_fingerprint == 0
    assert run.skipped_oscillation == 0


def test_exactly_one_proposal_per_conflict_id(writer: Connection, run: ReconcileReport) -> None:
    """ "Exactly one" is a uniqueness claim, not just a total."""
    del run
    duplicates = _scalar(
        writer,
        "SELECT count(*) FROM (SELECT conflict_id FROM proposals "
        "GROUP BY conflict_id HAVING count(*) > 1) d",
    )
    assert duplicates == 0
    orphans = _scalar(
        writer,
        "SELECT count(*) FROM conflicts c "
        "WHERE NOT EXISTS (SELECT 1 FROM proposals p WHERE p.conflict_id = c.id)",
    )
    assert orphans == 0


def test_every_proposal_is_born_pending_or_sensitive_hold(
    writer: Connection, run: ReconcileReport
) -> None:
    del run
    statuses = {
        row[0]: row[1]
        for row in writer.execute(
            text("SELECT status::text, count(*) FROM proposals GROUP BY status")
        )
    }
    assert set(statuses) <= {"pending", "sensitive_hold"}, statuses
    undecided = _scalar(
        writer,
        "SELECT count(*) FROM proposals WHERE decided_by IS NOT NULL OR decided_at IS NOT NULL",
    )
    assert undecided == 0


def test_every_proposal_carries_a_confidence_in_range_and_a_target(
    writer: Connection, run: ReconcileReport
) -> None:
    del run
    assert (
        _scalar(writer, "SELECT count(*) FROM proposals WHERE confidence < 0 OR confidence > 1")
        == 0
    )
    assert _scalar(writer, "SELECT count(*) FROM proposals WHERE target_canonical_id IS NULL") == 0
    assert _scalar(writer, "SELECT count(*) FROM proposals WHERE evidence = 'null'::jsonb") == 0


def test_every_action_matches_the_closed_vocabulary(
    writer: Connection, run: ReconcileReport
) -> None:
    """The CHECK would have refused the INSERT; this asserts the shape we meant."""
    del run
    wrong = _scalar(
        writer,
        "SELECT count(*) FROM proposals "
        "WHERE jsonb_typeof(action) <> 'object' "
        "   OR (SELECT count(*) FROM jsonb_object_keys(action)) <> 1 "
        "   OR NOT action ? 'set' "
        "   OR jsonb_typeof(action -> 'set') <> 'object'",
    )
    assert wrong == 0


def test_evidence_only_types_landed_with_an_empty_set(
    writer: Connection, run: ReconcileReport
) -> None:
    """SS6's "no field write" types are PRESENT in the queue, not skipped."""
    del run
    rows = {
        row[0]: (row[1], row[2])
        for row in writer.execute(
            text(
                "SELECT c.type, count(*), "
                "       count(*) FILTER (WHERE p.action = '{\"set\": {}}'::jsonb) "
                "  FROM proposals p JOIN conflicts c ON c.id = p.conflict_id "
                " GROUP BY c.type"
            )
        )
    }
    for conflict_type in ("C1", "C3", "C5", "C7", "C8", "C10", "C11", "C12", "C13"):
        if conflict_type not in rows:
            continue
        total, empty = rows[conflict_type]
        assert total == empty, f"{conflict_type} produced a field write; SS6 says evidence-only"


# =====================================================================================
# R13: the mirror is unchanged -- the graded acceptance criterion
# =====================================================================================
def test_the_read_only_mirror_is_byte_unchanged_by_the_run(writer: Connection) -> None:
    """Hash every landing and staging table, run, hash again, compare.

    Uses ``recon.suite.mirror``'s digest -- the same function the scorecard row
    uses -- so this test and the scorecard cannot disagree about what "unchanged"
    means. The digest is taken on the SAME connection the run writes through, so
    it sees the run's uncommitted inserts: if the reconciler touched a mirror
    table, the second digest would move.
    """
    before = mirror_digest(writer)
    assert sum(before.row_counts.values()) > 0, "an empty mirror would make this vacuous"

    reconcile(conn=writer, run_id="t7-mirror-run")

    after = mirror_digest(writer)
    assert before.changed_tables(after) == ()
    assert before.combined() == after.combined()
    for table in MIRROR_TABLES:
        assert before.digests[table] == after.digests[table], table
        assert before.row_counts[table] == after.row_counts[table], table


def test_the_run_writes_only_proposals_audit_and_conflicts(writer: Connection) -> None:
    """Wider than the mirror check: NOTHING else in the schema moves either.

    ``entities`` is the one that matters most -- a reconciler that could append a
    canonical row would be writing production data on a path with no human in it.
    """
    before = _table_counts(writer)
    reconcile(conn=writer, run_id="t7-scope-run")
    after = _table_counts(writer)

    grew = {name: (before[name], after[name]) for name in before if after[name] != before[name]}
    assert set(grew) <= {"proposals", "audit_log"}, grew
    assert after["entities"] == before["entities"]
    assert after["proposal_events"] == before["proposal_events"]
    assert after["budget_ledger"] == before["budget_ledger"]
    assert after["conflicts"] == before["conflicts"], "conflicts is UPDATEd, never INSERTed"


def test_recon_writer_cannot_approve_its_own_proposal(
    writer: Connection, run: ReconcileReport
) -> None:
    """The automation must not be able to decide the work it proposed.

    Not a code convention: ``recon_writer`` holds no UPDATE on ``proposals`` at
    all, so this is the grant refusing, and it refuses whatever the confidence is.
    """
    del run
    proposal_id = writer.execute(text("SELECT id FROM proposals LIMIT 1")).scalar_one()
    with pytest.raises(DBAPIError):
        writer.execute(
            text("UPDATE proposals SET status = 'approved' WHERE id = :id"), {"id": proposal_id}
        )
    writer.rollback()


# =====================================================================================
# R15: sensitive classification, and the trigger behind it
# =====================================================================================
def test_every_c14_conflict_is_born_sensitive_hold(
    writer: Connection, run: ReconcileReport
) -> None:
    del run
    total, held = writer.execute(
        text(
            "SELECT count(*), count(*) FILTER (WHERE p.status = 'sensitive_hold' AND p.sensitive) "
            "  FROM proposals p JOIN conflicts c ON c.id = p.conflict_id "
            " WHERE c.type = 'C14'"
        )
    ).one()
    assert total > 0, "no C14 conflicts were detected; this assertion would be vacuous"
    assert held == total


def test_every_c4_conflict_is_born_sensitive_hold(writer: Connection, run: ReconcileReport) -> None:
    """SS6 states it as an intended consequence: all C4 proposals are held."""
    del run
    total, held = writer.execute(
        text(
            "SELECT count(*), count(*) FILTER (WHERE p.status = 'sensitive_hold') "
            "  FROM proposals p JOIN conflicts c ON c.id = p.conflict_id "
            " WHERE c.type = 'C4'"
        )
    ).one()
    assert total > 0
    assert held == total


def test_every_proposal_whose_target_is_sensitive_is_held(
    writer: Connection, run: ReconcileReport
) -> None:
    """The general rule, over whatever targets the run actually produced."""
    del run
    from recon.reference import SENSITIVE_FIELDS

    rows = writer.execute(
        text(
            "SELECT evidence #>> '{classification,target_path}', status::text, count(*) "
            "  FROM proposals GROUP BY 1, 2"
        )
    ).all()
    assert rows
    for target_path, status, count in rows:
        if target_path in SENSITIVE_FIELDS:
            assert status == "sensitive_hold", (target_path, status, count)


def test_a_held_proposal_is_held_at_every_confidence_including_one(
    writer: Connection, run: ReconcileReport
) -> None:
    """R15's actual claim: confidence never unlocks a hold.

    Asserted over the real distribution rather than a constructed example -- if
    any held proposal scored high, it is still held.
    """
    del run
    highest = writer.execute(
        text("SELECT max(confidence) FROM proposals WHERE sensitive")
    ).scalar_one()
    leaked = _scalar(
        writer, "SELECT count(*) FROM proposals WHERE sensitive AND status <> 'sensitive_hold'"
    )
    assert leaked == 0
    assert highest is not None


def test_the_database_refuses_a_sensitive_proposal_born_pending(writer: Connection) -> None:
    """The KS002 backstop, tested as an INDEPENDENT control.

    ``recon.sensitive`` is what decides; this is what the database does if the
    decision is ever wrong. Testing them separately is the point -- a suite that
    only tested the trigger would pass with no classifier at all.
    """
    conflict_id = writer.execute(text("SELECT id FROM conflicts LIMIT 1")).scalar_one()
    with pytest.raises(DBAPIError) as excinfo:
        writer.execute(
            text(
                "INSERT INTO proposals (conflict_id, fingerprint, action, confidence, evidence, "
                "status, sensitive, created_run, target_canonical_id) "
                "VALUES (:cid, 'ks002-probe', '{\"set\": {}}'::jsonb, 1.0, '{}'::jsonb, "
                "'pending', true, 'probe', gen_random_uuid())"
            ),
            {"cid": conflict_id},
        )
    assert getattr(excinfo.value.orig, "sqlstate", None) == "KS002"
    writer.rollback()


def test_the_database_refuses_an_action_outside_the_closed_vocabulary(
    writer: Connection,
) -> None:
    conflict_id = writer.execute(text("SELECT id FROM conflicts LIMIT 1")).scalar_one()
    with pytest.raises(IntegrityError):
        writer.execute(
            text(
                "INSERT INTO proposals (conflict_id, fingerprint, action, confidence, evidence, "
                "status, sensitive, created_run, target_canonical_id) "
                "VALUES (:cid, 'vocab-probe', '{\"unset\": [\"x\"]}'::jsonb, 0.5, '{}'::jsonb, "
                "'pending', false, 'probe', gen_random_uuid())"
            ),
            {"cid": conflict_id},
        )
    writer.rollback()


# =====================================================================================
# R16: fingerprint dedup
# =====================================================================================
def test_a_second_run_with_no_source_change_creates_zero_new_proposals(
    writer: Connection,
) -> None:
    """The R16 acceptance criterion, proved by counting."""
    first = reconcile(conn=writer, run_id="t7-dedup-1")
    after_first = _scalar(writer, "SELECT count(*) FROM proposals")
    assert first.proposed == after_first > 0

    second = reconcile(conn=writer, run_id="t7-dedup-2")
    after_second = _scalar(writer, "SELECT count(*) FROM proposals")

    assert second.proposed == 0
    assert second.skipped_fingerprint == first.conflicts_seen
    assert after_second == after_first


def test_the_database_backstops_the_dedup_with_a_unique_index(
    writer: Connection, run: ReconcileReport
) -> None:
    """The control is the code; ``uq_proposals_open_fingerprint`` is the backstop."""
    del run
    row = writer.execute(
        text("SELECT conflict_id, fingerprint, target_canonical_id FROM proposals LIMIT 1")
    ).one()
    with pytest.raises(IntegrityError):
        writer.execute(
            text(
                "INSERT INTO proposals (conflict_id, fingerprint, action, confidence, evidence, "
                "status, sensitive, created_run, target_canonical_id) "
                "VALUES (:cid, :fp, '{\"set\": {}}'::jsonb, 0.5, '{}'::jsonb, "
                "'pending', false, 'probe', :target)"
            ),
            {"cid": row.conflict_id, "fp": row.fingerprint, "target": row.target_canonical_id},
        )
    writer.rollback()


# =====================================================================================
# R14 end to end: the stored score is reproducible and its inputs are stored with it
# =====================================================================================
def test_two_independent_runs_produce_an_identical_confidence_vector(
    conflict_store: str,
) -> None:
    """DESIGN's determinism check, over the real conflict set.

    Two separate connections, two separate transactions, both rolled back. The
    second run therefore scores the same conflicts from scratch rather than
    reading the first run's rows.
    """
    del conflict_store
    with role_connection(ROLE_RECON_WRITER, commit=False) as first_conn:
        first = reconcile(conn=first_conn, run_id="t7-determinism-a")
    with role_connection(ROLE_RECON_WRITER, commit=False) as second_conn:
        second = reconcile(conn=second_conn, run_id="t7-determinism-b")

    assert first.proposed == second.proposed > 0
    assert first.confidence_vector() == second.confidence_vector()
    assert first.confidence_digest() == second.confidence_digest()


def test_every_proposal_stores_the_arithmetic_that_produced_its_score(
    writer: Connection, run: ReconcileReport
) -> None:
    """R14 calls the signals inspectable; that means recorded, not recomputable.

    The recorded base plus the recorded contributions must equal the recorded
    total, and the recorded confidence must equal the stored column -- checked in
    SQL over every row, so one drifting proposal fails.
    """
    del run
    model = load_model()
    mismatched = _scalar(
        writer,
        """
        SELECT count(*) FROM proposals
         WHERE (evidence #>> '{confidence,confidence}')::numeric <> confidence
            OR (evidence #>> '{confidence,model_version}')::int <> :version
            OR evidence #> '{confidence,terms}' IS NULL
            OR jsonb_array_length(evidence #> '{confidence,terms}') <> :signals
        """,
        version=model.version,
        signals=len(model.signal_order),
    )
    assert mismatched == 0

    sample = writer.execute(text("SELECT evidence FROM proposals LIMIT 200")).scalars().all()
    from decimal import Decimal

    for evidence in sample:
        block = evidence["confidence"]
        total = Decimal(block["base"]) + sum(
            Decimal(term["contribution"]) for term in block["terms"]
        )
        assert total == Decimal(block["raw_total"])


def test_every_proposal_carries_the_evidence_the_conflict_was_detected_on(
    writer: Connection, run: ReconcileReport
) -> None:
    """R13: "with a confidence score and the evidence used"."""
    del run
    missing = _scalar(
        writer,
        """
        SELECT count(*) FROM proposals p JOIN conflicts c ON c.id = p.conflict_id
         WHERE p.evidence #>> '{conflict,fingerprint}' IS DISTINCT FROM c.fingerprint
            OR p.evidence #> '{conflict,observed_values}' IS DISTINCT FROM
               coalesce(c.observed_values, '{}'::jsonb)
            OR p.evidence #> '{conflict,entity_refs}' IS DISTINCT FROM c.entity_refs
            OR p.evidence #> '{completeness}' IS NULL
            OR p.evidence #> '{identity}' IS NULL
            OR p.evidence #> '{fix}' IS NULL
        """,
    )
    assert missing == 0


# =====================================================================================
# R18: the audit log
# =====================================================================================
def test_every_proposal_lands_an_audit_row_through_the_chokepoint(
    writer: Connection, run: ReconcileReport
) -> None:
    created = _scalar(writer, "SELECT count(*) FROM audit_log WHERE action = 'proposal.created'")
    assert created == run.proposed

    runs = _scalar(writer, "SELECT count(*) FROM audit_log WHERE action = 'reconcile.run'")
    assert runs == 1

    foreign_actor = _scalar(
        writer, "SELECT count(*) FROM audit_log WHERE actor NOT LIKE 'system:%'"
    )
    assert foreign_actor == 0
    assert _scalar(writer, "SELECT count(*) FROM audit_log WHERE actor = :a", a=AUDIT_ACTOR) >= 1


#: Exactly the keys `_audit_proposal` emits. Every one is on
#: `recon.privacy.SAFE_KEYS`, which is not a style choice: the redacting
#: chokepoint is default-deny on mapping KEYS, so a key outside the committed
#: vocabulary is replaced by a token in the default `safe` log mode and the audit
#: row stops being readable by the reviewer it exists for.
AUDIT_BODY_KEYS = (
    "proposal_id",
    "conflict_id",
    "fingerprint",
    "type",
    "rule_id",
    "status",
    "sensitive",
    "disposition",
    "field_path",
    "action",
    "confidence",
    "version",
    "label",
    "rule",
    "oscillating",
    "disagreeing_fields",
    "sources",
    "target_canonical_id",
    "created_run",
    "outcome",
)


def test_the_audit_row_carries_the_reviewer_facing_facts(
    writer: Connection, run: ReconcileReport
) -> None:
    del run
    row = writer.execute(
        text("SELECT detail FROM audit_log WHERE action = 'proposal.created' ORDER BY id LIMIT 1")
    ).scalar_one()
    body = row["body"]
    for key in AUDIT_BODY_KEYS:
        assert key in body, key
    assert body["version"] == load_model().version
    assert body["type"].startswith("C")
    assert body["label"].startswith("base=")
    assert "=>" in body["label"], "the audit row must carry the arithmetic, not just the answer"


def test_every_audit_body_key_survives_the_committed_redactor(
    writer: Connection, run: ReconcileReport
) -> None:
    """The binding between this module's key names and `recon.privacy.SAFE_KEYS`.

    Default-deny applies to mapping keys, so an audit body written in the names
    that read best in Python comes out of the chokepoint as a row of tokens --
    which is what happened to the first version of this body. This test is what
    stops it happening again: it reads the row the run actually wrote and
    asserts every intended key is still spelled the way it was written.
    """
    del run
    from recon.privacy import SAFE_KEY_PATTERNS, SAFE_KEYS

    prefixes, suffixes = SAFE_KEY_PATTERNS

    def allowlisted(key: str) -> bool:
        return key in SAFE_KEYS or key.startswith(prefixes) or key.endswith(suffixes)

    unlisted = [key for key in AUDIT_BODY_KEYS if not allowlisted(key)]
    assert not unlisted, f"audit body keys outside the committed vocabulary: {unlisted}"

    for action in ("proposal.created", "reconcile.run"):
        row = writer.execute(
            text("SELECT detail FROM audit_log WHERE action = :a ORDER BY id LIMIT 1"),
            {"a": action},
        ).scalar_one()
        assert row["mode"] in {"safe", "full"}
        if row["mode"] == "safe":
            assert row["body_sha256"], "the safe-mode row must hash the RAW body"
        for key in row["body"]:
            assert allowlisted(key), f"{action} emitted a key the redactor tokenised: {key}"


def test_the_run_summary_audit_row_carries_the_counts(
    writer: Connection, run: ReconcileReport
) -> None:
    body = writer.execute(
        text("SELECT detail FROM audit_log WHERE action = 'reconcile.run' LIMIT 1")
    ).scalar_one()["body"]
    assert body["conflicts_count"] == run.conflicts_seen
    assert body["proposed_count"] == run.proposed
    assert body["sensitive_count"] == run.sensitive_hold
    assert body["fingerprint"] == run.confidence_digest()


def test_the_audit_actor_trigger_refuses_a_human_looking_actor(writer: Connection) -> None:
    """KS003: ``recon_writer`` may not attribute an action to a person."""
    from recon.logging import insert_audit_row

    with pytest.raises(DBAPIError) as excinfo:
        insert_audit_row(writer, actor="reviewer:alice", action="proposal.created", subject="x")
    assert getattr(excinfo.value.orig, "sqlstate", None) == "KS003"
    writer.rollback()


# =====================================================================================
# the LLM seam: the proposal lands regardless
# =====================================================================================
def test_a_failing_rationale_hook_does_not_stop_a_single_proposal(
    writer: Connection,
) -> None:
    """The brief's absolute: LLM failure or cap hit -> the proposal STILL lands."""

    def explode(packet: object) -> str:
        raise RuntimeError("provider down / cap hit")

    report = reconcile(conn=writer, run_id="t7-llm-down", rationale=explode)
    conflicts = _scalar(writer, "SELECT count(*) FROM conflicts")
    assert report.proposed == conflicts > 0
    assert report.rationale_attached == 0
    assert _scalar(writer, "SELECT count(*) FROM proposals WHERE rationale IS NOT NULL") == 0


def test_a_working_rationale_hook_attaches_text_without_touching_the_score(
    writer: Connection,
) -> None:
    """The rationale is decoration on a decided proposal, never an input to it."""
    baseline = reconcile(conn=writer, run_id="t7-llm-baseline")
    writer.rollback()

    with role_connection(ROLE_RECON_WRITER, commit=False) as other:
        annotated = reconcile(
            conn=other,
            run_id="t7-llm-baseline",
            rationale=lambda packet: "because the app DB is authoritative",
        )
        assert annotated.rationale_attached == annotated.proposed
        assert annotated.confidence_digest() == baseline.confidence_digest()


def test_the_run_report_round_trips_through_canonical_json(
    writer: Connection, run: ReconcileReport
) -> None:
    """The report is what the scorecard and the audit row carry; it must serialize."""
    del writer
    payload = canonical_json(run.as_dict())
    assert '"conflicts_seen"' in payload
    assert '"confidence_digest"' in payload


# =====================================================================================
# the identity signals, read from real ER output
# =====================================================================================
def test_the_identity_signals_actually_fire_on_the_real_store(
    writer: Connection, run: ReconcileReport
) -> None:
    """Otherwise the three heaviest-weighted signals are untested green.

    They are read from `entity_link_candidates`, which the session fixture
    populates by running the committed ER materialization. If that table were
    empty every signal would be 0, every score would still be produced, and every
    other assertion in this file would still pass -- so this test exists to say
    that the signals are exercised rather than merely present.
    """
    del run
    for signal in (
        "hard_external_id_agreement",
        "normalized_email_agreement",
        "name_dob_exact",
    ):
        fired = _scalar(
            writer,
            "SELECT count(*) FROM proposals "
            "WHERE (evidence #>> ('{confidence,signals,' || :s || '}')::text[]) = 'true'",
            s=signal,
        )
        assert fired > 0, f"{signal} never fired; the identity signals are untested"

    assert (
        _scalar(writer, "SELECT count(*) FROM entity_link_candidates WHERE generation = 3") > 0
    ), "the ER candidate table is empty, so the identity signals could not have fired"


def test_a_merge_collapsed_conflict_is_not_confident(
    writer: Connection, run: ReconcileReport
) -> None:
    """C10 is the type whose whole point is that the automation cannot choose.

    Its two match-key classes reach two DIFFERENT students by construction
    (contract SS5.5), so no identity signal may fire and the conflicting-evidence
    penalty must. The first implementation counted "this class reached one of the
    conflict's students" as agreement, which made all three classes fire on
    exactly the type that contradicts itself -- every C10 scored a clamped
    1.0000. This test pins the corrected reading.
    """
    del run
    rows = writer.execute(
        text(
            "SELECT p.confidence, p.evidence #> '{identity,key_class_matches}' AS matches, "
            "       p.evidence #> '{identity,contradictions}' AS contradictions "
            "  FROM proposals p JOIN conflicts c ON c.id = p.conflict_id "
            " WHERE c.type = 'C10'"
        )
    ).all()
    assert rows, "no C10 conflicts in the store; this assertion would be vacuous"
    for confidence, matches, contradictions in rows:
        assert matches == {}, f"a C10 must corroborate no identity, got {matches}"
        assert contradictions, "a C10 must record its contradicting contact ref"
        assert confidence < Decimal("0.50"), (
            "a merge-collapsed record must never be confident: the automation "
            f"cannot choose between two students, got {confidence}"
        )


def test_no_conflict_type_saturates_at_full_confidence(
    writer: Connection, run: ReconcileReport
) -> None:
    """A model where every type can reach 1.0 is not discriminating between them."""
    del run
    saturated = [
        row[0]
        for row in writer.execute(
            text(
                "SELECT c.type FROM proposals p JOIN conflicts c ON c.id = p.conflict_id "
                " GROUP BY c.type HAVING min(p.confidence) >= 1.0"
            )
        )
    ]
    assert saturated == [], f"every proposal of {saturated} scored 1.0"
