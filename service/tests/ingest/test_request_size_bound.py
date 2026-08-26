"""The REQUEST is bounded, not only the records inside it (R2, R19).

`MAX_PAYLOAD_BYTES` bounds one **record** at 256 KiB, and every one of the 24
committed malformed cases is a statement about a record. Nothing bounded the
**request**: `raw_request_body` buffered whatever arrived, and
`RecordsRequest.records` was an unbounded `list[str]`. On a public host that is an
unauthenticated memory-amplification path -- the endpoint is authenticated, but
the body is read by the dependency *before* the handler runs, which is exactly
what makes "401 before 422" true, so an anonymous caller could still make the
process buffer an arbitrary body and then be told 401.

Two bounds, and they bound different things -- stated precisely, because the
tempting summary ("both stop a huge request") is false of the second:

* **bytes on the wire** -- `max_body_bytes()`, enforced *while reading*, 413 with
  the same `oversized_body` problem type a 256 KiB record already earns
  (`EXPECT_CODES`), so one vocabulary covers both. This is the memory bound.
* **records after parsing** -- `MAX_RECORDS_PER_BATCH`. This is **not** a memory
  bound and is not claimed as one: `parse_body` has already run `json.loads` over
  the whole body before any validator fires, so the objects exist by then. It
  bounds what the endpoint goes on to *do and retain* -- validate every element,
  build a rejection per failure, return a `problems` array -- which is the term
  that scales with record count rather than with bytes.

The important test in this module is
:func:`test_the_read_is_abandoned_before_the_whole_body_is_buffered`. Everything
else asserts the status code, and a status code can be produced by a check that
runs *after* the body is already in memory -- which would leave the
amplification exactly where it was while turning the suite green.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from recon.app import create_app
from recon.ingest import (
    DEFAULT_MAX_BODY_BYTES,
    MAX_BODY_BYTES_ENV,
    MAX_RECORDS_PER_BATCH,
    OversizedBody,
    max_body_bytes,
    raw_request_body,
)
from tests.ingest.conftest import TRIGGER_HEADERS

#: A syntactically fine CRM contact -- these tests are about the envelope, so the
#: record inside it must never be the reason for a rejection.
_CONTACT = (
    '{"crm_id":"CRM-9600001","email":"bounded@example.test","first_name":"Ada",'
    '"last_name":"Byron","lifecycle_stage":"lead","created_at":"2026-02-01T00:00:00Z",'
    '"updated_at":"2026-02-02T00:00:00Z","external_id":null,"dob":"2012-05-04",'
    '"grade":"4","state":"TX","marketing_consent":true}'
)


@pytest.fixture
def api(owner_engine: Any, trigger_secret: str) -> TestClient:
    """The real app with the live database behind it, authenticated.

    Authenticated deliberately: an unauthenticated caller is 401 whatever the
    body is (`test_trigger_auth.py`), so a size test that skipped the header
    would pass on the 401 and prove nothing about the size check.
    """
    with TestClient(create_app()) as client:
        yield client


def _envelope(records: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "source": "crm",
        "entity_type": "contact",
        "generation": 962,
        "records": records,
        "persist": False,
        **extra,
    }


def _body_of_exactly(size: int) -> bytes:
    """A valid envelope padded to exactly `size` bytes.

    The padding is an unmodelled key, so the padding itself can never be the
    thing that is rejected -- pydantic ignores it and the body still parses.
    """
    envelope = _envelope([_CONTACT])
    base = json.dumps({**envelope, "pad": ""}, separators=(",", ":"), sort_keys=True).encode()
    padding = size - len(base)
    assert padding >= 0, f"cannot pad down to {size} bytes; the envelope alone is {len(base)}"
    body = json.dumps(
        {**envelope, "pad": "x" * padding}, separators=(",", ":"), sort_keys=True
    ).encode()
    assert len(body) == size, (len(body), size)
    return body


# ----------------------------------------------------------------------------------
# the cap itself
# ----------------------------------------------------------------------------------


def test_the_default_cap_admits_a_real_batch_of_maximal_records() -> None:
    """A bound that refuses a legitimate batch is an outage, not a control."""
    from recon.reference import MAX_PAYLOAD_BYTES

    assert DEFAULT_MAX_BODY_BYTES > MAX_PAYLOAD_BYTES
    assert DEFAULT_MAX_BODY_BYTES % MAX_PAYLOAD_BYTES == 0
    assert max_body_bytes() == DEFAULT_MAX_BODY_BYTES


def test_the_cap_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read from the environment at call time, so a deploy can tighten it."""
    monkeypatch.setenv(MAX_BODY_BYTES_ENV, "4096")
    assert max_body_bytes() == 4096


