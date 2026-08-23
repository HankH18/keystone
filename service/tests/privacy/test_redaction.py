"""Redaction over the records `recon.seed` actually produces (SPEC R21, R26)."""

from __future__ import annotations

import json
import traceback
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from recon.normalize import norm_email, norm_name
from recon.privacy import (
    KIND_DOB,
    KIND_EMAIL,
    KIND_FLAG,
    KIND_HOUSEHOLD,
    KIND_NAME,
    KIND_OPAQUE,
    KIND_STUDENT_NUMBER,
    PII_KEYS,
    SAFE_KEYS,
    SEQUENCE_SAFE_KEYS,
    SHAPE_DETECTORS,
    SHAPELESS_KINDS,
    STRUCTURAL_KEYS,
    UNRENDERABLE,
    Redactor,
    _stringify,
    canonical_json,
    default_redactor,
    is_token,
    redact,
    scrub_text,
)

# --- the field set the contract defines (§1), asserted against the code -----
#
# Not a restatement for its own sake: this is the list the ticket names, and it
# is what makes "the redactor covers the contract's field set" a test rather
# than a claim.
CONTRACT_PII_FIELDS: tuple[tuple[str, str], ...] = (
    ("email", KIND_EMAIL),
    ("guardian_email", KIND_EMAIL),
    ("guardian2_email", KIND_EMAIL),
    ("payer_email", KIND_EMAIL),
    ("billing_owner_email", KIND_EMAIL),
    ("first_name", KIND_NAME),
    ("last_name", KIND_NAME),
    ("payer_name", KIND_NAME),
    ("student_first_name", KIND_NAME),
    ("student_last_name", KIND_NAME),
    ("dob", KIND_DOB),
    ("student_number", KIND_STUDENT_NUMBER),
    ("household_id", KIND_HOUSEHOLD),
    ("marketing_consent", KIND_FLAG),
    ("communication_opt_out", KIND_FLAG),
)


@pytest.mark.parametrize(
    ("key", "kind"), CONTRACT_PII_FIELDS, ids=[entry[0] for entry in CONTRACT_PII_FIELDS]
)
def test_contract_field_is_classified(key: str, kind: str) -> None:
    """Every PII field the contract §1 defines has a committed kind."""
    assert PII_KEYS[key] == kind


def test_no_pii_key_is_also_allow_listed() -> None:
    """A key cannot be both personal and safe; that overlap would be a leak."""
    assert not (set(PII_KEYS) & SAFE_KEYS)


# ---------------------------------------------------------------------------
# every PII path in the real dataset is redacted
# ---------------------------------------------------------------------------


def test_dataset_supplies_the_expected_field_paths(pii_field_paths: tuple[str, ...]) -> None:
    """Guard against a silently empty table: the dataset must produce PII."""
    leaves = {path.rsplit(".", 1)[-1] for path in pii_field_paths}
    expected = {
        "email",
        "guardian_email",
        "guardian2_email",
        "payer_email",
        "billing_owner_email",
        "first_name",
        "last_name",
        "payer_name",
        "student_first_name",
        "student_last_name",
        "name",
        "dob",
        "student_number",
        "household_id",
        "marketing_consent",
        "communication_opt_out",
    }
    missing = expected - leaves
    assert not missing, f"the dev dataset produced no value for {sorted(missing)}"


def test_every_pii_leaf_in_every_record_is_tokenised(
    dev_records: dict[str, list[dict[str, Any]]],
) -> None:
    """Table-driven over the real records: no PII leaf survives redaction.

    Walks each generated record to its leaves -- including the nested payments
    ``metadata`` object -- and asserts that every non-null value under a PII key
    came back as a token, never as itself.
    """
    checked = 0
    for label, records in dev_records.items():
        for record in records:
            redacted = default_redactor.redact(record)
            for path, value in _leaves(record):
                leaf = path.rsplit(".", 1)[-1]
                if leaf not in PII_KEYS or value is None:
                    continue
                got = _at(redacted, path)
                assert is_token(got), f"{label}.{path} was not redacted: {got!r}"
                assert got != value
                checked += 1
    assert checked > 1000, f"only {checked} PII leaves checked; the sample is too small"


def test_nulls_and_non_pii_survive(dev_records: dict[str, list[dict[str, Any]]]) -> None:
    """Redaction preserves structure: same keys, nulls intact, safe keys verbatim."""
    for records in dev_records.values():
        for record in records[:25]:
            redacted = default_redactor.redact(record)
            assert set(redacted) == set(record)
            for path, value in _leaves(record):
                leaf = path.rsplit(".", 1)[-1]
                if value is None:
                    assert _at(redacted, path) is None, path
                elif leaf in SAFE_KEYS and isinstance(value, str):
                    assert _at(redacted, path) == value, path


