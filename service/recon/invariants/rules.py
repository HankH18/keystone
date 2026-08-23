"""The rule registry: `rules/NNN_name.vX.sql`, discovered, parsed and linted.

One SQL file per rule (SS5.5's fourteen detection rules plus `R-000`, SS5.8's
`no_rule_in_scope` stamp). Every file returns the same four columns --
`(record_ref, entity_type, verdict, detail)` -- so the runner needs no per-rule
Python branch, and a fifteenth rule is a file, not a code change.

**SQL rules never normalize** (SS2). `rules/*.sql` may not call `lower()`,
`trim()`, `replace()`, `regexp_*` or any casefold on an identity field, may not
compute an ordinal, and may not use `IS DISTINCT FROM` (SS5.1 pins the
comparison form as `a IS NOT NULL AND b IS NOT NULL AND a <> b`). Normalization
is materialized upstream by Python into the `stg_*` columns. :func:`lint_rule_sql`
is what makes that a build failure rather than a convention, and
`tests/invariants/test_rule_lint.py` runs it over every committed file.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ABSENCE_RULES",
    "DB_VERDICT",
    "FORBIDDEN_SQL_TOKENS",
    "RULE_COLUMNS",
    "SCOPE_ENTITY_TYPES",
    "RuleSpec",
    "RuleSyntaxError",
    "lint_rule_sql",
    "load_rules",
    "rules_dir",
]

#: Every rule file returns exactly these columns, in this order.
RULE_COLUMNS: tuple[str, ...] = ("record_ref", "entity_type", "verdict", "detail")

#: SS5.3 -- the rules whose predicate depends on the **absence** of records from a
#: source. They are skipped for the whole run when any generation-3 load is
#: incomplete, emitting `unchecked` / `source_incomplete` rather than firing.
ABSENCE_RULES: frozenset[str] = frozenset(
    {"R-001", "R-002", "R-005", "R-007", "R-008", "R-009", "R-013"}
)

#: SS5.5's rule-scope table: which `stg_*` table each rule stamps a row for.
SCOPE_ENTITY_TYPES: dict[str, str] = {
    "stg_student": "student",
    "stg_payment": "payment",
    "stg_crm_contact": "contact",
    "stg_enrollment": "enrollment",
    "stg_crm_deal": "deal",
}

#: SS5.8 pins the verdict vocabulary as `ok` / `conflict` / `unchecked`. The
#: committed `invariant_verdict` Postgres enum (migration 0001, owned by another
#: ticket) spells the first two `pass` / `fail`. The mapping is total and
#: injective, it is applied at the write boundary only, and every in-memory API
#: here speaks the contract's words. See `contract_gaps` in the T-6 report.
DB_VERDICT: dict[str, str] = {"ok": "pass", "conflict": "fail", "unchecked": "unchecked"}

#: Tokens a rule file may not contain (SS2). `\b` on both ends so a column called
#: `lifecycle_norm` is not mistaken for a `lower(` call.
FORBIDDEN_SQL_TOKENS: tuple[tuple[str, str], ...] = (
    (r"\blower\s*\(", "lower() -- normalization belongs in recon/normalize.py"),
    (r"\bupper\s*\(", "upper() -- normalization belongs in recon/normalize.py"),
    (r"\binitcap\s*\(", "initcap() -- normalization belongs in recon/normalize.py"),
    (r"\b[lrb]?trim\s*\(", "trim() -- normalization belongs in recon/normalize.py"),
    (r"\breplace\s*\(", "replace() -- normalization belongs in recon/normalize.py"),
    (r"\btranslate\s*\(", "translate() -- normalization belongs in recon/normalize.py"),
    (r"\bregexp_\w+\s*\(", "regexp_* -- normalization belongs in recon/normalize.py"),
    (r"\bunaccent\s*\(", "unaccent() -- normalization belongs in recon/normalize.py"),
    (r"\bcasefold\b", "casefold -- normalization belongs in recon/normalize.py"),
    (r"\bnormalize\s*\(", "normalize() -- normalization belongs in recon/normalize.py"),
    (
        r"IS\s+DISTINCT\s+FROM",
        "IS DISTINCT FROM -- SS5.1 pins `a IS NOT NULL AND b IS NOT NULL "
        "AND a <> b`; NULL-tolerant inequality is a false-positive machine",
    ),
    # --- forms that normalize, casefold or re-parse without naming a normalizer ---
    # None of these appear in the committed rules; the grep exists so the NEXT rule
    # cannot reach for them. SS2's ban is on the *operation*, not on a token list, and
    # `email ILIKE other_email` is a casefold comparison exactly as `lower()` is.
    (
        r"\bILIKE\b",
        "ILIKE -- a casefold comparison; SS2 bans casefolding an identity field "
        "in SQL. Compare the materialized `*_norm` column instead",
    ),
    (
        r"\bSIMILAR\s+TO\b",
        "SIMILAR TO -- pattern matching on an identity field; SS2 bans regex "
        "matching in SQL, normalization is materialized upstream",
    ),
    (
        r"!?~\*?",
        "~ / ~* / !~ / !~* -- the regex-match operators, which `regexp_*` names "
        "in function form; SS2 bans both",
    ),
    (
        r"\bsplit_part\s*\(",
        'split_part() -- SS4.3 P2: "No name splitting is performed on either '
        'side"; both sides call the same committed `norm_name`',
    ),
    (
        r"\bsubstring\s*\(",
        "substring() -- re-parsing an identity field in SQL; the normalized form "
        "belongs in a `stg_*` column (SS3)",
    ),
    (
        r"\b(?:left|right)\s*\(",
        "left()/right() -- re-parsing an identity field in SQL; the normalized "
        "form belongs in a `stg_*` column (SS3)",
    ),
    (
        # `COLLATE "C"` is required and everywhere; anything else is a locale
        # dependency. `(?-i:...)` keeps the C case-sensitive under the lint's
        # IGNORECASE search, because `"c"` is a different collation from `"C"`.
        r'\bCOLLATE\s+(?!(?-i:"C")(?![\w.]))\S+',
        'COLLATE with a non-`"C"` collation -- SS4.6 survivorship and every '
        "`min()`/`<` in the rule set are defined on BYTE order; an ICU or en_US "
        "collation ignores `-` at the primary level and silently picks a "
        "different record out of a duplicate pair",
    ),
)

_HEADER = re.compile(r"^--\s*@(?P<key>[a-z_]+):\s*(?P<value>.*?)\s*$", re.MULTILINE)
_FILENAME = re.compile(r"^(?P<number>\d{3})_(?P<name>[a-z0-9_]+)\.(?P<version>v\d+)\.sql$")
_COMMENT = re.compile(r"--[^\n]*")
_LITERAL = re.compile(r"'(?:[^']|'')*'")

#: Where the committed rules live. `KEYSTONE_RULES_DIR` overrides it for a test
#: that wants a scratch tree; nothing else may relocate them.
RULES_DIR_ENV = "KEYSTONE_RULES_DIR"


def rules_dir() -> Path:
    """Absolute path to the committed `rules/` tree (repo root, not `service/`)."""
    override = os.environ.get(RULES_DIR_ENV)
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parents[3] / "rules"


class RuleSyntaxError(ValueError):
    """A rule file's name, header or body is not well formed."""


