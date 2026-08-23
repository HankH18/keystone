"""Server-side payload validation (R2) -- one entry point, one vocabulary.

`validate_payload` turns a **literal payload string** into a `RawRecord` or an
`AdapterError`. It takes a string rather than a parsed object on purpose: contract
SS7's corpus stores `raw` as the literal line precisely so a truncated body, a
non-object line and an oversized body are representable at all, and a validator
that only ever sees `dict` cannot be tested against half of its own mandate.

The checks run in the order the failure modes shadow each other:

1. **size** -- an oversized body is rejected before it is parsed, because parsing
   it is the denial-of-service the limit exists to prevent (`MAX_PAYLOAD_BYTES`);
2. **parse** -- `unparseable_json` (400), which includes the `NaN` / `Infinity` /
   `-Infinity` literals Python's parser accepts by default and RFC 8259 does not.
   They are refused here rather than in a model because `extra="allow"` lets one
   ride in on a field no model names, and `json.dumps` re-emits it as a bare
   `NaN` -- a jsonb column rejects that, which is a 500 for a payload problem;
3. **depth** -- `excessive_nesting` (400) past `MAX_JSON_DEPTH`. Every later pass
   over the payload recurses, so this bound is what makes them safe;
4. **number values** -- `non_finite_number` (422): a *parsed* float that is not
   finite. Step 2's `parse_constant` is a guard on the input **syntax** and is
   therefore not a guard on the resulting **value**: `1e400` contains no `Infinity`
   token, is well-formed JSON, and overflows to `inf` while being parsed. It then
   re-emits as a bare `Infinity` and is refused by `jsonb` at the landing COPY --
   the same 503-for-a-payload-problem the literal would have caused. Checked over
   the whole parsed document for the same `extra="allow"` reason as step 7;
5. **shape** -- a JSON array/scalar line is `non_object_line` (400);
6. **primary key** -- absent is `missing_required_field`, present-and-null is
   `null_primary_key`; both 422, but they are different facts and are logged as
   different kinds;
7. **schema** -- the SS1 Pydantic model. A pydantic ``missing`` error becomes
   `missing_required_field`, anything else `wrong_scalar_type`. The model is also
   where every field is bounded by the *column type it lands in* -- an unparseable
   timestamp, an out-of-range integer -- so "the validator accepted it and the
   database refused it" is not a reachable state;
8. **storable text** -- a string Postgres cannot hold: a NUL (`text` and `jsonb`
   both refuse `U+0000`) or an unpaired surrogate (not encodable as UTF-8, so
   psycopg cannot even put it on the wire). Checked over the whole parsed payload,
   keys included, for the same `extra="allow"` reason as the number check;
9. **ref safety** -- a natural key carrying a control character can never become a
   source ref (SS5.4), so it is refused here rather than corrupting a fingerprint
   downstream.

Steps 2, 4, 7 and 8 exist because of one rule: **a payload this function accepts
must be one the pipeline can store.** Anything else turns a malformed record into a
500 at the COPY -- the failure R2 names first and the one an operator cannot act on.

Total by construction
---------------------
`validate_payload` has **two** outcomes and no third: a `RawRecord`, or an
`AdapterError` carrying a 4xx. Enumerating "which exception should I also catch"
one clause at a time is what let two escapes through already -- first a
`RecursionError` from `json.loads` (a `RuntimeError`, so `except ValueError`
missed it), and before that a driver error at the write. The paths below can each
raise something that is not a `ValueError`:

============================  =================================================
step                          what else it can raise
============================  =================================================
`raw.encode`                  `UnicodeEncodeError` (lone surrogate) -- caught
`json.loads`                  `RecursionError` (deep nesting), `MemoryError`
`str(parsed[pk])`             `ValueError` past `sys.int_info.str_digits_check_
                              threshold` for a 4300+ digit integer
`model_validate`              `ValidationError`, and anything a field validator
                              raises that pydantic does not wrap
`canonical_json` / the walks  `RecursionError`, `TypeError`
============================  =================================================

Rather than grow that table into `except` clauses, the whole body runs inside one
guard that converts **any** `Exception` into an `unprocessable_payload` 422 (steps
2--4 still classify the common ones precisely, so the backstop stays rare and its
appearance in a log is itself a signal). `KeyboardInterrupt` / `SystemExit` are
deliberately not caught: they are not verdicts about a payload.

That totality is what makes per-line isolation real. `read_bounded` turns any
non-`AdapterError` escaping a read into an R3 **source** failure, so before this
guard a single hostile line failed a whole 40,000-line generation with
`records_read=0` -- converting a per-record fault into a per-source one. The same
conversion is applied a second time in `validate_batch`, so the isolation is a
property of the loop as well as of the function it calls.

`validate_batch` adds the one check that is not a property of a single record:
**duplicate primary key** (409). SS7 makes a repeated PK a structural rejection in
any generation and SS12 D-3 deletes the "same `payment_id` twice" conflict branch
outright, so this is a rejection and never a conflict. Both members of a duplicate
group are rejected, not just the second: which line "came first" in a snapshot is
not a fact about the data, and admitting one of two contradictory rows would land
an arbitrary winner in an append-only table. That is also exactly what the
committed corpus asserts -- `MAL-019` and `MAL-020` share `CRM-9000019` and *both*
carry `expect_code: 409`.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from recon.adapters.base import (
    MAX_JSON_DEPTH,
    MAX_PAYLOAD_BYTES,
    AdapterError,
    RawRecord,
    canonical_json,
    row_hash,
)
from recon.adapters.identifiers import IDENTIFIER_RULE, identifier_fault
from recon.adapters.models import model_for, primary_key_field
from recon.reference import make_ref

__all__ = [
    "json_depth",
    "non_finite_number",
    "partition",
    "scan_document",
    "unstorable_text",
    "validate_batch",
    "validate_payload",
]

#: What `json.dumps(..., ensure_ascii=True)` emits for the two shapes step 6 hunts:
#: a NUL, and any surrogate code point. Both are cheap substring scans over the
#: canonical encoding that is computed anyway, so the exact (recursive) check runs
#: only for the rare payload that trips one -- an astral character legitimately
#: emits a surrogate *pair* and must not be rejected, which is why a hit here is a
#: suspicion and never a verdict.
_NUL_ESCAPE = "\\u0000"
_SURROGATE_ESCAPES = ("\\ud", "\\uD")


def _describe(error: Mapping[str, Any]) -> str:
    location = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
    return f"{location}: {error.get('msg', 'invalid')}"


def _reject_json_constant(literal: str) -> Any:
    """`json.loads(parse_constant=...)`: refuse `NaN` / `Infinity` / `-Infinity`."""
    raise ValueError(f"{literal} is not a JSON number (RFC 8259 has no {literal} literal)")


def _safe(text: Any) -> str:
    """A field name that is safe to put in a log line and in `jsonb` error detail.

    The offending key may itself be the unstorable string, and the rejection's
    detail is written to `ingest_runs.error_detail` (jsonb) -- so quoting it raw
    would move the failed write from the payload to the error about the payload.
    """
    return str(text).encode("unicode_escape").decode("ascii")


def unstorable_text(value: Any, path: str = "") -> str | None:
    """The path of the first string Postgres could not store, or `None`.

    Walks keys as well as values: a NUL in a key is as fatal to a `jsonb` write as
    one in a value, and `extra="allow"` means a key no model names still lands.
    """
    if isinstance(value, str):
        if "\x00" in value:
            return f"{path or '<root>'} (NUL)"
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return f"{path or '<root>'} (unpaired surrogate)"
        return None
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{path}.{_safe(key)}" if path else _safe(key)
            found = unstorable_text(key, child)
            if found is not None:
                return found
            found = unstorable_text(item, child)
            if found is not None:
                return found
        return None
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = unstorable_text(item, f"{path}[{index}]")
            if found is not None:
                return found
    return None


def _children(value: Any) -> Iterable[Any] | None:
    """The container's members, or `None` when `value` is a leaf."""
    if isinstance(value, Mapping):
        return value.values()
    if isinstance(value, (list, tuple)):
        return value
    return None