# ---------------------------------------------------------------------------
# nested / jsonb structures -- where the PII actually lives
# ---------------------------------------------------------------------------


def test_payments_metadata_is_nested_and_redacted(
    dev_records: dict[str, list[dict[str, Any]]],
) -> None:
    """`payment.metadata.student_*_name` is a nested object, not a top-level key."""
    named = [
        record
        for record in dev_records["payments.payment"]
        if record["metadata"]["student_first_name"] is not None
    ]
    assert named, "no payment in the sample carries the metadata name pair"
    for record in named:
        redacted = default_redactor.redact(record)
        assert is_token(redacted["metadata"]["student_first_name"])
        assert is_token(redacted["metadata"]["student_last_name"])
        # a sibling non-PII key inside the same nested object is untouched
        assert redacted["metadata"]["program"] == record["metadata"]["program"]


def test_evidence_packet_shape_is_redacted_at_depth(
    dev_records: dict[str, list[dict[str, Any]]],
) -> None:
    """A jsonb evidence packet: source-qualified paths, several levels deep."""
    contact = dev_records["crm.contact"][0]
    student = dev_records["appdb.student"][0]
    packet = {
        "conflict": {
            "type": "C4",
            "observed_values": {
                "crm.contact.email": contact["email"],
                "appdb.student.guardian_email": student["guardian_email"],
                "appdb.student.first_name": student["first_name"],
            },
            "supporting": [
                {"source": "crm", "record": contact},
                {"source": "appdb", "record": student},
            ],
        },
        "action": {"set": {"appdb.student.guardian_email": contact["email"]}},
    }
    redacted = default_redactor.redact(packet)
    blob = canonical_json(redacted)
    for value in (
        contact["email"],
        student["guardian_email"],
        student["first_name"],
        student["student_number"],
    ):
        assert value not in blob, f"{value!r} survived a nested redaction"
    observed = redacted["conflict"]["observed_values"]
    # the dotted field path keeps its KIND: it is not degraded to `opaque`
    assert f":{KIND_EMAIL}:" in observed["crm.contact.email"]
    assert f":{KIND_NAME}:" in observed["appdb.student.first_name"]
    assert f":{KIND_EMAIL}:" in redacted["action"]["set"]["appdb.student.guardian_email"]


def test_lists_and_deep_nesting_are_walked() -> None:
    """Lists of mappings, and mappings inside lists inside mappings."""
    payload = {"batch": [{"rows": [{"guardian_email": "a.b@keystone.test"}]}]}
    redacted = default_redactor.redact(payload)
    assert is_token(redacted["batch"][0]["rows"][0]["guardian_email"])


def test_recursion_is_bounded() -> None:
    """A self-referential structure terminates instead of blowing the stack."""
    node: dict[str, Any] = {"run_id": "r1"}
    node["child"] = node
    rendered = canonical_json(default_redactor.redact(node))
    assert "cycle" in rendered or "depth-limit" in rendered


def test_mapping_keys_that_are_themselves_pii_are_redacted() -> None:
    """A dict keyed by email address -- the key is the leak, not the value."""
    redacted = default_redactor.redact({"counts": {"guardian@keystone.test": 3}})
    (key,) = redacted["counts"]
    assert is_token(key)
    assert "guardian@keystone.test" not in canonical_json(redacted)


# ---------------------------------------------------------------------------
# determinism and correlatability
# ---------------------------------------------------------------------------


def test_token_is_deterministic(dev_records: dict[str, list[dict[str, Any]]]) -> None:
    """Same input => same token, across records and across Redactor instances."""
    other = Redactor()
    for records in dev_records.values():
        for record in records[:50]:
            assert default_redactor.redact(record) == default_redactor.redact(record)
            assert other.redact(record) == default_redactor.redact(record)


