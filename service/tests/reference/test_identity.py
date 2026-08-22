"""SS4.1 refs / `anchor_ref` / `person_key` and SS4.8 household inference."""

from __future__ import annotations

import uuid

import pytest

from recon.reference import (
    IDENTITY_REF_CLASSES,
    KEYSTONE_NS,
    anchor_ref,
    household_anchor_student,
    household_key,
    household_members,
    household_members_appdb,
    is_identity_ref,
    make_ref,
    parse_ref,
    person_key,
    ref_source,
    sources_involved,
    student_ref,
)


def _student(student_id: str, guardian: str, guardian2: str | None = None) -> dict:
    return {"id": student_id, "guardian_email": guardian, "guardian2_email": guardian2}


# --------------------------------------------------------------------------- refs


@pytest.mark.parametrize(
    ("source", "entity_type", "key", "expected"),
    [
        ("crm", "contact", "CRM-0000001", "crm:contact:CRM-0000001"),
        ("crm", "deal", "DEAL-0000001", "crm:deal:DEAL-0000001"),
        ("appdb", "student", "abc-123", "appdb:student:abc-123"),
        ("appdb", "enrollment", "def-456", "appdb:enrollment:def-456"),
        ("payments", "payment", "pi_0000001", "payments:payment:pi_0000001"),
    ],
)
def test_ref_construction_and_parsing(
    source: str, entity_type: str, key: str, expected: str
) -> None:
    assert make_ref(source, entity_type, key) == expected
    assert parse_ref(expected) == (source, entity_type, key)
    assert ref_source(expected) == source


def test_there_is_no_refund_ref_class() -> None:
    """SS12 D-1: a refund is a `payment` row transitioning, not a second entity."""
    with pytest.raises(ValueError, match="unknown ref class"):
        make_ref("payments", "refund", "re_1")


@pytest.mark.parametrize("ref", ["", "crm:contact:", "crm:contact", "nope:thing:1"])
def test_malformed_refs_are_refused(ref: str) -> None:
    with pytest.raises(ValueError):
        parse_ref(ref)


def test_identity_ref_classes_exclude_deals_and_enrollments() -> None:
    """SS4.1: deals and enrollments are never identity refs."""
    assert IDENTITY_REF_CLASSES == ("appdb:student:", "crm:contact:", "payments:payment:")
    assert is_identity_ref("appdb:student:s1")
    assert is_identity_ref("crm:contact:CRM-1")
    assert is_identity_ref("payments:payment:pi_1")
    assert not is_identity_ref("crm:deal:DEAL-1")
    assert not is_identity_ref("appdb:enrollment:e1")


def test_sources_involved_is_derived_mechanically_and_sorted() -> None:
    refs = ["payments:payment:pi_1", "crm:contact:CRM-1", "appdb:student:s1", "crm:deal:DEAL-1"]
    assert sources_involved(refs) == ("appdb", "crm", "payments")
    assert sources_involved(reversed(refs)) == ("appdb", "crm", "payments")
    assert sources_involved(["appdb:student:s1", "appdb:enrollment:e1"]) == ("appdb",)


# --------------------------------------------------------------- anchor / person_key


def test_anchor_ref_prefers_the_earlier_source_class_outright() -> None:
    refs = ["payments:payment:pi_0000001", "crm:contact:CRM-0000001", "appdb:student:zzz"]
    # `appdb:student:zzz` sorts last byte-wise but wins on class preference.
    assert anchor_ref(refs) == "appdb:student:zzz"
    assert anchor_ref(refs[:2]) == "crm:contact:CRM-0000001"
    assert anchor_ref(refs[:1]) == "payments:payment:pi_0000001"


def test_anchor_ref_breaks_ties_within_a_class_by_byte_order() -> None:
    refs = ["crm:contact:CRM-0000009", "crm:contact:CRM-0000002", "crm:contact:CRM-0000031"]
    assert anchor_ref(refs) == "crm:contact:CRM-0000002"


