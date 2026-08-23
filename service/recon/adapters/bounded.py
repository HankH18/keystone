"""Bounded reads (R3): a source may fail, but it may not hang a sync.

DESIGN pins "Timeouts bounded at 10s -> structured error". A single bound is not
enough, because two different failures look identical from the outside and only
one of them is a stall:

* a source that **stops** -- no first record, or no next record -- is caught by
  `stall_timeout` (the 10s DESIGN names);
* a source that **drips** -- one record every few seconds, forever -- never
  trips a stall bound at all, so the whole load also carries `deadline_seconds`.

Either bound raises `AdapterError` carrying `status` **and** `latency_ms`, which
is the pair R3 asks for: an error that says "we gave up" without saying how long
we waited cannot be acted on.

Mechanics. The adapter runs on a worker thread and hands records over a bounded
queue, so the consumer's clock is independent of the source's behaviour: a
`read()` that blocks inside `socket.recv` or `time.sleep` cannot stop the reader
from timing out, which is precisely what makes the bound wall-clock real rather
than cooperative. The queue is small, so a fast source does not race ahead
building an unbounded buffer.

**Honest limitation.** CPython cannot kill a thread. On timeout the reader stops
consuming, sets a stop flag the pump checks between records, and returns; a source
wedged inside a single uninterruptible call leaks that one daemon thread for as
long as it stays wedged. What R3 requires -- and what this delivers -- is that the
*sync* is bounded and returns a structured error; the leaked thread is a daemon,
holds no lock the pipeline needs, and dies with the process.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Iterator

from recon.adapters.base import (
    ADAPTER_LOAD_DEADLINE_SECONDS,
    ADAPTER_STALL_TIMEOUT_SECONDS,
    AdapterError,
    RawRecord,
    ReadOnlyAdapter,
    SourceUnavailable,
)

__all__ = ["read_bounded"]

_QUEUE_SIZE = 512
_PUT_POLL_SECONDS = 0.05


def _as_adapter_error(
    exc: BaseException,
    *,
    source_id: str,
    generation: int,
    latency_ms: float,
) -> AdapterError:
    if isinstance(exc, AdapterError):
        if exc.latency_ms is None:
            exc.latency_ms = latency_ms
        return exc
    if isinstance(exc, SourceUnavailable):
        return AdapterError(
            "source_unavailable",
            exc.detail,
            source_id=source_id,
            generation=generation,
            latency_ms=latency_ms,
            upstream_status=exc.upstream_status,
        )
    return AdapterError(
        "source_error",
        f"{type(exc).__name__}: {exc}",
        source_id=source_id,
        generation=generation,
        latency_ms=latency_ms,
    )


def read_bounded(
    adapter: ReadOnlyAdapter,
    generation: int,
    *,
    stall_timeout: float = ADAPTER_STALL_TIMEOUT_SECONDS,
    deadline_seconds: float | None = ADAPTER_LOAD_DEADLINE_SECONDS,
) -> Iterator[RawRecord]:
    """Iterate `adapter.read(generation)` under a stall bound and a total deadline.

    Raises `AdapterError` -- never a bare exception, never a 500 -- for a stall, a
    blown deadline, an upstream 5xx, or any exception the source raises mid-stream.
    """
    source_id = getattr(adapter, "source_id", "unknown")
    items: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=_QUEUE_SIZE)
    stop = threading.Event()
    started = time.monotonic()

    def pump() -> None:
        try:
            for record in adapter.read(generation):
                while not stop.is_set():
                    try:
                        items.put(("item", record), timeout=_PUT_POLL_SECONDS)
                        break
                    except queue.Full:
                        continue
                if stop.is_set():
                    return
            _offer(("end", None))
        except BaseException as exc:  # every failure becomes a structured AdapterError
            _offer(("error", exc))

    def _offer(message: tuple[str, object]) -> None:
        while not stop.is_set():
            try:
                items.put(message, timeout=_PUT_POLL_SECONDS)
                return
            except queue.Full:
                continue

    worker = threading.Thread(
        target=pump, name=f"adapter-read-{source_id}-gen{generation}", daemon=True
    )
    worker.start()

    try:
        while True:
            elapsed = time.monotonic() - started
            wait = stall_timeout
            if deadline_seconds is not None:
                wait = min(wait, deadline_seconds - elapsed)
            if wait <= 0:
                raise AdapterError(
                    "source_timeout",
                    f"load exceeded its {deadline_seconds:g}s deadline",
                    source_id=source_id,
                    generation=generation,
                    latency_ms=elapsed * 1000.0,
                )
            try:
                kind, payload = items.get(timeout=wait)
            except queue.Empty:
                elapsed = time.monotonic() - started
                blown = deadline_seconds is not None and elapsed >= deadline_seconds
                detail = (
                    f"load exceeded its {deadline_seconds:g}s deadline"
                    if blown
                    else f"source produced no record for {stall_timeout:g}s"
                )
                raise AdapterError(
                    "source_timeout",
                    detail,
                    source_id=source_id,
                    generation=generation,
                    latency_ms=elapsed * 1000.0,
                ) from None

            if kind == "item":
                yield payload  # type: ignore[misc]
                continue
            if kind == "end":
                return
            raise _as_adapter_error(
                payload,  # type: ignore[arg-type]
                source_id=source_id,
                generation=generation,
                latency_ms=(time.monotonic() - started) * 1000.0,
            )
    finally:
        stop.set()