@dataclass(frozen=True, slots=True)
class RuleSpec:
    """One versioned rule: its identity, its SS5.5 scope, and its SQL."""

    rule_id: str
    rule_version: str
    name: str
    path: Path
    scope_table: str
    entity_type: str
    conflict_type: str | None
    sql: str

    @property
    def is_absence_rule(self) -> bool:
        """SS5.3: skipped (not fired) when a generation-3 load is incomplete."""
        return self.rule_id in ABSENCE_RULES

    @property
    def emits_conflicts(self) -> bool:
        return self.conflict_type is not None

    def gated_sql(self) -> str:
        """The SS5.3 substitute: one `unchecked` / `source_incomplete` row per scope row.

        Handing an absence rule an incomplete generation manufactures thousands of
        false positives, so the rule is not run at all -- but every row in its scope
        still has to be stamped, because SS5.8's per-record stamping is the grading
        contract and a silently missing row is indistinguishable from a passing one.
        """
        return (
            "SELECT scope.source_ref AS record_ref,\n"
            f"       '{self.entity_type}' AS entity_type,\n"
            "       'unchecked' AS verdict,\n"
            "       jsonb_build_object('reason', 'source_incomplete') AS detail\n"
            f"  FROM {self.scope_table} AS scope\n"
            " WHERE scope.generation = %(generation)s"
        )


