"""Every assumption in `dashboard/src/lib/contract.ts`, answered one by one.

`CONTRACT_ASSUMPTIONS` is exported from the client *as data* precisely so a
service ticket can be held to it: "a prose list in a comment cannot be asserted
on, and this one is." So this module parses that list out of the TypeScript
source -- ids, subjects and failure modes -- and requires each id to have a
verdict here.

Two directions, and both matter:

* an assumption the service HONOURS has a test that exercises it live;
* an assumption the service CANNOT honour is named in `NOT_ANSWERED` with the
  reason and the consequence for the reviewer. That list is the report the
  ticket owes, and this module fails if it is silently empty of an id that no
  test covers -- so an unanswered assumption cannot pass as an answered one.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from recon.api.review import CONFLICT_STATUS_NOTE
from tests.api.conftest import ADMIN_HEADERS

CONTRACT_TS = Path(__file__).resolve().parents[3] / "dashboard" / "src" / "lib" / "contract.ts"

#: Assumptions this service does NOT fully honour, with the reason. Empty entries
#: are not allowed: every id here must carry a sentence a reviewer can act on.
#: Nothing is deferred any more. A4 was the last entry and it is answered below:
#: `recon/api/scorecard.py` was built and mounted after this list was written, so
#: leaving A4 here would have been a claim that a control is MISSING when it
#: exists -- the inverse of the phantom control, and just as misleading to anyone
#: deciding what still needs building.
NOT_ANSWERED: dict[str, str] = {}

#: Assumptions answered by a test in this repository, and where.
ANSWERED_BY: dict[str, str] = {
    "A1": "tests/api/test_review_api.py::test_the_pagination_envelope_is_the_assumed_one",
    "A2": "tests/api/test_review_api.py::test_a2_per_id_gets_exist_and_agree_with_the_list",
    "A3": "tests/api/test_filters.py::test_a3_the_conflict_id_filter_is_applied",
    "A4": "tests/suite/test_scorecard_endpoint.py::test_it_serves_the_artifact_the_suite_wrote",
    "A5": "tests/api/test_decisions.py::test_approve_records_a_named_reviewer",
    "A6": "tests/api/test_contract_assumptions.py::test_a6_the_conflict_status_vocabulary",
    "A7": "tests/api/test_review_api.py::test_a_conflict_row_carries_every_pinned_column",
    "A8": "tests/api/test_filters.py::test_a8_the_proposals_type_filter_is_applied",
    "A9": "tests/api/test_review_api.py::test_a9_evidence_carries_observed_values",
    "A10": "tests/api/test_review_api.py::test_a10_action_target_path_is_readable_from_the_action",
}


def _declared_assumptions() -> dict[str, str]:
    """`{id: failure mode}` parsed out of the client's exported list."""
    source = CONTRACT_TS.read_text()
    block = source.split("export const CONTRACT_ASSUMPTIONS", 1)[1]
    ids = re.findall(r"id:\s*'(A\d+)'", block)
    failures = re.findall(r"failure:\s*'(loud|silent)'", block)
    assert len(ids) == len(failures), "contract.ts: every assumption must name a failure mode"
    assert ids, "no assumptions parsed from contract.ts -- this module would prove nothing"
    return dict(zip(ids, failures, strict=True))


def test_every_declared_assumption_has_a_verdict() -> None:
    """No assumption may be neither answered nor reported. That gap is the defect."""
    declared = _declared_assumptions()
    covered = set(ANSWERED_BY) | set(NOT_ANSWERED)
    missing = sorted(set(declared) - covered)
    assert not missing, (
        f"contract.ts declares {missing} and this ticket neither answers them nor reports "
        "why not. An assumption with no verdict is exactly how a silent failure ships."
    )
    stray = sorted(covered - set(declared))
    assert not stray, f"{stray} are claimed here and not declared in contract.ts"
    assert all(reason.strip() for reason in NOT_ANSWERED.values())


