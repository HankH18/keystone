"""SS2: `rules/*.sql` may not normalize, and the lint is what makes that a build failure.

    "SQL rules may not normalize. `rules/*.sql` must never call `lower()`, `trim()`,
     `replace()`, `regexp_*` or any casefold on an identity field, and may not compute
     an ordinal. Normalization is materialized upstream by Python into `stg_*` columns
     (SS3). A committed lint test greps the rule files for these tokens and fails the
     build."

SS5.1 adds one more: the comparison form is pinned as
`a IS NOT NULL AND b IS NOT NULL AND a <> b`, and "the committed rule lint
additionally **fails any `rules/*.sql` containing `IS DISTINCT FROM`**".

Both halves are tested: the committed files are clean, **and** the lint actually
catches each token. A grep that matches nothing is indistinguishable from a grep that
is broken, so every pattern gets a positive control.
"""

from __future__ import annotations

import pytest

from recon.invariants.rules import (
    ABSENCE_RULES,
    FORBIDDEN_SQL_TOKENS,
    RuleSyntaxError,
    _strip_sql,
    lint_rule_sql,
    load_rules,
    rules_dir,
)
from recon.reference import CONFLICT_TYPES, RULE_ID_BY_TYPE

RULES = load_rules()


def test_every_committed_rule_passes_the_lint() -> None:
    for spec in RULES:
        assert lint_rule_sql(spec.sql, origin=spec.path.name) == ()


@pytest.mark.parametrize(
    "snippet",
    [
        "SELECT lower(c.email) FROM stg_crm_contact c",
        "SELECT upper(c.first_name) FROM stg_crm_contact c",
        "SELECT trim(c.first_name) FROM stg_crm_contact c",
        "SELECT btrim(c.first_name) FROM stg_crm_contact c",
        "SELECT replace(c.email, '.', '') FROM stg_crm_contact c",
        "SELECT translate(c.email, '.', '') FROM stg_crm_contact c",
        "SELECT regexp_replace(c.email, '[+].*@', '@') FROM stg_crm_contact c",
        "SELECT initcap(c.first_name) FROM stg_crm_contact c",
        "SELECT unaccent(c.first_name) FROM stg_crm_contact c",
        "SELECT normalize(c.first_name, NFKD) FROM stg_crm_contact c",
        "SELECT a WHERE x IS DISTINCT FROM y",
        "SELECT a WHERE x IS   DISTINCT\n  FROM y",
        # Forms that normalize, casefold or re-parse without naming a normalizer.
        "SELECT 1 FROM stg_crm_contact c, stg_crm_deal d WHERE c.email ILIKE d.name",
        "SELECT 1 FROM stg_crm_contact c WHERE c.first_name ~* '^jo'",
        "SELECT 1 FROM stg_crm_contact c WHERE c.first_name ~ '^Jo'",
        "SELECT 1 FROM stg_crm_contact c WHERE c.first_name !~ '^Jo'",
        "SELECT 1 FROM stg_crm_contact c WHERE c.first_name SIMILAR TO 'Jo%'",
        "SELECT split_part(p.payer_name, ' ', 1) FROM stg_payment p",
        "SELECT substring(c.email from 1 for 3) FROM stg_crm_contact c",
        "SELECT left(c.first_name, 3) FROM stg_crm_contact c",
        "SELECT right(c.first_name, 3) FROM stg_crm_contact c",
        'SELECT min(c.email COLLATE "en_US") FROM stg_crm_contact c',
        'SELECT min(c.email COLLATE "C.UTF-8") FROM stg_crm_contact c',
    ],
)
def test_the_lint_actually_catches_the_forbidden_forms(snippet: str) -> None:
    """A positive control per pattern -- otherwise a broken grep reads as a clean tree."""
    assert lint_rule_sql(snippet) != ()


@pytest.mark.parametrize(
    "snippet",
    [
        # Every one of these was returned CLEAN by the pre-fix `_strip_sql`, which ran
        # the string-literal stripper BEFORE the comment stripper: the apostrophe in
        # the comment opened a phantom literal that the regex closed at the next `'`
        # anywhere later, replacing the entire body in between with `''`. Eight of the
        # fifteen committed files were linted on a body that no longer contained their
        # SQL -- `009_stale_pointer.v1.sql` on 4% of it -- and `load_rules()` enforces
        # this lint, so the bypass was live at load time and not only in this test.
        "-- the person's email\nSELECT lower(c.email) FROM stg_crm_contact c",
        "-- SS5.5's scope table\nSELECT trim(s.first_name) FROM stg_student s WHERE s.x = 'a'",
        "-- it's fine\n-- really\nSELECT a WHERE x IS DISTINCT FROM y AND z = 'q'",
        "SELECT 1 -- don't normalize\n, upper(c.last_name) FROM stg_crm_contact c",
    ],
)
def test_an_apostrophe_in_a_comment_cannot_blind_the_lint(snippet: str) -> None:
    """Comments are stripped FIRST, and the order is what makes the grep real."""
    assert lint_rule_sql(snippet) != ()