def json_depth(value: Any) -> int:
    """Deepest container nesting in `value`; 0 for a scalar, 1 for `{}` or `[]`.

    **Iterative on purpose.** This is the check that stops the recursive passes
    from blowing the stack, so implementing it recursively would put the crash it
    prevents inside the prevention.
    """
    deepest = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, level = stack.pop()
        children = _children(item)
        if children is None:
            continue
        if level > deepest:
            deepest = level
        for child in children:
            stack.append((child, level + 1))
    return deepest


def scan_document(value: Any) -> tuple[int, bool]:
    """`(deepest nesting, any non-finite float?)` in **one** iterative pass.

    The hot path: this runs on every record of every 40,000-line snapshot, so the
    two verdicts share one traversal and the traversal does as little as possible.
    Two things it deliberately does *not* do:

    * it never pushes a leaf that is neither a container nor a float -- strings,
      ints, bools and nulls are the overwhelming majority of a payload's nodes and
      settle both questions where they are found;
    * it builds **no path strings**. Formatting `f"{path}.{key}"` for every key of
      every record cost more than every other check in this module put together,
      to produce a label that is thrown away for the ~100% of records that are
      fine. When the flag comes back set, :func:`non_finite_number` walks again and
      builds the one path an operator needs -- the same suspicion/verdict split
      `unstorable_text` uses, for the same reason.
    """
    deepest = 0
    found = False
    stack: list[tuple[Any, int]] = []
    if isinstance(value, float):
        return 0, not math.isfinite(value)
    if isinstance(value, (Mapping, list, tuple)):
        stack.append((value, 1))
    while stack:
        item, level = stack.pop()
        if level > deepest:
            deepest = level
        children = item.values() if isinstance(item, Mapping) else item
        child_level = level + 1
        for child in children:
            if isinstance(child, float):
                if not math.isfinite(child):
                    found = True
            elif isinstance(child, (Mapping, list, tuple)):
                stack.append((child, child_level))
    return deepest, found


