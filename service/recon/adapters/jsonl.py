"""The three fixture-backed sources (R1): CRM, app DB, payments.

Each is a `ReadOnlyAdapter` over a snapshot tree laid out as contract SS8 pins it::

    <root>/<source_id>/gen<N>/<entity_type>.jsonl

**The root is injected, never hardcoded.** `CrmAdapter(root)` takes the tree it
reads; `default_fixtures_root()` merely supplies the repository's own tree when a
caller has no opinion, and `KEYSTONE_FIXTURES_DIR` overrides that. That is what
makes the port swappable in the sense DESIGN means: a real HubSpot connector
implements `source_id` / `generations()` / `read()` against an HTTP client instead
of a directory, and every consumer in this package -- ingestion, `/health`, the
benchmark -- keeps working, because none of them names a file.

`generations()` is a **real listing** of the tree, not a constant: a source that
has only shipped two snapshots reports two, and asking for a third raises
`source_missing` rather than yielding nothing (an empty read is
indistinguishable from "the source is empty", which is exactly the false-negative
SS5.3 exists to prevent).

Rejections. `read()` follows DESIGN's signature -- "validated or raises
`AdapterError`". Set `on_reject` and the same iteration instead *reports* each
structural rejection to that sink and carries on with the rest of the snapshot,
which is what ingestion wants: a rejected row must be counted and logged, never
silently dropped, and one bad line in a 40,000-line snapshot must not discard the
other 39,999. Both behaviours refuse to skip silently; they differ only in whether
the first rejection ends the load.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

from recon.adapters.base import MAX_PAYLOAD_BYTES, AdapterError, RawRecord
from recon.adapters.models import SOURCE_ENTITY_TYPES
from recon.adapters.validation import validate_batch, validate_payload

__all__ = [
    "SOURCE_ADAPTERS",
    "AppDbAdapter",
    "CrmAdapter",
    "JsonlSnapshotAdapter",
    "PaymentsAdapter",
    "build_adapters",
    "default_fixtures_root",
]

_GEN_PREFIX = "gen"


def _physical_lines(text: str) -> list[str]:
    """The file's lines, **positionally faithful**, ready for `validate_batch`.

    `validate_batch` numbers its results `index + 1` and that number is reported to
    an operator as `line`. So the list handed to it must be the file's physical
    lines with nothing removed: dropping blank lines first (the obvious
    `if line` filter) shifts the reported line number of every rejection after the
    blank by one, which points an operator at the wrong physical line of a
    40,000-line snapshot -- and loses the blank line itself, unrejected and
    uncounted, which is the silent skip R2 forbids.

    **Decision: a blank interior line is REJECTED, not ignored.** It is not a
    record, so it cannot be landed; `json.loads("")` fails, so it earns the
    ordinary `unparseable_json` 400 with its true line number, is counted in
    `records_rejected` and is logged like any other structural rejection. The one
    thing that is *not* a line is the empty string after the file's final
    newline -- that is the terminator of the last record, and every well-formed
    JSONL file has one.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def default_fixtures_root() -> Path:
    """The repository's own `fixtures/` tree, or `KEYSTONE_FIXTURES_DIR`.

    A default, not a constant: every adapter still takes its root as an argument.
    """
    override = os.environ.get("KEYSTONE_FIXTURES_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "fixtures"


class JsonlSnapshotAdapter:
    """A read-only source backed by one JSONL snapshot tree.

    Subclasses pin `source_id` and `entity_types`; nothing else differs between
    the three sources, which is the point -- the port is the interface, not the
    per-source code.
    """

    source_id: str = ""
    entity_types: tuple[str, ...] = ()

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        source_id: str | None = None,
        entity_types: tuple[str, ...] | None = None,
        max_payload_bytes: int = MAX_PAYLOAD_BYTES,
        on_reject: Callable[[AdapterError], None] | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else default_fixtures_root()
        if source_id is not None:
            self.source_id = source_id
        if entity_types is not None:
            self.entity_types = entity_types
        if not self.source_id:
            raise ValueError("source_id is required")
        if not self.entity_types:
            self.entity_types = SOURCE_ENTITY_TYPES.get(self.source_id, ())
        if not self.entity_types:
            raise ValueError(f"no entity types known for source {self.source_id!r}")
        self.max_payload_bytes = max_payload_bytes
        self.on_reject = on_reject

    # -- the port -------------------------------------------------------------

    def generations(self) -> list[int]:
        """Snapshot generations present in the tree, ascending."""
        base = self.root / self.source_id
        if not base.is_dir():
            return []
        found: list[int] = []
        for child in base.iterdir():
            if not child.is_dir() or not child.name.startswith(_GEN_PREFIX):
                continue
            suffix = child.name[len(_GEN_PREFIX) :]
            if suffix.isdigit():
                found.append(int(suffix))
        return sorted(found)

    def read(self, generation: int) -> Iterator[RawRecord]:
        """Validated records for `generation`, one entity type after another."""
        for entity_type in self.entity_types:
            yield from self._read_entity(generation, entity_type)

    # -- reachability ---------------------------------------------------------

    def probe(self) -> dict[str, object]:
        """A real, cheap reachability check for `/health` -- not a hardcoded `ok`.

        It answers the question a health check is actually asked: *could a read
        start right now, and would the first thing it found be well-formed?* For
        every entity type of the newest generation it opens the snapshot and
        validates its **first** record through the same `validate_payload` the
        pipeline uses. Reading the whole 40,000-line file would give a slightly
        stronger answer and make `/health` cost a full parse per call, which is
        how health checks end up disabled.

        `ok` needs every entity type readable and valid; one bad or missing entity
        type is `degraded` (the source answers, but not completely); none readable
        is `down`.
        """
        generations = self.generations()
        if not generations:
            return {
                "status": "down",
                "detail": f"no generations under {self.root / self.source_id}",
                "generations": [],
            }
        latest = max(generations)
        entities: dict[str, object] = {}
        healthy = 0
        for entity_type in self.entity_types:
            path = self.path_for(latest, entity_type)
            if not path.is_file():
                entities[entity_type] = {"status": "down", "detail": f"missing {path.name}"}
                continue
            with path.open("r", encoding="utf-8") as handle:
                first = handle.readline().strip()
            if not first:
                entities[entity_type] = {"status": "down", "detail": "snapshot is empty"}
                continue
            try:
                record = validate_payload(
                    self.source_id,
                    entity_type,
                    latest,
                    first,
                    line_no=1,
                    max_payload_bytes=self.max_payload_bytes,
                )
            except AdapterError as error:
                entities[entity_type] = {
                    "status": "down",
                    "detail": error.detail,
                    "kind": error.kind,
                }
                continue
            healthy += 1
            entities[entity_type] = {"status": "ok", "sample_key": record.natural_key}

        if healthy == len(self.entity_types):
            status = "ok"
        elif healthy:
            status = "degraded"
        else:
            status = "down"
        return {
            "status": status,
            "generations": generations,
            "latest_generation": latest,
            "entities": entities,
        }

    # -- internals ------------------------------------------------------------

    def path_for(self, generation: int, entity_type: str) -> Path:
        return self.root / self.source_id / f"{_GEN_PREFIX}{generation}" / f"{entity_type}.jsonl"

    def _read_entity(self, generation: int, entity_type: str) -> Iterator[RawRecord]:
        path = self.path_for(generation, entity_type)
        if not path.is_file():
            raise AdapterError(
                "source_missing",
                f"no snapshot at {path}",
                source_id=self.source_id,
                entity_type=entity_type,
                generation=generation,
            )
        lines = _physical_lines(path.read_text(encoding="utf-8"))
        for result in validate_batch(
            self.source_id,
            entity_type,
            generation,
            lines,
            max_payload_bytes=self.max_payload_bytes,
        ):
            if isinstance(result, AdapterError):
                if self.on_reject is None:
                    raise result
                self.on_reject(result)
                continue
            yield result


class CrmAdapter(JsonlSnapshotAdapter):
    """HubSpot-shaped CRM: contacts and deals (SS1.1, SS1.2)."""

    source_id = "crm"
    entity_types = SOURCE_ENTITY_TYPES["crm"]


class AppDbAdapter(JsonlSnapshotAdapter):
    """The application's own Postgres: students and enrollments (SS1.3, SS1.4)."""

    source_id = "appdb"
    entity_types = SOURCE_ENTITY_TYPES["appdb"]


class PaymentsAdapter(JsonlSnapshotAdapter):
    """Stripe-shaped payments; a refund is a payment with `status='refunded'` (SS1.5)."""

    source_id = "payments"
    entity_types = SOURCE_ENTITY_TYPES["payments"]


#: Source id -> implementation. The registry ingestion and `/health` iterate.
SOURCE_ADAPTERS: dict[str, type[JsonlSnapshotAdapter]] = {
    "crm": CrmAdapter,
    "appdb": AppDbAdapter,
    "payments": PaymentsAdapter,
}


def build_adapters(
    root: Path | str | None = None,
    *,
    on_reject: Callable[[AdapterError], None] | None = None,
    max_payload_bytes: int = MAX_PAYLOAD_BYTES,
) -> dict[str, JsonlSnapshotAdapter]:
    """One adapter per source, all reading the same injected tree."""
    return {
        source_id: adapter_class(root, on_reject=on_reject, max_payload_bytes=max_payload_bytes)
        for source_id, adapter_class in SOURCE_ADAPTERS.items()
    }