def test_the_stripped_body_retains_substantially_all_of_each_committed_rule() -> None:
    """A structural guard on the stripper itself, per committed file.

    The positive controls above only fire on snippets *this* test file authors. This
    one asserts the property that actually failed: that what the lint greps is still
    the rule. Anything that swallows a body -- a stripper ordering bug, an unbalanced
    quote, a `$$`-quoted block -- shows up as a collapse in length here and fails the
    build, instead of silently turning the grep into a no-op on a real rule.
    """
    for spec in RULES:
        raw = spec.path.read_text(encoding="utf-8")
        stripped = _strip_sql(raw)
        # Comments are legitimately most of a well-documented rule file, so the floor
        # is against the comment-free size, not the file size.
        comment_free = sum(
            len(line.split("--", 1)[0]) + 1 for line in raw.splitlines() if line.strip()
        )
        assert len(stripped.strip()) >= 0.6 * comment_free, (
            f"{spec.path.name}: `_strip_sql` kept {len(stripped.strip())} chars of a "
            f"~{comment_free}-char comment-free body -- the lint is inspecting "
            "something that is no longer the rule"
        )


def test_lint_ignores_comments_and_string_literals() -> None:
    """A rule may *quote* the prohibition it obeys, and may name a reason code.

    The lint strips comments and literals first: otherwise the only way to document
    "this rule does not call lower()" inside the file would be to not document it.
    """
    body = (
        "-- this rule must never call lower() or use IS DISTINCT FROM\n"
        "SELECT 'regexp_replace is not used here' AS note FROM stg_student"
    )
    assert lint_rule_sql(body) == ()


def test_every_forbidden_token_has_a_message() -> None:
    for pattern, message in FORBIDDEN_SQL_TOKENS:
        assert pattern and message


def test_the_rule_set_is_r000_through_r014() -> None:
    """SS5.5's fourteen detection rules plus SS5.8's synthetic `R-000`."""
    ids = [spec.rule_id for spec in RULES]
    assert ids == [f"R-{number:03d}" for number in range(15)]


def test_each_detection_rule_declares_its_contract_conflict_type() -> None:
    """`R-0NN` implements `C<NN>` (SS5.5); `R-000` implements none."""
    by_id = {spec.rule_id: spec for spec in RULES}
    assert by_id["R-000"].conflict_type is None
    for conflict_type in CONFLICT_TYPES:
        rule_id = RULE_ID_BY_TYPE[conflict_type]
        assert by_id[rule_id].conflict_type == conflict_type


def test_rule_scopes_match_the_contract_scope_table() -> None:
    """The table below SS5.5, restated literally.

    Getting a scope wrong does not fail loudly -- it stamps the wrong rows and the
    per-record grading contract of SS5.8 quietly stops holding.
    """
    expected = {
        "R-001": "stg_student",
        "R-005": "stg_student",
        "R-006": "stg_student",
        "R-008": "stg_student",
        "R-014": "stg_student",
        "R-002": "stg_payment",
        "R-011": "stg_payment",
        "R-012": "stg_payment",
        "R-013": "stg_payment",
        "R-003": "stg_crm_contact",
        "R-004": "stg_crm_contact",
        "R-010": "stg_crm_contact",
        "R-007": "stg_enrollment",
        "R-009": "stg_enrollment",
        "R-000": "stg_crm_deal",
    }
    assert {spec.rule_id: spec.scope_table for spec in RULES} == expected


def test_stg_crm_deal_is_in_the_scope_of_no_detection_rule() -> None:
    """SS5.5: "`stg_crm_deal` is in the scope of **no** rule", hence every deal row
    carries the synthetic `R-000` stamp of SS5.8."""
    detection = [spec for spec in RULES if spec.conflict_type is not None]
    assert all(spec.scope_table != "stg_crm_deal" for spec in detection)


def test_absence_rules_are_exactly_the_seven_the_contract_names() -> None:
    """SS5.3: "C1, C2, C5, C7, C8, C9, C13"."""
    contract_names = frozenset({"R-001", "R-002", "R-005", "R-007", "R-008", "R-009", "R-013"})
    assert contract_names == ABSENCE_RULES
    assert {spec.rule_id for spec in RULES if spec.is_absence_rule} == ABSENCE_RULES


def test_a_malformed_rule_file_is_rejected(tmp_path, monkeypatch) -> None:
    """Discovery refuses a file it cannot identify rather than skipping it silently."""
    (tmp_path / "0xx_bad_name.sql").write_text("SELECT 1")
    monkeypatch.setenv("KEYSTONE_RULES_DIR", str(tmp_path))
    with pytest.raises(RuleSyntaxError):
        load_rules()


def test_a_rule_that_normalizes_is_rejected_at_load_time(tmp_path, monkeypatch) -> None:
    """The lint is enforced by the loader, not only by this test file."""
    (tmp_path / "001_bad.v1.sql").write_text(
        "-- @rule_id: R-001\n-- @scope: stg_student\nSELECT lower(s.first_name) FROM stg_student s"
    )
    monkeypatch.setenv("KEYSTONE_RULES_DIR", str(tmp_path))
    with pytest.raises(RuleSyntaxError, match="lower"):
        load_rules()


def test_rules_live_where_claude_md_pins_them() -> None:
    assert rules_dir().name == "rules"
    assert (rules_dir() / "001_paid_but_no_deal.v1.sql").is_file()