def test_anchor_ref_ignores_non_identity_refs() -> None:
    refs = ["crm:deal:DEAL-1", "appdb:enrollment:e1", "crm:contact:CRM-1"]
    assert anchor_ref(refs) == "crm:contact:CRM-1"
    with pytest.raises(ValueError, match="at least one identity ref"):
        anchor_ref(["crm:deal:DEAL-1", "appdb:enrollment:e1"])


def test_person_key_is_a_pure_function_of_the_anchor_ref() -> None:
    """SS4.1: NOT a hash of the ref set -- the ref set changes across generations
    and hashing it would split lineage."""
    gen1 = ["appdb:student:s1", "crm:contact:CRM-1"]
    gen3 = ["appdb:student:s1", "crm:contact:CRM-1", "payments:payment:pi_9", "crm:deal:D-1"]
    assert person_key(gen1) == person_key(gen3)
    assert person_key(gen1) == uuid.uuid5(KEYSTONE_NS, "appdb:student:s1")


def test_person_key_is_order_independent_and_stable() -> None:
    refs = ["crm:contact:CRM-2", "appdb:student:s1", "payments:payment:pi_1"]
    assert person_key(refs) == person_key(reversed(refs)) == person_key(sorted(refs))
    assert str(person_key(refs)) == str(person_key(refs))


def test_person_key_differs_between_persons() -> None:
    assert person_key(["appdb:student:s1"]) != person_key(["appdb:student:s2"])
    assert person_key(["crm:contact:CRM-1"]) != person_key(["appdb:student:CRM-1"])


def test_an_unattributed_payment_is_its_own_person() -> None:
    """SS4.1/SS5.2: each payment attributed to no person is an entity of its own."""
    assert person_key(["payments:payment:pi_1"]) == uuid.uuid5(KEYSTONE_NS, "payments:payment:pi_1")


# ------------------------------------------------------------------------ households


def test_household_key_is_the_primary_guardian_email_only() -> None:
    student = _student("s1", " `Parent.Guardian+billing@Gmail.com` ", "other@corp.com")
    assert household_key(student) == "parentguardian@gmail.com"


def test_guardian2_email_is_never_part_of_the_key() -> None:
    """SS4.8: `guardian2_email` is corroborating evidence only."""
    a = _student("s1", "primary@corp.com", "shared@corp.com")
    b = _student("s2", "different@corp.com", "shared@corp.com")
    assert household_key(a) != household_key(b)


def test_siblings_with_dirty_variants_of_one_address_share_a_household() -> None:
    """`G1`: dirty variants are fine -- they normalize equal."""
    siblings = [
        _student("s2", "P.Guardian@gmail.com"),
        _student("s1", " pguardian@GMAIL.com "),
        _student("s3", "`pguardian+school@gmail.com`"),
    ]
    grouped = household_members_appdb(siblings)
    assert list(grouped) == ["pguardian@gmail.com"]
    assert [member["id"] for member in grouped["pguardian@gmail.com"]] == ["s1", "s2", "s3"]


def test_grouping_is_exact_and_never_transitive() -> None:
    """SS4.8: explicitly not transitive closure over shared addresses, never union-find."""
    members = [
        _student("s1", "a@corp.com", "b@corp.com"),
        _student("s2", "b@corp.com", "c@corp.com"),
        _student("s3", "c@corp.com"),
    ]
    grouped = household_members_appdb(members)
    assert sorted(grouped) == ["a@corp.com", "b@corp.com", "c@corp.com"]
    assert all(len(v) == 1 for v in grouped.values())


def test_students_without_a_guardian_email_form_no_household() -> None:
    grouped = household_members_appdb([_student("s1", None), _student("s2", "   ")])  # type: ignore[arg-type]
    assert grouped == {}


def test_household_anchor_student_is_the_lowest_student_ref() -> None:
    """SS4.8: deterministic for a 2-4 child household where "primary" is not."""
    members = [_student("s31", "p@corp.com"), _student("s2", "p@corp.com")]
    anchor = household_anchor_student(members)
    assert anchor["id"] == "s2"
    assert student_ref(anchor) == "appdb:student:s2"
    assert household_anchor_student(reversed(members))["id"] == "s2"