def test_token_is_stable_across_processes(dev_records: dict[str, list[dict[str, Any]]]) -> None:
    """The digest is a committed constant, not a per-process random salt."""
    import subprocess
    import sys

    from tests.privacy.conftest import SERVICE_ROOT

    value = dev_records["appdb.student"][0]["guardian_email"]
    script = f"from recon.privacy import default_redactor as r;print(r.token({value!r}, 'email'))"
    out = subprocess.run(
        [sys.executable, "-c", script],
        cwd=SERVICE_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == default_redactor.token(value, KIND_EMAIL)


def test_same_mailbox_different_spelling_shares_a_digest() -> None:
    """Correlatability: the digest is taken over the normalised form."""
    raw = '  "Brenmar-.Fairbank-Mead+school@Gmail.com" '
    canonical = norm_email(raw)
    assert canonical is not None
    assert default_redactor.digest(raw, KIND_EMAIL) == default_redactor.digest(
        canonical, KIND_EMAIL
    )
    # ...and the shape still shows they were spelled differently
    assert default_redactor.token(raw, KIND_EMAIL) != default_redactor.token(canonical, KIND_EMAIL)


def test_same_person_shares_a_digest_across_sources(
    dev_records: dict[str, list[dict[str, Any]]],
) -> None:
    """A name written with A.3 quote dirt still correlates with its clean twin."""
    dirty = "“Fairbank-Mead“"
    clean = norm_name(dirty)
    assert clean == "fairbank-mead"
    assert default_redactor.digest(dirty, KIND_NAME) == default_redactor.digest(clean, KIND_NAME)


def test_different_values_get_different_tokens(raw_pii_values: tuple[str, ...]) -> None:
    """No collision across the real dataset's distinct values."""
    tokens = {value: default_redactor.token(value, KIND_OPAQUE) for value in raw_pii_values}
    assert len(set(tokens.values())) == len(tokens)


def test_redaction_is_idempotent(dev_records: dict[str, list[dict[str, Any]]]) -> None:
    """`redact(redact(x)) == redact(x)` -- a second retention sweep is a no-op."""
    for records in dev_records.values():
        for record in records[:50]:
            once = default_redactor.redact(record)
            assert default_redactor.redact(once) == once


def test_a_different_salt_produces_different_tokens() -> None:
    """The salt is real: it participates in the digest."""
    other = Redactor(salt="keystone/pii/other")
    assert other.token("a@keystone.test", KIND_EMAIL) != default_redactor.token(
        "a@keystone.test", KIND_EMAIL
    )


# ---------------------------------------------------------------------------
# default-deny, shape detection, free text
# ---------------------------------------------------------------------------


def test_unknown_key_is_denied_by_default() -> None:
    """A field nobody predicted -- `phone`, say -- is redacted without a rule.

    The dataset produces no phone number (contract §1), so no phone pattern is
    invented. Default-deny is what covers it.

    **Default-deny covers the key as well as the value.** This assertion used to
    index the result by ``redacted["phone"]``, which only worked because an
    unpredicted key was emitted verbatim -- and that was the hole: a mapping
    keyed by a personal name, a dob or a household id has no shape for
    `_detect_kind` to catch, so the key went straight into the log. The
    value-level claim below is unchanged; the key-level one is new, and it is
    why the lookup is by iteration rather than by name.
    """
    redacted = default_redactor.redact({"phone": "555-0134", "nickname": "Bee"})
    assert len(redacted) == 2
    for key, value in redacted.items():
        assert is_token(key), f"unpredicted key {key!r} survived default-deny"
        assert is_token(value), f"value under {key!r} survived default-deny"
    blob = canonical_json(redacted)
    assert "555-0134" not in blob and "Bee" not in blob
    assert "phone" not in blob and "nickname" not in blob


def test_email_under_an_allow_listed_key_is_still_caught() -> None:
    """Value-shape detection overrides the allow-list, not the other way round."""
    redacted = default_redactor.redact({"label": "guardian@keystone.test", "grade": "4"})
    assert is_token(redacted["label"])
    assert redacted["grade"] == "4"


def test_free_text_keeps_its_prose_and_loses_its_pii() -> None:
    """An error message stays debuggable; the address and the S-number do not."""
    message = (
        "duplicate key value violates unique constraint: Key (email)="
        "(brenmar-fairbank-mead@gmail.com) for S-001204 already exists"
    )
    scrubbed = scrub_text(message)
    assert "duplicate key value violates unique constraint" in scrubbed
    assert "brenmar-fairbank-mead@gmail.com" not in scrubbed
    assert "S-001204" not in scrubbed
    assert scrub_text(scrubbed) == scrubbed


def test_flag_token_carries_no_digest() -> None:
    """Hashing a two-valued domain would BE the value, so a flag gets no digest."""
    on = default_redactor.token(True, KIND_FLAG)
    off = default_redactor.token(False, KIND_FLAG)
    assert on == off == "[pii:flag:redacted]"


def test_shape_carries_no_character_of_the_value() -> None:
    """The preview is a character-class mask, never a prefix of the value."""
    token = default_redactor.token("Brenmar-Fairbank@gmail.com", KIND_EMAIL)
    shape = token.rsplit(":", 1)[1].rstrip("]")
    assert set(shape) <= set("a9@.-_+:/ ?~")
    for fragment in ("Bren", "Fair", "gmail", "mar-"):
        assert fragment not in token


def test_token_is_ascii_and_json_safe(raw_pii_values: tuple[str, ...]) -> None:
    """Tokens survive `json.dumps(..., ensure_ascii=True)` unchanged and unescaped."""
    for value in raw_pii_values[:200]:
        token = default_redactor.token(value, KIND_OPAQUE)
        assert token.isascii()
        assert "]" not in token[:-1]
        assert json.loads(json.dumps(token, ensure_ascii=True)) == token


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _leaves(obj: Any, prefix: str = "") -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    if isinstance(obj, dict):
        for name, value in obj.items():
            out.extend(_leaves(value, f"{prefix}.{name}" if prefix else str(name)))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            out.extend(_leaves(value, f"{prefix}[{index}]"))
    else:
        out.append((prefix, obj))
    return out


def _at(obj: Any, path: str) -> Any:
    node = obj
    for part in path.split("."):
        while part.endswith("]"):
            part, _, index = part[:-1].rpartition("[")
            if part:
                node = node[part]
            node = node[int(index)]
            part = ""
        if part:
            node = node[part]
    return node


def test_redact_module_function_matches_the_default_redactor() -> None:
    """The convenience wrapper is the same redactor, not a second policy."""
    payload = {"guardian_email": "a@keystone.test"}
    assert redact(payload) == default_redactor.redact(payload)


# ---------------------------------------------------------------------------
# keyed free-text scrubbing -- the only way a NAME can be found in prose
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "gone", "kept"),
    [
        pytest.param(
            'rejected {"first_name": "Zedail", "grade": "6"}',
            ("Zedail",),
            ('"grade": "6"', "rejected"),
            id="json-pair",
        ),
        pytest.param(
            "Key (last_name)=(Fairbank-Mead) already exists",
            ("Fairbank-Mead",),
            ("already exists", "(last_name)="),
            id="postgres-unique-violation",
        ),
        pytest.param(
            "match failed: payer_name=Brenmar Fairbank-Mead",
            ("Brenmar",),
            ("match failed", "payer_name="),
            id="bare-pair",
        ),
        pytest.param(
            '{"dob": "2014-09-07", "household_id": "HH-000997"}',
            ("2014-09-07", "HH-000997"),
            ('"dob"', '"household_id"'),
            id="dob-and-household",
        ),
        pytest.param(
            '{"guardian2_email": null, "marketing_consent": false}',
            (),
            ("null",),
            id="null-is-not-personal-data",
        ),
    ],
)
def test_keyed_free_text_forms(text: str, gone: tuple[str, ...], kept: tuple[str, ...]) -> None:
    """A name has no shape; the key beside it is what makes it findable."""
    scrubbed = scrub_text(text)
    for needle in gone:
        assert needle not in scrubbed, f"{needle!r} survived: {scrubbed}"
    for needle in kept:
        assert needle in scrubbed, f"{needle!r} was destroyed: {scrubbed}"
    assert scrub_text(scrubbed) == scrubbed, "scrubbing is not idempotent"