@pytest.mark.parametrize("unusable", ["", "   ", "0", "-1", "many", "1.5"])
def test_an_unusable_configured_cap_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, unusable: str
) -> None:
    """Fail *closed on the default*: a typo must not remove the bound.

    `int("")` raising and `int("-1")` succeeding are the same class of operator
    error, and neither may be read as "no limit".
    """
    monkeypatch.setenv(MAX_BODY_BYTES_ENV, unusable)
    assert max_body_bytes() == DEFAULT_MAX_BODY_BYTES


def test_a_body_over_the_cap_is_refused_with_413(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MAX_BODY_BYTES_ENV, "4096")
    response = api.request(
        "POST",
        "/internal/ingest/records",
        content=_body_of_exactly(4097),
        headers={**TRIGGER_HEADERS, "content-type": "application/json"},
    )
    assert response.status_code == 413, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"] == "https://keystone.invalid/problems/oversized_body"
    assert body["status"] == 413
    assert body["limit_bytes"] == 4096


def test_the_boundary_byte_is_the_boundary(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exactly at the cap is accepted; one byte more is not. `>` not `>=`."""
    monkeypatch.setenv(MAX_BODY_BYTES_ENV, "4096")
    headers = {**TRIGGER_HEADERS, "content-type": "application/json"}

    at_limit = api.request(
        "POST", "/internal/ingest/records", content=_body_of_exactly(4096), headers=headers
    )
    assert at_limit.status_code != 413, at_limit.text

    over_limit = api.request(
        "POST", "/internal/ingest/records", content=_body_of_exactly(4097), headers=headers
    )
    assert over_limit.status_code == 413, over_limit.text


def test_the_refusal_does_not_echo_the_body(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The body of a refused request is where a secret gets pasted by accident."""
    monkeypatch.setenv(MAX_BODY_BYTES_ENV, "4096")
    body = _body_of_exactly(4097)
    response = api.request(
        "POST",
        "/internal/ingest/records",
        content=body,
        headers={**TRIGGER_HEADERS, "content-type": "application/json"},
    )
    assert response.status_code == 413
    assert "CRM-9600001" not in response.text
    assert "xxxx" not in response.text


def test_an_unauthenticated_oversized_request_is_still_401_first(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R19 outranks the size check in the *response*, not in the read.

    The bytes are already refused by then -- the dependency stopped reading --
    but the caller is told 401, so the cap is not an oracle for anonymous
    callers.
    """
    monkeypatch.setenv(MAX_BODY_BYTES_ENV, "4096")
    response = api.request(
        "POST",
        "/internal/ingest/records",
        content=_body_of_exactly(4097),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 401, response.text


def test_the_two_job_triggers_carry_the_same_bound(
    api: TestClient, monkeypatch: pytest.MonkeyPatch, trigger_secret: str
) -> None:
    """`/internal/sync` and `/internal/reconcile` share `raw_request_body`.

    They share the dependency, so they share the read; the point of asserting it
    is that they also share the *answer*, rather than one of them buffering the
    body and then 422-ing on an envelope it should never have parsed.
    """
    monkeypatch.setenv(MAX_BODY_BYTES_ENV, "4096")
    monkeypatch.setenv("TRIGGER_SECRET_RECONCILE", trigger_secret)
    from recon.config import get_settings

    get_settings.cache_clear()
    try:
        for path in ("/internal/sync", "/internal/reconcile"):
            response = api.request(
                "POST",
                path,
                content=b'{"run_id":"x","pad":"' + b"x" * 5000 + b'"}',
                headers={**TRIGGER_HEADERS, "content-type": "application/json"},
            )
            assert response.status_code == 413, f"{path}: {response.status_code} {response.text}"
            assert response.json()["limit_bytes"] == 4096
    finally:
        get_settings.cache_clear()


# ----------------------------------------------------------------------------------
# the part a status code cannot prove
# ----------------------------------------------------------------------------------


def test_the_read_is_abandoned_before_the_whole_body_is_buffered() -> None:
    """The memory bound itself: the dependency stops PULLING at the cap.

    A check written after `await request.body()` returns the same 413 and bounds
    nothing -- the body is in the process by then. So this drives the dependency
    against an ASGI receive channel that counts how many chunks it was asked
    for. `TestClient` cannot show this: starlette's test transport answers the
    first `receive()` with the entire body in one message, so every HTTP test
    above exercises the *verdict*, not the *read*.
    """
    chunk = b"x" * 1024
    total_chunks = 1024  # a 1 MiB body against a 4 KiB cap
    pulled = 0

    async def receive() -> dict[str, Any]:
        nonlocal pulled
        pulled += 1
        if pulled > total_chunks:
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.request", "body": chunk, "more_body": True}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/internal/ingest/records",
        "raw_path": b"/internal/ingest/records",
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [(b"content-type", b"application/json")],
        "client": ("test", 1),
        "server": ("test", 80),
    }

    async def drive() -> bytes:
        return await raw_request_body(Request(scope, receive))

    import os

    previous = os.environ.get(MAX_BODY_BYTES_ENV)
    os.environ[MAX_BODY_BYTES_ENV] = "4096"
    try:
        body = asyncio.run(drive())
    finally:
        if previous is None:
            os.environ.pop(MAX_BODY_BYTES_ENV, None)
        else:
            os.environ[MAX_BODY_BYTES_ENV] = previous

    assert isinstance(body, OversizedBody), type(body)
    assert pulled <= 8, (
        f"the dependency pulled {pulled} of {total_chunks} 1 KiB chunks against a "
        "4096-byte cap; it is buffering the body and checking the size afterwards, "
        "which bounds the status code and not the memory"
    )
    assert len(body) == 0, "the refused body must not be handed on to a parser"


def test_a_declared_content_length_over_the_cap_is_refused_without_reading() -> None:
    """The cheap arm: a truthful `Content-Length` is refused at zero bytes read."""
    pulled = 0

    async def receive() -> dict[str, Any]:  # pragma: no cover - must never be called
        nonlocal pulled
        pulled += 1
        return {"type": "http.request", "body": b"x" * 100_000, "more_body": False}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/internal/ingest/records",
        "raw_path": b"/internal/ingest/records",
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [(b"content-type", b"application/json"), (b"content-length", b"100000")],
        "client": ("test", 1),
        "server": ("test", 80),
    }

    import os

    previous = os.environ.get(MAX_BODY_BYTES_ENV)
    os.environ[MAX_BODY_BYTES_ENV] = "4096"
    try:
        body = asyncio.run(raw_request_body(Request(scope, receive)))
    finally:
        if previous is None:
            os.environ.pop(MAX_BODY_BYTES_ENV, None)
        else:
            os.environ[MAX_BODY_BYTES_ENV] = previous

    assert isinstance(body, OversizedBody)
    assert pulled == 0, "a declared over-cap length must be refused without reading the body"


def test_a_lying_content_length_does_not_get_past_the_stream_bound() -> None:
    """`Content-Length: 1` with a megabyte behind it is still stopped."""
    pulled = 0

    async def receive() -> dict[str, Any]:
        nonlocal pulled
        pulled += 1
        return {"type": "http.request", "body": b"x" * 1024, "more_body": True}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/internal/ingest/records",
        "raw_path": b"/internal/ingest/records",
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [(b"content-type", b"application/json"), (b"content-length", b"1")],
        "client": ("test", 1),
        "server": ("test", 80),
    }

    import os

    previous = os.environ.get(MAX_BODY_BYTES_ENV)
    os.environ[MAX_BODY_BYTES_ENV] = "4096"
    try:
        body = asyncio.run(raw_request_body(Request(scope, receive)))
    finally:
        if previous is None:
            os.environ.pop(MAX_BODY_BYTES_ENV, None)
        else:
            os.environ[MAX_BODY_BYTES_ENV] = previous

    assert isinstance(body, OversizedBody)
    assert pulled <= 8, pulled


# ----------------------------------------------------------------------------------
# the second bound: records after parsing
# ----------------------------------------------------------------------------------


def test_a_batch_with_too_many_records_is_a_422(api: TestClient) -> None:
    """Well inside the byte cap, far outside what a slice may contain."""
    records = [""] * (MAX_RECORDS_PER_BATCH + 1)
    response = api.post(
        "/internal/ingest/records", json=_envelope(records), headers=TRIGGER_HEADERS
    )
    assert response.status_code == 422, response.status_code
    body = response.json()
    assert body["type"] == "https://keystone.invalid/problems/invalid_request"
    assert any(error["loc"][-1] == "records" for error in body["errors"]), body["errors"]


def test_both_caps_leave_the_largest_committed_slice_room() -> None:
    """A bound is sized against real traffic, not against a round number.

    The endpoint takes *a snapshot slice*, so the largest slice the committed
    manifest expects -- and the real byte size of the file holding it -- is what
    the two defaults have to admit. Measured here rather than asserted from
    memory: if the generator grows the dataset, this is the test that says the
    caps need re-sizing, instead of a load failing in production.
    """
    import json as _json
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    counts = _json.loads((repo_root / "fixtures" / "manifest.json").read_text())["expected_counts"]
    largest_records = max(
        value
        for generation in counts.values()
        if isinstance(generation, dict)
        for value in generation.values()
        if isinstance(value, int)
    )
    assert largest_records <= MAX_RECORDS_PER_BATCH, (
        f"the record cap ({MAX_RECORDS_PER_BATCH}) is below the largest committed "
        f"slice ({largest_records}); the bound would refuse a legitimate load"
    )

    largest_bytes = max(
        path.stat().st_size for path in (repo_root / "fixtures").rglob("*.jsonl") if path.is_file()
    )
    assert largest_bytes < DEFAULT_MAX_BODY_BYTES, (
        f"the byte cap ({DEFAULT_MAX_BODY_BYTES}) is below the largest committed "
        f"slice file ({largest_bytes} bytes); a whole slice could not be posted"
    )