def test_household_anchor_student_matches_the_grouped_order() -> None:
    members = [_student(f"s{n}", "p@corp.com") for n in (9, 10, 1)]
    grouped = household_members_appdb(members)
    assert grouped["p@corp.com"][0] is household_anchor_student(members)


def test_household_anchor_student_requires_a_member() -> None:
    with pytest.raises(ValueError, match="at least one member"):
        household_anchor_student([])


def test_a_ref_cannot_be_built_without_a_natural_key() -> None:
    with pytest.raises(ValueError, match="empty natural_key"):
        make_ref("crm", "contact", "")


# =====================================================================================
# SS4.1 -- `is_identity_ref` and the SCOPED payment clause (resolving MINOR-5)
# =====================================================================================


def test_a_payment_ref_is_an_identity_ref_ONLY_when_the_payment_resolved_to_nobody() -> None:
    """SS4.1: identity refs are student and contact refs, "plus -- and **only** for a
    payment that the cascade attributes to **no** person -- that payment's own
    `payments:payment:<id>`".

    The clause is scoped, so the scope is an ARGUMENT. Collapsing it into an
    unconditional `True` makes every attributed payment's ref an identity ref, which
    (a) lets a payment win `anchor_ref` for a person who already has a student ref and
    (b) makes C11's two payment refs -- which belong to an ATTRIBUTED pair by C11's own
    predicate -- count as identity refs in SS8's clean-sample intersection probe. SS5.4
    calls that out by name: "C2's and C11's `entity_refs` are payment refs, which are
    identity refs only for an *unattributed* payment".
    """
    unattributed = "payments:payment:pi_0000900"
    assert is_identity_ref(unattributed)  # default: attributed to no person
    assert is_identity_ref(unattributed, payment_attributed=False)
    assert not is_identity_ref(unattributed, payment_attributed=True)


@pytest.mark.parametrize("ref", ["appdb:student:s1", "crm:contact:CRM-1"])
def test_the_payment_flag_scopes_the_payment_class_only(ref: str) -> None:
    """It can never make a student or a contact ref non-identity."""
    assert is_identity_ref(ref)
    assert is_identity_ref(ref, payment_attributed=True)
    assert is_identity_ref(ref, payment_attributed=False)


@pytest.mark.parametrize("ref", ["crm:deal:DEAL-1", "appdb:enrollment:e1"])
def test_deals_and_enrollments_are_never_identity_refs_under_either_scope(ref: str) -> None:
    assert not is_identity_ref(ref)
    assert not is_identity_ref(ref, payment_attributed=True)
    assert not is_identity_ref(ref, payment_attributed=False)


def test_anchor_ref_reads_the_unattributed_default_and_is_correct_to() -> None:
    """The only place a payment ref legitimately appears INSIDE a person's ref set is
    SS5.2's "each payment attributed to no person"; an attributed payment contributes
    its ref as evidence, never as identity. So `anchor_ref`'s default is the right one."""
    assert anchor_ref(["payments:payment:pi_1"]) == "payments:payment:pi_1"
    # ...and a payment never outranks a student or a contact (SS4.1 preference order).
    assert anchor_ref(["payments:payment:pi_0", "crm:contact:CRM-9"]) == "crm:contact:CRM-9"
    assert anchor_ref(["payments:payment:pi_0", "appdb:student:zzz"]) == "appdb:student:zzz"


# =====================================================================================
# SS4.8 ruling 15 -- `household_members` is EXPORTED, with a pinned key set and ordering
# =====================================================================================


def _contact(crm_id: str, email: str | None) -> dict:
    return {"crm_id": crm_id, "email": email}


def test_household_members_is_importable_from_the_shared_module() -> None:
    """SS4.8 ruling 15 / SS0: neither side may re-implement a shared symbol, and a symbol
    the contract DEFINES but the module does not EXPORT is exactly the symbol a consumer
    re-implements -- the R23 drift this module exists to prevent."""
    import recon.reference as reference

    assert "household_members" in reference.__all__
    assert "KEY_CLASSES" in reference.__all__
    assert reference.KEY_CLASSES == ("ext", "email", "namedob")


