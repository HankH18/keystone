"""`validate_payload` is TOTAL, and one hostile line costs exactly that line (R2, R3).

Three defects share one root and are fixed together here.

**A not-a-ValueError escapes.** `validate_payload` wrapped `json.loads` in
`except ValueError`, and a deeply nested document raises `RecursionError` -- a
`RuntimeError`. About 20 KB of brackets, well inside `MAX_PAYLOAD_BYTES`, walked
out of the function and reached the client as a bare HTTP 500. That is the second
non-`ValueError` to escape this function, so the fix is not a third `except`
clause: the function is now total by construction (any input maps to a `RawRecord`
or an `AdapterError` with a 4xx) and a deterministic, *iteratively checked* depth
bound means the recursive passes that follow can no longer be reached with a
document deep enough to blow the stack.

**A guard on the syntax is not a guard on the value.** `parse_constant` fires only
for the literal `NaN` / `Infinity` / `-Infinity` TOKENS. `1e400` contains no such
token, is well-formed JSON, and overflows to `inf` while being parsed -- so it was
accepted, `canonical_json` re-emitted a bare `Infinity`, and `jsonb` refused it at
the landing COPY: a 503 for a payload problem. Non-finiteness is now judged on the
parsed *value*, over the whole document, so it is caught on undeclared fields
riding in on `extra="allow"`, inside arrays and inside nested objects -- not only
on the one field (`deal.amount`) whose model happened to check it.

**A per-record fault must not become a per-source failure.** `read_bounded` turns
any non-`AdapterError` escaping a read into an R3 *source* error, so one hostile
line made a whole generation report `status=failed`, `records_read=0`,
`records_rejected=0` -- discarding 39,999 good records and contradicting this
module's own promise that "one malformed line in a 40,000-line snapshot costs
exactly that line". Fixing the trigger is not the same as fixing the structure, so
the isolation is tested against a real snapshot with a deliberately hostile line
in it, and asserted on the landing table rather than on a return value.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from recon.adapters import (
    MAX_JSON_DEPTH,
    MAX_PAYLOAD_BYTES,
    AdapterError,
    JsonlSnapshotAdapter,
    RawRecord,
    canonical_json,
    validate_batch,
    validate_payload,
)
from recon.adapters.validation import json_depth, non_finite_number, scan_document
from recon.app import create_app
from recon.ingest import expected_counts_from_manifest, ingest_source
from tests.ingest.conftest import TRIGGER_HEADERS
from tests.ingest.test_storable_types import BASE, _raw

GENERATION = 973


@pytest.fixture
def api(owner_engine, trigger_secret) -> TestClient:
    with TestClient(create_app()) as client:
        yield client


def _post(client: TestClient, source: str, entity_type: str, raws: list[str], run_id: str):
    return client.post(
        "/internal/ingest/records",
        json={
            "source": source,
            "entity_type": entity_type,
            "generation": GENERATION,
            "records": raws,
            "run_id": run_id,
        },
        headers=TRIGGER_HEADERS,
    )


def _nested(depth: int) -> str:
    """A CRM contact carrying `depth` levels of nesting on an undeclared field.

    Assembled as **text**, never through `json.dumps`: the encoder recurses too, so
    building the hostile payload with it would blow the test's own stack before the
    validator ever saw the string.
    """
    body = dict(BASE[("crm", "contact")])
    body["crm_id"] = f"CRM-973-deep{depth}"
    prefix = json.dumps(body)[:-1]
    return f'{prefix},"extra":{"[" * (depth - 1)}1{"]" * (depth - 1)}}}'


# ===========================================================================
# BLOCKER 1 -- deep nesting is a 4xx, never an escape
# ===========================================================================
def test_the_depth_check_is_iterative_and_agrees_with_the_shape_it_measures() -> None:
    """`json_depth` is the check that stops the stack blowing; it may not recurse."""
    assert json_depth(1) == 0
    assert json_depth({}) == 1
    assert json_depth([[]]) == 2
    assert json_depth({"a": {"b": [1, 2]}}) == 3
    # 200_000 levels: a recursive implementation of this function dies here, which
    # is the point -- the guard cannot be the thing that needs guarding.
    deep: Any = 0
    for _ in range(200_000):
        deep = [deep]
    assert json_depth(deep) == 200_000


#: Documents that exercise every branch of the fused scan: containers and leaves,
#: floats finite and not, the root itself being each kind, and the empties.
SCAN_CORPUS: list[Any] = [
    1,
    "s",
    None,
    True,
    1.5,
    float("inf"),
    float("-inf"),
    float("nan"),
    {},
    [],
    [[]],
    {"a": {}},
    {"a": 1.5, "b": "x", "c": None, "d": True, "e": 3},
    {"a": [1, 2, [3, [4]]]},
    {"a": {"b": {"c": [float("inf")]}}},
    {"a": [{"b": float("nan")}]},
    [1e308, -1e308, 0.0, -0.0],
    {"deep": [[[[[[[[[[1]]]]]]]]]]},
    {"tuple-ish": (1, 2.5, float("inf"))},
    {str(i): i * 1.0 for i in range(50)},
]


@pytest.mark.parametrize("document", SCAN_CORPUS, ids=lambda d: repr(d)[:40])
def test_the_fused_scan_agrees_with_the_two_checks_it_replaced(document: Any) -> None:
    """One traversal answers both questions, or it may not replace two that did.

    `scan_document` exists only because two full walks per record cost 43% of the
    validation path's throughput. A faster check that answers differently is not an
    optimisation, so its answers are pinned to the functions it stands in for --
    `json_depth`, and `non_finite_number` on an unbounded depth.
    """
    depth, has_non_finite = scan_document(document)
    assert depth == json_depth(document)
    assert has_non_finite == (non_finite_number(document, max_depth=10**6) is not None)


def test_the_fused_scan_is_iterative() -> None:
    """Same rule as `json_depth`: the guard may not be what blows the stack."""
    deep: Any = 0
    for _ in range(200_000):
        deep = [deep]
    assert scan_document(deep) == (200_000, False)
    deep = float("inf")
    for _ in range(200_000):
        deep = {"a": deep}
    assert scan_document(deep) == (200_000, True)


@pytest.mark.parametrize("depth", [5_000, 9_997, 20_000, 60_000])
def test_a_deeply_nested_payload_is_a_400_not_a_recursion_error(depth: int) -> None:
    """The reported blocker, at and well past the ~9,997-level threshold."""
    raw = _nested(depth)
    assert len(raw.encode()) <= MAX_PAYLOAD_BYTES, (
        "the payload must be reachable by an ordinary request for this to matter"
    )
    with pytest.raises(AdapterError) as excinfo:
        validate_payload("crm", "contact", GENERATION, raw, line_no=1)
    assert excinfo.value.kind == "excessive_nesting"
    assert excinfo.value.status == 400


def test_a_deeply_nested_payload_over_http_is_a_400(api: TestClient) -> None:
    response = _post(api, "crm", "contact", [_nested(20_000)], run_id="hostile-deep")
    assert response.status_code < 500, (
        f"a nested payload produced {response.status_code}; R2 forbids a 500 on bad input"
    )
    assert response.status_code == 400
    assert response.json()["type"].endswith("excessive_nesting")


def test_the_depth_limit_is_a_constant_not_a_stack_measurement() -> None:
    """The verdict on a payload must be a property of the payload.

    `RecursionError` fires at a depth that depends on how much stack the caller had
    already used, so the same document could be accepted on the request thread and
    refused on the adapter's reader thread. A constant bound removes that.
    """
    assert MAX_JSON_DEPTH == 32
    accepted = validate_payload("crm", "contact", GENERATION, _nested(MAX_JSON_DEPTH), line_no=1)
    assert isinstance(accepted, RawRecord), "a document at the limit is accepted"
    with pytest.raises(AdapterError) as excinfo:
        validate_payload("crm", "contact", GENERATION, _nested(MAX_JSON_DEPTH + 1), line_no=1)
    assert excinfo.value.kind == "excessive_nesting"


def test_an_accepted_payload_can_be_canonically_encoded() -> None:
    """The reason the bound exists: every later pass over the payload recurses."""
    record = validate_payload("crm", "contact", GENERATION, _nested(MAX_JSON_DEPTH), line_no=1)
    assert json.loads(canonical_json(record.payload)) == record.payload


# ===========================================================================
# BLOCKER 2 -- a value that is not finite, wherever it can arise
# ===========================================================================
NON_FINITE_LITERALS = ("1e400", "-1e400", "1E999", "-1E999", "1e309", "2e308", "1e1000")


@pytest.mark.parametrize("literal", NON_FINITE_LITERALS)
def test_an_overflowing_literal_really_does_parse_to_infinity(literal: str) -> None:
    """The premise, asserted rather than assumed: these are valid JSON."""
    value = json.loads(literal)
    assert isinstance(value, float) and not math.isfinite(value)


@pytest.mark.parametrize("literal", NON_FINITE_LITERALS)
def test_an_overflowing_literal_on_an_undeclared_field_is_rejected(literal: str) -> None:
    """`extra="allow"` means a non-finite number can ride in on a field no model names."""
    raw = _raw("crm", "contact", "inf1")[:-1] + f', "score": {literal}}}'
    with pytest.raises(AdapterError) as excinfo:
        validate_payload("crm", "contact", GENERATION, raw, line_no=1)
    assert excinfo.value.kind == "non_finite_number"
    assert excinfo.value.status == 422


#: One case per place a number can sit: a declared field, an undeclared one, an
#: array member, a nested object, a nested object's undeclared field, and a key
#: whose value is a list of lists.
NON_FINITE_PLACEMENTS: list[tuple[str, str, str, str]] = [
    ("declared-deal-amount", "crm", "deal", _raw("crm", "deal", "p1").replace("5000.0", "1e400")),
    (
        "declared-amount-cents-as-float",
        "payments",
        "payment",
        _raw("payments", "payment", "p2").replace('"amount_cents": 50000', '"amount_cents": 1e400'),
    ),
    (
        "undeclared-top-level",
        "crm",
        "contact",
        _raw("crm", "contact", "p3")[:-1] + ', "fee": 1e400}',
    ),
    (
        "inside-an-array",
        "crm",
        "contact",
        _raw("crm", "contact", "p4")[:-1] + ', "history": [1, 2, -1e400]}',
    ),
    (
        "inside-a-nested-object",
        "payments",
        "payment",
        _raw("payments", "payment", "p5", metadata={"student_first_name": "Ada", "fee": 1e400})
        .replace('"fee": Infinity', '"fee": 1e400')
        .replace('"fee": inf', '"fee": 1e400'),
    ),
    (
        "inside-a-nested-array-of-objects",
        "crm",
        "contact",
        _raw("crm", "contact", "p6")[:-1] + ', "rows": [{"a": [{"b": 1e400}]}]}',
    ),
    (
        "as-an-undeclared-nan-by-overflow-negative",
        "appdb",
        "student",
        _raw("appdb", "student", "p7")[:-1] + ', "gpa": -1e400}',
    ),
]


@pytest.mark.parametrize(
    ("case_id", "source", "entity_type", "raw"),
    NON_FINITE_PLACEMENTS,
    ids=[case[0] for case in NON_FINITE_PLACEMENTS],
)
def test_a_non_finite_value_is_rejected_wherever_it_sits(
    api: TestClient, case_id: str, source: str, entity_type: str, raw: str
) -> None:
    """Over HTTP as well as directly: never a 5xx, always a structured 4xx."""
    with pytest.raises(AdapterError) as excinfo:
        validate_payload(source, entity_type, GENERATION, raw, line_no=1)
    assert excinfo.value.status < 500

    response = _post(api, source, entity_type, [raw], run_id=f"hostile-{case_id}")
    assert response.status_code < 500, (
        f"{case_id} produced {response.status_code}: a number the store cannot hold "
        "is the payload's problem. It reached the landing COPY as a bare Infinity "
        "and came back as a 503."
    )
    assert 400 <= response.status_code < 500
    assert response.json()["accepted"] == 0


def test_nothing_a_validator_accepts_re_encodes_as_a_bare_infinity() -> None:
    """The property behind the rule, over every placement above and the finite twins.

    `canonical_json` output must be re-readable by a strict JSON parser -- which is
    what `jsonb` is. `Infinity` / `NaN` in that string is precisely the 503.
    """
    for _case_id, source, entity_type, raw in NON_FINITE_PLACEMENTS:
        with pytest.raises(AdapterError):
            validate_payload(source, entity_type, GENERATION, raw, line_no=1)

    finite = _raw("crm", "contact", "finite")[:-1] + ', "fee": 1e308, "history": [1.5, -2.25]}'
    record = validate_payload("crm", "contact", GENERATION, finite, line_no=1)
    json.loads(record.payload_json, parse_constant=_refuse)
    assert "Infinity" not in record.payload_json and "NaN" not in record.payload_json


def _refuse(literal: str) -> Any:
    raise AssertionError(f"canonical_json emitted a bare {literal}, which jsonb rejects")


def test_the_non_finite_walk_finds_the_path_and_not_merely_the_fact() -> None:
    """An operator has to be told *where*, or the rejection is not actionable."""
    assert non_finite_number({"a": {"b": [1, float("inf")]}}) == "a.b[1] (inf)"
    assert non_finite_number({"ok": 1.5, "n": float("nan")}) == "n (nan)"
    assert non_finite_number({"ok": 1.5, "deep": [[[1e308]]]}) is None


def test_a_finite_extreme_is_still_accepted() -> None:
    """The negative control: this rejects non-finite values, not large ones."""
    raw = _raw("crm", "contact", "big")[:-1] + ', "fee": 1.7976931348623157e308}'
    record = validate_payload("crm", "contact", GENERATION, raw, line_no=1)
    assert record.payload["fee"] == 1.7976931348623157e308


# ===========================================================================
# BLOCKER 1, generalised -- the function is TOTAL
# ===========================================================================
#: Payload shapes chosen to raise something that is *not* a `ValueError` on some
#: path of the validator: the parser, `str()` of a huge int, the model, the
#: canonical encoder, the storability walks.
TOTALITY_CORPUS: list[str] = [
    "",
    " ",
    "\t\n",
    "null",
    "true",
    "[]",
    "{}",
    "[1,2,3]",
    '"a string"',
    "0",
    "{",
    '{"crm_id":',
    '{"crm_id":"A",}',
    "[" * 30_000 + "]" * 30_000,
    "{" + '"a":{' * 20_000,
    '{"crm_id":' + "9" * 6_000 + "}",
    '{"crm_id":"A","x":' + "9" * 6_000 + "}",
    '{"crm_id":"A","x":1e400}',
    '{"crm_id":"A","x":NaN}',
    '{"crm_id":"A","x":Infinity}',
    '{"crm_id":"\\u0000"}',
    '{"\\u0000":"A"}',
    '{"crm_id":"\\ud800"}',
    '{"crm_id":"A","crm_id":"B"}',
    '{"crm_id":null}',
    '{"crm_id":[]}',
    '{"crm_id":{}}',
    '{"crm_id":1.5}',
    "\ud800",
    "x" * 300_000,
    '{"crm_id":"' + "x" * 300_000 + '"}',
]


@pytest.mark.parametrize(
    ("source", "entity_type"),
    sorted(BASE),
    ids=lambda pair: "/".join(pair) if isinstance(pair, tuple) else str(pair),
)
def test_validate_payload_is_total_over_the_hostile_corpus(source: str, entity_type: str) -> None:
    """Two outcomes and no third, for every source and every hostile shape.

    Enumerating "which exception should I also catch" one clause at a time is what
    let the RecursionError through. This asserts the property instead: nothing but
    an `AdapterError` carrying a 4xx ever leaves this function.
    """
    for raw in TOTALITY_CORPUS:
        try:
            result = validate_payload(source, entity_type, GENERATION, raw, line_no=1)
        except AdapterError as error:
            assert 400 <= error.status < 500, f"{raw[:40]!r} -> {error.status}"
            assert error.kind
            # the rejection must itself be storable: it is written to jsonb
            json.dumps(error.problem())
            continue
        except BaseException as exc:  # the escape IS the defect under test
            pytest.fail(
                f"{type(exc).__name__} escaped validate_payload for "
                f"{source}/{entity_type} on {raw[:60]!r}: an escape here is a 500 "
                "on the HTTP path and an R3 source failure on the file path"
            )
        assert isinstance(result, RawRecord)


def test_the_backstop_kind_exists_and_is_a_4xx() -> None:
    """The catch-all is a *documented* rejection, not an undifferentiated 500."""
    from recon.adapters import KIND_STATUS

    assert KIND_STATUS["unprocessable_payload"] == 422
    assert KIND_STATUS["excessive_nesting"] == 400
    assert KIND_STATUS["non_finite_number"] == 422


def test_the_backstop_does_not_swallow_an_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    """`Exception`, not `BaseException`: Ctrl-C is not a verdict about a payload."""
    import recon.adapters.validation as validation_module

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(validation_module, "_validate_payload", boom)
    with pytest.raises(KeyboardInterrupt):
        validation_module.validate_payload("crm", "contact", GENERATION, "{}", line_no=1)


def test_an_arbitrary_internal_fault_becomes_a_rejection_not_an_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backstop, exercised: an exception no clause names is still a 4xx."""
    import recon.adapters.validation as validation_module

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise MemoryError("out of memory while judging a payload")

    monkeypatch.setattr(validation_module, "_validate_payload", boom)
    with pytest.raises(AdapterError) as excinfo:
        validation_module.validate_payload("crm", "contact", GENERATION, "{}", line_no=1)
    assert excinfo.value.kind == "unprocessable_payload"
    assert excinfo.value.status == 422


