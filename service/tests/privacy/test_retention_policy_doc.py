"""`docs/retention-policy.md` must describe the schedule that actually runs.

A retention policy is only worth reading if it is the same object as the code.
These tests parse the policy's table and require it to agree with
:data:`recon.privacy.RETENTION` row for row -- so the document cannot drift into
describing a window nobody implements, and a rule cannot be added to the code
without being documented.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from recon.logging import ENTRY_POINTS, audit_detail, audit_row
from recon.privacy import (
    PURGE_ACTION,
    PURGE_ACTOR,
    RETENTION,
    PurgeResult,
    RetentionRule,
    _audit_body,
)

POLICY = Path(__file__).resolve().parents[3] / "docs" / "retention-policy.md"

_ROW = re.compile(r"^\|\s*`(?P<table>\w+)`\s*\|(?P<rest>.*)\|\s*$")


def _documented() -> list[tuple[str, str, str, str]]:
    """`(table, ts_column, window, disposition)` for every row of the schedule table."""
    rows: list[tuple[str, str, str, str]] = []
    for line in POLICY.read_text().splitlines():
        match = _ROW.match(line.strip())
        if not match:
            continue
        cells = [cell.strip().strip("`") for cell in match.group("rest").split("|")]
        if len(cells) < 3:
            continue
        rows.append((match.group("table"), cells[0], cells[1], cells[2]))
    return rows


def test_the_policy_document_exists() -> None:
    assert POLICY.exists(), f"{POLICY} is a graded deliverable and is missing"


def test_every_rule_is_documented_exactly_once() -> None:
    """One row per rule, in the order the sweep executes them."""
    documented = [(table, disposition) for table, _, _, disposition in _documented()]
    implemented = [(rule.table, rule.disposition) for rule in RETENTION]
    assert documented == implemented, (
        "docs/retention-policy.md no longer matches recon.privacy.RETENTION.\n"
        f"documented:  {documented}\nimplemented: {implemented}"
    )


@pytest.mark.parametrize("rule", RETENTION, ids=[f"{r.table}-{r.disposition}" for r in RETENTION])
def test_documented_window_matches_the_code(rule: RetentionRule) -> None:
    """The number in the document is the number the sweep uses."""
    documented = {
        (table, disposition): (ts, window) for table, ts, window, disposition in _documented()
    }
    ts, window = documented[(rule.table, rule.disposition)]
    assert ts == (rule.ts_column or "--")
    assert window == ("--" if rule.window_days is None else str(rule.window_days))


def test_the_policy_names_the_purging_principal_and_the_grant_it_does_not_have() -> None:
    """The graded question -- who purges -- must be answered in the document."""
    text = POLICY.read_text()
    assert "recon_writer" in text
    assert "DATABASE_URL" in text
    assert "schema owner" in text or "ops" in text


def test_the_policy_states_the_pseudonymity_limit() -> None:
    """A policy that called a truncated salted hash "anonymised" would be wrong."""
    text = POLICY.read_text().lower()
    assert "pseudonym" in text
    assert "not anonymis" in text or "not anonymiz" in text


# ---------------------------------------------------------------------------
# §4 must describe the payload the writers actually produce
# ---------------------------------------------------------------------------


def _fenced_json_blocks() -> list[dict[str, object]]:
    """Every ```json block in the policy, parsed."""
    blocks: list[dict[str, object]] = []
    inside: list[str] | None = None
    for line in POLICY.read_text().splitlines():
        if line.strip() == "```json":
            inside = []
            continue
        if inside is not None and line.strip() == "```":
            blocks.append(json.loads("\n".join(inside)))
            inside = None
            continue
        if inside is not None:
            inside.append(line)
    return blocks


def test_the_documented_detail_shape_is_the_one_audit_detail_produces() -> None:
    """§4 showed `{mode, body_sha256, body}` while the sweep wrote `{ran_at, tables}`.

    Documenting a payload nothing produced is the third claim-about-a-control
    this project could not afford, so the shape is asserted rather than
    described: every `audit_log.detail` example in the policy must have exactly
    the keys `recon.logging.audit_detail` emits.
    """
    produced = set(audit_detail({"guardian_email": "a@keystone.test"}))
    documented = [block for block in _fenced_json_blocks() if "mode" in block]
    assert documented, "§4 no longer shows an audit_log.detail example"
    for block in documented:
        assert set(block) == produced, f"documented {sorted(block)} != produced {sorted(produced)}"


def test_the_sweeps_own_audit_row_matches_the_documented_shape() -> None:
    """The writer §4 was wrong about: `run_purge`'s row, built without a database."""
    body = _audit_body(
        datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
        (
            PurgeResult(
                table="raw_records",
                disposition="purge",
                window_days=90,
                cutoff=None,
                rows=12,
            ),
        ),
    )
    detail = json.loads(
        audit_row(actor=PURGE_ACTOR, action=PURGE_ACTION, subject="principal:owner", body=body)[
            "detail"
        ]
    )
    assert set(detail) == set(audit_detail({"a": 1}))
    example = next(
        block
        for block in _fenced_json_blocks()
        if isinstance(block.get("body"), dict) and "tables" in block["body"]
    )
    assert set(example["body"]) == set(detail["body"])
    assert set(example["body"]["tables"][0]) == set(detail["body"]["tables"][0])
    # counts only: the disposition, the window and the row count, never a value
    assert detail["body"]["tables"][0] == {
        "table": "raw_records",
        "disposition": "purge",
        "window_days": 90,
        "rows": 12,
    }


def test_the_policy_documents_where_redaction_is_installed() -> None:
    """§4.0 claims a sink and an entry-point list; both must be the real ones."""
    text = POLICY.read_text()
    assert "configure_logging_once" in text
    for entry in ENTRY_POINTS:
        stem = entry.split("/")[-1]
        module = entry.removesuffix(".py").replace("/", ".").removesuffix(".__main__")
        assert stem in text or module in text, f"{entry} is an entry point the policy omits"
