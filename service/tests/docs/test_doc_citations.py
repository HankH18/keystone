"""Every ``path:line`` citation in the graded prose must still point at the code it names.

``ARCHITECTURE.md`` and ``AI_USAGE.md`` cite the implementation by ``path:line``. Prose
cannot be trusted to re-verify itself, so this module does it -- but it is deliberate
about *which half* of a citation it enforces, because the first version of this suite
enforced the wrong half and was red more often than it was useful.

What is enforced, on every run
------------------------------
The **symbol**, never the integer.

``exists``
    the path resolves, every line in the span is inside the file, and at least one line
    in the span is non-blank. A blank line is never a citation of anything.

``anchored``
    the prose names a code symbol near the citation, and the cited file **defines** it --
    a ``def``/``class``, a MODULE-level binding, an alembic ``name="..."`` constraint, a
    YAML key. A rename, a deletion, or a move to another file turns this
    red. Pure line drift does not, and that is the point: the suite went red once for a
    verifier who had touched nothing, because an unrelated stream moved
    ``def materialize(`` from line 818 to line 814. A gate that reddens whenever any
    unrelated line moves is a gate people learn to ignore, and a gate people ignore
    protects nothing.

    Citations that name no code -- the measurement ones, ``api/internal.py:114-117`` for
    a timing quoted in a docstring -- anchor on a number of three digits or more that the
    prose quotes and the span carries. A citation that names neither code nor a number
    falls back to sharing a distinctive word with the span. That fallback is reached
    **only** when there is nothing else to anchor on.

``points at a definition, not a mention``
    the tier that closes the hole the previous version left wide open. A verifier changed
    ``ARCHITECTURE.md``'s ``apply.py:1885`` (``def assert_sources_are_unwritable``) to
    ``apply.py:184`` -- the string ``"assert_sources_are_unwritable",`` inside ``__all__``
    -- and the whole suite stayed green, because "the symbol appears somewhere in the
    span" was the entire test. It no longer is: when the cited span mentions a symbol the
    prose names and **every** such mention is an export-list entry, an import, or a
    comment/docstring line, the citation fails and the failure prints the definition's
    real line. A span that mentions nothing is left to the ``anchored`` tier -- that is
    line drift, and line drift is not a defect here.

What is NOT enforced, and how to make it exact
----------------------------------------------
The integers. They are re-derived from the working tree on demand::

    cd service && uv run python -m tests.docs.test_doc_citations --write

which rewrites every citation whose anchor symbol has moved out from under it, and::

    cd service && uv run python -m tests.docs.test_doc_citations --check

which reports them and exits non-zero without touching a file. ``--check`` is
deliberately **not** wired into pytest; wiring it in would re-create the fragile gate.

The updater will not move a citation whose cited span still mentions its anchor symbol,
so a deliberate call-site citation (``reconciler.py:1627`` for the line that *calls*
``_rationale``) survives a resync intact.

There is no quarantine and no exemption list. There used to be one holding eleven of
``AI_USAGE.md``'s thirteen citations -- 85% of a graded document, every entry genuinely
stale, none of them checked by either tier. ``test_neither_document_loses_its_citations``
is what stops a future red run being "fixed" by deleting the sentence instead.
"""

from __future__ import annotations

import argparse
import ast
import re
from collections import defaultdict
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The documents this suite holds to the code. Both are graded deliverables, and both are
#: checked by every tier: no document is exempt, and neither is any citation inside one.
DOCUMENTS: tuple[str, ...] = ("ARCHITECTURE.md", "AI_USAGE.md")

#: How many citations each document carried when this floor was last set. A red citation
#: must be repaired or re-pointed, never deleted: without this, "the doc no longer cites
#: anything" is a passing run.
MINIMUM_CITATIONS: dict[str, int] = {"ARCHITECTURE.md": 50, "AI_USAGE.md": 17}

#: Where a bare cited path may live, in resolution order. ``llm.py`` means
#: ``service/recon/llm.py``; ``recon/llm.py`` and ``tests/...`` mean ``service/...``.
SEARCH_PREFIXES: tuple[str, ...] = (
    "",
    "service",
    "service/recon",
    "service/migrations/versions",
    "docs",
)

_EXTENSIONS = "py|sql|yaml|yml|ts|tsx|md|txt|json"

CITATION_RE = re.compile(
    rf"(?P<path>[A-Za-z0-9_./-]+\.(?:{_EXTENSIONS}))"
    r":(?P<start>\d+)(?:-(?P<end>\d+))?"
)