def non_finite_number(value: Any, max_depth: int = MAX_JSON_DEPTH) -> str | None:
    """The path of the first non-finite number in `value`, or `None`.

    The **diagnostic** half of :func:`scan_document`: it walks the document again
    to build a human-readable path, and runs only for a payload that already
    tripped the cheap flag. Also the standalone answer for a caller (a test, a
    tool) that wants the location rather than a yes/no.

    Walks the whole document -- values, list members, and the values under keys no
    model declares -- because `extra="allow"` means a `1e400` can arrive anywhere,
    and the field-level guards (`_deal_amount`) only cover the fields SS1 names.

    Iterative, and bounded by `max_depth`: the depth check runs before this one, so
    the bound is a belt on an already-fastened brace rather than a second verdict.
    """
    stack: list[tuple[Any, str, int]] = [(value, "", 1)]
    while stack:
        item, path, level = stack.pop()
        if isinstance(item, float) and not math.isfinite(item):
            return f"{path or '<root>'} ({item!r})"
        if level > max_depth:
            continue
        if isinstance(item, Mapping):
            for key, child in item.items():
                label = _safe(key)
                stack.append((child, f"{path}.{label}" if path else label, level + 1))
        elif isinstance(item, (list, tuple)):
            for index, child in enumerate(item):
                stack.append((child, f"{path}[{index}]", level + 1))
    return None


def validate_payload(
    source_id: str,
    entity_type: str,
    generation: int,
    raw: str,
    *,
    line_no: int | None = None,
    max_payload_bytes: int = MAX_PAYLOAD_BYTES,
) -> RawRecord:
    """Validate one literal payload string. Returns a `RawRecord` or raises `AdapterError`.

    **Total**: every input produces one of those two outcomes. See the module
    docstring -- an exception that escaped here reached a client as a 500 (R2) or
    was reclassified as a source failure (R3), and both have happened.
    """
    context: dict[str, Any] = {
        "source_id": source_id,
        "entity_type": entity_type,
        "generation": generation,
        "line_no": line_no,
    }
    try:
        return _validate_payload(
            source_id, entity_type, generation, raw, context, max_payload_bytes
        )
    except AdapterError:
        raise
    except Exception as exc:
        # The backstop. `Exception`, not `BaseException`: a KeyboardInterrupt or a
        # SystemExit is not a verdict about this payload and must keep propagating.
        # The detail is escaped because the payload may itself be the thing Postgres
        # cannot store, and this text is written to `ingest_runs.error_detail` (jsonb).
        raise AdapterError(
            "unprocessable_payload",
            f"payload could not be validated: {_safe(f'{type(exc).__name__}: {exc}')}",
            **context,
        ) from None