def test_keyed_scrub_names_the_field_it_removed() -> None:
    """The scrubbed text still says WHICH field went, which is the debug value."""
    scrubbed = scrub_text('{"guardian_email": "a@keystone.test"}')
    assert scrubbed.startswith('{"guardian_email": "[pii:email:')


def test_scrubbing_a_serialised_record_leaves_no_pii(
    dev_records: dict[str, list[dict[str, Any]]],
) -> None:
    """The realistic case: a whole record rendered into an error message."""
    for label, records in dev_records.items():
        for record in records[:25]:
            scrubbed = scrub_text(f"rejected {json.dumps(record, sort_keys=True)}")
            for key, value in record.items():
                if key in PII_KEYS and isinstance(value, str) and len(value) >= 4:
                    assert value not in scrubbed, f"{label}.{key} leaked into free text"


# ===========================================================================
# The four holes an independent verifier found. Each block reproduces the
# leak first (the docstring quotes the observed output), then asserts the fix.
# ===========================================================================


class _Rejection(Exception):
    """An exception carrying a record -- what the ingest path raises on reject."""


@pytest.mark.parametrize(
    "key",
    ["error", "detail", "message", "exception", "reason", "note", "traceback"],
    ids=lambda k: f"under-{k}",
)
def test_a_non_string_value_is_redacted_not_stringified_afterwards(
    key: str, dev_records: dict[str, list[dict[str, Any]]]
) -> None:
    """An exception carrying a record must not survive as `str(exc)` in the log.

    `Redactor._leaf` used to return any non-`str` unchanged, and the renderer
    then called `str(value)` on it -- *after* redaction had finished. So
    ``log.error(..., error=ValueError(f"cannot land {record}"))`` wrote the whole
    record, first name, last name and guardian email included. That is the single
    most likely way personal data reaches a log here, because it is exactly what
    the ingest path does on a rejection.
    """
    student = dev_records["appdb.student"][0]
    redacted = redact({key: _Rejection(f"cannot land {student}")})
    blob = canonical_json(redacted)
    assert isinstance(redacted[key], str), "the redactor must emit the final string form"
    for field, value in student.items():
        if field in PII_KEYS and isinstance(value, str) and len(value) >= 4:
            assert value not in blob, f"{field} survived inside an exception under {key!r}"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(_Rejection("cannot land {'first_name': 'Amriyo'}"), id="exception"),
        pytest.param(uuid.UUID("00000000-0000-0000-0000-000000000001"), id="uuid"),
        pytest.param(Decimal("12.50"), id="decimal"),
        pytest.param(datetime(2015, 12, 16, tzinfo=UTC), id="datetime"),
        pytest.param(object(), id="bare-object"),
        pytest.param(b"first_name=Amriyo", id="bytes"),
    ],
)
def test_nothing_leaves_the_redactor_that_the_renderer_would_still_stringify(value: Any) -> None:
    """Every leaf comes back as something JSON can already spell.

    This is the property, not an example of it: if the redactor hands the
    renderer an object, the renderer stringifies it and that string was never
    redacted.
    """
    redacted = redact({"detail": value, "unknown_field": value})
    for emitted in redacted.values():
        assert isinstance(emitted, str | int | float | bool) or emitted is None, (
            f"{type(emitted).__name__} would be stringified by the renderer, after redaction"
        )
    assert "Amriyo" not in canonical_json(redacted)


