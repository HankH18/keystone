"""Read-only source adapters (R1).

No adapter may expose a write method: `ReadOnlyAdapter` has none, and
`tests/ingest/test_read_only_port.py` proves it structurally over every class
exported here.
"""

from recon.adapters.base import (
    ADAPTER_LOAD_DEADLINE_SECONDS,
    ADAPTER_STALL_TIMEOUT_SECONDS,
    KIND_STATUS,
    MAX_JSON_DEPTH,
    MAX_PAYLOAD_BYTES,
    WRITE_NAME_TOKENS,
    AdapterError,
    RawRecord,
    ReadOnlyAdapter,
    SourceUnavailable,
    canonical_json,
    row_hash,
)
from recon.adapters.bounded import read_bounded
from recon.adapters.faults import FAULT_MODES, FaultInjectingAdapter, stub_records
from recon.adapters.identifiers import (
    IDENTIFIER_MAX_LENGTH,
    IDENTIFIER_RULE,
    IdentifierError,
    identifier_fault,
    validate_identifier,
)
from recon.adapters.jsonl import (
    SOURCE_ADAPTERS,
    AppDbAdapter,
    CrmAdapter,
    JsonlSnapshotAdapter,
    PaymentsAdapter,
    build_adapters,
    default_fixtures_root,
)
from recon.adapters.models import (
    ENTITY_MODELS,
    INT32_MAX,
    INT32_MIN,
    MAX_AMOUNT_CENTS,
    PRIMARY_KEYS,
    SOURCE_ENTITY_TYPES,
    TIMESTAMP_FIELDS,
)
from recon.adapters.validation import (
    json_depth,
    non_finite_number,
    partition,
    scan_document,
    unstorable_text,
    validate_batch,
    validate_payload,
)

__all__ = [
    "ADAPTER_LOAD_DEADLINE_SECONDS",
    "ADAPTER_STALL_TIMEOUT_SECONDS",
    "ENTITY_MODELS",
    "FAULT_MODES",
    "IDENTIFIER_MAX_LENGTH",
    "IDENTIFIER_RULE",
    "INT32_MAX",
    "INT32_MIN",
    "KIND_STATUS",
    "MAX_AMOUNT_CENTS",
    "MAX_JSON_DEPTH",
    "MAX_PAYLOAD_BYTES",
    "PRIMARY_KEYS",
    "SOURCE_ADAPTERS",
    "SOURCE_ENTITY_TYPES",
    "TIMESTAMP_FIELDS",
    "WRITE_NAME_TOKENS",
    "AdapterError",
    "AppDbAdapter",
    "CrmAdapter",
    "FaultInjectingAdapter",
    "IdentifierError",
    "JsonlSnapshotAdapter",
    "PaymentsAdapter",
    "RawRecord",
    "ReadOnlyAdapter",
    "SourceUnavailable",
    "build_adapters",
    "canonical_json",
    "default_fixtures_root",
    "identifier_fault",
    "json_depth",
    "non_finite_number",
    "partition",
    "read_bounded",
    "row_hash",
    "scan_document",
    "stub_records",
    "unstorable_text",
    "validate_batch",
    "validate_identifier",
    "validate_payload",
]