def _validate_payload(
    source_id: str,
    entity_type: str,
    generation: int,
    raw: str,
    context: dict[str, Any],
    max_payload_bytes: int,
) -> RawRecord:
    """The checks themselves. Only ever called through `validate_payload`'s guard."""
    try:
        size = len(raw.encode("utf-8"))
    except UnicodeEncodeError:
        # The very first thing done to a payload is measuring it, and measuring it
        # means encoding it -- so an unpaired surrogate in the *line* blows up
        # before any check can classify it. It is the same defect step 6 catches
        # inside the parsed object, one layer out.
        raise AdapterError(
            "unstorable_value",
            "payload carries an unpaired surrogate and is not encodable as UTF-8, "
            "so it can be neither measured nor stored",
            **context,
        ) from None
    if size > max_payload_bytes:
        raise AdapterError(
            "oversized_body",
            f"payload is {size} bytes; the limit is {max_payload_bytes}",
            **context,
        )

    try:
        parsed = json.loads(raw, parse_constant=_reject_json_constant)
    except ValueError as exc:
        raise AdapterError("unparseable_json", f"payload is not JSON: {exc}", **context) from None
    except RecursionError:
        # `json.loads` recurses per nesting level, and `RecursionError` is a
        # `RuntimeError` -- so before this clause a ~20 KB body of nothing but
        # brackets, comfortably inside MAX_PAYLOAD_BYTES, walked out of this
        # function and reached the client as a bare 500.
        raise AdapterError(
            "excessive_nesting",
            "payload nests containers more deeply than the JSON parser can read "
            f"(the accepted limit is {MAX_JSON_DEPTH})",
            **context,
        ) from None

    depth, has_non_finite = scan_document(parsed)
    if depth > MAX_JSON_DEPTH:
        # Reached only by a document the parser survived. Every pass after this one
        # -- `model_validate`, `canonical_json`, `unstorable_text` -- recurses, so
        # accepting an unbounded depth here only moves the RecursionError later.
        raise AdapterError(
            "excessive_nesting",
            f"payload nests containers {depth} deep; the limit is {MAX_JSON_DEPTH}",
            **context,
        )

    if has_non_finite:
        offending_number = non_finite_number(parsed) or "<root>"
        # `parse_constant` fires for the literal `NaN` / `Infinity` / `-Infinity`
        # TOKENS only. `1e400` carries no such token, is valid JSON syntax, and
        # overflows to `inf` while being parsed -- so the syntax guard never saw it
        # and `canonical_json` re-emitted a bare `Infinity` that `jsonb` refused at
        # the landing COPY. A guard on the input syntax is not a guard on the value.
        raise AdapterError(
            "non_finite_number",
            f"{offending_number} is not a finite JSON number: it overflows a "
            "double, so it re-encodes as a bare Infinity/NaN that jsonb rejects",
            **context,
        )

    if not isinstance(parsed, dict):
        raise AdapterError(
            "non_object_line",
            f"payload is a JSON {type(parsed).__name__}, not an object",
            **context,
        )

    pk_field = primary_key_field(source_id, entity_type)
    if pk_field not in parsed:
        raise AdapterError(
            "missing_required_field",
            f"primary key {pk_field!r} is absent",
            **context,
        )
    if parsed[pk_field] is None:
        raise AdapterError("null_primary_key", f"primary key {pk_field!r} is null", **context)

    model = model_for(source_id, entity_type)
    try:
        model.model_validate(parsed)
    except ValidationError as exc:
        errors = exc.errors()
        missing = [e for e in errors if e.get("type") == "missing"]
        kind = "missing_required_field" if missing else "wrong_scalar_type"
        reported = missing or errors
        detail = "; ".join(_describe(error) for error in reported[:5])
        raise AdapterError(kind, detail, **context) from None

    natural_key = str(parsed[pk_field])
    # The natural key is an identifier that lands in `raw_records.natural_key`
    # (text) and becomes a source ref, so it is judged by the SAME rule as every
    # other identifier -- `recon.adapters.identifiers`, the one implementation.
    # It carries no length bound: unlike `run_id` it is not concatenated into a
    # `load_id` or a lock key, and inventing a bound the column does not have
    # would reject a conforming source (SS7's "never reject a well-formed record").
    fault = identifier_fault(natural_key, max_length=None)
    if fault is not None:
        raise AdapterError(
            "invalid_natural_key",
            f"{pk_field} is not a usable natural key: {fault}. {IDENTIFIER_RULE} "
            "(SS5.4 refuses one in a ref for the same reason)",
            # The key itself is deliberately NOT echoed: it may be the very value
            # the store cannot hold, and this detail is written to jsonb.
            **context,
        )
    try:
        make_ref(source_id, entity_type, natural_key)
    except ValueError as exc:
        raise AdapterError(
            "invalid_natural_key", str(exc), natural_key=natural_key, **context
        ) from None

    payload_json = canonical_json(parsed)
    if _NUL_ESCAPE in payload_json or any(escape in payload_json for escape in _SURROGATE_ESCAPES):
        # The scan above is a suspicion (an astral character emits a surrogate
        # *pair*, and a literal backslash-u-0-0-0-0 in the text escapes to the same
        # bytes); the walk is the verdict.
        offender = unstorable_text(parsed)
        if offender is not None:
            raise AdapterError(
                "unstorable_value",
                # The natural key is deliberately NOT echoed: it may itself be the
                # unstorable string, and this detail is written to jsonb.
                f"{offender} cannot be stored: Postgres text and jsonb reject NUL "
                "(U+0000) and unpaired surrogates",
                **context,
            )

    return RawRecord(
        source_id=source_id,
        entity_type=entity_type,
        natural_key=natural_key,
        generation=generation,
        payload=parsed,
        row_hash=row_hash(source_id, entity_type, natural_key, payload_json),
        payload_json=payload_json,
    )


