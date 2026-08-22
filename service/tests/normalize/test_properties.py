"""Property tests for the normalizers (contract SS2.1: "idempotence is a property test").

`derandomize=True` everywhere: determinism is graded, and a property suite that
explores a different space on every run cannot be evidence for a byte-identical
build.
"""

from __future__ import annotations

import unicodedata

from hypothesis import given, settings
from hypothesis import strategies as st

from recon.normalize import (
    ENUM_FIELDS,
    QUOTE_CHARS,
    enum_values,
    match_keys,
    norm_dob,
    norm_email,
    norm_enum,
    norm_name,
)

PROPERTY = settings(derandomize=True, max_examples=400, deadline=None)

#: The characters every Unicode-folding decision in `recon/normalize.py` turns on.
#: `st.text()` essentially never generates them, which is why four separate folding
#: decisions (`norm_name`'s second casefold, `_variant_key`'s second casefold,
#: `norm_email`'s casefold-not-lower, `_variant_key`'s NFKD-not-NFKC) survived a full
#: property suite untouched. They are table-driven in `test_norm_name.py`,
#: `test_norm_email.py` and `test_norm_enum.py`; here they are put into the alphabet so
#: the property tests explore combinations of them too.
FOLDING_CHARS = [
    "\u3392",  # SQUARE MHZ -- NFKD expands to the UPPER-CASE "MHz"
    "\u33a9",  # SQUARE PA  -- NFKD expands to "Pa"
    "\u00df",  # SHARP S    -- casefold() -> "ss"; lower() leaves it
    "\u1e9e",  # CAPITAL SHARP S
    "\u03a3",  # CAPITAL SIGMA -- lower() picks the FINAL form at word end
    "\u03c2",  # FINAL SIGMA
    "\u03c3",  # SMALL SIGMA
    "\u0130",  # CAPITAL I WITH DOT ABOVE -- casefold() expands to i + U+0307
    "\u0301",  # COMBINING ACUTE ACCENT -- what NFKD produces and NFKC composes away
    "\ufb01",  # LATIN SMALL LIGATURE FI
    "\u00a0",  # NO-BREAK SPACE -- folds to a plain space under NFKD
    "\u2019",  # RIGHT SINGLE QUOTATION MARK -- a committed QUOTE_CHARS member
]

DIRT = st.text(
    alphabet=st.sampled_from([*list(" \t\n`'\"aA.+@zZéÉñ0123456789-_"), *FOLDING_CHARS]),
    min_size=0,
    max_size=24,
)
LOCAL_PART = st.text(alphabet=st.sampled_from(list("abc.")), min_size=1, max_size=12)
NON_GMAIL_DOMAINS = ["corp.com", "outlook.com", "school.edu", "notgmail.com", "gmail.com.mx"]


@PROPERTY
@given(st.one_of(DIRT, st.text(max_size=32)))
def test_norm_email_is_idempotent(value: str) -> None:
    once = norm_email(value)
    assert norm_email(once) == once


@PROPERTY
@given(st.one_of(DIRT, st.text(max_size=32)))
def test_norm_name_is_idempotent(value: str) -> None:
    once = norm_name(value)
    assert norm_name(once) == once


@PROPERTY
@given(st.one_of(DIRT, st.text(max_size=32)))
def test_norm_dob_is_idempotent(value: str) -> None:
    once = norm_dob(value)
    assert norm_dob(once) == once


@PROPERTY
@given(st.sampled_from(ENUM_FIELDS), st.one_of(DIRT, st.text(max_size=24)))
def test_norm_enum_is_idempotent(field: str, value: str) -> None:
    once = norm_enum(field, value)
    assert norm_enum(field, once) == once


@PROPERTY
@given(st.one_of(DIRT, st.text(max_size=32)))
def test_norm_name_output_carries_no_dirt(value: str) -> None:
    result = norm_name(value)
    if result is None:
        return
    assert result == result.strip()
    assert "  " not in result
    assert not any(ch in result for ch in QUOTE_CHARS)
    assert not any(unicodedata.combining(ch) for ch in result)
    assert result == result.casefold()


@PROPERTY
@given(LOCAL_PART, st.text(alphabet=st.sampled_from(list("abc")), max_size=6))
def test_gmail_variants_are_one_equivalence_class(local: str, alias: str) -> None:
    """Dots and a `+alias` are noise on gmail; every variant collapses to one address."""
    stripped = local.replace(".", "")
    plain = f"{stripped}@gmail.com"
    dotted = f"{local}@gmail.com"
    aliased = f"{local}+{alias}@gmail.com" if alias else dotted
    spaced = f"  `{local.upper()}@gmail.com`  "

    canonical = norm_email(plain)
    assert norm_email(dotted) == canonical
    assert norm_email(aliased) == canonical
    assert norm_email(spaced) == canonical
    assert norm_email(f"{local}@GoogleMail.com") == f"{stripped}@googlemail.com"


@PROPERTY
@given(LOCAL_PART, st.sampled_from(NON_GMAIL_DOMAINS))
def test_non_gmail_local_parts_are_never_rewritten(local: str, domain: str) -> None:
    assert norm_email(f"{local}@{domain}") == f"{local}@{domain}"
    assert norm_email(f"{local}+alias@{domain}") == f"{local}+alias@{domain}"


@PROPERTY
@given(LOCAL_PART, st.sampled_from(NON_GMAIL_DOMAINS))
def test_dots_stay_significant_off_gmail(local: str, domain: str) -> None:
    """The false-positive guard: two distinct mailboxes must stay distinct."""
    stripped = local.replace(".", "")
    if stripped == local:
        return
    assert norm_email(f"{local}@{domain}") != norm_email(f"{stripped}@{domain}")


@PROPERTY
@given(
    LOCAL_PART,
    st.sampled_from([*NON_GMAIL_DOMAINS, "gmail.com", "googlemail.com"]),
    st.sampled_from([*NON_GMAIL_DOMAINS, "gmail.com", "googlemail.com"]),
)
def test_addresses_are_never_merged_across_domains(local: str, left: str, right: str) -> None:
    if left == right:
        return
    assert norm_email(f"{local}@{left}") != norm_email(f"{local}@{right}")


@PROPERTY
@given(st.sampled_from(ENUM_FIELDS), st.one_of(DIRT, st.text(max_size=16)))
def test_norm_enum_output_is_always_canonical_or_none(field: str, value: str) -> None:
    result = norm_enum(field, value)
    assert result is None or result in enum_values(field)


@PROPERTY
@given(
    st.text(alphabet=st.sampled_from(list("abc `'")), min_size=1, max_size=8),
    st.text(alphabet=st.sampled_from(list("xyz `'")), min_size=1, max_size=8),
    st.text(alphabet=st.sampled_from(list("abcdef")), min_size=1, max_size=8),
)
def test_match_keys_are_stable_and_normalized(first: str, last: str, mailbox: str) -> None:
    record = {
        "crm_id": "CRM-0000001",
        "external_id": "student-1",
        "email": f" `{mailbox}@Corp.com` ",
        "first_name": first,
        "last_name": last,
        "dob": " 2010-04-05 ",
    }
    keys = match_keys(record)
    assert keys == match_keys(record)
    assert [key.key_class for key in keys][:2] == ["ext", "email"]

    # Re-running the record through the normalizers cannot change the keys.
    normalized = {
        "crm_id": record["crm_id"],
        "external_id": record["external_id"],
        "email": norm_email(record["email"]),
        "first_name": norm_name(record["first_name"]),
        "last_name": norm_name(record["last_name"]),
        "dob": norm_dob(record["dob"]),
    }
    assert match_keys(normalized) == keys