#: Any file path, cited or not. Blanked out of a citation's context before candidates are
#: read, so ``invariants/context.py`` cannot anchor itself with the word "context".
_PATHISH_RE = re.compile(rf"[A-Za-z0-9_./-]+\.(?:{_EXTENSIONS})(?::\d+(?:-\d+)?)?")
_BARE_LINE_REF_RE = re.compile(r":\d+")

_BACKTICK_RE = re.compile(r"`([^`]*)`")
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DOTTED_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
#: ``reconcile()`` is a symbol. ``Measured (`` is a sentence -- the space is the whole
#: difference, and without it every capitalised word before a parenthesis anchored.
_CALL_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\(")
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_CAMEL_RE = re.compile(r"[a-z][A-Z]")

#: Words that are code-shaped in prose but far too common to anchor anything. Matching on
#: one of these would turn "the doc names what it cites" into "the doc used English".
#:
#: The parameter names matter as much as the English. ``AI_USAGE.md`` cites
#: ``json.dumps(obj, ...)``, and a citation that had drifted onto an unrelated
#: ``redact(obj, key=key)`` scored as anchored -- on ``obj``. A token that appears in half
#: the functions in the package is not evidence that the document named this one.
_STOPWORDS = frozenset(
    {
        "and",
        "args",
        "cls",
        "conn",
        "data",
        "func",
        "item",
        "kwargs",
        "obj",
        "res",
        "ret",
        "val",
        "any",
        "are",
        "call",
        "class",
        "def",
        "dict",
        "else",
        "false",
        "for",
        "from",
        "get",
        "has",
        "import",
        "int",
        "into",
        "its",
        "key",
        "list",
        "new",
        "none",
        "not",
        "old",
        "one",
        "only",
        "raise",
        "return",
        "row",
        "run",
        "self",
        "set",
        "str",
        "text",
        "the",
        "true",
        "try",
        "two",
        "type",
        "value",
        "with",
    }
)


class Citation:
    """One ``path:line`` reference, with the prose that is supposed to explain it."""

    def __init__(
        self,
        document: str,
        doc_line: int,
        path: str,
        start: int,
        end: int,
        context: str,
        line_text: str,
        column: int,
        raw: str,
    ):
        self.document = document
        self.doc_line = doc_line
        self.path = path
        self.start = start
        self.end = end
        self.context = context
        #: The document line this citation sits on, verbatim. Used to decide WHICH named
        #: symbol a citation belongs to when the prose names several: the nearest one.
        self.line_text = line_text
        #: Character offset of the citation inside ``line_text``.
        self.column = column
        #: The citation exactly as written, so the updater can substitute it in place.
        self.raw = raw

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.document, self.path, self.start)

    @property
    def span(self) -> str:
        return f"{self.path}:{self.start}" + (f"-{self.end}" if self.end != self.start else "")

    def __repr__(self) -> str:  # pragma: no cover - pytest ids only
        return f"{self.document}:{self.doc_line} -> {self.span}"


def _read(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def resolve(cited_path: str) -> Path | None:
    """The real file a cited path names, or ``None`` when nothing matches."""
    for prefix in SEARCH_PREFIXES:
        candidate = REPO_ROOT / prefix / cited_path if prefix else REPO_ROOT / cited_path
        if candidate.is_file():
            return candidate
    return None


def collect(document: str) -> list[Citation]:
    """Every citation in ``document``, each carrying its line and both neighbours."""
    lines = _read(REPO_ROOT / document)
    found: list[Citation] = []
    for index, line in enumerate(lines):
        for match in CITATION_RE.finditer(line):
            start = int(match.group("start"))
            end = int(match.group("end") or start)
            context = "\n".join(lines[max(0, index - 1) : index + 2])
            found.append(
                Citation(
                    document=document,
                    doc_line=index + 1,
                    path=match.group("path"),
                    start=start,
                    end=end,
                    context=context,
                    line_text=line,
                    column=match.start(),
                    raw=match.group(0),
                )
            )
    return found


ALL_CITATIONS: list[Citation] = [c for document in DOCUMENTS for c in collect(document)]


# ===========================================================================
# what a file DEFINES, and where it merely mentions
# ===========================================================================
@dataclass(frozen=True)
class FileFacts:
    """Where a file defines each symbol, and which of its lines are mentions only.

    ``definitions`` is what the ``anchored`` tier binds to -- it is a property of the
    file, not of any line number, which is exactly why line drift cannot break it.

    ``mention_only`` is what the third tier binds to: an ``__all__`` entry, an import, a
    comment or a docstring line. Those lines *contain* the symbol and are the reason
    "does the name appear in the span" was never a real check.
    """

    definitions: dict[str, tuple[int, ...]]
    mention_only: frozenset[int]


_SQL_DEFINITION_RE = re.compile(
    r"(?i)\bcreate\s+(?:or\s+replace\s+)?"
    r"(?:table|index|unique\s+index|view|trigger|function|type|schema|role)\s+"
    r"(?:if\s+not\s+exists\s+)?[\"']?(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
)
_SQL_CONSTRAINT_RE = re.compile(r"(?i)\bconstraint\s+[\"']?(?P<name>[A-Za-z_][A-Za-z0-9_]*)")
_YAML_KEY_RE = re.compile(r"^\s*(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)\s*:")
_TS_DEFINITION_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?"
    r"(?:const|let|var|function|class|interface|type|enum)\s+(?P<name>[A-Za-z_$][\w$]*)"
)
_COMMENT_PREFIXES: dict[str, str] = {".yaml": "#", ".yml": "#", ".sql": "--"}


