"""A database-less run must not be able to report success.

The failure this closes was not a bug in any test -- it was a bug in what green
*meant*. 76 of 81 tests skipped when ``DATABASE_URL`` was unset, and the
ticketed verify chain reported success anyway. **A green that proves nothing is
worse than a red**: red is information, and a green that is indistinguishable
from a real one is worse than no signal at all.

``KEYSTONE_REQUIRE_DB=1`` turns "no DATABASE_URL" from a skip into a hard
failure. That switch is itself load-bearing, so it is tested the only way that
proves anything: by running pytest in a subprocess with no database configured
and asserting the run **fails**, paired with a control run that shows the
ordinary skip behaviour is untouched.

These tests are deliberately about the harness rather than the schema, but they
live in this package because this package is what they govern.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.schema.conftest import REQUIRE_DB_ENV, database_is_required

#: A schema test that always skips/errors purely on database availability, so
#: the subprocess run is fast and its outcome depends on nothing else.
PROBE_TEST = "tests/schema/test_schema_shape.py::test_every_designed_table_exists"


def _pytest_without_a_database(service_root: Path, *, require: bool) -> subprocess.CompletedProcess:
    """Run one schema test with no usable DATABASE_URL.

    ``DATABASE_URL`` is set to the empty string rather than unset: an env var
    takes priority over any developer's local ``service/.env``, so the
    subprocess cannot accidentally find a database and make this test
    meaningless. Empty is falsy in ``recon.config``, which is what raises
    ``DatabaseNotConfigured``.
    """
    env = dict(os.environ)
    env["DATABASE_URL"] = ""
    env.pop(REQUIRE_DB_ENV, None)
    if require:
        env[REQUIRE_DB_ENV] = "1"

    return subprocess.run(
        # `-o addopts=` clears the project's `-q`, which would otherwise stack
        # with the `-q` below into `-qq` and suppress the summary line this
        # test reads. `-rs` makes the skip reason explicit in the output.
        [
            sys.executable,
            "-m",
            "pytest",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            "-q",
            "-rs",
            PROBE_TEST,
        ],
        cwd=service_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_without_the_flag_a_missing_database_still_skips(service_root: Path) -> None:
    """Control: a laptop with no docker stays usable, and says so."""
    run = _pytest_without_a_database(service_root, require=False)
    assert run.returncode == 0, f"expected a clean skip run:\n{run.stdout}\n{run.stderr}"
    assert "1 skipped" in run.stdout, run.stdout
    assert "DATABASE_URL is not set" in run.stdout, run.stdout


def test_with_the_flag_a_missing_database_is_a_hard_failure(service_root: Path) -> None:
    """The gate itself: KEYSTONE_REQUIRE_DB=1 makes a database-less run RED.

    Without this, the fix to conftest would be a comment. The subprocess is the
    real `pytest` entry point, so what is proved is what CI actually runs.
    """
    run = _pytest_without_a_database(service_root, require=True)
    assert run.returncode != 0, (
        "a run with KEYSTONE_REQUIRE_DB=1 and no DATABASE_URL exited 0, which is "
        f"exactly the green-that-proves-nothing this gate exists to stop:\n{run.stdout}"
    )
    # A session-fixture failure surfaces as a collection/setup ERROR, which is
    # precisely the "hard error, not a skip" outcome the gate is specified to
    # produce -- and it is impossible to mistake for a pass.
    assert "1 error" in run.stdout, run.stdout
    summary = run.stdout.strip().splitlines()[-1]
    assert "skipped" not in summary, (
        f"the test must ERROR, not skip, when {REQUIRE_DB_ENV} is set:\n{run.stdout}"
    )
    assert REQUIRE_DB_ENV in run.stdout, (
        f"the failure must name {REQUIRE_DB_ENV} so the cause is obvious:\n{run.stdout}"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("1", True, id="one"),
        pytest.param("true", True, id="true"),
        pytest.param("yes", True, id="yes"),
        pytest.param("", False, id="empty"),
        pytest.param("0", False, id="zero"),
        pytest.param("false", False, id="false"),
        pytest.param("no", False, id="no"),
        pytest.param("off", False, id="off"),
    ],
)
def test_the_flag_is_read_permissively(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    """A typo must err towards running the tests, never towards skipping them."""
    monkeypatch.setenv(REQUIRE_DB_ENV, value)
    assert database_is_required() is expected


def test_the_flag_is_absent_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(REQUIRE_DB_ENV, raising=False)
    assert database_is_required() is False


def test_ci_sets_the_flag_and_migrates_before_pytest(service_root: Path) -> None:
    """The workflow is the other half of the fix; assert it, do not assume it.

    Actions cannot be run locally, so the workflow is asserted structurally:
    the service job must export the flag, and it must run
    `alembic upgrade head` *before* pytest -- an unmigrated database makes
    `owner_engine` fail rather than skip, which is red but for the wrong reason.
    """
    import yaml  # dependency of the service extras; parsing proves the YAML is valid

    workflow = yaml.safe_load((service_root.parent / ".github/workflows/ci.yml").read_text())
    service_job = workflow["jobs"]["service"]

    assert service_job["env"].get(REQUIRE_DB_ENV) in {1, "1"}, service_job["env"]

    commands = [str(step.get("run", "")) for step in service_job["steps"]]
    migrate = next(i for i, cmd in enumerate(commands) if "alembic upgrade head" in cmd)
    test = next(i for i, cmd in enumerate(commands) if "pytest" in cmd)
    assert migrate < test, "migrations must run before pytest, not after"