def test_household_members_is_the_appdb_group_union_the_matching_contacts() -> None:
    students = [_student("s2", "P.Guardian@gmail.com"), _student("s1", " pguardian@GMAIL.com ")]
    contacts = [
        _contact("CRM-0000009", "pguardian@gmail.com"),
        _contact("CRM-0000002", "`P.Guardian@Gmail.com`"),  # dirty variant, normalizes equal
    ]
    members = household_members(students, contacts)
    assert list(members) == ["pguardian@gmail.com"]
    # app-DB students FIRST by student ref, THEN CRM contacts by contact ref (SS4.8)
    assert [m.get("id") or m["crm_id"] for m in members["pguardian@gmail.com"]] == [
        "s1",
        "s2",
        "CRM-0000002",
        "CRM-0000009",
    ]
    assert members["pguardian@gmail.com"][0] is household_anchor_student(students)


def test_ordering_is_total_so_input_order_cannot_change_the_result() -> None:
    students = [_student(f"s{n}", "p@corp.com") for n in (9, 10, 1)]
    contacts = [_contact(f"CRM-{n}", "P@Corp.com") for n in (3, 1, 2)]
    forward = household_members(students, contacts)
    backward = household_members(list(reversed(students)), list(reversed(contacts)))
    assert forward == backward


def test_a_contact_matching_no_household_key_creates_no_key() -> None:
    """SS4.8 ruling 15: the key set is exactly the `household_key` values of the
    STUDENTS. A contact whose `norm_email` matches no student is a deal-less lead
    (SS11.4, `G11`) and belongs to no household -- there are 18,175 of them, and letting
    each mint a household would swamp every `|household_members_appdb(k)|` test."""
    students = [_student("s1", "p@corp.com")]
    leads = [_contact("CRM-1", "lead@corp.com"), _contact("CRM-2", None), _contact("CRM-3", "  ")]
    members = household_members(students, leads)
    assert list(members) == ["p@corp.com"]
    assert [m["id"] for m in members["p@corp.com"]] == ["s1"]


def test_household_members_defaults_to_the_appdb_group_alone() -> None:
    students = [_student("s2", "p@corp.com"), _student("s1", "p@corp.com")]
    assert household_members(students) == household_members_appdb(students)


def test_the_child_count_is_taken_from_the_APPDB_group_never_the_union() -> None:
    """SS4.8: `P3`'s "exactly one child" and C8's "at least two children" both evaluate
    `|household_members_appdb(k)|`. A planted C3 duplicate pair is TWO contacts for ONE
    student (SS5.6 C3), so counting the union would turn a single-child household into a
    three-member one and silently disable `P3`."""
    students = [_student("s1", "p@corp.com")]
    c3_pair = [_contact("CRM-1", "p@corp.com"), _contact("CRM-2", "p@corp.com")]
    assert len(household_members_appdb(students)["p@corp.com"]) == 1
    assert len(household_members(students, c3_pair)["p@corp.com"]) == 3


# =====================================================================================
# SS5.4 -- a natural key may not carry a CONTROL CHARACTER
# =====================================================================================


@pytest.mark.parametrize(
    ("key", "label"),
    [
        ("a\x1fb", "US -- the fingerprint's intra-section joiner"),
        ("a\x1eb", "RS -- canon_value's sequence joiner"),
        ("a\x00b", "NUL"),
        ("a\nb", "newline"),
        ("a\tb", "tab"),
        ("a\rb", "carriage return"),
        ("\x1fleading", "leading US"),
        ("trailing\x1f", "trailing US"),
    ],
    ids=lambda x: x if len(x) > 4 else repr(x),
)
def test_make_ref_refuses_a_natural_key_containing_a_control_character(
    key: str, label: str
) -> None:
    """SS5.4. A ref is an element of the fingerprint's section 2, joined by `\\x1f`.

    A ref carrying a raw `\\x1f` is indistinguishable from two refs once the payload is
    assembled, so two different conflicts would share one fingerprint -- and the
    fingerprint is the idempotency key R16's oscillation dedup and the whole proposal
    pipeline are keyed on. No committed natural key (`crm_id`, a uuid, `pi_*`,
    `external_ref`) can contain a control character, so a key that does is corrupt input.
    """
    with pytest.raises(ValueError, match="control character"):
        make_ref("appdb", "student", key)


