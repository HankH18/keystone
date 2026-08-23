"""There are FOUR sinks, and this module is what keeps that number honest.

``recon/logging.py``'s docstring and ``docs/retention-policy.md`` §4.0 both said
"exactly two ways a log line leaves this service". Four existed:

(a) a structlog event -- covered;
(b) an ``audit_log`` row -- covered for one of its three writers;
(c) a direct terminal write -- ``print`` in three entry points, and a raw
    ``traceback.print_exc()`` for every exception escaping a suite check;
(d) a stdlib ``logging`` record -- uvicorn's access log, which writes every
    request path and query string to the same terminal.

Counting them in prose is what let the count be wrong. So the count lives in
:data:`recon.logging.SINKS`, the exceptions live in
:data:`recon.logging.AUDIT_WRITERS` and
:data:`recon.logging.UNROUTED_TERMINAL_WRITERS`, and the tests below walk the
**source** and require the enumerations to match what is really there. A fifth
sink -- or a fourth ``audit_log`` writer, or a new ``print`` -- fails here on the
commit that introduces it, which is the only time it is cheap to notice.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from recon.logging import (
    AUDIT_INSERT_SQL,
    AUDIT_WRITERS,
    SINKS,
    UNROUTED_TERMINAL_WRITERS,
)

SERVICE_ROOT = Path(__file__).resolve().parents[2]
POLICY = SERVICE_ROOT.parent / "docs" / "retention-policy.md"

#: The module that IS the chokepoint. It holds the only sanctioned ``print``
#: (inside ``console``) and the only stdlib-logging wiring.
CHOKEPOINT = "recon/logging.py"

#: Attribute calls that dump text straight to a terminal.
_DUMPERS = frozenset({"print_exc", "print_exception", "print_stack", "print_last"})


def _recon_sources() -> list[Path]:
    return sorted(p for p in (SERVICE_ROOT / "recon").rglob("*.py"))


def _relative(path: Path) -> str:
    return str(path.relative_to(SERVICE_ROOT))


def _terminal_writes() -> dict[str, list[str]]:
    """``module -> ["module:line", ...]`` for every direct terminal write in recon/.

    Four spellings, because all four have been used in this package or are one
    edit away: ``print(...)``, ``traceback.print_exc(...)`` and friends,
    ``sys.stdout.write(...)`` / ``sys.stderr.write(...)``, and
    ``logging.basicConfig(...)`` (which installs a handler of its own).
    """
    found: dict[str, list[str]] = {}
    for path in _recon_sources():
        relative = _relative(path)
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            hit = False
            if isinstance(func, ast.Name) and func.id == "print":
                hit = True
            elif isinstance(func, ast.Attribute):
                if func.attr in _DUMPERS or func.attr == "basicConfig":
                    hit = True
                elif func.attr == "write":
                    owner = func.value
                    hit = (
                        isinstance(owner, ast.Attribute)
                        and owner.attr in {"stdout", "stderr"}
                        and isinstance(owner.value, ast.Name)
                        and owner.value.id == "sys"
                    )
            if hit:
                found.setdefault(relative, []).append(f"{relative}:{node.lineno}")
    return found


def _audit_insert_modules() -> dict[str, list[str]]:
    """``module -> ["module:line", ...]`` for every ``INSERT INTO audit_log``."""
    pattern = re.compile(r"INSERT\s+INTO\s+audit_log", re.IGNORECASE)
    found: dict[str, list[str]] = {}
    for path in _recon_sources():
        relative = _relative(path)
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if pattern.search(line):
                found.setdefault(relative, []).append(f"{relative}:{number}")
    return found


# ---------------------------------------------------------------------------
# the count itself
# ---------------------------------------------------------------------------


def test_the_enumeration_names_every_sink_the_service_has() -> None:
    """Four, named, each with a chokepoint. Not a number in a sentence."""
    names = [sink.name for sink in SINKS]
    assert names == [
        "structlog event",
        "audit_log row",
        "direct terminal write",
        "stdlib logging record",
    ], names
    for sink in SINKS:
        assert sink.chokepoint.strip(), f"{sink.name} claims no chokepoint"
        assert sink.covers.strip(), f"{sink.name} says nothing about what it covers"


def test_the_module_docstring_no_longer_claims_two_sinks() -> None:
    """The sentence that was wrong, asserted gone rather than assumed fixed."""
    source = (SERVICE_ROOT / CHOKEPOINT).read_text()
    docstring = ast.get_docstring(ast.parse(source)) or ""
    assert "exactly two ways" not in docstring
    assert "FOUR sinks" in docstring


def test_the_policy_document_says_four_too() -> None:
    """§4.0 made the same claim, so it has to be corrected in the same commit."""
    text = POLICY.read_text()
    assert "exactly two ways" not in text, "docs/retention-policy.md §4.0 still says two"
    for sink in SINKS:
        assert sink.name in text, f"the policy does not list the {sink.name!r} sink"


# ---------------------------------------------------------------------------
# sink (c): direct terminal writes
# ---------------------------------------------------------------------------


def test_every_terminal_writer_is_the_chokepoint_or_a_declared_exception() -> None:
    """Enumerated the way ENTRY_POINTS is: a new `print` fails on its own commit.

    The chokepoint (``recon.logging.console``) plus the modules declared in
    ``UNROUTED_TERMINAL_WRITERS`` is the complete permitted set. A module that
    grows a ``print`` -- or a ``traceback.print_exc``, which is how a whole
    record used to reach the terminal from ``recon/suite/__main__.py`` -- is a
    new sink, and shows up here as an undeclared one.
    """
    declared = {writer.module for writer in UNROUTED_TERMINAL_WRITERS} | {CHOKEPOINT}
    found = _terminal_writes()
    undeclared = {module: sites for module, sites in found.items() if module not in declared}
    assert not undeclared, (
        "these modules write to the terminal without going through "
        f"recon.logging.console, and are not declared in "
        f"recon.logging.UNROUTED_TERMINAL_WRITERS: {undeclared}"
    )


def test_no_declared_exception_has_quietly_been_fixed_or_deleted() -> None:
    """The declaration must not outlive the thing it declares.

    Otherwise the list becomes a piece of stale prose of exactly the kind this
    module exists to prevent -- and a reader would think a sink is open that is
    not.
    """
    found = _terminal_writes()
    stale = [w.module for w in UNROUTED_TERMINAL_WRITERS if w.module not in found]
    assert not stale, (
        "these modules are declared as unrouted terminal writers but no longer "
        f"write to the terminal; delete the declaration: {stale}"
    )
    for writer in UNROUTED_TERMINAL_WRITERS:
        assert "Required change:" in writer.note or "required change:" in writer.note, writer.module


def test_the_suite_runner_no_longer_dumps_a_raw_traceback() -> None:
    """The specific site: `python -m recon.suite` printed one per failing check.

    Asserted against the parsed source, not against the text -- ``run_check``'s
    docstring names the call it replaced, and a substring search would read that
    sentence as the defect it describes.
    """
    module = "recon/suite/__main__.py"
    assert module not in _terminal_writes(), (
        f"{module} still writes to the terminal outside recon.logging.console"
    )
    assert "console(" in (SERVICE_ROOT / module).read_text(), (
        "the scorecard is no longer written through the chokepoint"
    )


# ---------------------------------------------------------------------------
# sink (b): audit_log rows
# ---------------------------------------------------------------------------


def test_every_audit_log_writer_is_enumerated() -> None:
    """`audit_row` has ONE caller; the enumeration has to name the others.

    ``docs/retention-policy.md`` claimed the chokepoint covered "every bound
    field" of an ``audit_log`` row while two of the three writers bound
    ``actor``, ``action`` and ``subject`` raw. The claim is now data
    (:data:`recon.logging.AUDIT_WRITERS`) and this compares it with the source.
    """
    found = set(_audit_insert_modules())
    declared = {writer.module for writer in AUDIT_WRITERS}
    assert found == declared, (
        f"audit_log INSERT sites in the source: {sorted(found)}; "
        f"declared in recon.logging.AUDIT_WRITERS: {sorted(declared)}"
    )


@pytest.mark.parametrize("writer", AUDIT_WRITERS, ids=[w.module for w in AUDIT_WRITERS])
def test_a_writer_marked_routed_really_uses_the_chokepoint(writer: object) -> None:
    """`routed=True` is a claim about the source, so it is read off the source."""
    module = writer.module  # type: ignore[attr-defined]
    source = (SERVICE_ROOT / module).read_text()
    uses = "audit_row(" in source or "insert_audit_row(" in source
    if writer.routed:  # type: ignore[attr-defined]
        assert uses, f"{module} is marked routed but never calls audit_row/insert_audit_row"
    else:
        assert "Required change:" in writer.note, (  # type: ignore[attr-defined]
            f"{module} is an unrouted audit writer and must carry the exact fix"
        )
        # ...and the declaration cannot outlive the gap: the moment the writer
        # is routed, this fails until AUDIT_WRITERS is updated to say so. A
        # known-gap list that stays put after the gap closes is the same stale
        # prose that made the docs wrong in the first place.
        assert not uses, (
            f"{module} now routes through the chokepoint; flip it to routed=True "
            "in recon.logging.AUDIT_WRITERS and update docs/retention-policy.md §4.0"
        )


def test_the_chokepoint_binds_every_column_the_insert_names() -> None:
    """`insert_audit_row` and `AUDIT_INSERT_SQL` cannot drift apart.

    A column added to the SQL and not to `audit_row` would be a bound value
    nothing redacted -- which is the whole defect, one column smaller.
    """
    from recon.logging import audit_row

    bound = set(re.findall(r":(\w+)", AUDIT_INSERT_SQL))
    assert bound == set(audit_row(actor="system:x", action="a", subject="s", body={"n": 1}))


def test_the_policy_does_not_claim_the_audit_chokepoint_is_universal() -> None:
    """An overclaim in the graded doc is worse than the gap it hides."""
    text = POLICY.read_text()
    for writer in AUDIT_WRITERS:
        if not writer.routed:
            assert writer.module in text, (
                f"{writer.module} writes audit_log rows outside the chokepoint and the "
                "policy does not say so"
            )