@pytest.mark.parametrize(
    "key",
    ["Amriyo", "Fairbank-Mead", "2015-12-16", "H-000042", "S-000123", "a.b@keystone.test"],
    ids=lambda k: f"key-{k}",
)
def test_a_mapping_key_that_is_a_datum_is_denied_by_default(key: str) -> None:
    """Default-deny applies to keys, not only to values.

    `_redact_key` used to tokenise a key only when `_detect_kind` recognised its
    SHAPE -- and only an address and an `S-000000` have shapes. A personal name,
    a dob or a household id used as a key went into the log verbatim, and
    evidence packets really are keyed by field path *and* by value.
    """
    redacted = redact({key: 1})
    assert key not in redacted, f"{key!r} was emitted verbatim as a mapping key"
    assert is_token(next(iter(redacted)))
    assert key not in canonical_json(redacted)


@pytest.mark.parametrize(
    "key", ["first_name", "crm.contact.email", "run_id", "amount_cents", "evidence", "detail"]
)
def test_a_mapping_key_that_is_a_field_name_survives(key: str) -> None:
    """The committed vocabulary is what keeps a log readable. A field NAME is
    not personal data -- ``first_name`` names a field, it is not a name."""
    assert key in redact({key: None})


def test_a_key_the_vocabulary_has_not_met_fails_safe_not_open() -> None:
    """The failure mode is an unreadable key, never a leaked one."""
    redacted = redact({"some_new_column": "Amriyo"})
    assert "some_new_column" not in redacted
    assert "Amriyo" not in canonical_json(redacted)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"note": ("Amriyo", "2015-12-16")}, id="tuple-under-a-text-key"),
        pytest.param({"detail": ["Amriyo", "H-000042"]}, id="list-under-a-text-key"),
        pytest.param({"label": ["Amriyo"]}, id="list-under-a-safe-key"),
        pytest.param({"rule": {"Amriyo"}}, id="set-under-a-safe-key"),
        pytest.param({"evidence": {"observed": [["Amriyo"]]}}, id="nested-list"),
    ],
)
def test_sequence_elements_do_not_inherit_a_free_text_or_allow_listed_key(
    payload: dict[str, Any],
) -> None:
    """`redact({"note": ("Amriyo", "2015-12-16")})` returned both values untouched.

    `_walk` passed the parent key down to every element, so an element under a
    TEXT_KEY or a SAFE_KEY was judged as free text -- and free text is
    recognised by shape, which a name, a dob and a household id do not have.
    """
    blob = canonical_json(redact(payload))
    for needle in ("Amriyo", "2015-12-16", "H-000042"):
        assert needle not in blob, f"{needle!r} survived as a sequence element: {blob}"


def test_sequence_elements_under_a_pii_key_keep_that_kind() -> None:
    """The exception that is allowed: every element of a PII key IS that kind."""
    redacted = redact({"first_name": ["Amriyo", "Zedail"]})
    assert all(f":{KIND_NAME}:" in token for token in redacted["first_name"])


def test_sequence_elements_of_a_committed_vocabulary_stay_readable() -> None:
    """SEQUENCE_SAFE_KEYS: field names and refs are not data, and stay legible."""
    redacted = redact(
        {
            "disagreeing_fields": ["name_first", "dob"],
            "sources": ["crm.contact", "appdb.student"],
            "entity_refs": ["crm:contact:C-1", "appdb:student:S-1"],
        }
    )
    assert redacted["disagreeing_fields"] == ["name_first", "dob"]
    assert redacted["sources"] == ["crm.contact", "appdb.student"]
    assert redacted["entity_refs"] == ["crm:contact:C-1", "appdb:student:S-1"]


