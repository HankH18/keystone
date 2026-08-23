"""A rejection names the line it actually came from (R2).

R2 requires a structured rejection *and a log entry*, and the only field in that
entry an operator can act on is the line number: "line 18,204 of
`payments/gen3/payment.jsonl`" is a fix, "line 18,203" is a wild goose chase
through a 40,000-line snapshot.

The adapter used to drop empty lines before enumerating (`[line for line in ...
if line]`), which broke that in two ways at once:

* the blank line itself was neither landed, nor rejected, nor logged -- a silent
  skip, the exact failure R2 names;
* every rejection *after* it reported a line number one too low, and two blanks
  put it two low. The number was not merely wrong, it was wrong by an amount that
  depended on data upstream of the record being reported.

**The decision, stated: a blank interior line is REJECTED, not ignored.** It is not
a record and cannot be landed; `json.loads("")` fails, so it earns the ordinary
`unparseable_json` 400 at its true line number, is counted in
`records_rejected` and is logged like any other structural rejection. The one
thing that is not a line is the empty string after the file's final newline --
every well-formed JSONL file ends with one, and treating it as a record would
reject every snapshot in the tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from recon.adapters import AdapterError, JsonlSnapshotAdapter
from recon.ingest import ingest_source

GENERATION = 906


def _contact(index: int) -> str:
    return (
        f'{{"crm_id":"CRM-990{index:04d}","email":"line{index}@example.test",'
        '"first_name":"Ada","last_name":"Byron","lifecycle_stage":"lead",'
        '"created_at":"2026-02-01T00:00:00Z","updated_at":"2026-02-02T00:00:00Z",'
        '"external_id":null,"dob":"2012-05-04","grade":"4","state":"TX",'
        '"marketing_consent":true}'
    )


def _tree(tmp_path: Path, body: str) -> JsonlSnapshotAdapter:
    path = tmp_path / "crm" / f"gen{GENERATION}" / "contact.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return JsonlSnapshotAdapter(tmp_path, source_id="crm", entity_types=("contact",))


def _rejections(adapter: JsonlSnapshotAdapter) -> tuple[list, list]:
    collected: list[AdapterError] = []
    adapter.on_reject = collected.append
    records = list(adapter.read(GENERATION))
    return records, collected


def test_a_blank_interior_line_is_rejected_and_does_not_shift_later_lines(
    tmp_path: Path,
) -> None:
    """The blank is line 3; the broken record after it is line 5, not line 4."""
    body = "\n".join([_contact(1), _contact(2), "", _contact(4), '{"crm_id": ', _contact(6)]) + "\n"
    records, rejections = _rejections(_tree(tmp_path, body))

    assert len(records) == 4, "the four well-formed records must still be read"
    assert [rejection.line_no for rejection in rejections] == [3, 5]
    blank, broken = rejections
    assert blank.kind == "unparseable_json" and blank.status == 400
    assert broken.kind == "unparseable_json" and broken.line_no == 5, (
        "the record after a blank line must report its own physical line, or the "
        "structured 4xx points an operator at the wrong line"
    )


def test_two_blank_lines_shift_nothing_either(tmp_path: Path) -> None:
    body = "\n".join(["", "", _contact(3), '{"crm_id": ', _contact(5)]) + "\n"
    _, rejections = _rejections(_tree(tmp_path, body))
    assert [rejection.line_no for rejection in rejections] == [1, 2, 4]


def test_a_whitespace_only_line_is_rejected_too(tmp_path: Path) -> None:
    body = "\n".join([_contact(1), "   ", _contact(3)]) + "\n"
    records, rejections = _rejections(_tree(tmp_path, body))
    assert len(records) == 2
    assert [rejection.line_no for rejection in rejections] == [2]


def test_the_final_newline_is_a_terminator_not_a_blank_line(tmp_path: Path) -> None:
    """The negative control: every well-formed snapshot ends with one."""
    body = "\n".join([_contact(1), _contact(2), _contact(3)]) + "\n"
    records, rejections = _rejections(_tree(tmp_path, body))
    assert len(records) == 3
    assert rejections == [], "a trailing newline must not manufacture a rejection"


def test_a_file_without_a_trailing_newline_keeps_its_last_record(tmp_path: Path) -> None:
    body = "\n".join([_contact(1), _contact(2)])
    records, rejections = _rejections(_tree(tmp_path, body))
    assert len(records) == 2
    assert rejections == []


def test_an_empty_file_is_zero_records_and_zero_rejections(tmp_path: Path) -> None:
    records, rejections = _rejections(_tree(tmp_path, ""))
    assert records == [] and rejections == []


@pytest.mark.parametrize("body_ends_with_newline", [True, False])
def test_the_blank_line_is_counted_by_the_pipeline(
    tmp_path: Path, body_ends_with_newline: bool
) -> None:
    """Counted, not merely rejected: the accounting invariant sees it as a line."""
    lines = [_contact(1), "", _contact(3)]
    body = "\n".join(lines) + ("\n" if body_ends_with_newline else "")
    adapter = _tree(tmp_path, body)

    result = ingest_source(
        adapter,
        GENERATION,
        run_id="blank-line",
        persist=False,
        stall_timeout=2.0,
        deadline_seconds=10.0,
    )

    (load,) = result.loads
    load.check()
    assert load.read == 3, "the blank line is one of the file's three physical lines"
    assert load.loaded == 2
    assert load.rejected == 1
    assert result.status == "partial"
    assert result.complete is False
