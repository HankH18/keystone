"""SS8 -- writing the tree, byte-identically, every time.

One encoder, used for every file:

    json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":"))

plus a trailing newline. JSONL lines are individually sorted-key encoded and every
file is emitted in a fixed, sorted record order, so two runs at the same seed produce
an identical tree down to the byte (`G30`). `ensure_ascii=True` matters more than it
looks: A.3 sprinkles curly quotes and accents through the corpus, and a non-ASCII byte
would otherwise make the tree depend on the writer's locale.

`fixtures/manifest.json` carries the per-file `sha256` and the expected gen-N record
count per `(source, entity_type)` -- the input SS5.3's completeness ledger is stamped
from, and what lets an absence rule tell "this source is incomplete" (`unchecked`)
apart from "this record was deleted" (a conflict).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

__all__ = [
    "PRESERVED_NAMES",
    "dumps",
    "reset_directory",
    "write_json",
    "write_jsonl",
    "write_tree",
]


def dumps(obj: Any) -> str:
    """The single pinned encoder (SS8). No other JSON call in the package."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def write_json(path: Path, obj: Any) -> tuple[str, int]:
    """Write one JSON document; return `(sha256, bytes)`."""
    payload = f"{dumps(obj)}\n".encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest(), len(payload)


def write_jsonl(path: Path, records: Sequence[Mapping[str, Any]]) -> tuple[str, int]:
    """Write one JSONL file in the given (already sorted) record order."""
    payload = "".join(f"{dumps(record)}\n" for record in records).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest(), len(payload)


#: Repo markers that live inside `fixtures/` and `golden/` and are NOT generated.
#: `.gitignore` even carries an explicit `!fixtures/.gitkeep` un-ignore to keep the
#: first of these; a wholesale `rmtree` deleted it on every run, so the generator
#: destroyed tracked repository content as a side effect of emitting its own output.
PRESERVED_NAMES: tuple[str, ...] = (".gitkeep", ".gitignore")


def reset_directory(path: Path) -> None:
    """Clear a generated tree, preserving the repo markers the generator does not own.

    Everything the *generator* may have written is removed -- a shrinking dataset must
    never leave a stale file behind, and `test_repeated_run_into_a_dirty_directory_is_
    still_identical` binds that. `PRESERVED_NAMES` are read back and rewritten verbatim,
    so a git-tracked `.gitkeep` survives a run instead of being deleted by it.
    """
    preserved: dict[str, bytes] = {}
    if path.exists():
        for name in PRESERVED_NAMES:
            candidate = path / name
            if candidate.is_file():
                preserved[name] = candidate.read_bytes()
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    for name, payload in sorted(preserved.items()):
        (path / name).write_bytes(payload)


def tree_digest(paths: Iterable[Path], root: Path) -> str:
    """One digest over an entire tree: `sha256` of `relpath + sha256(file)` lines."""
    lines = []
    for path in sorted(paths):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{path.relative_to(root).as_posix()} {digest}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def write_tree(
    root: Path,
    files: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """Write every JSONL fixture under `root`, returning the manifest's `files` block."""
    manifest: dict[str, dict[str, Any]] = {}
    for relative, records in sorted(files.items()):
        digest, size = write_jsonl(root / relative, records)
        manifest[relative] = {"sha256": digest, "bytes": size, "records": len(records)}
    return manifest