@pytest.mark.parametrize(
    "key",
    [
        "CRM-0000001",
        "pi_0000001",
        "3f2b1c8e-0000-5000-8000-000000000001",
        "student-1",
        "a b",  # an ordinary space is NOT a control character
        "key:with:colons",
        "key/with/slashes",
        "ünïcode-kéy",
        "a\\b",  # a backslash is legal; the payload escapes it
        "\x7f",  # DEL is outside the refused C0 range
    ],
)
def test_an_ordinary_natural_key_is_still_accepted(key: str) -> None:
    """The guard is narrow on purpose: exactly `\\x00`-`\\x1f`, nothing else."""
    assert make_ref("appdb", "student", key) == f"appdb:student:{key}"


def test_the_control_character_guard_and_the_payload_escaping_are_two_guards() -> None:
    """`make_ref` refuses the ref; the fingerprint escapes it anyway, for a ref that
    reaches the hash without passing through `make_ref` (SS5.4)."""
    with pytest.raises(ValueError, match="control character"):
        make_ref("appdb", "student", "a\x1fb")
    from recon.reference import fingerprint

    values = {"household_key": "p@corp.com", "dropped_source": "crm", "eligible_member_count": 3}
    assert fingerprint("C8", ["appdb:student:a\x1fb"], [], values) != fingerprint(
        "C8", ["appdb:student:a", "appdb:student:b"], [], values
    )


# =====================================================================================
# SS4.8 -- `student_ref` / `household_anchor_student` refuse a non-student record
# =====================================================================================


def test_student_ref_refuses_a_record_with_no_id() -> None:
    """A CRM contact carries `crm_id`, not `id`.

    `str(None)` builds the plausible-looking `appdb:student:None`, which sorts before
    every real student ref -- so the failure is silent and wrong-way-round rather than
    loud.
    """
    with pytest.raises(ValueError, match="app-DB student record"):
        student_ref(_contact("CRM-0000001", "p@corp.com"))
    with pytest.raises(ValueError, match="app-DB student record"):
        student_ref({})
    with pytest.raises(ValueError, match="app-DB student record"):
        student_ref({"id": None, "guardian_email": "p@corp.com"})


def test_household_anchor_student_refuses_the_household_members_union() -> None:
    """SS4.8. `household_members` returns app-DB students **then** CRM contacts; the
    anchor is defined over the app-DB group alone (`household_members_appdb`).

    Handed the union, the old code returned the CONTACT: `student_ref(contact)` yielded
    `appdb:student:None`, which sorts first. The household's anchor enrollment -- and
    therefore its one `program` (`G29`, SS1.2) -- would have been read off a record that
    has no enrollment at all.
    """
    students = [_student("s2", "p@corp.com"), _student("s1", "p@corp.com")]
    contacts = [_contact("CRM-0000001", "p@corp.com")]
    members = household_members(students, contacts)["p@corp.com"]
    assert len(members) == 3  # two students then one contact

    with pytest.raises(ValueError, match="app-DB student record"):
        household_anchor_student(members)

    # the supported call -- the app-DB group -- still works and is unchanged
    appdb_group = household_members_appdb(students)["p@corp.com"]
    assert household_anchor_student(appdb_group)["id"] == "s1"


def test_a_lone_contact_can_never_masquerade_as_the_anchor() -> None:
    """The narrow version of the same bug: one contact, no students at all."""
    with pytest.raises(ValueError, match="app-DB student record"):
        household_anchor_student([_contact("CRM-0000001", "p@corp.com")])