def test_every_sequence_safe_key_is_also_an_allow_listed_key() -> None:
    """An element cannot be safer than the key it inherits."""
    assert SEQUENCE_SAFE_KEYS <= SAFE_KEYS


def test_structural_keys_are_never_value_allow_listed() -> None:
    """`{"student": "Amriyo Fairbank"}` must still redact its value."""
    assert not (STRUCTURAL_KEYS & SAFE_KEYS)
    for word in sorted(STRUCTURAL_KEYS):
        redacted = redact({word: "Amriyo"})
        assert word in redacted, f"{word!r} should survive in key position"
        assert is_token(redacted[word]), f"{word!r} allow-listed a VALUE"


@pytest.mark.parametrize(
    ("text", "gone"),
    [
        pytest.param(
            "cannot land {'first_name': 'Amriyo', 'dob': '2015-12-16'}",
            ("Amriyo", "2015-12-16"),
            id="python-dict-repr",
        ),
        pytest.param(
            "rejected Student(first_name='Amriyo', household_id='H-000042')",
            ("Amriyo", "H-000042"),
            id="dataclass-repr",
        ),
        pytest.param(
            # verbatim what `"%s failed" % record` produces for that mapping
            "{'last_name': 'Fairbank-Mead', 'student_number': 'S-000123'} failed",
            ("Fairbank-Mead", "S-000123"),
            id="percent-format",
        ),
        pytest.param(
            "{'metadata': {'student_first_name': 'Amriyo'}}",
            ("Amriyo",),
            id="nested-repr",
        ),
        pytest.param(
            "mixed {\"first_name\": \"Amriyo\"} and {'last_name': 'Fairbank'}",
            ("Amriyo", "Fairbank"),
            id="both-spellings-in-one-string",
        ),
    ],
)
def test_scrub_text_reads_python_repr_not_only_json(text: str, gone: tuple[str, ...]) -> None:
    """`'first_name': 'Amriyo'` is the common case, and it used to match nothing.

    `_JSON_PAIR_PATTERN` required DOUBLE-quoted keys, so every f-string, `%s`,
    `repr()` and traceback of a dict -- all of which use single quotes -- went
    through untouched. Addresses and student numbers survived only because they
    have shape detectors; names, dobs and household ids leaked.
    """
    scrubbed = scrub_text(text)
    for needle in gone:
        assert needle not in scrubbed, f"{needle!r} survived: {scrubbed}"
    assert scrub_text(scrubbed) == scrubbed, "scrubbing a repr is not idempotent"


def test_scrubbed_repr_keeps_its_own_quoting() -> None:
    """A repr must come back a repr, not silently rewritten as JSON."""
    scrubbed = scrub_text("{'first_name': 'Amriyo'}")
    assert scrubbed.startswith("{'first_name': '[pii:name:")
    assert scrubbed.endswith("]'}")


def test_a_traceback_of_a_rejection_carries_no_personal_value(
    dev_records: dict[str, list[dict[str, Any]]],
) -> None:
    """The realistic exception path, formatted exactly as structlog formats it."""
    student = dev_records["appdb.student"][0]
    try:
        raise _Rejection(f"cannot land {student!r}")
    except _Rejection as exc:
        rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    scrubbed = canonical_json(redact({"exception": rendered}))
    for field, value in student.items():
        if field in PII_KEYS and isinstance(value, str) and len(value) >= 4:
            assert value not in scrubbed, f"{field} survived in a traceback"


# ---------------------------------------------------------------------------
# value-shape detection: which kinds have one, which cannot, and why
# ---------------------------------------------------------------------------