def validate_batch(
    source_id: str,
    entity_type: str,
    generation: int,
    raws: Iterable[str],
    *,
    max_payload_bytes: int = MAX_PAYLOAD_BYTES,
) -> list[RawRecord | AdapterError]:
    """Validate a whole snapshot file, adding the load-scoped duplicate-PK check.

    Returns one result per input line, in input order: never fewer, so a caller
    that zips results back onto lines can never lose one (a lost line is the
    "silent skip" R2 forbids).

    **One bad line costs exactly that line.** The `except Exception` below is the
    structural half of that promise, and is not redundant with the guard inside
    `validate_payload`: this loop is the only thing standing between a per-record
    fault and `read_bounded`, which turns any non-`AdapterError` escaping a read
    into an R3 *source* failure -- so before both guards existed, one hostile line
    made a whole generation report `status=failed`, `records_read=0`,
    `records_rejected=0`, and discarded the other 39,999 records. Two independent
    layers, because the cost of the fault leaking is a whole snapshot.
    """
    results: list[RawRecord | AdapterError] = []
    seen: dict[str, list[int]] = {}
    for index, raw in enumerate(raws):
        try:
            record = validate_payload(
                source_id,
                entity_type,
                generation,
                raw,
                line_no=index + 1,
                max_payload_bytes=max_payload_bytes,
            )
        except AdapterError as error:
            results.append(error)
            continue
        except Exception as exc:
            results.append(
                AdapterError(
                    "unprocessable_payload",
                    f"payload could not be validated: {_safe(f'{type(exc).__name__}: {exc}')}",
                    source_id=source_id,
                    entity_type=entity_type,
                    generation=generation,
                    line_no=index + 1,
                )
            )
            continue
        seen.setdefault(record.natural_key, []).append(index)
        results.append(record)

    for natural_key, positions in seen.items():
        if len(positions) < 2:
            continue
        lines = ", ".join(str(position + 1) for position in positions)
        for position in positions:
            results[position] = AdapterError(
                "duplicate_primary_key",
                f"{natural_key!r} appears {len(positions)} times in this generation "
                f"(lines {lines}); a repeated primary key is a structural rejection "
                f"(contract SS7, SS12 D-3), never a conflict",
                source_id=source_id,
                entity_type=entity_type,
                generation=generation,
                natural_key=natural_key,
                line_no=position + 1,
            )
    return results


def partition(
    results: Sequence[RawRecord | AdapterError],
) -> tuple[list[RawRecord], list[AdapterError]]:
    """Split a `validate_batch` result into accepted records and rejections."""
    accepted = [item for item in results if isinstance(item, RawRecord)]
    rejected = [item for item in results if isinstance(item, AdapterError)]
    return accepted, rejected