def test_the_silent_assumptions_are_the_ones_we_answered_live() -> None:
    """A3 and A8 -- the two the client cannot verify -- must not be on the excuse list."""
    declared = _declared_assumptions()
    silent = {key for key, mode in declared.items() if mode == "silent"}
    assert silent == {"A3", "A8"}
    assert not (silent & set(NOT_ANSWERED)), (
        f"{sorted(silent & set(NOT_ANSWERED))} fails silently on the reviewer's screen and "
        "is unanswered. A silent failure may not be deferred."
    )


def test_a6_the_conflict_status_vocabulary(review_api: TestClient, reader: Any) -> None:
    """A6, answered with what is actually in the column -- and with what is not.

    The client assumes `open` and `escalated:oscillation` and renders anything
    else as a labelled badge. The service serves the composite when the row
    carries an `escalation_reason` or the `oscillating` flag, and bare
    `escalated` otherwise -- which is the usual case, because `recon_writer` has
    no grant on `escalation_reason` (§8 of `docs/proposal-policy.md`).

    **This store has no escalated conflict at all**, and the test says so out
    loud rather than passing quietly: `tests/apply/store.py` inherits a
    generation-3-only `field_lineage` from `tests/er/dataset`, contract §7's
    A -> B -> A window therefore has nothing to scan, and nothing oscillates. The
    escalation half of A6 is exercised over three generations by
    `tests/reconciler`, which builds its own database for exactly that reason.
    """
    with reader.connect() as conn:
        escalated = conn.execute(
            text("SELECT count(*) FROM conflicts WHERE status = 'escalated'")
        ).scalar_one()
        total = conn.execute(text("SELECT count(*) FROM conflicts")).scalar_one()
    assert total > 0

    served = {}
    for status in ("open", "escalated", "escalated:oscillation"):
        body = review_api.get(
            "/api/conflicts", params={"status": status, "page_size": 5}, headers=ADMIN_HEADERS
        ).json()
        served[status] = body["total"]
        if body["total"]:
            assert {row["status"] for row in body["items"]} == {status}

    assert served["open"] == total - escalated, (
        f"the endpoint serves {served['open']} open conflicts and the table holds "
        f"{total - escalated}; the status filter is not the column"
    )
    assert served["escalated"] + served["escalated:oscillation"] == escalated, (
        f"{escalated} escalated conflicts are in the table and "
        f"{served['escalated'] + served['escalated:oscillation']} are servable under either "
        "spelling -- an escalated row that no status filter can reach is invisible to a "
        "reviewer working from the status column"
    )
    assert "escalation_reason" in CONFLICT_STATUS_NOTE


def test_the_client_and_the_service_agree_on_the_sensitive_field_list() -> None:
    """`contract.ts` copies contract SS6's list; a drift would mislabel the UI."""
    from recon.reference import AUTO_APPLY_ELIGIBLE, SENSITIVE_FIELDS

    source = CONTRACT_TS.read_text()

    def parsed(name: str) -> set[str]:
        block = source.split(f"export const {name}: ReadonlySet<string> = new Set([", 1)[1]
        return set(re.findall(r"'([^']+)'", block.split("])", 1)[0]))

    assert parsed("SENSITIVE_FIELDS") == set(SENSITIVE_FIELDS)
    assert parsed("AUTO_APPLY_ELIGIBLE") == set(AUTO_APPLY_ELIGIBLE)


@pytest.mark.parametrize("assumption", sorted(ANSWERED_BY))
def test_each_answered_assumption_names_a_real_test(assumption: str) -> None:
    """The pointer in `ANSWERED_BY` must resolve, or this file is a promise."""
    path, _, name = ANSWERED_BY[assumption].partition("::")
    module = Path(__file__).resolve().parents[2] / path
    assert module.is_file(), f"{assumption} points at {path}, which does not exist"
    assert f"def {name}(" in module.read_text(), f"{assumption} points at a missing test {name}"