def test_the_shape_vocabulary_is_complete_and_honest() -> None:
    """Every kind is either detectable by shape or named as not detectable.

    The module used to claim that "value-shape detection catches PII that
    arrives under a key nobody predicted, including under an allow-listed one".
    That was true for `email` and `student_number` and for nothing else, so
    `name`, `dob` and `household` under an allow-listed key, in an event name or
    in prose were emitted verbatim. Two of the three now have detectors. The
    third cannot have one, and saying so is the fix -- so the claim is a
    partition over the kinds rather than a sentence, and a kind added without a
    decision fails here.
    """
    detectable = {kind for kind, _ in SHAPE_DETECTORS}
    assert detectable == {KIND_EMAIL, KIND_STUDENT_NUMBER, KIND_DOB, KIND_HOUSEHOLD}
    assert not detectable & set(SHAPELESS_KINDS)
    all_kinds = {
        KIND_EMAIL,
        KIND_STUDENT_NUMBER,
        KIND_DOB,
        KIND_HOUSEHOLD,
        KIND_NAME,
        KIND_FLAG,
        KIND_OPAQUE,
    }
    assert all_kinds <= detectable | set(SHAPELESS_KINDS)
    assert KIND_NAME in SHAPELESS_KINDS
    assert "no shape" in SHAPELESS_KINDS[KIND_NAME]
    # every kind PII_KEYS can produce is accounted for
    assert set(PII_KEYS.values()) <= detectable | set(SHAPELESS_KINDS)


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        pytest.param("brenmar.fairbank@keystone.test", KIND_EMAIL, id="email"),
        pytest.param("S-000123", KIND_STUDENT_NUMBER, id="student-number"),
        pytest.param("2015-12-16", KIND_DOB, id="dob"),
        pytest.param("HH-004821", KIND_HOUSEHOLD, id="household"),
    ],
)
def test_a_shaped_value_is_caught_under_every_allow_listed_key(value: str, kind: str) -> None:
    """Shape detection outranks the allow-list, for all four detectable kinds.

    `dob` and `household_id` had no detector at all, so under `status`, `title`
    or `label` -- all allow-listed operational keys -- they went out verbatim.
    """
    for key in ("status", "title", "label", "field", "stage", "grade", "key_class"):
        redacted = redact({key: value})
        assert is_token(redacted[key]), f"{value!r} survived under the allow-listed {key!r}"
        assert f":{kind}:" in redacted[key], f"{value!r} under {key!r} got the wrong kind"


def test_a_timestamp_is_not_mistaken_for_a_date_of_birth() -> None:
    """The deliberate boundary of the dob detector.

    A rule that tokenised every ISO-8601 value would tokenise every `created_at`
    in every log line while removing no personal data at all, and an unreadable
    log is what gets an allow-list widened under pressure. So the detector wants
    a *bare calendar date*.
    """
    stamps = {
        "created_at": "2026-02-01T00:00:00Z",
        "updated_at": "2026-02-02T12:30:59.123456+00:00",
        "occurred_at": "2026-02-03T08:00:00",
        "timestamp": "2026-08-22T12:00:00Z",
    }
    redacted = redact(stamps)
    assert redacted == stamps, redacted
    # ...and the date inside one is not clipped out of it either
    assert scrub_text("failed at 2026-02-01T00:00:00Z") == "failed at 2026-02-01T00:00:00Z"
    # while a bare one in the same sentence IS removed
    assert "2015-12-16" not in scrub_text("born 2015-12-16, failed at 2026-02-01T00:00:00Z")


def test_a_dob_and_a_household_are_removed_from_unstructured_prose() -> None:
    """No key, no JSON, no repr -- the position the hunt never drove."""
    scrubbed = scrub_text("HH-000997 has a child born 2014-09-07 and a sibling born 2016-01-02")
    for needle in ("HH-000997", "2014-09-07", "2016-01-02"):
        assert needle not in scrubbed, f"{needle!r} survived: {scrubbed}"
    assert "has a child born" in scrubbed
    assert scrub_text(scrubbed) == scrubbed, "scrubbing is not idempotent"


# ---------------------------------------------------------------------------
# the allow-list is narrow enough that a source-controlled key cannot ride in
# ---------------------------------------------------------------------------


def test_a_source_controlled_primary_key_is_not_allow_listed() -> None:
    """`natural_key` held a surname, a household id and a dob on the real terminal.

    It was on `SAFE_KEYS`, so it was emitted verbatim after a substring scrub --
    and a surname has no shape for the scrub to find. Its content is chosen by
    the *source*, so it cannot be safe by construction, and it is tokenised now.
    """
    assert "natural_key" not in SAFE_KEYS
    assert "source_key" not in SAFE_KEYS
    assert PII_KEYS["natural_key"] == KIND_OPAQUE
    assert PII_KEYS["source_key"] == KIND_OPAQUE

    composite = "Fairbank-Mead|HH-004821|2015-12-16"
    redacted = redact({"natural_key": composite, "source_key": composite})
    for key, token in redacted.items():
        assert is_token(token), key
        assert "Fairbank" not in token and "HH-004821" not in token and "2015-12-16" not in token
    # deterministic: two rows sharing a natural key still visibly share one
    assert redacted["natural_key"] == redact({"natural_key": composite})["natural_key"]


