"""A fault-injecting source, for proving R3 rather than asserting it.

Four failure shapes, because they fail differently and only one of them is caught
by a naive timeout:

``hang``            never produces a first record -- the classic dead connection.
``slow_drip``       produces records forever, spaced under any stall bound, and
                    never ends. A stall timeout alone runs on this one until the
                    heat death of the process; the load deadline is what stops it.
``midstream_error`` yields some records, then raises. Everything already read is
                    real, so the generation is *partial* -- not empty, not
                    complete -- which is the case SS5.3's completeness ledger
                    exists for.
``http_5xx``        the upstream answers 503. The structured error has to carry
                    that status, not flatten it into "something went wrong".

This class is an adapter and is held to the adapter rules: it is included in the
structural no-write test, and it has no write method either.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence

from recon.adapters.base import (
    AdapterError,
    RawRecord,
    SourceUnavailable,
    canonical_json,
    row_hash,
)

__all__ = ["FAULT_MODES", "FaultInjectingAdapter", "stub_records"]

FAULT_MODES: tuple[str, ...] = (
    "ok",
    "hang",
    "slow_drip",
    "midstream_error",
    "http_5xx",
)


def stub_records(
    count: int,
    *,
    source_id: str = "stub",
    entity_type: str = "contact",
    generation: int = 1,
) -> tuple[RawRecord, ...]:
    """`count` well-formed records, deterministic and content-free."""
    records = []
    for index in range(count):
        natural_key = f"STUB-{index:07d}"
        payload = {"crm_id": natural_key, "n": index}
        payload_json = canonical_json(payload)
        records.append(
            RawRecord(
                source_id=source_id,
                entity_type=entity_type,
                natural_key=natural_key,
                generation=generation,
                payload=payload,
                row_hash=row_hash(source_id, entity_type, natural_key, payload_json),
                payload_json=payload_json,
            )
        )
    return tuple(records)


class FaultInjectingAdapter:
    """A `ReadOnlyAdapter` that fails on demand."""

    def __init__(
        self,
        *,
        source_id: str = "stub",
        mode: str = "ok",
        records: Sequence[RawRecord] = (),
        available_generations: Sequence[int] = (1,),
        gap_seconds: float = 0.05,
        fail_after: int = 0,
        upstream_status: int = 503,
    ) -> None:
        if mode not in FAULT_MODES:
            raise ValueError(f"unknown fault mode {mode!r}; expected one of {FAULT_MODES}")
        self.source_id = source_id
        self.mode = mode
        self.records = tuple(records)
        self.available_generations = tuple(available_generations)
        self.gap_seconds = gap_seconds
        self.fail_after = fail_after
        self.upstream_status = upstream_status
        #: Records actually handed over, so a test can assert a *partial* read.
        #:
        #: Named for the read it counts, not for "emit". `emit` is a write-shaped
        #: verb on `recon.adapters.base.WRITE_NAME_TOKENS`, which matches on
        #: *substrings*, so an attribute called `emitted` would be read by the
        #: structural no-write check as a write on a source. It is not one -- it
        #: counts records this adapter gave to its caller -- and keeping the two
        #: apart is what let the token be listed at all.
        self.records_handed_over = 0

    def generations(self) -> list[int]:
        return list(self.available_generations)

    def read(self, generation: int) -> Iterator[RawRecord]:
        if generation not in self.available_generations:
            raise AdapterError(
                "source_missing",
                f"generation {generation} is not available from {self.source_id}",
                source_id=self.source_id,
                generation=generation,
            )
        if self.mode == "hang":
            while True:
                time.sleep(0.05)
        if self.mode == "slow_drip":
            index = 0
            while True:
                time.sleep(self.gap_seconds)
                record = self.records[index % len(self.records)] if self.records else None
                if record is None:
                    continue
                self.records_handed_over += 1
                index += 1
                yield record
        for index, record in enumerate(self.records):
            if self.mode in {"midstream_error", "http_5xx"} and index >= self.fail_after:
                if self.mode == "http_5xx":
                    raise SourceUnavailable(
                        f"{self.source_id} returned {self.upstream_status}",
                        upstream_status=self.upstream_status,
                    )
                raise RuntimeError(f"{self.source_id} broke after {index} records")
            self.records_handed_over += 1
            yield record
        if self.mode in {"midstream_error", "http_5xx"} and self.fail_after >= len(self.records):
            if self.mode == "http_5xx":
                raise SourceUnavailable(
                    f"{self.source_id} returned {self.upstream_status}",
                    upstream_status=self.upstream_status,
                )
            raise RuntimeError(f"{self.source_id} broke at end of stream")
