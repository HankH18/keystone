"""The two PRODUCTION callers of the clusterer, exercised as production runs them.

Why this file exists
--------------------
Every other test in this package calls :func:`recon.incidents.cluster_conflicts`
directly, and all of them were green during the commit in which the feature was
**unreachable**: `cluster_conflicts` had no call site outside `tests/incidents/`,
`recon.suite.pipeline` truncated `conflict_incidents` on every graded pass, and
`GET /api/incidents` served `{"items": [], "total": 0}` on the documented path
for ever. A green suite proved the function worked; nothing proved anything ran
it.

So these tests do not import the function. They drive the two callers:

* `python -m recon.incidents` in a **subprocess**, which is the only way to test
  a `__main__` block, an argparse contract and a process's stdout for real;
* `recon.suite.pipeline._cluster_incidents`, which is step 9 of the graded pass.

They live in `tests/incidents/` rather than `tests/suite/` because what they
grade is the reachability of this feature, and because the pipeline's own tests
build a `PipelineRun` by hand rather than running one.

Live database, no stubs. The subprocess gets this session's `DATABASE_URL` and
writes real rows through `recon_writer`.

**Nothing here pins a cluster COUNT.** The first cut did -- "3,050 conflicts ->
38 incidents" -- and it passed alone and failed in a full-suite run, because
`tests/er/scratchdb.use_database` repoints `DATABASE_URL` process-wide and
`tests/apply` calls it before `tests/incidents` is collected. The database this
package then sees is a legitimate one with 25 conflicts carrying
`oscillating = false` where the graded set has `true`, and `descriptor()` reads
that flag, so 33 incidents there is correct and 38 would have been wrong. The
counts belong to the committed golden file, and `test_golden_counts.py` pins
them there with no database at all. What is left here is **reachability**: that
a real caller ran, wrote what it said it wrote, and reported it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from recon.budget import DAILY_SCOPE, PriceTable
from recon.db import get_engine
from recon.incidents import (
    DEFAULT_THRESHOLD,
    MOCK_EMBEDDING_MODEL,
    MockEmbeddingProvider,
    cluster_vectors,
    descriptor,
    load_conflicts,
)

SERVICE_ROOT = Path(__file__).resolve().parents[2]


def _run_cli(
    *args: str, dsn: str, unset_daily_scope: bool = False
) -> subprocess.CompletedProcess[str]:
    """`python -m recon.incidents` as its own process, with a clean environment.

    ``PYTEST_CURRENT_TEST`` is **deleted**, deliberately. `recon.budget` refuses
    the real ``daily`` scope while that variable is set
    (:class:`~recon.budget.RealDailyScopeRefused`), and an operator's CLI run
    does not have it -- so leaving it in would test a path production never
    takes. ``KEYSTONE_DAILY_SCOPE`` is pointed at a throwaway row instead, which
    is how `recon.suite.burst` and the graded pipeline keep a harness off the
    day's real budget without dropping the cap.

    ``unset_daily_scope`` removes even that, which is the ONLY configuration a
    real operator has: no pytest variable and nothing saying where the daily cap
    lives. It exists because with both guards in place this helper could not
    reach the production ``daily`` row at all, so no test using it could ever
    have caught the CLI charging it -- and the CLI *was* charging it. See
    :func:`test_an_operator_run_does_not_touch_the_production_daily_scope`.
    """
    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    if unset_daily_scope:
        env.pop("KEYSTONE_DAILY_SCOPE", None)
    env["DATABASE_URL"] = dsn
    env["KEYSTONE_REQUIRE_DB"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "recon.incidents", *args],
        cwd=SERVICE_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.fixture
def cli_daily_scope(ledger_scope: str) -> str:
    """Point the mandated daily cap at this test's own ledger row, for the child.

    ``ledger_scope`` already sets ``KEYSTONE_DAILY_SCOPE`` in this process; the
    subprocess inherits it through ``os.environ``. Named as its own fixture so a
    reader of the tests below sees that the child is capped and where.
    """
    return ledger_scope


def test_the_cli_clusters_for_real_and_prints_machine_readable_json(
    golden_conflict_ids: list[int],
    embedding_prices: PriceTable,
    cli_daily_scope: str,
    configured_url: str,
    owner_engine: Engine,
) -> None:
    """The documented operator path, end to end, in a process pytest does not own.

    Asserted on the DATABASE, not on the return value: the rows are what
    `GET /api/incidents` serves, and the exit code is what a cron would read.
    """
    before = _incident_count(owner_engine)
    expected_conflicts, expected_incidents = _expected(owner_engine)
    assert expected_conflicts >= 3050, "the golden fixture did not land"

    result = _run_cli(dsn=configured_url)
    assert result.returncode == 0, f"stderr:\n{result.stderr}"

    body = json.loads(result.stdout)
    assert body["embedding_model"] == MOCK_EMBEDDING_MODEL
    assert body["conflicts"] == expected_conflicts, (
        "the CLI clustered a different population than the one in the database"
    )
    # Recomputed in THIS process from the same rows rather than compared to a
    # literal -- see the module docstring for the database this may be pointed
    # at. What it binds is that the subprocess clustered these conflicts, not
    # that some fixed number came out.
    assert body["incidents"] == expected_incidents
    assert body["incidents"] > 14, (
        "fewer incidents than there are conflict types would mean the clustering "
        "is coarser than a GROUP BY type"
    )
    assert len(body["labels"]) == len(body["sizes"]) == body["incidents"]
    assert len(set(body["labels"])) == body["incidents"], "two incidents read identically"
    assert sum(body["sizes"]) == body["conflicts"], "the incidents must partition the conflicts"

    after = _incident_count(owner_engine)
    assert after == before + body["incidents"], (
        "the CLI reported incidents it did not write; a run that prints a summary "
        "and leaves no rows is the failure mode this test exists for"
    )
    _drop_scope(owner_engine, f"run:{body['run_id']}")


def test_the_cli_prints_nothing_but_json_on_stdout(
    golden_conflict_ids: list[int],
    embedding_prices: PriceTable,
    cli_daily_scope: str,
    configured_url: str,
    owner_engine: Engine,
) -> None:
    """`python -m recon.incidents | jq` must work.

    Not cosmetic. The first cut of :func:`recon.incidents.main` did not call
    `recon.logging.configure_logging_once`, so structlog ran its DEFAULT chain --
    which writes to **stdout** and, much worse, **redacts nothing**. Both halves
    are asserted: one line on stdout, and log events on stderr where they belong.
    """
    result = _run_cli(dsn=configured_url)
    assert result.returncode == 0, f"stderr:\n{result.stderr}"
    assert len(result.stdout.strip().splitlines()) == 1, (
        f"stdout is not a single JSON document:\n{result.stdout[:2000]}"
    )
    body = json.loads(result.stdout)
    assert "budget.reserved" in result.stderr, (
        "the reserve/settle events did not reach stderr, so either the ledger was "
        "not exercised or the logging chain is not installed"
    )
    # The child provisions `run:<its own generated id>` as ops, and the
    # `ledger_scope` fixture tears down only the scopes IT created -- so without
    # this every CLI test would leave a ledger row and 24 reservations on the
    # database permanently. Observed: two orphaned scopes per full run.
    _drop_scope(owner_engine, f"run:{body['run_id']}")


@pytest.mark.parametrize("status", [None, "dismissed"])
def test_the_cli_refuses_a_misconfigured_provider_however_little_work_there_is(
    configured_url: str,
    monkeypatch: pytest.MonkeyPatch,
    status: str | None,
) -> None:
    """A misconfiguration exits non-zero with one readable line. No database rows.

    `EXIT_REFUSED` is 1 and argparse's usage error is 2, so a wrapper can tell
    "the service said no" from "you typed it wrong".

    The `dismissed` parametrisation is the one that matters, and it caught a real
    defect: `cluster_conflicts` returns early when the selected population is
    empty and therefore never builds a provider, so a typo'd
    `EMBEDDING_PROVIDER` used to exit **0** with a clean empty summary. The
    early return is correct -- an empty run must not reserve money -- so the fix
    was to build the provider in `main` before anything else. How much work
    there is must not decide whether a misconfiguration is reported.
    """
    monkeypatch.setenv("EMBEDDING_PROVIDER", "word2vec")
    args = () if status is None else ("--status", status)
    result = _run_cli(*args, dsn=configured_url)
    assert result.returncode == 1
    assert "EmbeddingProviderNotConfigured" in result.stderr
    assert "word2vec" in result.stderr
    assert result.stdout.strip() == ""
    assert "Traceback" not in result.stderr


def test_a_replayed_run_id_is_refused_rather_than_paying_twice(
    golden_conflict_ids: list[int],
    embedding_prices: PriceTable,
    cli_daily_scope: str,
    configured_url: str,
) -> None:
    """Two CLI runs under one `--run-id` must not buy the same embeddings twice."""
    run_id = cli_daily_scope.removeprefix("run:")
    first = _run_cli("--run-id", run_id, dsn=configured_url)
    assert first.returncode == 0, f"stderr:\n{first.stderr}"
    second = _run_cli("--run-id", run_id, dsn=configured_url)
    assert second.returncode == 1
    assert "EmbeddingBudgetReplayed" in second.stderr


def test_an_operator_run_does_not_touch_the_production_daily_scope(
    golden_conflict_ids: list[int],
    embedding_prices: PriceTable,
    configured_url: str,
    owner_engine: Engine,
) -> None:
    """The invocation README documents, in the environment an operator has.

    Measured before the fix, on a database loaded with the golden set: one bare
    ``python -m recon.incidents`` put **56,487 microusd and 24 reservation rows**
    on the shared ``daily`` ledger row. Nothing rolls that row, so the seeded
    5 USD cap is a lifetime budget ~88 hand runs exhaust; and the rows left
    behind turn ``tests/budget/test_ledger.py::
    test_a_test_process_cannot_touch_the_real_daily_scope`` red on that database
    for ever, because its guard is "no row on ``daily``, from anything".

    This is the one test in the package that runs the child with **no**
    ``KEYSTONE_DAILY_SCOPE``, which is why it can go red at all: every other CLI
    test inherits the throwaway row the ``ledger_scope`` fixture sets, and a
    child that is already pointed away from ``daily`` cannot demonstrate that the
    code points itself away.

    It asserts both directions -- the shared row is untouched AND the run's own
    ops-provisioned row carries the whole charge -- because "spent nothing
    anywhere" would also satisfy the first half, and that would mean the pass
    never metered anything.
    """
    daily_before = _spent(owner_engine, DAILY_SCOPE)
    daily_rows_before = _reservations(owner_engine, DAILY_SCOPE)

    result = _run_cli(dsn=configured_url, unset_daily_scope=True)
    assert result.returncode == 0, f"stderr:\n{result.stderr}"
    body = json.loads(result.stdout)

    # The LEDGER first, then what the CLI said about it. A regression here is a
    # charge on a real row, and the test that advertises that property must die
    # on the charge rather than on a JSON field that merely describes it.
    assert _spent(owner_engine, DAILY_SCOPE) == daily_before, (
        "the documented operator command spent the deployment's shared daily budget"
    )
    assert _reservations(owner_engine, DAILY_SCOPE) == daily_rows_before, (
        "the documented operator command left reservation rows on the production "
        "daily scope, which reds tests/budget/test_ledger.py on this database"
    )
    own_scope = f"run:{body['run_id']}"
    assert body["daily_cap_scope"] == own_scope, (
        "the pass reported the mandated daily cap on a row that is not its own"
    )
    spent = _spent(owner_engine, own_scope)
    assert spent > 0, (
        "nothing was charged anywhere, so the daily row being clean is not evidence "
        "that the cap was moved -- it would be evidence that nothing was metered"
    )
    assert body["incidents"] > 14, "the run that spent that money wrote no clusters"
    _drop_scope(owner_engine, own_scope)


def test_charging_the_daily_cap_is_opt_in_and_names_the_shared_row(
    golden_conflict_ids: list[int],
    embedding_prices: PriceTable,
    configured_url: str,
    owner_engine: Engine,
) -> None:
    """``--charge-daily-cap`` hands R17's mandated cap back to the shared row.

    Run with **no** ``KEYSTONE_DAILY_SCOPE``, because that is the only environment
    in which the flag changes anything: with a configured row set,
    :func:`recon.incidents._daily_cap_for` yields it on both branches, so a test
    that sets one passes whether or not the flag is honoured. ``--status`` names a
    value outside the ``conflict_status`` enum, so the population is empty on any
    database and the pass reports which row it named without reserving a microusd
    on it -- which is what lets this run in the operator's environment at all.
    """
    daily_before = _spent(owner_engine, DAILY_SCOPE)
    daily_rows_before = _reservations(owner_engine, DAILY_SCOPE)

    result = _run_cli(
        "--charge-daily-cap",
        "--status",
        "not-a-conflict-status",
        dsn=configured_url,
        unset_daily_scope=True,
    )
    assert result.returncode == 0, f"stderr:\n{result.stderr}"
    body = json.loads(result.stdout)

    assert body["conflicts"] == 0, (
        "--status selected rows, so this pass could have metered the shared daily row"
    )
    assert body["daily_cap_scope"] == DAILY_SCOPE, (
        "--charge-daily-cap must name the shared daily row; it reported "
        f"{body['daily_cap_scope']!r}"
    )
    assert _spent(owner_engine, DAILY_SCOPE) == daily_before
    assert _reservations(owner_engine, DAILY_SCOPE) == daily_rows_before
    _drop_scope(owner_engine, f"run:{body['run_id']}")


# ---------------------------------------------------------------------------
# the graded pass's step 9
# ---------------------------------------------------------------------------
def test_the_graded_pipeline_stage_regenerates_the_incidents_it_truncates(
    golden_conflict_ids: list[int],
    embedding_prices: PriceTable,
    owner_engine: Engine,
) -> None:
    """`recon.suite.pipeline._cluster_incidents` -- step 9 of the graded pass.

    This calls the stage directly. That `build_pipeline` still calls it is a
    separate claim and is asserted separately, by
    :func:`test_build_pipeline_still_calls_the_clustering_stage`.

    Two properties, and the second is the one that is easy to get wrong:

    1. it writes real incidents with real members;
    2. it does **not** touch the production ``daily`` ledger row. A graded pass
       that charged the day's real budget would exhaust a 5 USD cap in about 88
       runs and start failing the suite for a reason unrelated to the code under
       test, which is why the stage provisions and points at its own row.
    """
    from recon.suite.pipeline import INCIDENT_SCOPE_CAP_MICROUSD, _cluster_incidents

    # Read through `get_engine()`, not `owner_engine`: `_cluster_incidents` uses
    # the process's CURRENT `DATABASE_URL`, and another package may have
    # repointed it (see the module docstring). Asking the same engine the code
    # under test asks is what makes these counts comparable.
    engine = get_engine()
    daily_before = _spent(engine, DAILY_SCOPE)
    members_before = _member_count(engine)
    expected_conflicts, expected_incidents = _expected(engine)

    stage = _cluster_incidents()

    assert stage.ok, stage.error
    assert stage.conflicts == expected_conflicts
    assert stage.incidents == expected_incidents
    assert stage.incidents > 14
    assert stage.model == MOCK_EMBEDDING_MODEL
    assert _member_count(engine) == members_before + expected_conflicts
    assert _spent(engine, DAILY_SCOPE) == daily_before, (
        "the graded pass charged the production daily scope; it must charge the "
        f"harness row {stage.scope!r} instead"
    )
    spent = _spent(engine, stage.scope)
    assert 0 < spent <= INCIDENT_SCOPE_CAP_MICROUSD, (
        f"the stage spent {spent} against a cap of {INCIDENT_SCOPE_CAP_MICROUSD}"
    )
    # The env override is a process-wide mutation; the stage must put it back or
    # every later reservation in this process lands on the wrong row.
    assert os.environ.get("KEYSTONE_DAILY_SCOPE") in (None, ""), (
        "the clustering stage left KEYSTONE_DAILY_SCOPE set"
    )
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM budget_reservations WHERE scope = :s"), {"s": stage.scope})
        conn.execute(text("DELETE FROM budget_ledger WHERE scope = :s"), {"s": stage.scope})


def test_build_pipeline_still_calls_the_clustering_stage() -> None:
    """The stage's own call site, which the test above does not cover.

    `build_pipeline` has no caller in the test tree -- it is the whole graded pass
    and costs minutes -- so deleting step 9 from it left `tests/incidents tests/suite`
    at 143 passed while every graded run truncated `conflict_incidents` and
    regenerated nothing. That is the same unreachability this module was written for,
    one level up: the stage is reachable from this file and from nowhere else.

    Read off the AST rather than the source text, so a mention in a docstring or a
    comment cannot satisfy it, and the binding is the **wiring** and not just the
    name: the value `_cluster_incidents()` returns must be the `incidents=` argument
    of the `PipelineRun` `build_pipeline` builds. What this cannot see is a call that
    is present but unreachable at runtime; running the real pass is what would prove
    that, and the graded harness is where it is run.
    """
    import ast
    import inspect

    from recon.suite import pipeline

    tree = ast.parse(inspect.getsource(pipeline))
    node = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "build_pipeline"
        ),
        None,
    )
    assert node is not None, "recon.suite.pipeline no longer defines build_pipeline"

    staged = {
        target.id
        for statement in ast.walk(node)
        if isinstance(statement, ast.Assign)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Name)
        and statement.value.func.id == "_cluster_incidents"
        for target in statement.targets
        if isinstance(target, ast.Name)
    }
    assert staged, (
        "build_pipeline no longer calls _cluster_incidents: step 2 truncates "
        "`incidents`/`conflict_incidents` on every graded pass and step 9 is what "
        "regenerates them, so the endpoint would serve an empty batch again"
    )

    wired = [
        keyword.value.id
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "PipelineRun"
        for keyword in call.keywords
        if keyword.arg == "incidents" and isinstance(keyword.value, ast.Name)
    ]
    assert wired, "build_pipeline builds a PipelineRun without passing incidents="
    assert set(wired) <= staged, (
        f"build_pipeline reports incidents={wired} but _cluster_incidents()'s result "
        f"is bound to {sorted(staged)}: the stage runs and the run reports something "
        "else, which is what the scorecard's R25 note would then be about"
    )


def test_the_graded_layer_reset_names_both_incident_tables() -> None:
    """``incidents`` must be reset with its members, not left orphaned.

    ``conflict_incidents`` is emptied by the CASCADE off ``conflicts`` whatever
    the list says. ``incidents`` is not -- and leaving it made the previous
    pass's rows the newest batch with every member gone, so the endpoint answered
    "38 incidents, 0 conflicts in each". Observed on a real database before this
    entry was added.
    """
    from recon.suite.pipeline import GRADED_TABLES

    assert "incidents" in GRADED_TABLES
    assert "conflict_incidents" in GRADED_TABLES


# ---------------------------------------------------------------------------
def _expected(engine: Engine) -> tuple[int, int]:
    """`(conflicts, incidents)` for whatever `engine` currently holds.

    Computed in-process with the same `descriptor` / `cluster_vectors` the caller
    uses, so it does NOT re-verify the clustering arithmetic --
    `test_golden_counts.py` does that against the committed file. What it gives
    these tests is a moving target they can legitimately assert equality against:
    "the subprocess clustered THESE rows" rather than "some number came out".
    """
    with engine.connect() as conn:
        conflicts = load_conflicts(conn)
    provider = MockEmbeddingProvider()
    vectors = [provider._vector(descriptor(record)) for record in conflicts]
    return len(conflicts), len(cluster_vectors(vectors, threshold=DEFAULT_THRESHOLD))


def _incident_count(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM incidents")).scalar_one())


def _member_count(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM conflict_incidents")).scalar_one())


def _spent(engine: Engine, scope: str) -> int:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT spent_microusd FROM budget_ledger WHERE scope = :s"), {"s": scope}
        ).scalar()
    return int(row or 0)


def _reservations(engine: Engine, scope: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM budget_reservations WHERE scope = :s"), {"s": scope}
            ).scalar_one()
        )


def _drop_scope(engine: Engine, scope: str) -> None:
    """Remove a scope the CLI subprocess provisioned for itself.

    The ``ledger_scope`` fixture tears down only the scopes IT created; a child
    process that calls :func:`recon.budget.provision_run_scope` names its own,
    and nothing else will ever remove it. Owner principal -- ``recon_writer``
    holds no DELETE on either table, which is the point of the split.
    """
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM budget_reservations WHERE scope = :s"), {"s": scope})
        conn.execute(text("DELETE FROM budget_ledger WHERE scope = :s"), {"s": scope})