def test_the_reported_leak_in_full() -> None:
    """The exact event `recon.ingest` emitted, asserted end to end.

    A duplicate-primary-key rejection carries the natural key twice: once as a
    field, and once interpolated **bare** into the free-text `detail`. Tokenising
    the field alone leaves the second copy, which has no shape and no adjacent
    key -- the sibling pass is what removes it.
    """
    composite = "Fairbank-Mead|HH-004821|2015-12-16"
    event = {
        "event": "ingest.record_rejected",
        "run_id": "http-anon",
        "kind": "duplicate_primary_key",
        "natural_key": composite,
        "record_ref": f"crm:contact:{composite}",
        "detail": (
            f"{composite!r} appears 2 times in this generation (lines 1, 2); "
            f"a repeated primary key is a structural rejection"
        ),
        "status": 409,
        "title": "duplicate primary key",
    }
    blob = canonical_json(redact(event))
    for needle in ("Fairbank-Mead", "HH-004821", "2015-12-16", composite):
        assert needle not in blob, f"{needle!r} still leaks: {blob}"
    # the line is still debuggable: the structure, the kind and the counts survive
    assert "duplicate_primary_key" in blob and "appears 2 times" in blob
    assert '"status":409' in blob


def test_the_sibling_pass_does_not_corrupt_unrelated_prose() -> None:
    """A literal is replaced at word boundaries only, longest first.

    A blind substring replacement would turn `Adenoid` into `[pii:name:…]oid`,
    which corrupts the log without protecting anything -- and the shortest
    values are excluded entirely for the same reason.
    """
    redacted = redact({"first_name": "Aden", "detail": "Adenoid Adaptive readAden Aden failed"})
    assert "Adenoid" in redacted["detail"], redacted["detail"]
    assert "Adaptive" in redacted["detail"]
    assert "readAden" in redacted["detail"]
    assert "[pii:name:" in redacted["detail"]
    # longest first: the composite goes before any of its own parts
    composite = redact(
        {
            "last_name": "Fairbank",
            "natural_key": "Fairbank|HH-004821",
            "detail": "rejected Fairbank|HH-004821 twice",
        }
    )
    assert "Fairbank" not in composite["detail"], composite["detail"]


# ---------------------------------------------------------------------------
# a value whose __str__ raises must not take the caller down
# ---------------------------------------------------------------------------


class _Hostile:
    """A value whose rendering raises -- an ORM row, a lazy model, a mock."""

    def __init__(self, secret: str) -> None:
        self.secret = secret

    def __str__(self) -> str:
        raise RuntimeError(f"cannot render {self.secret}")


def test_a_value_whose_str_raises_is_contained() -> None:
    """`_stringify` used to let it escape, and the interpreter printed the message.

    That message routinely quotes the record, and it is printed by the
    interpreter -- outside the chain, after redaction has already been
    abandoned. So the failure is contained: the value becomes a token over
    `<unrenderable>` plus its type, which names nobody.
    """
    hostile = _Hostile("Amriyo Fairbank-Mead")
    redacted = redact({"evidence": {"record": hostile}, "detail": "see attached"})
    blob = canonical_json(redacted)
    assert "Amriyo" not in blob and "Fairbank-Mead" not in blob
    # the rendering failed, was contained, and produced a token over a marker
    assert _stringify(hostile) == f"{UNRENDERABLE}:_Hostile"
    assert is_token(redacted["evidence"]["record"])
    # and under a PII key, and in a sequence, and as a bare leaf
    assert "Amriyo" not in canonical_json(redact({"first_name": hostile}))
    assert "Amriyo" not in canonical_json(redact({"note": [hostile]}))
    assert "Amriyo" not in canonical_json(redact(hostile, key="anything"))


def test_a_url_query_string_is_tokenised_parameter_by_parameter() -> None:
    """uvicorn's access line is `key=value&key=value`, and it now goes through here.

    Without `&` terminating the bare form, the first parameter's rule swallowed
    the entire rest of the request line into one token: safe, but it destroyed
    the line, and an unreadable log is what gets an allow-list widened under
    pressure. Every parameter is judged on its own instead.
    """
    line = (
        '127.0.0.1:1 - "GET /internal/ingest?guardian_email=amriyo.fairbank@keystone.test'
        "&dob=2015-12-16&household_id=HH-004821&last_name=Fairbank-Mead&generation=1"
        ' HTTP/1.1" 404'
    )
    scrubbed = scrub_text(line)
    for needle in ("amriyo.fairbank", "2015-12-16", "HH-004821", "Fairbank-Mead"):
        assert needle not in scrubbed, f"{needle!r} survived: {scrubbed}"
    for kept in ("GET /internal/ingest", "generation=1", 'HTTP/1.1" 404'):
        assert kept in scrubbed, f"{kept!r} was destroyed: {scrubbed}"
    for kind in (KIND_EMAIL, KIND_DOB, KIND_HOUSEHOLD, KIND_NAME):
        assert f"[pii:{kind}:" in scrubbed, f"no {kind} token in {scrubbed}"
    assert scrub_text(scrubbed) == scrubbed, "scrubbing a request line is not idempotent"
