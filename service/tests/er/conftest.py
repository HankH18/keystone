"""Fixtures for the entity-resolution suite (T-5).

The dataset itself is built by `tests.er.dataset`, once per process; these
fixtures only hand it out. Reads are made through the `DATABASE_URL` principal
(the schema owner) because they are assertions *about* what the pipeline wrote --
every write under test went through `recon_writer`, which is the boundary that
matters.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine

from tests.er.dataset import GOLDEN, Dataset, ensure_dataset, ensure_history_dataset


@pytest.fixture(scope="session")
def dataset() -> Dataset:
    """The generation-3 fixture tree, ingested and materialized."""
    return ensure_dataset()


@pytest.fixture(scope="session")
def history_dataset() -> Dataset:
    """A dev-profile tree with all three generations and lineage 1-3."""
    return ensure_history_dataset()


@pytest.fixture(scope="session")
def reader(dataset: Dataset) -> Iterator[Engine]:
    """Read-only engine on the materialized database."""
    from recon.db import get_engine

    yield get_engine()


@pytest.fixture(scope="session")
def history_reader(history_dataset: Dataset) -> Iterator[Engine]:
    """Read-only engine on the three-generation database."""
    engine = create_engine(history_dataset.dsn.replace("postgresql://", "postgresql+psycopg://"))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def expected_views() -> list[dict[str, Any]]:
    """`golden/expected-views.json` -- the committed, hand-checkable join contract."""
    views = json.loads((GOLDEN / "expected-views.json").read_text())
    assert len(views) >= 25, "SS8 requires at least 25 hand-checkable entity views"
    return views