def _strip_sql(sql: str) -> str:
    """Body with comments and string literals removed -- what the lint inspects.

    Literals are stripped so a rule may name a *reason code* or a documented token
    in an error string without tripping the grep, and comments so the prohibition
    can be quoted in the file that obeys it.

    **Comments are stripped FIRST, and the order is load-bearing.** A `--` comment
    may contain an apostrophe -- "the person's grade", "SS5.5's scope table" -- and
    an apostrophe is what opens a SQL string literal. Running the literal stripper
    over the raw text lets that lone apostrophe open a phantom literal that
    `_LITERAL` closes at the next `'` anywhere later in the file, replacing every
    byte in between (the whole rule body, in practice) with `''`. The grep then
    inspects a body that no longer contains the SQL, and :func:`lint_rule_sql`
    returns CLEAN for a file that calls `lower()` on an identity field. That is a
    silent bypass of SS2's prohibition on 8 of the 15 committed files, and because
    :func:`load_rules` is what enforces the lint, it is live at load time and not
    only in the test. Comments-first cannot regress that way: `_COMMENT` matches to
    end-of-line and never spans one.
    """
    return _LITERAL.sub("''", _COMMENT.sub(" ", sql))


def lint_rule_sql(sql: str, *, origin: str = "<sql>") -> tuple[str, ...]:
    """Return the lint violations in one rule body (empty tuple when clean)."""
    body = _strip_sql(sql)
    found: list[str] = []
    for pattern, message in FORBIDDEN_SQL_TOKENS:
        if re.search(pattern, body, re.IGNORECASE):
            found.append(f"{origin}: {message}")
    return tuple(found)


def _parse(path: Path) -> RuleSpec:
    match = _FILENAME.match(path.name)
    if match is None:
        raise RuleSyntaxError(
            f"{path.name!r} does not match the pinned rule filename shape "
            "`NNN_name.vX.sql` (CLAUDE.md repo layout)"
        )
    sql = path.read_text(encoding="utf-8")
    header = {m.group("key"): m.group("value") for m in _HEADER.finditer(sql)}

    required = ("rule_id", "scope")
    missing = [key for key in required if key not in header]
    if missing:
        raise RuleSyntaxError(f"{path.name}: missing header field(s) {missing}")

    scope = header["scope"]
    if scope not in SCOPE_ENTITY_TYPES:
        raise RuleSyntaxError(
            f"{path.name}: scope {scope!r} is not one of SS5.5's scope tables "
            f"{sorted(SCOPE_ENTITY_TYPES)}"
        )

    rule_id = header["rule_id"]
    if rule_id != f"R-{match.group('number')}":
        raise RuleSyntaxError(
            f"{path.name}: header rule_id {rule_id!r} disagrees with the filename number"
        )

    version = header.get("rule_version", match.group("version"))
    if version != match.group("version"):
        raise RuleSyntaxError(
            f"{path.name}: header rule_version {version!r} disagrees with the filename"
        )

    conflict_type = header.get("conflict") or None
    if conflict_type == "-":
        conflict_type = None

    violations = lint_rule_sql(sql, origin=path.name)
    if violations:
        raise RuleSyntaxError("; ".join(violations))

    return RuleSpec(
        rule_id=rule_id,
        rule_version=version,
        name=match.group("name"),
        path=path,
        scope_table=scope,
        entity_type=SCOPE_ENTITY_TYPES[scope],
        conflict_type=conflict_type,
        sql=sql,
    )


def iter_rule_paths(directory: Path | None = None) -> Iterator[Path]:
    """Every committed rule file, in filename order (which is rule-id order)."""
    root = directory or rules_dir()
    if not root.is_dir():
        raise FileNotFoundError(f"rules directory {root} does not exist")
    yield from sorted(root.glob("*.sql"))


def load_rules(directory: Path | None = None) -> tuple[RuleSpec, ...]:
    """Parse and lint every committed rule. Raises on the first malformed file."""
    specs = tuple(_parse(path) for path in iter_rule_paths(directory))
    seen: dict[str, Path] = {}
    for spec in specs:
        if spec.rule_id in seen:
            raise RuleSyntaxError(
                f"duplicate rule_id {spec.rule_id!r} in {spec.path.name} and "
                f"{seen[spec.rule_id].name}"
            )
        seen[spec.rule_id] = spec.path
    return specs
