"""The descriptor: deterministic, PII-free, and built from the committed vocabulary.

These need no database. The descriptor is the input to every vector in the
system, so if it is unstable or leaks a value, nothing downstream can be fixed
by tuning a threshold.
"""

from __future__ import annotations

import json
import re

import pytest

from recon.incidents import ConflictRecord, descriptor, label_for
from recon.privacy import redact
from recon.reference import COMPARED_FIELD_PATHS, CONFLICT_TYPES, SOURCE_IDS
from tests.incidents.conftest import golden_records


def _redacted_values(observed: dict[str, object]) -> list[tuple[str, str]]:
    """`(key, raw value)` for every value `recon.privacy.redact` judged personal.

    The oracle is the redactor itself, not a guess about what a personal value
    looks like: whatever it turned into a `[pii:...]` token is what must not
    appear in the descriptor verbatim. A hand-rolled heuristic would either miss
    a kind (too weak) or flag `enrolled` and `tuition` (too strong -- those are
    categorical values the clustering depends on and the redactor passes).
    """
    redacted = redact(dict(observed))
    return [
        (key, str(value))
        for key, value in sorted(observed.items())
        if isinstance(value, str) and str(redacted.get(key, "")).startswith("[pii:")
    ]


#: A conflict carrying every awkward value shape at once: a name, an email, a
#: date of birth, an entity ref, a timestamp, a count, a boolean, a null, a
#: list, and two categorical values that must SURVIVE because they are the
#: signal the clustering runs on.
LOADED = ConflictRecord(
    id=1,
    fingerprint="f" * 64,
    type="C6",
    rule_id="R-006",
    entity_refs=("appdb:student:004a5d09-232d-5960-aa15-db426dcf694d", "crm:contact:CRM-0006048"),
    sources=("crm", "appdb"),
    disagreeing_fields=("crm.contact.grade", "appdb.student.grade"),
    observed_values={
        "appdb.student.grade": "11",
        "crm.contact.grade": "9",
        "appdb.student.first_name": "galeav",
        "crm.contact.email": "umalwen-jarrow-gray@gmail.com",
        "appdb.student.dob": "2020-05-15",
        "enrollment_ref": "appdb:enrollment:6fb6b419-4b5d-5456-844e-a5d1b6d35768",
        "updated_at": "2026-07-12T14:10:55Z",
        "amount_cents": 1200137,
        "metadata_name_pair_present": False,
        "external_ref": None,
        "paid_payment_refs": ["payments:payment:pi_0014855"],
    },
)


def test_the_descriptor_is_stable_across_calls() -> None:
    """Same conflict, same text -- byte for byte, however often it is asked."""
    assert descriptor(LOADED) == descriptor(LOADED)


def test_the_descriptor_does_not_depend_on_key_or_ref_order() -> None:
    """The row's own ordering must not reach the vector.

    `entity_refs`, `sources`, `disagreeing_fields` and `observed_values` all
    arrive from JSONB, whose key order is a property of how Postgres stored the
    value. If any of them reached the descriptor in arrival order, two identical
    conflicts written on different days would embed differently.
    """
    shuffled = ConflictRecord(
        id=LOADED.id,
        fingerprint=LOADED.fingerprint,
        type=LOADED.type,
        rule_id=LOADED.rule_id,
        entity_refs=tuple(reversed(LOADED.entity_refs)),
        sources=tuple(reversed(LOADED.sources)),
        disagreeing_fields=tuple(reversed(LOADED.disagreeing_fields)),
        observed_values=dict(reversed(list(LOADED.observed_values.items()))),
        oscillating=LOADED.oscillating,
    )
    assert descriptor(shuffled) == descriptor(LOADED)


def test_no_personal_value_survives_into_the_descriptor() -> None:
    """The values `redact` calls personal must not appear anywhere in the text.

    Asserted against the raw strings themselves, not against a regex for what a
    leak might look like: the failure this guards is "someone added a new value
    kind and forgot the reduction", and a shape-matching assertion would pass
    for the new kind while it leaked.
    """
    text = descriptor(LOADED)
    for leak in (
        "galeav",
        "umalwen-jarrow-gray@gmail.com",
        "umalwen",
        "jarrow-gray",
        "2020-05-15",
        "gmail.com",
    ):
        assert leak not in text, f"{leak!r} reached the descriptor"


def test_the_pii_digest_is_dropped_but_the_kind_and_shape_are_kept() -> None:
    """`[pii:name:8c3b..:aaaaaa]` becomes `pii.name.shape.aaaaaa`.

    Both halves matter. Keeping the *kind and shape* is what lets two name
    disagreements look alike; dropping the *digest* is what stops every distinct
    name from becoming its own cluster, because the digest is unique per value.
    """
    text = descriptor(LOADED)
    assert "pii.name.shape." in text
    assert "pii.email.shape." in text
    assert "pii.dob.shape." in text
    # The digest is 12 hex characters in `redact`'s token. None of them may
    # survive: a digest in the descriptor is a per-value fingerprint.
    redacted = json.dumps(redact(dict(LOADED.observed_values)))
    for digest in re.findall(r"\[pii:[a-z_]+:([0-9a-f]{6,}):", redacted):
        assert digest not in text, f"the per-value digest {digest!r} reached the descriptor"