def _python_facts(lines: list[str]) -> FileFacts | None:
    """Definitions and mention-only lines read off the parsed AST. ``None`` if unparsable."""
    try:
        tree = ast.parse("\n".join(lines))
    except SyntaxError:  # pragma: no cover - every cited .py in this repo parses
        return None

    definitions: defaultdict[str, list[int]] = defaultdict(list)
    mention: set[int] = set()

    def _span(node: ast.AST) -> range:
        start = getattr(node, "lineno", 0)
        return range(start, (getattr(node, "end_lineno", None) or start) + 1)

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            definitions[node.name].append(node.lineno)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            mention.update(_span(node))
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            # A bare string expression: a module/class/function docstring, or one of the
            # long prose blocks this repo writes between statements.
            if isinstance(node.value.value, str):
                mention.update(_span(node))
        elif (
            # ``sa.CheckConstraint(..., name="ck_budget_spent_within_cap")`` -- for a
            # database object the naming site IS the definition.
            isinstance(node, ast.keyword)
            and node.arg == "name"
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            definitions[node.value.value].append(node.value.lineno)

    # MODULE-level bindings only. Class bodies and function bodies are deliberately
    # excluded: a dataclass field called ``run_id`` or ``fingerprint`` is not "where
    # run_id is defined" in any sense a document could mean, and treating it as one made
    # ubiquitous field names outrank the function the prose was actually naming.
    for statement in tree.body:
        targets: list[ast.expr] = []
        if isinstance(statement, ast.Assign):
            targets = list(statement.targets)
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            definitions[target.id].append(statement.lineno)
            if target.id == "__all__":
                # The export list names every public symbol and defines none of them.
                mention.update(_span(statement))

    mention.update(n for n, line in enumerate(lines, 1) if line.lstrip().startswith("#"))
    return FileFacts(
        definitions={name: tuple(sorted(set(at))) for name, at in definitions.items()},
        mention_only=frozenset(mention),
    )


def _text_facts(suffix: str, lines: list[str]) -> FileFacts:
    """Definitions for the non-Python files the documents cite: YAML, SQL, TS, plain text.

    A ``.txt``/``.md``/``.json`` file defines nothing -- citations into those are the
    measurement ones, and they anchor on the number they quote.
    """
    definitions: defaultdict[str, list[int]] = defaultdict(list)
    mention: set[int] = set()
    comment = _COMMENT_PREFIXES.get(suffix)
    for number, line in enumerate(lines, 1):
        if comment and line.lstrip().startswith(comment):
            mention.add(number)
            continue
        if suffix in {".yaml", ".yml"}:
            found = _YAML_KEY_RE.match(line)
            if found:
                definitions[found.group("name")].append(number)
        elif suffix == ".sql":
            for pattern in (_SQL_DEFINITION_RE, _SQL_CONSTRAINT_RE):
                for match in pattern.finditer(line):
                    definitions[match.group("name")].append(number)
        elif suffix in {".ts", ".tsx"}:
            found = _TS_DEFINITION_RE.match(line)
            if found:
                definitions[found.group("name")].append(number)
    return FileFacts(
        definitions={name: tuple(sorted(set(at))) for name, at in definitions.items()},
        mention_only=frozenset(mention),
    )


@cache
def facts_for(path: Path) -> FileFacts:
    lines = _read(path)
    if path.suffix == ".py":
        parsed = _python_facts(lines)
        if parsed is not None:
            return parsed
    return _text_facts(path.suffix.lower(), lines)


# ===========================================================================
# what the prose names
# ===========================================================================
def _identifier_like(token: str) -> bool:
    return "_" in token or bool(_CAMEL_RE.search(token))


def _usable(token: str) -> bool:
    """A token strong enough to be evidence that the document named THIS symbol.

    ``snake_case``/``CamelCase`` earns the shorter floor because the shape itself is
    distinctive; a bare lowercase word has to be at least four characters.
    """
    if token.lower() in _STOPWORDS:
        return False
    return len(token) >= 3 if _identifier_like(token) else len(token) >= 4


def _normalise_number(raw: str) -> str:
    return raw.replace(",", "")


def _blank(match: re.Match[str]) -> str:
    """Blank a match out **without moving anything after it** -- offsets stay valid."""
    return " " * len(match.group(0))


def _mask_paths(text: str) -> str:
    """Text with every file path and bare ``:NNN`` reference blanked, same length."""
    return _BARE_LINE_REF_RE.sub(_blank, _PATHISH_RE.sub(_blank, text))


def symbol_candidates(context: str) -> set[str]:
    """The code names the prose actually uses near a citation."""
    text = _mask_paths(context)
    candidates: set[str] = set()
    for span in _BACKTICK_RE.findall(text):
        candidates.update(_TOKEN_RE.findall(span))
    for dotted in _DOTTED_RE.findall(text):
        candidates.add(dotted.rsplit(".", 1)[1])
    candidates.update(_CALL_RE.findall(text))
    candidates.update(token for token in _TOKEN_RE.findall(text) if _identifier_like(token))
    return {token for token in candidates if _usable(token)}


def number_candidates(context: str) -> set[str]:
    """Measurements the prose quotes. Three digits or more, commas normalised away."""
    numbers = {_normalise_number(raw) for raw in _NUMBER_RE.findall(_mask_paths(context))}
    return {number for number in numbers if sum(ch.isdigit() for ch in number) >= 3}


def prose_candidates(context: str) -> set[str]:
    """The last-resort anchor: distinctive words, for a citation that names no code."""
    words = {word.lower() for word in re.findall(r"[A-Za-z]{5,}", _mask_paths(context))}
    return {word for word in words if word not in _STOPWORDS}


def defined_candidates(citation: Citation, facts: FileFacts) -> list[str]:
    """Symbols the prose names near this citation that the cited file actually defines."""
    named = symbol_candidates(citation.context)
    return sorted(name for name in named if facts.definitions.get(name))


def anchor_symbol(
    citation: Citation, facts: FileFacts, source: list[str] | None = None
) -> str | None:
    """The ONE symbol this citation belongs to, chosen deterministically.

    Ranked, best tier first, because proximity alone is not enough -- these documents wrap
    at 100 columns, and a citation pushed onto the next line is *nearer* the symbol that
    follows it than the one it belongs to:

    0. the cited line IS this symbol's definition. Unambiguous, and true of every citation
       that has not drifted;
    1. the cited span mentions this symbol -- a deliberate call-site citation;
    2. nearest occurrence on the citation's own document line;
    3. named only on a neighbouring line.

    Alphabetical order breaks every remaining tie, so the choice never depends on dict or
    set iteration order.
    """
    defined = defined_candidates(citation, facts)
    if not defined:
        return None
    span = [] if source is None else source[citation.start - 1 : min(citation.end, len(source))]
    masked = _mask_paths(citation.line_text)
    ranked: list[tuple[int, int, str]] = []
    for name in defined:
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        offsets = [m.start() for m in pattern.finditer(masked)]
        distance = min((abs(at - citation.column) for at in offsets), default=10**6)
        if citation.start in facts.definitions[name]:
            tier = 0
        elif any(pattern.search(line) for line in span):
            tier = 1
        elif offsets:
            tier = 2
        else:
            tier = 3
        ranked.append((tier, distance, name))
    return min(ranked)[2]


def _span_body(citation: Citation, source: list[str]) -> str:
    return "\n".join(source[citation.start - 1 : citation.end])


def quoted_numbers_in_span(citation: Citation, source: list[str]) -> list[str]:
    """Measurements the prose quotes that the cited span actually carries."""
    body = _span_body(citation, source)
    body_numbers = {_normalise_number(raw) for raw in _NUMBER_RE.findall(body)}
    return sorted(number_candidates(citation.context) & body_numbers)


def anchors(citation: Citation, facts: FileFacts, source: list[str]) -> tuple[bool, str]:
    """``(matched, why)`` for the ``anchored`` tier. No line number is consulted."""
    named = symbol_candidates(citation.context)
    defined = defined_candidates(citation, facts)
    if defined:
        chosen = anchor_symbol(citation, facts, source) or defined[0]
        return True, f"the cited file defines {chosen!r} at {list(facts.definitions[chosen])}"

    shared = quoted_numbers_in_span(citation, source)
    if shared:
        return True, f"quotes {shared[0]}"

    if named:
        return False, (
            f"the document names {sorted(named)} near this citation, and the cited file "
            f"DEFINES none of it -- renamed, deleted, or the wrong file"
        )
    numbers = number_candidates(citation.context)
    if numbers:
        return False, (
            f"the document quotes {sorted(numbers)} near this citation, and none of it is "
            f"on {citation.span}"
        )

    body = _span_body(citation, source).lower()
    prose_hit = sorted(word for word in prose_candidates(citation.context) if word in body)
    if prose_hit:
        return True, f"shares the word {prose_hit[0]!r}"
    return False, "the document names nothing that appears on the cited line"


def mention_only_failure(
    citation: Citation, facts: FileFacts, source: list[str]
) -> tuple[str, int, str] | None:
    """``(symbol, line, why)`` when the cited span only MENTIONS what the prose names.

    Returns ``None`` -- i.e. passes -- in two cases, and both are deliberate:

    * the span mentions no named symbol at all. That is line drift, and the ``anchored``
      tier is what binds this citation; failing here would re-create the fragile gate.
    * the span carries a measurement the prose quotes. Those citations point *into* a
      docstring on purpose, and a docstring is a mention by construction.
    """
    if quoted_numbers_in_span(citation, source):
        return None
    defined = defined_candidates(citation, facts)
    if not defined:
        return None

    occurrences: list[tuple[str, int]] = []
    for number in range(citation.start, min(citation.end, len(source)) + 1):
        line = source[number - 1]
        occurrences.extend(
            (name, number) for name in defined if re.search(rf"\b{re.escape(name)}\b", line)
        )
    if not occurrences:
        return None
    if any(number not in facts.mention_only for _, number in occurrences):
        return None

    name, number = occurrences[0]
    return (
        name,
        number,
        f"line {number} only MENTIONS {name!r} -- it is an export-list entry, an import, "
        f"or a comment/docstring line, not a definition. {name!r} is defined at "
        f"{list(facts.definitions[name])}",
    )


def _did_you_mean(citation: Citation, facts: FileFacts) -> str:
    """Where the named symbols actually live, so a red run is one edit from green."""
    named = sorted(symbol_candidates(citation.context))
    hits = [
        f"{name} -> {list(facts.definitions[name])}"
        for name in named
        if facts.definitions.get(name)
    ]
    body = "; ".join(hits[:4]) if hits else "(this file defines none of them)"
    return f"  defined here: {body}"


# ===========================================================================
# the tiers
# ===========================================================================
@pytest.mark.parametrize("citation", ALL_CITATIONS, ids=repr)
def test_a_citation_points_at_a_line_that_exists_and_is_not_blank(citation: Citation) -> None:
    resolved = resolve(citation.path)
    assert resolved is not None, (
        f"{citation.document}:{citation.doc_line} cites {citation.span}, and no file called "
        f"{citation.path!r} exists under {sorted(SEARCH_PREFIXES)}"
    )
    source = _read(resolved)
    assert citation.end >= citation.start, f"{citation.span} is a backwards range"
    assert citation.end <= len(source), (
        f"{citation.document}:{citation.doc_line} cites {citation.span}, but "
        f"{resolved.relative_to(REPO_ROOT)} has only {len(source)} lines"
    )
    body = source[citation.start - 1 : citation.end]
    assert any(line.strip() for line in body), (
        f"{citation.document}:{citation.doc_line} cites {citation.span}, which is BLANK in "
        f"{resolved.relative_to(REPO_ROOT)}. A blank line documents nothing."
    )


@pytest.mark.parametrize("citation", ALL_CITATIONS, ids=repr)
def test_the_cited_file_still_defines_what_the_document_names(citation: Citation) -> None:
    """The durable half. A rename or a deletion fails; moving a function does not."""
    resolved = resolve(citation.path)
    assert resolved is not None, f"unresolvable path {citation.path!r}"
    source = _read(resolved)
    assert citation.end <= len(source), f"{citation.span} is past the end of the file"
    facts = facts_for(resolved)
    matched, why = anchors(citation, facts, source)
    assert matched, (
        f"{citation.document}:{citation.doc_line} cites {citation.span} but {why}.\n"
        f"  cited line: {source[citation.start - 1].strip()[:100]!r}\n"
        f"{_did_you_mean(citation, facts)}"
    )


@pytest.mark.parametrize("citation", ALL_CITATIONS, ids=repr)
def test_a_citation_points_at_a_definition_not_a_mention(citation: Citation) -> None:
    """An ``__all__`` entry, an import, or a comment naming the symbol is not a citation."""
    resolved = resolve(citation.path)
    assert resolved is not None, f"unresolvable path {citation.path!r}"
    source = _read(resolved)
    assert citation.end <= len(source), f"{citation.span} is past the end of the file"
    facts = facts_for(resolved)
    failure = mention_only_failure(citation, facts, source)
    assert failure is None, (
        f"{citation.document}:{citation.doc_line} cites {citation.span}, and "
        f"{failure[2] if failure else ''}.\n"
        f"  cited line: {source[citation.start - 1].strip()[:100]!r}\n"
        f"  re-point the citation, or run: uv run python -m tests.docs.test_doc_citations --write"
    )


def test_neither_document_loses_its_citations() -> None:
    """A red citation gets repaired, never deleted along with the sentence around it."""
    counted = {document: 0 for document in DOCUMENTS}
    for citation in ALL_CITATIONS:
        counted[citation.document] += 1
    short = {
        document: (counted[document], floor)
        for document, floor in MINIMUM_CITATIONS.items()
        if counted[document] < floor
    }
    assert not short, (
        f"{short} -- (found, floor). These documents cite the implementation by path:line "
        f"and that is graded. Dropping a citation is not how a citation gets fixed; "
        f"re-point it, or lower MINIMUM_CITATIONS deliberately and say why."
    )


#: A file this repository will always have, used to exercise the machinery against real
#: source rather than a fixture. It carries all three shapes at once: a real ``def``, the
#: same name inside ``__all__``, and a local ``import`` of a name from another module.
_SELF_TEST_FILE = "apply.py"
_SELF_TEST_SYMBOL = "assert_sources_are_unwritable"


def _self_test_lines() -> tuple[Path, list[str], FileFacts, int, int]:
    """``(path, source, facts, definition line, export-list line)`` -- all derived, never pinned."""
    resolved = resolve(_SELF_TEST_FILE)
    assert resolved is not None, f"{_SELF_TEST_FILE} no longer resolves"
    source = _read(resolved)
    facts = facts_for(resolved)
    defined_at = facts.definitions.get(_SELF_TEST_SYMBOL)
    assert defined_at, f"{_SELF_TEST_FILE} no longer defines {_SELF_TEST_SYMBOL!r}"
    exported_at = [
        number
        for number in sorted(facts.mention_only)
        if re.search(rf"\b{_SELF_TEST_SYMBOL}\b", source[number - 1])
    ]
    assert exported_at, (
        f"{_SELF_TEST_FILE} no longer MENTIONS {_SELF_TEST_SYMBOL!r} on an export/import/"
        f"comment line, so this self-test can no longer prove a mention is rejected"
    )
    return resolved, source, facts, defined_at[0], exported_at[0]


def _synthetic(path: str, line: int, symbol: str) -> Citation:
    """A citation written exactly the way these documents write one."""
    text = f"`{symbol}()` (`{path}:{line}`) is the thing."
    match = CITATION_RE.search(text)
    assert match is not None
    return Citation(
        document="<synthetic>",
        doc_line=1,
        path=path,
        start=line,
        end=line,
        context=text,
        line_text=text,
        column=match.start(),
        raw=match.group(0),
    )


def test_a_citation_onto_an_export_list_entry_is_rejected_and_the_definition_is_not() -> None:
    """The exact probe that went green before: ``apply.py:1885`` moved onto ``__all__``.

    Both directions, so this cannot pass by rejecting everything: the definition line is
    accepted, the export-list mention of the same name on the same file is refused.
    """
    _, source, facts, defined_at, exported_at = _self_test_lines()

    good = _synthetic(_SELF_TEST_FILE, defined_at, _SELF_TEST_SYMBOL)
    assert mention_only_failure(good, facts, source) is None, (
        f"{_SELF_TEST_FILE}:{defined_at} IS the definition of {_SELF_TEST_SYMBOL!r} and "
        f"must be accepted: {source[defined_at - 1].strip()!r}"
    )

    bad = _synthetic(_SELF_TEST_FILE, exported_at, _SELF_TEST_SYMBOL)
    failure = mention_only_failure(bad, facts, source)
    assert failure is not None, (
        f"{_SELF_TEST_FILE}:{exported_at} is {source[exported_at - 1].strip()!r} -- a MENTION "
        f"of {_SELF_TEST_SYMBOL!r}, not its definition, and the suite accepted it. That is "
        f"the defect this tier exists to close."
    )
    assert failure[0] == _SELF_TEST_SYMBOL and failure[1] == exported_at


def test_the_repair_entry_point_produces_a_derivable_repair() -> None:
    """Every line the updater would write must actually carry the symbol it was chosen for.

    This is the half that cannot rot: it re-derives the repair from the tree on every run,
    so a stale committed number is reported with its fix rather than sitting unnoticed. It
    does **not** assert there is no drift -- that assertion is the fragile gate this suite
    was rebuilt to remove.
    """
    for citation, symbol, target in stale_citations():
        resolved = resolve(citation.path)
        assert resolved is not None
        source = _read(resolved)
        assert re.search(rf"\b{re.escape(symbol)}\b", source[target - 1]), (
            f"the updater would re-point {citation.document}:{citation.doc_line} at "
            f"{citation.path}:{target} for {symbol!r}, and that line does not carry it: "
            f"{source[target - 1].strip()!r}"
        )
        print(
            f"line drift (not a failure): {citation.document}:{citation.doc_line} "
            f"{citation.span} -> {citation.path}:{target} for {symbol!r}"
        )


def test_the_updater_leaves_a_citation_that_still_sits_on_its_symbol_alone() -> None:
    """Drift is repaired; a citation still pointing at its symbol is never moved.

    Without this, a resync would quietly re-point every deliberate call-site citation at
    the definition it calls -- silently changing what the sentence around it claims.
    """
    _, source, facts, defined_at, _exported_at = _self_test_lines()

    pattern = re.compile(rf"\b{_SELF_TEST_SYMBOL}\b")

    on_target = _synthetic(_SELF_TEST_FILE, defined_at, _SELF_TEST_SYMBOL)
    assert anchor_symbol(on_target, facts, source) == _SELF_TEST_SYMBOL
    assert drift_of(on_target) is None, (
        f"the updater would move {_SELF_TEST_FILE}:{defined_at}, which IS the definition "
        f"of {_SELF_TEST_SYMBOL!r}"
    )

    # A call-site style citation: the line mentions the symbol without defining it.
    call_site = next(
        (
            number
            for number, line in enumerate(source, 1)
            if pattern.search(line) and number != defined_at and number not in facts.mention_only
        ),
        None,
    )
    if call_site is not None:
        assert drift_of(_synthetic(_SELF_TEST_FILE, call_site, _SELF_TEST_SYMBOL)) is None, (
            f"the updater would re-point a deliberate call-site citation "
            f"({_SELF_TEST_FILE}:{call_site}) at the definition it calls"
        )

    adrift = next(
        number for number, line in enumerate(source, 1) if line.strip() and not pattern.search(line)
    )
    off_target = _synthetic(_SELF_TEST_FILE, adrift, _SELF_TEST_SYMBOL)
    assert drift_of(off_target) == (_SELF_TEST_SYMBOL, defined_at), (
        f"{_SELF_TEST_FILE}:{adrift} carries no {_SELF_TEST_SYMBOL!r} and the updater did "
        f"not offer to re-point it at {defined_at}"
    )


#: The nine beats the brief requires of the reconcile-cycle sequence diagram, each with
#: the marker that proves it is drawn rather than described in the prose underneath.
#: Three of these (sync, invariant check, conflict detected) were missing outright.
REQUIRED_BEATS: tuple[tuple[str, str], ...] = (
    ("scheduled trigger with a per-job secret", r"X-Trigger-Secret"),
    ("sync", r"POST /internal/sync"),
    ("invariant check", r"run_invariant_stage"),
    ("conflict detected", r"persist_run"),
    ("reconciler proposes with confidence and evidence", r"derivation packet"),
    ("spend-cap check", r"reserve worst case"),
    ("write pending", r"INSERT proposals"),
    ("reviewer approves", r"/approve"),
    ("audit entry", r"INSERT audit_log"),
)


def _sequence_diagram() -> str:
    text = (REPO_ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```mermaid\n(.*?)```", text, flags=re.DOTALL)
    sequences = [block for block in blocks if block.lstrip().startswith("sequenceDiagram")]
    assert len(sequences) == 1, f"expected exactly one sequenceDiagram, found {len(sequences)}"
    return sequences[0]


@pytest.mark.parametrize(("beat", "marker"), REQUIRED_BEATS, ids=[b for b, _ in REQUIRED_BEATS])
def test_the_reconcile_sequence_diagram_carries_all_nine_beats(beat: str, marker: str) -> None:
    assert re.search(marker, _sequence_diagram()), (
        f"the sequence diagram in ARCHITECTURE.md is missing the {beat!r} beat "
        f"(no {marker!r} in the diagram body). The brief names all nine; a beat argued "
        f"for in the prose underneath is not a beat that was drawn."
    )


def test_every_participant_the_sequence_diagram_uses_is_declared() -> None:
    """A typo'd participant renders as a silent extra lifeline, not as an error."""
    diagram = _sequence_diagram()
    declared = set(re.findall(r"^\s*participant\s+(\w+)", diagram, flags=re.MULTILINE))
    used: set[str] = set()
    for left, right in re.findall(r"^\s*(\w+)\s*--?>>\s*(\w+)\s*:", diagram, flags=re.MULTILINE):
        used.update({left, right})
    for note in re.findall(r"^\s*Note (?:over|right of|left of)\s+([\w, ]+):", diagram, re.M):
        used.update(part.strip() for part in note.split(",") if part.strip())
    assert used <= declared, (
        f"undeclared participants in the sequence diagram: {sorted(used - declared)}"
    )


# ===========================================================================
# the repair entry point -- `python -m tests.docs.test_doc_citations --write`
# ===========================================================================
def drift_of(citation: Citation) -> tuple[str, int] | None:
    """``(anchor symbol, corrected line)`` when a citation no longer sits on its symbol.

    ``None`` means "leave it alone", and it covers two cases on purpose: a measurement
    citation, which names no symbol and is held by the number it quotes; and a citation
    whose span still mentions its anchor -- the definition it names, or a deliberate
    call-site reference. Moving the second kind would silently change what the sentence
    around it claims.
    """
    resolved = resolve(citation.path)
    if resolved is None:
        return None
    source = _read(resolved)
    if citation.start > len(source):
        return None
    facts = facts_for(resolved)
    if quoted_numbers_in_span(citation, source):
        # A measurement citation: held by the number it quotes, which the ``anchored``
        # tier checks on every run. There is no symbol to re-derive a line from.
        return None
    symbol = anchor_symbol(citation, facts, source)
    if symbol is None:
        return None
    pattern = re.compile(rf"\b{re.escape(symbol)}\b")
    span = source[citation.start - 1 : min(citation.end, len(source))]
    if any(pattern.search(line) for line in span):
        return None
    return symbol, facts.definitions[symbol][0]


def stale_citations() -> list[tuple[Citation, str, int]]:
    """``(citation, anchor symbol, corrected line)`` for every citation that has drifted.

    A citation is left alone when its span still mentions its anchor symbol: that covers
    the definition it names *and* a deliberate call-site citation, and it is what stops a
    resync from quietly re-pointing ``reconciler.py:1627`` (which calls ``_rationale``) at
    ``reconciler.py:1753`` (which defines it).
    """
    drifted: list[tuple[Citation, str, int]] = []
    for citation in ALL_CITATIONS:
        moved = drift_of(citation)
        if moved is not None:
            drifted.append((citation, moved[0], moved[1]))
    return drifted


def _resync(write: bool) -> int:
    """Rewrite (or report) the committed line numbers from the current working tree."""
    drifted = stale_citations()
    if not drifted:
        print("every citation still sits on its anchor symbol; nothing to resync")
        return 0

    by_document: defaultdict[str, list[tuple[Citation, str, int]]] = defaultdict(list)
    for entry in drifted:
        by_document[entry[0].document].append(entry)

    for document, entries in sorted(by_document.items()):
        path = REPO_ROOT / document
        lines = path.read_text(encoding="utf-8").split("\n")
        # Right-to-left, bottom-to-top: every substitution changes the length of the line
        # it lands on, and rewriting in this order keeps the recorded columns valid.
        for citation, symbol, target in sorted(
            entries, key=lambda e: (e[0].doc_line, e[0].column), reverse=True
        ):
            end = target + (citation.end - citation.start)
            replacement = f"{citation.path}:{target}" + (f"-{end}" if end != target else "")
            index = citation.doc_line - 1
            line = lines[index]
            assert line[citation.column : citation.column + len(citation.raw)] == citation.raw, (
                f"{document}:{citation.doc_line} moved under the updater; re-run it"
            )
            lines[index] = (
                line[: citation.column] + replacement + line[citation.column + len(citation.raw) :]
            )
            print(
                f"{'rewrote' if write else 'stale  '} {document}:{citation.doc_line} "
                f"{citation.raw} -> {replacement}   ({symbol})"
            )
        if write:
            path.write_text("\n".join(lines), encoding="utf-8")
    return 0 if write else 1


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - operator entry point
    parser = argparse.ArgumentParser(
        prog="python -m tests.docs.test_doc_citations",
        description=(
            "Re-derive the path:line numbers in ARCHITECTURE.md and AI_USAGE.md from the "
            "current working tree. The suite enforces the SYMBOL half of every citation on "
            "every run; this is how the integer half is made exact on demand."
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="rewrite the documents in place")
    group.add_argument("--check", action="store_true", help="report drift, exit 1, change nothing")
    args = parser.parse_args(argv)
    return _resync(write=args.write)


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
