"""`claim_run` writes its audit row through the redacting chokepoint (R15, R18).

`recon.logging.AUDIT_WRITERS` declared this writer as unrouted and stated the
exact fix::

    claim_run() binds actor/action/subject raw and redacts only `detail`.
    Required change: replace the _CLAIM_INSERT execute with
    recon.logging.insert_audit_row(...); NOTE the lookup compares `subject`
    against a raw run_id, so it must compare against redact(run_id,
    key='subject') once the insert is routed, or a replay stops being detected.

`run_id` is **client-supplied** -- it arrives in the trigger's request body -- so
`subject` is a column an outside caller chooses the contents of. A run id shaped
like a student number went into `audit_log` verbatim, in the one table the
retention policy promises is redacted.

The second half is the part that makes this dangerous to half-fix: the replay
check *reads that column back*. Redact the insert and leave the lookup raw and
every replay of such a run id is claimed again, which re-runs the job. That is a
correctness regression wearing a privacy fix, so both are asserted here, and the
replay assertion is written against the same run id that gets tokenised rather
than against a convenient plain one.

Why this module lives in `tests/ingest/`: it is the trigger endpoints' claim, and
those endpoints share `recon.ingest`'s body dependency and its `parse_body`; the
ingest package's conftest is what supplies a live database and the teardown of
the rows these tests write. `recon/api/internal.py` and this directory are the
same ticket's scope.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text

from recon.api.internal import JOB_SYNC, claim_run, trigger_action
from recon.budget import AUDIT_ACTOR
from recon.logging import audit_row
from recon.privacy import redact

#: A run id that is a legal identifier (`recon.adapters.identifiers` accepts it)
#: and carries a personal *shape* (`S-\\d{6}`, contract SS1's student number).
#: The point of the case is that both are true at once: nothing rejects it on the
#: way in, and it must not be stored as written.
SENSITIVE_RUN_ID = "hardening-claim-S-914722"

#: A plain one, to show the redaction is not a blanket mangling of every run id:
#: an operator still has to be able to find their run in `audit_log`.
PLAIN_RUN_ID = "hardening-claim-plain-914722"


@pytest.fixture
def claimed(owner_engine: Engine) -> Iterator[None]:
    """Remove the claims these tests make, before and after."""

    def purge() -> None:
        with owner_engine.begin() as conn:
            conn.execute(
                text("DELETE FROM audit_log WHERE action = :action AND detail::text LIKE :like"),
                {"action": trigger_action(JOB_SYNC), "like": "%hardening-claim%"},
            )
            conn.execute(
                text("DELETE FROM audit_log WHERE action = :action AND subject LIKE :like"),
                {"action": trigger_action(JOB_SYNC), "like": "hardening-claim%"},
            )

    purge()
    yield
    purge()


def _subjects(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT subject FROM audit_log WHERE action = :action ORDER BY id"),
            {"action": trigger_action(JOB_SYNC)},
        ).fetchall()
    return [row.subject for row in rows if row.subject]


def test_a_personal_shape_in_a_client_supplied_run_id_is_not_stored_verbatim(
    owner_engine: Engine, claimed: None
) -> None:
    """The defect: `audit_log.subject` held whatever the caller sent."""
    assert claim_run(JOB_SYNC, SENSITIVE_RUN_ID) is True

    subjects = [s for s in _subjects(owner_engine) if "hardening-claim" in s]
    assert subjects, "the claim wrote no audit row at all"
    assert not any("S-914722" in subject for subject in subjects), (
        f"the raw student-number shape reached audit_log.subject: {subjects}"
    )
    assert any("[pii:student_number:" in subject for subject in subjects), subjects


def test_the_stored_subject_is_exactly_what_the_chokepoint_would_bind(
    owner_engine: Engine, claimed: None
) -> None:
    """Routed, not merely scrubbed by a second implementation.

    Compared against `recon.logging.audit_row` rather than against a token
    written down here: a hand-rolled redaction that happened to agree today is
    the drift this codebase has already paid for twice.
    """
    assert claim_run(JOB_SYNC, SENSITIVE_RUN_ID) is True

    expected = audit_row(
        actor=AUDIT_ACTOR, action=trigger_action(JOB_SYNC), subject=SENSITIVE_RUN_ID
    )
    stored = [s for s in _subjects(owner_engine) if "hardening-claim" in s]
    assert stored == [expected["subject"]], (stored, expected["subject"])


def test_the_replay_check_still_works_for_a_run_id_that_gets_tokenised(
    owner_engine: Engine, claimed: None
) -> None:
    """The regression a half-fix introduces: idempotency lost, job re-run.

    Redacting the insert without redacting the lookup leaves the second claim
    unable to see the first, so this returns `True` twice and the sync runs
    again on a re-fired cron.
    """
    assert claim_run(JOB_SYNC, SENSITIVE_RUN_ID) is True
    assert claim_run(JOB_SYNC, SENSITIVE_RUN_ID) is False, (
        "the second claim of the same run id was not detected as a replay; the "
        "lookup and the insert disagree about how `subject` is stored"
    )
    assert claim_run(JOB_SYNC, SENSITIVE_RUN_ID) is False


def test_a_plain_run_id_is_still_findable_in_the_audit_table(
    owner_engine: Engine, claimed: None
) -> None:
    """R15 accountability: `subject` is allow-listed, so it is scrubbed, not
    tokenised. A run id with no personal shape must survive intact or nobody can
    query for their own run."""
    assert claim_run(JOB_SYNC, PLAIN_RUN_ID) is True
    assert PLAIN_RUN_ID in _subjects(owner_engine)
    assert claim_run(JOB_SYNC, PLAIN_RUN_ID) is False


def test_two_different_run_ids_do_not_collide_after_redaction(
    owner_engine: Engine, claimed: None
) -> None:
    """Redaction must not merge distinct claims into one.

    A tokenising redactor that dropped the digest would map every
    `S-\\d{6}`-shaped run id onto the same subject, and the *second* run of a
    different id would be reported as a replay and silently skipped.
    """
    other = "hardening-claim-S-914723"
    assert redact(SENSITIVE_RUN_ID, key="subject") != redact(other, key="subject")

    assert claim_run(JOB_SYNC, SENSITIVE_RUN_ID) is True
    assert claim_run(JOB_SYNC, other) is True, "a different run id was mistaken for a replay"


def test_the_claim_no_longer_binds_an_audit_insert_of_its_own() -> None:
    """The structural half: the module must contain no audit-table INSERT.

    `tests/privacy/test_sinks.py` enumerates every audit writer in the package by
    scanning the source for that statement; this asserts the same property from
    the other side, so the fix cannot be "route the row and leave the raw
    statement lying next to it".

    **The same detector, deliberately.** A first draft of this test used a plain
    substring, and it failed on the sentence in `claim_run`'s own docstring that
    *describes* the statement it no longer issues -- the same trap
    `tests/privacy/test_sinks.py` documents for the suite runner's traceback.
    Sharing the detector means a wording that fools one fools both, and is
    caught here instead of quietly re-classifying the module in the enumeration.
    """
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "recon" / "api" / "internal.py").read_text()
    pattern = re.compile(r"INSERT\s+INTO\s+audit_log", re.IGNORECASE)
    sites = [
        f"line {number}"
        for number, line in enumerate(source.splitlines(), 1)
        if pattern.search(line)
    ]
    assert not sites, f"recon/api/internal.py still binds an audit_log INSERT at {sites}"
    assert "insert_audit_row(" in source
