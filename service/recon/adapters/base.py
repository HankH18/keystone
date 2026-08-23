"""The read-only source port (R1) and the one error type it raises (R2, R3).

`ReadOnlyAdapter` is the whole port. It has **three** members -- `source_id`,
`generations()` and `read(generation)` -- and deliberately no fourth: there is no
write method, no "sync back", no "upsert". DESIGN.md pins this ("Adapters expose
**no write methods** -- the Protocol has none; adding one is a design violation")
and `tests/ingest/test_read_only_port.py` enforces it *structurally*, by
introspecting the classes, so the rule survives a refactor that a grep would miss.

Two error surfaces, deliberately distinct:

*structural breakage of a payload* (contract SS7)
    A truncated body, a wrong scalar type, a null primary key, a repeated primary
    key, a non-object line, an oversized body. These raise `AdapterError` with a
    **4xx** status drawn from the committed `recon.seed.malformed.EXPECT_CODES`
    table -- the same table the fixture corpus is built from, imported rather than
    restated so the adapter and the fixture cannot drift apart.

*the source itself failing* (R3)
    A hang, a stall, a mid-stream exception, an upstream 5xx. These raise
    `AdapterError` with a **502/504** status, an `upstream_status` when the source
    reported one, and always a `latency_ms` -- so "how long did we wait before
    giving up" is in the error, not only in a log line.

Neither is ever a 500 to the caller and neither is ever a silent skip: every
rejection is either raised or handed to an `on_reject` sink that counts it.

An **unrecognised enum value is not structural breakage** (contract SS7, `G27`).
`{"lifecycle_stage": "not_a_stage"}` is a well-formed record: it validates, it
lands, `norm_enum` returns `None`, and the field is recorded in the staging row's
`unchecked_fields`. Rejecting it here would delete a mandated `unchecked` path
from the pipeline and invent a rejection the contract forbids.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from recon.reference import MAX_PAYLOAD_BYTES
from recon.seed.malformed import EXPECT_CODES

__all__ = [
    "ADAPTER_LOAD_DEADLINE_SECONDS",
    "ADAPTER_STALL_TIMEOUT_SECONDS",
    "KIND_STATUS",
    "MAX_JSON_DEPTH",
    "MAX_PAYLOAD_BYTES",
    "PROBLEM_BASE",
    "WRITE_NAME_TOKENS",
    "AdapterError",
    "RawRecord",
    "ReadOnlyAdapter",
    "SourceUnavailable",
    "canonical_json",
    "row_hash",
]

#: Substrings that make an attribute name *look like* a write. The structural test
#: asserts that no adapter class carries one anywhere in its MRO. Committed here so
#: the port and its test share one list.
WRITE_NAME_TOKENS: tuple[str, ...] = (
    "write",
    "insert",
    "update",
    "delete",
    "save",
    "put",
    "patch",
    "post",
    "upsert",
    "truncate",
    "execute",
)

#: DESIGN.md: "Timeouts bounded at 10s -> structured error". This is the **stall**
#: bound: the longest a source may go without handing over a record.
ADAPTER_STALL_TIMEOUT_SECONDS: float = 10.0

#: The second half of "never hang a sync". A stall bound alone does not stop a
#: source that drips one record every nine seconds forever, so a whole load also
#: carries a total wall-clock budget. Both are arguments, so a caller (the bench,
#: a test) can tighten them; neither is optional.
ADAPTER_LOAD_DEADLINE_SECONDS: float = 300.0

#: The deepest container nesting a payload may carry, as a **hard, deterministic**
#: bound rather than whatever the interpreter's stack happens to allow.
#:
#: Every structural pass over a parsed payload is recursive -- `json.loads` itself,
#: `json.dumps` in `canonical_json`, and `unstorable_text` -- so a document nested
#: deeply enough raises `RecursionError`, which is a `RuntimeError` and not a
#: `ValueError`: it walked straight out of `validate_payload` and reached a client
#: as a bare 500, on a ~20 KB body well inside `MAX_PAYLOAD_BYTES`.
#:
#: The bound is checked *iteratively*, so the check itself cannot be the thing that
#: recurses, and it is a constant rather than a fraction of `sys.getrecursionlimit()`
#: on purpose: `RecursionError` fires at a depth that depends on how much stack the
#: caller had already consumed, so the same payload could be accepted on the request
#: thread and refused on the adapter's reader thread. A verdict on a payload must be
#: a property of the payload.
#:
#: 32 is ~16x the deepest shape contract SS1 describes (a payment's `metadata`
#: object, depth 2) and ~1/30th of the default recursion limit, so it refuses no
#: conforming source and leaves every recursive pass a wide margin.
MAX_JSON_DEPTH: int = 32

PROBLEM_BASE = "https://keystone.invalid/problems/"

#: Structural-rejection kinds come from the committed fixture table; the
#: source-failure kinds are added here. `KIND_STATUS` is the single mapping from a
#: failure kind to the status a caller sees.
KIND_STATUS: dict[str, int] = {
    **EXPECT_CODES,
    # a natural key carrying a control character cannot become a source ref (SS5.4)
    "invalid_natural_key": 422,
    # a value the *store* cannot hold: a NUL or an unpaired surrogate in text or
    # jsonb. Structurally a 422 like any other unusable payload -- it is named
    # separately because "we cannot store this" is a different operator action
    # from "this is the wrong type", and because it can arrive on a field no model
    # declares (`extra="allow"`), so no pydantic error could ever carry it.
    "unstorable_value": 422,
    # a document nested deeper than `MAX_JSON_DEPTH`. 400 rather than 422: like
    # `unparseable_json` this is a verdict on the document's *form*, reached before
    # any field of it is looked at, and it is the answer for both ways the depth
    # shows up -- the parser hitting `RecursionError` on the way in, and a document
    # that parsed but is too deep for the passes that follow.
    "excessive_nesting": 400,
    # a JSON number that is syntactically valid and whose **parsed value** is not
    # finite: `1e400` overflows to `inf` during parsing, so `parse_constant` -- which
    # only fires for the literal `NaN` / `Infinity` tokens -- never sees it. 422 and
    # not 400 because, unlike those tokens, this really is well-formed JSON; it is
    # the value that cannot be stored (`json.dumps` re-emits a bare `Infinity`, which
    # `jsonb` refuses at the COPY, i.e. a 5xx for a payload problem).
    "non_finite_number": 422,
    # The backstop that makes `validate_payload` total: any *other* exception raised
    # while judging a payload is reported as a rejection of that payload rather than
    # escaping as a 500 (R2) or being reclassified as a source failure (R3). It is
    # deliberately its own word -- an operator seeing it is looking at a payload the
    # validator could not classify, which is a different action from any of the
    # kinds above.
    "unprocessable_payload": 422,
    # R3: the source did not answer in time / at all / answered 5xx
    "source_timeout": 504,
    "source_unavailable": 502,
    "source_error": 502,
    # the load itself could not be opened
    "source_missing": 424,
}


class SourceUnavailable(RuntimeError):
    """Raised *by a source implementation* when the upstream refuses.

    Carries the upstream's own status so `read_bounded` can put it in the
    structured error rather than flattening every failure into "something broke".
    """

    def __init__(self, detail: str, *, upstream_status: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.upstream_status = upstream_status


class AdapterError(Exception):
    """One structured failure at the adapter boundary.

    `kind` is the vocabulary word (``wrong_scalar_type``, ``source_timeout``, ...);
    `status` is the HTTP status it becomes. `problem()` renders RFC7807
    ``{type, title, status, detail}`` plus the extension members a reader actually
    needs to act on it -- which source, which record, and, for R3, how long we
    waited and what the upstream said.
    """

    def __init__(
        self,
        kind: str,
        detail: str,
        *,
        status: int | None = None,
        source_id: str | None = None,
        entity_type: str | None = None,
        generation: int | None = None,
        natural_key: str | None = None,
        line_no: int | None = None,
        latency_ms: float | None = None,
        upstream_status: int | None = None,
    ) -> None:
        super().__init__(f"{kind}: {detail}")
        if kind not in KIND_STATUS and status is None:
            raise ValueError(f"unknown adapter failure kind {kind!r} and no explicit status")
        self.kind = kind
        self.detail = detail
        self.status = status if status is not None else KIND_STATUS[kind]
        self.source_id = source_id
        self.entity_type = entity_type
        self.generation = generation
        self.natural_key = natural_key
        self.line_no = line_no
        self.latency_ms = latency_ms
        self.upstream_status = upstream_status

    @property
    def title(self) -> str:
        return self.kind.replace("_", " ")

    def problem(self) -> dict[str, Any]:
        """RFC7807-style problem document. Never carries a 5xx for a bad payload."""
        document: dict[str, Any] = {
            "type": f"{PROBLEM_BASE}{self.kind}",
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
        }
        for name, value in (
            ("kind", self.kind),
            ("source", self.source_id),
            ("entity_type", self.entity_type),
            ("generation", self.generation),
            ("natural_key", self.natural_key),
            ("line", self.line_no),
            ("latency_ms", self.latency_ms),
            ("upstream_status", self.upstream_status),
        ):
            if value is not None:
                document[name] = value
        return document

    def log_fields(self) -> dict[str, Any]:
        """The structured-log payload. Same facts, log-shaped."""
        return {k: v for k, v in self.problem().items() if k != "type"}


def canonical_json(payload: Any) -> str:
    """The one JSON encoding used for hashing and for landing (contract SS8).

    `sort_keys` + `ensure_ascii` + no spaces: two runs, two machines and two
    locales produce the same bytes, so `row_hash` is a property of the record and
    not of the process that read it.
    """
    return json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def row_hash(source_id: str, entity_type: str, natural_key: str, payload_json: str) -> str:
    """Content hash of one source record.

    **Generation is deliberately not an input.** A record re-emitted verbatim in
    generation 3 (contract SS7: every snapshot is complete, unchanged records are
    re-emitted) must hash to the value it had in generation 1 -- that equality is
    what makes "unchanged since the last snapshot" a cheap query instead of a
    full-payload comparison. The generation is carried in its own column.
    """
    material = "\x1f".join((source_id, entity_type, natural_key, payload_json))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RawRecord:
    """One validated source record on its way to `raw_records` (DESIGN SSData models).

    The five pinned members are `source_id, entity_type, natural_key, generation,
    payload, row_hash`. `payload_json` is the canonical encoding retained from the
    hashing step so the landing COPY does not serialize the same dict twice; it is
    an optimisation, not part of the contract.
    """

    source_id: str
    entity_type: str
    natural_key: str
    generation: int
    payload: Mapping[str, Any]
    row_hash: str
    payload_json: str


@runtime_checkable
class ReadOnlyAdapter(Protocol):
    """The port. Three members, none of which can write anything (R1)."""

    source_id: str

    def generations(self) -> list[int]:
        """Snapshot generations this source can currently serve, ascending."""
        ...

    def read(self, generation: int) -> Iterator[RawRecord]:
        """Validated records for one generation, or raise `AdapterError`."""
        ...