def test_categorical_values_survive_because_they_are_the_signal() -> None:
    """Grades, statuses, programs and payment types are what split a type.

    If these were reduced away, every C6 conflict would embed identically and
    the clustering would be exactly `GROUP BY type`.
    """
    text = descriptor(LOADED)
    assert "obs appdb.student.grade 11" in text
    assert "obs crm.contact.grade 9" in text


def test_refs_numbers_and_dates_are_reduced_to_their_kind() -> None:
    """Per-conflict identifiers are noise; their kind is not."""
    text = descriptor(LOADED)
    assert "obs enrollment_ref ref.appdb.enrollment" in text
    assert "obs updated_at date" in text
    assert "obs amount_cents num.1e6" in text
    assert "obs metadata_name_pair_present false" in text
    assert "obs external_ref null" in text
    assert "obs paid_payment_refs list.1[ref.payments.payment]" in text
    assert "6fb6b419" not in text
    assert "1200137" not in text


def test_refkinds_carries_kinds_and_never_the_refs() -> None:
    """Two conflicts about two different students are candidates for one incident."""
    text = descriptor(LOADED)
    assert "refkinds ref.appdb.student ref.crm.contact" in text
    assert "004a5d09" not in text
    assert "CRM-0006048" not in text


@pytest.mark.parametrize("record", golden_records(step=311), ids=lambda record: str(record["type"]))
def test_every_golden_conflict_produces_a_pii_free_descriptor(record: dict[str, object]) -> None:
    """Sampled across the real grading contract, not only the hand-built case.

    `step=311` is a stride over the committed file, which is written in a pinned
    order, so this samples all fourteen conflict types deterministically.
    """
    conflict = ConflictRecord(
        id=0,
        fingerprint="0" * 64,
        type=str(record["type"]),
        rule_id=str(record["rule_id"]),
        entity_refs=tuple(str(ref) for ref in record["entity_refs"]),  # type: ignore[union-attr]
        sources=tuple(str(one) for one in record["sources_involved"]),  # type: ignore[union-attr]
        disagreeing_fields=tuple(
            str(path)
            for path in record["disagreeing_fields"]  # type: ignore[union-attr]
        ),
        observed_values=dict(record["observed_values"]),  # type: ignore[arg-type]
    )
    text = descriptor(conflict)

    # A `pii.<kind>.shape.<shape>` token deliberately KEEPS the value's shape,
    # and a shape contains structural characters: an email's shape carries `@`
    # and a date-of-birth's carries `9999-99-99`. Those are the redactor's own
    # output, not a leak -- so they are removed before the leak check, and the
    # check then applies to everything the descriptor emitted OUTSIDE a
    # redaction token. Asserting on the whole string would flag `redact`'s
    # output as a leak and prove nothing about the values themselves.
    outside_tokens = re.sub(r"pii\.[a-z_]+\.shape\.\S*", "", text)
    assert "@" not in outside_tokens, f"an email reached the descriptor unredacted: {text}"
    assert not re.search(r"\d{4}-\d{2}-\d{2}", outside_tokens), f"a raw date reached it: {text}"
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", outside_tokens), (
        f"a raw uuid reached it: {text}"
    )

    # And the values themselves, verbatim: nothing personal that went in comes
    # back out. This is the assertion that actually binds -- it is checked
    # against the strings the row carried, not against a guess at their shape.
    personal = _redacted_values(dict(conflict.observed_values))
    for key, value in personal:
        assert value not in text, f"{key}={value!r} reached the descriptor verbatim"


def test_the_label_is_committed_vocabulary_and_nothing_else() -> None:
    """A label can carry no personal data because no value is ever a candidate."""
    label = label_for(LOADED, ordinal=3)
    assert label == "C6/R-006 appdb+crm fields=appdb.student.grade,crm.contact.grade #3"
    tokens = re.split(r"[ /+,=#]", label)
    for token in tokens:
        if not token or token.isdigit():
            continue
        assert (
            token in CONFLICT_TYPES
            or token in SOURCE_IDS
            or token in COMPARED_FIELD_PATHS
            or token == LOADED.rule_id
            or token == "fields"
        ), f"{token!r} in the label is not from the committed vocabulary"


def test_the_label_refuses_a_field_outside_compared_fields() -> None:
    """A path the contract never pinned is a bug in the caller, not a new label."""
    rogue = ConflictRecord(
        id=1,
        fingerprint="f" * 64,
        type="C6",
        rule_id="R-006",
        entity_refs=(),
        sources=("appdb",),
        disagreeing_fields=("appdb.student.social_security_number",),
    )
    with pytest.raises(ValueError, match="COMPARED_FIELDS"):
        label_for(rogue, ordinal=1)
