"""A principal switch has to move **every** DSN variable, not just ``DATABASE_URL``.

The defect this package binds: ``recon.budget.ops_engine`` and
``recon.api.internal._invariant_dsn`` both *prefer* ``OPS_DATABASE_URL`` whenever
it is set and fall back to ``DATABASE_URL`` only when it is not. Three helpers
repointed the process at another database -- the spend burst's ``_connected_as``
and the two scratch-database fixtures -- and two of them moved ``DATABASE_URL``
alone. On a shell configured like the deployed web service (``infra/render.yaml``
sets ``OPS_DATABASE_URL`` to the production owner DSN) that is not isolation, it
is a cross-database write: one run of ``tests/integration/test_sync_pipeline.py``
put 752,000 ``invariant_results`` rows and three ``budget_ledger`` scopes into a
database the run neither created nor drops.

The three now share :func:`recon.db.connected_to`. This module is the binding:
it pins the variable list, the switch, the exact restore, and each of the three
call sites against the environment they actually leave behind. It lives in
``tests/budget`` because ``OPS_DATABASE_URL`` is the ops principal's variable and
:func:`recon.budget.ops_engine` is what reads it.

Nothing here opens a connection: every DSN below names an unroutable host, and
both engines are built lazily, so what is asserted is the resolution -- which is
the whole defect.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from recon.budget import OPS_DATABASE_URL_ENV, ops_engine
from recon.db import (
    PRINCIPAL_ENV_VARS,
    connected_to,
    reset_engine_cache,
    restore_principal,
    switch_principal,
)

#: Three distinct unroutable DSNs: the process's own, an ops principal that is
#: NOT the same database, and the one a helper switches to.
AMBIENT = "postgresql://writer:pw@ambient.invalid:5432/ambient_db"
AMBIENT_OPS = "postgresql://owner:pw@ambient.invalid:5432/ambient_ops_db"
SCRATCH = "postgresql://owner:pw@scratch.invalid:5432/scratch_db"


@pytest.fixture(autouse=True)
def _no_engine_survives(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Own both variables through ``monkeypatch`` and leave no cached engine.

    The code under test writes to ``os.environ`` directly. Touching both
    variables here first makes ``monkeypatch`` responsible for restoring
    whatever the surrounding session was configured with, whether the tests
    below leave them set, blank or absent.
    """
    monkeypatch.setenv("DATABASE_URL", AMBIENT)
    monkeypatch.setenv(OPS_DATABASE_URL_ENV, AMBIENT_OPS)
    reset_engine_cache()
    try:
        yield
    finally:
        reset_engine_cache()


def _database(url: object) -> str:
    return str(url).rsplit("/", 1)[-1]


# ===========================================================================
# the variable list
# ===========================================================================
def test_the_principal_variables_are_exactly_the_two_that_name_a_principal() -> None:
    """``recon.db`` spells ``OPS_DATABASE_URL`` rather than importing it.

    ``recon.budget`` imports ``recon.db``, so the constant cannot travel the
    other way without a cycle. This assertion is what keeps the duplicate
    honest: rename the budget-side constant and this goes red.
    """
    assert PRINCIPAL_ENV_VARS == ("DATABASE_URL", OPS_DATABASE_URL_ENV)


# ===========================================================================
# the primitive
# ===========================================================================
def test_switching_moves_every_principal_variable() -> None:
    """A switch that leaves ``OPS_DATABASE_URL`` behind is not a switch."""
    switch_principal(SCRATCH)

    assert os.environ["DATABASE_URL"] == SCRATCH
    assert os.environ[OPS_DATABASE_URL_ENV] == SCRATCH
    assert _database(ops_engine().url) == "scratch_db"


def test_switching_returns_the_environment_it_replaced() -> None:
    previous = switch_principal(SCRATCH)

    assert previous == {"DATABASE_URL": AMBIENT, OPS_DATABASE_URL_ENV: AMBIENT_OPS}