# ===========================================================================
# MAJOR 5 -- one bad line costs exactly one line
# ===========================================================================
def _snapshot(tmp_path: Path, good: int, hostile: str, at: int) -> Path:
    """A snapshot tree with `good` valid contacts and one hostile line at `at`."""
    lines = []
    for index in range(good):
        body = dict(BASE[("crm", "contact")])
        body["crm_id"] = f"CRM-973{index:05d}"
        body["email"] = f"g{index}@example.test"
        lines.append(json.dumps(body))
    lines.insert(at, hostile)

    root = tmp_path / "fixtures"
    directory = root / "crm" / f"gen{GENERATION}"
    directory.mkdir(parents=True)
    (directory / "contact.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (directory / "deal.jsonl").write_text("", encoding="utf-8")
    return root


@pytest.mark.parametrize(
    ("case", "hostile"),
    [
        ("deep-nesting", "[" * 20_000 + "]" * 20_000),
        ("nested-object-payload", _nested(20_000)),
        ("non-finite-number", '{"crm_id":"CRM-973HOT","x":1e400}'),
        ("truncated", '{"crm_id":"CRM-973HOT"'),
        ("blank", ""),
    ],
)
def test_one_hostile_line_in_a_large_snapshot_costs_exactly_that_line(
    owner_engine, tmp_path: Path, case: str, hostile: str
) -> None:
    """The module docstring's promise, on a real read of a real file.

    Before the fix the `deep-nesting` row failed the WHOLE generation:
    `status=failed`, `records_read=0`, `records_rejected=0`, 4,999 good records
    discarded -- because a non-`AdapterError` escaping `validate_payload` is caught
    by `read_bounded` and reclassified as an R3 *source* failure.
    """
    good = 5_000
    root = _snapshot(tmp_path, good, hostile, at=good // 2)
    adapter = JsonlSnapshotAdapter(
        root, source_id="crm", entity_types=("contact",), on_reject=lambda _e: None
    )
    run_id = f"hostile-line-{case}"

    result = ingest_source(adapter, GENERATION, run_id=run_id)

    assert result.status == "partial", (
        f"{case}: a single bad line reported status={result.status!r} for the whole "
        f"source (error={result.error and result.error.kind})"
    )
    assert result.records_read == good + 1
    assert result.records_rejected == 1
    assert result.records_ok == good
    assert result.records_read == result.records_ok + result.records_rejected
    assert result.error is None, (
        f"{case}: a per-record fault was reported as a per-source failure "
        f"({result.error and result.error.kind})"
    )

    with owner_engine.connect() as conn:
        landed = conn.execute(
            text("SELECT count(*) FROM raw_records WHERE run_id = :r"), {"r": run_id}
        ).scalar()
    assert landed == good, f"{case}: {landed} of {good} good records survived the bad one"


def test_the_rejection_names_the_physical_line_it_came_from(tmp_path: Path) -> None:
    """An operator has to be pointed at the right line of a 5,000-line file."""
    good = 5_000
    at = 2_500
    root = _snapshot(tmp_path, good, "[" * 20_000 + "]" * 20_000, at=at)
    rejections: list[AdapterError] = []
    adapter = JsonlSnapshotAdapter(
        root, source_id="crm", entity_types=("contact",), on_reject=rejections.append
    )
    list(adapter.read(GENERATION))

    assert len(rejections) == 1
    assert rejections[0].line_no == at + 1
    assert rejections[0].kind == "excessive_nesting"


def test_validate_batch_isolates_a_fault_its_callee_somehow_lets_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The structural half: the loop is a second, independent isolation layer.

    `validate_payload` is total, so this cannot happen today -- which is exactly why
    it is forced here. The structure that converts a per-record fault into a
    per-source failure has to be gone, not merely un-triggered.

    The patch replaces **`validate_payload` itself**, not the inner
    `_validate_payload`: patching the inner one would be caught by the outer guard
    and this test would pass without the batch-level clause ever running -- green
    proving the wrong thing. `validate_batch` resolves `validate_payload` from the
    module globals, so this is the call it really makes.
    """
    import recon.adapters.validation as validation_module

    real = validation_module.validate_payload
    calls = {"n": 0}

    def sometimes_explodes(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RecursionError("a fault no clause names")
        return real(*args, **kwargs)

    monkeypatch.setattr(validation_module, "validate_payload", sometimes_explodes)

    raws = [json.dumps({**BASE[("crm", "contact")], "crm_id": f"CRM-973B{i}"}) for i in range(4)]
    results = validate_batch("crm", "contact", GENERATION, raws)

    assert len(results) == len(raws), "a lost line is the silent skip R2 forbids"
    bad = [item for item in results if isinstance(item, AdapterError)]
    assert len(bad) == 1
    assert bad[0].status == 422 and bad[0].line_no == 2
    assert sum(isinstance(item, RawRecord) for item in results) == 3


# ===========================================================================
# MINOR 6 -- deployment state may not 500 a request
# ===========================================================================
CORRUPT_MANIFESTS: list[tuple[str, str]] = [
    ("truncated", '{"expected_counts": {"gen973": {"crm.contact"'),
    ("not-json", "this is not json at all"),
    ("empty-file", ""),
    ("counts-is-a-list", '{"expected_counts": ["gen973"]}'),
    ("per-entity-is-a-list", '{"expected_counts": {"gen973": ["crm.contact"]}}'),
    ("count-is-a-string", '{"expected_counts": {"gen973": {"crm.contact": "many"}}}'),
    ("count-is-null", '{"expected_counts": {"gen973": {"crm.contact": null}}}'),
    ("count-is-an-object", '{"expected_counts": {"gen973": {"crm.contact": {"n": 1}}}}'),
    ("top-level-is-a-list", "[1, 2, 3]"),
    ("top-level-is-a-number", "42"),
]


@pytest.mark.parametrize(("case", "body"), CORRUPT_MANIFESTS, ids=[c for c, _ in CORRUPT_MANIFESTS])
def test_a_corrupt_manifest_degrades_instead_of_raising(
    tmp_path: Path, case: str, body: str
) -> None:
    """Expected counts are an optional strengthening; an unreadable one is not fatal."""
    (tmp_path / "manifest.json").write_text(body, encoding="utf-8")
    assert expected_counts_from_manifest(tmp_path) == {}


def test_a_partly_corrupt_manifest_keeps_the_entries_it_can_read(tmp_path: Path) -> None:
    """It degrades per entry, so one bad count does not discard the good ones."""
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "expected_counts": {
                    "gen973": {"crm.contact": 12, "crm.deal": "many"},
                    "notagen": {"crm.contact": 5},
                    "gen974": ["nope"],
                }
            }
        ),
        encoding="utf-8",
    )
    assert expected_counts_from_manifest(tmp_path) == {("crm", "contact", 973): 12}


def test_a_corrupt_manifest_does_not_500_an_authenticated_post(
    api: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The reported shape: deployment state turning every request into a 500."""
    (tmp_path / "manifest.json").write_text('{"expected_counts": {"gen973": {"crm', "utf-8")
    monkeypatch.setenv("KEYSTONE_FIXTURES_DIR", str(tmp_path))

    raw = _raw("crm", "contact", "manifest")
    response = _post(api, "crm", "contact", [raw], run_id="hostile-manifest")
    assert response.status_code < 500, (
        f"a wrong-shaped fixtures/manifest.json produced {response.status_code}: "
        "deployment state may not turn a valid request into a server error"
    )
    assert response.status_code == 200