def test_restoring_puts_back_a_different_ops_principal() -> None:
    """The two variables are restored independently, not to one shared value."""
    previous = switch_principal(SCRATCH)
    restore_principal(previous)

    assert os.environ["DATABASE_URL"] == AMBIENT
    assert os.environ[OPS_DATABASE_URL_ENV] == AMBIENT_OPS
    assert _database(ops_engine().url) == "ambient_ops_db"


def test_a_variable_that_was_unset_is_restored_to_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Popped, not blanked: "restored" has to mean restored."""
    monkeypatch.delenv(OPS_DATABASE_URL_ENV)

    previous = switch_principal(SCRATCH)
    assert os.environ[OPS_DATABASE_URL_ENV] == SCRATCH
    restore_principal(previous)

    assert OPS_DATABASE_URL_ENV not in os.environ
    assert _database(ops_engine().url) == "ambient_db"


def test_the_context_manager_switches_and_restores_exactly() -> None:
    with connected_to(SCRATCH) as active:
        assert active == SCRATCH
        assert _database(ops_engine().url) == "scratch_db"

    assert os.environ["DATABASE_URL"] == AMBIENT
    assert os.environ[OPS_DATABASE_URL_ENV] == AMBIENT_OPS


def test_the_context_manager_restores_after_a_failure() -> None:
    with pytest.raises(RuntimeError), connected_to(SCRATCH):
        raise RuntimeError("the block failed")

    assert os.environ["DATABASE_URL"] == AMBIENT
    assert os.environ[OPS_DATABASE_URL_ENV] == AMBIENT_OPS


# ===========================================================================
# the three call sites
# ===========================================================================
def test_the_burst_connects_the_ops_calls_as_the_role_it_names() -> None:
    """``_connected_as`` is the harness that proves the sweeper's guard fires.

    With ``DATABASE_URL`` alone it could not: ``ops_engine`` stayed on the
    ambient ops DSN, the sweep ran as the owner and the graded row came out
    ``sweep_as_capped=refused:False/open:0`` -- red, and unobtainable, wherever
    ``OPS_DATABASE_URL`` is set.
    """
    from recon.db import ROLE_RECON_WRITER
    from recon.suite.burst import _connected_as

    with _connected_as(ROLE_RECON_WRITER):
        assert ops_engine().url.username == ROLE_RECON_WRITER

    assert os.environ[OPS_DATABASE_URL_ENV] == AMBIENT_OPS


def test_the_invariants_scratch_helper_moves_the_ops_principal_too() -> None:
    from tests.invariants.scratchdb import use_database

    with use_database(SCRATCH):
        assert _database(ops_engine().url) == "scratch_db"

    assert os.environ["DATABASE_URL"] == AMBIENT
    assert os.environ[OPS_DATABASE_URL_ENV] == AMBIENT_OPS


def test_the_er_scratch_helper_moves_the_ops_principal_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live one: ``tests/integration`` and ``tests/suite`` drive real ops calls.

    It is not a context manager -- its callers hold the process on a scratch
    database for a whole session -- so the ambient snapshot is what makes the
    hand-back an exact restore, and it is reset here so this test cannot leave
    the suites that share the module holding a snapshot of an invented DSN.
    """
    from tests.er import scratchdb

    monkeypatch.setattr(scratchdb, "_AMBIENT", None)

    scratchdb.use_database(SCRATCH)
    assert os.environ["DATABASE_URL"] == SCRATCH
    assert _database(ops_engine().url) == "scratch_db"

    scratchdb.use_database(AMBIENT)  # how every caller hands the process back
    assert os.environ["DATABASE_URL"] == AMBIENT
    assert os.environ[OPS_DATABASE_URL_ENV] == AMBIENT_OPS
    assert _database(ops_engine().url) == "ambient_ops_db"


def test_the_er_scratch_helper_restores_an_ops_variable_that_was_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.er import scratchdb

    monkeypatch.setattr(scratchdb, "_AMBIENT", None)
    monkeypatch.delenv(OPS_DATABASE_URL_ENV)

    scratchdb.use_database(SCRATCH)
    assert os.environ[OPS_DATABASE_URL_ENV] == SCRATCH

    scratchdb.use_database(AMBIENT)
    assert OPS_DATABASE_URL_ENV not in os.environ
