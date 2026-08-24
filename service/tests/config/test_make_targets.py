"""The documented path has to exist as commands, not only as prose.

Three separate gaps sat on the grader's route, and each of them looked like a
different component's fault when it fired:

* **`alembic upgrade head` appeared nowhere.** Not in `README.md`, not in the
  `Makefile`, not in `.env.example`. It is run by CI and by the Render
  pre-deploy hook -- everywhere except where a human would read it. Without it
  there are no tables, no writer roles and no demo API keys, so `make serve`
  answers 503 and every DB-backed route raises.
* **`make sync` did not exist, and shipped code said it did.**
  `recon.suite.pipeline` tells you, twice, to run `make sync` when the database
  is not loaded. `POST /internal/sync` is the only way to load it -- there is no
  CLI -- so a grader who hit the suite's own precondition was handed a command
  that was not in the file.
* **`SEED` was inert.** `.env.example` called it "the master seed for
  `recon.seed`" while `make seed` never passed it and nothing else read it. A
  documented control that does nothing is worse than an admitted gap.

So this module tests the Makefile the way the grader uses it: `make -n` expands
the real recipe, and the assertions are about what that expansion actually
contains. It also closes the loop that let `make sync` be cited before it
existed -- every `make <target>` named in shipped source must be a target.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

from recon.config import REPO_ROOT, SERVICE_ROOT

MAKEFILE = REPO_ROOT / "Makefile"

#: `target: prereqs ## help`
_RULE = re.compile(r"^(?P<name>[a-z][a-z0-9-]*)\s*:(?!=)(?P<prereqs>[^=#]*)")

#: A `make <target>` citation inside a docstring or a message.
_CITATION = re.compile(r"`{1,2}make ([a-z][a-z0-9-]*)`{1,2}")

#: Targets whose recipe talks to a database that must already be migrated.
NEEDS_A_MIGRATED_DATABASE = ("serve", "sync", "suite", "test")

pytestmark = pytest.mark.skipif(
    shutil.which("make") is None, reason="GNU make is not installed on this machine"
)


def _makefile() -> str:
    assert MAKEFILE.is_file(), f"{MAKEFILE} is missing"
    return MAKEFILE.read_text(encoding="utf-8")


def _rules() -> dict[str, tuple[str, ...]]:
    """Target name -> its prerequisites."""
    rules: dict[str, tuple[str, ...]] = {}
    for line in _makefile().splitlines():
        if line.startswith("\t"):
            continue
        match = _RULE.match(line)
        if not match:
            continue
        name = match.group("name")
        if name == "PHONY":
            continue
        rules[name] = tuple(match.group("prereqs").split())
    return rules


def _dry_run(target: str) -> str:
    """The recipe `make <target>` would run, expanded but not executed.

    `-n` never executes anything, so this is safe against a configured machine:
    it cannot start a server, reach a database or post a trigger.
    """
    completed = subprocess.run(
        ["make", "-n", target],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "MAKEFLAGS": ""},
    )
    assert completed.returncode == 0, (
        f"`make -n {target}` failed:\n{completed.stdout}\n{completed.stderr}"
    )
    return completed.stdout


# ===========================================================================
# the targets exist


@pytest.mark.parametrize(
    "target", ["up", "down", "migrate", "db-ready", "seed", "serve", "sync", "suite", "test"]
)
def test_the_documented_target_exists(target: str) -> None:
    assert target in _rules(), f"`make {target}` is documented but the Makefile has no such target"


def test_every_target_is_phony() -> None:
    """A file named `test` in the repository root must not shadow a target."""
    rules = _rules()
    match = re.search(r"^\.PHONY:(?P<names>.*)$", _makefile(), flags=re.MULTILINE)
    assert match is not None, "the Makefile no longer declares .PHONY"
    declared = set(match.group("names").split())
    assert not sorted(set(rules) - declared), (
        f"targets missing from .PHONY: {sorted(set(rules) - declared)}"
    )


def test_no_shipped_code_cites_a_target_that_does_not_exist() -> None:
    """`recon.suite.pipeline` cited `make sync` for two commits before it existed.

    Telling an operator to run a command that is not in the file is worse than
    telling them nothing, because it reads as settled.
    """
    rules = _rules()
    dangling: dict[str, set[str]] = {}
    for path in sorted((SERVICE_ROOT / "recon").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        cited = set(_CITATION.findall(path.read_text(encoding="utf-8")))
        missing = cited - set(rules)
        if missing:
            dangling[path.relative_to(REPO_ROOT).as_posix()] = missing
    assert not dangling, f"shipped code names make targets that do not exist: {dangling}"


# ===========================================================================
# the recipes do what the documentation says


def test_migrate_runs_alembic_upgrade_head_from_the_service_directory() -> None:
    """`alembic.ini` uses relative paths, so the working directory is load-bearing."""
    recipe = _dry_run("migrate")
    assert "alembic upgrade head" in recipe, recipe
    assert "--directory service" in recipe, (
        "alembic must run with the working directory set to service/: alembic.ini's "
        "script_location and prepend_sys_path are both relative"
    )


def test_migrate_refuses_without_a_database_url() -> None:
    """The failure has to name the fix, not raise a traceback out of alembic."""
    recipe = _dry_run("migrate")
    assert "DATABASE_URL" in recipe
    assert "cp .env.example .env" in recipe, (
        "the guard must tell the operator how to configure it, not just that it is missing"
    )


@pytest.mark.parametrize("target", NEEDS_A_MIGRATED_DATABASE)
def test_a_target_that_needs_a_migrated_database_depends_on_the_guard(target: str) -> None:
    """Either depend on the migration or fail clearly; never 503 and shrug."""
    assert "db-ready" in _rules()[target], (
        f"`make {target}` needs a migrated database but does not depend on `db-ready`"
    )


def test_db_ready_checks_the_revision_rather_than_the_connection() -> None:
    """ "The database answered" is not "the database has tables"."""
    recipe = _dry_run("db-ready")
    assert "alembic current" in recipe, recipe
    assert "(head)" in recipe, "the guard must compare against head, not merely connect"
    assert "make migrate" in recipe, "the guard must name the command that fixes it"


def test_sync_posts_the_trigger_with_its_secret() -> None:
    """The only way to load the database, and it had no target at all."""
    recipe = _dry_run("sync")
    assert "/internal/sync" in recipe, recipe
    assert "X-Trigger-Secret" in recipe, recipe
    assert "TRIGGER_SECRET_SYNC" in recipe, recipe
    assert "run_id" in recipe, recipe


def test_sync_refuses_without_a_trigger_secret() -> None:
    """An unset secret is a 401 from a healthy-looking service; say so up front."""
    recipe = _dry_run("sync")
    assert "TRIGGER_SECRET_SYNC is not set" in recipe, recipe


def test_seed_passes_the_documented_seed_through() -> None:
    """`SEED` in `.env.example` was inert; `make seed` is what makes it real."""
    for target in ("seed", "seed-dev"):
        recipe = _dry_run(target)
        assert "SEED:+--seed" in recipe, (
            f"`make {target}` does not pass SEED through to `python -m recon.seed --seed`, "
            "so the variable .env.example documents controls nothing"
        )


def test_test_cannot_go_falsely_green() -> None:
    """A missing database once let 76 of 81 tests skip while CI reported success."""
    recipe = _dry_run("test")
    assert "KEYSTONE_REQUIRE_DB" in recipe, (
        "`make test` must require a database, or the DB-backed suites skip and the "
        "green means nothing"
    )


# ===========================================================================
# the `.env` loader


def test_every_target_that_needs_configuration_loads_the_env_file() -> None:
    """`env_file` populates Settings and never writes `os.environ`.

    So the variables read straight from the environment -- the spend caps, the
    role passwords, `OPS_DATABASE_URL`, every `KEYSTONE_*` override -- and the
    `VITE_*` values Vite inlines are reachable only because the Makefile exports
    the file. A target that skipped the loader would work for `DATABASE_URL` and
    silently ignore all of them.
    """
    for target in ("env", "migrate", "db-ready", "seed", "serve", "sync", "suite", "dash", "test"):
        recipe = _dry_run(target)
        assert "while IFS= read -r _line" in recipe, (
            f"`make {target}` does not load {MAKEFILE.parent.name}/.env, so every variable "
            "that is read from os.environ rather than through Settings is inert for it"
        )


def test_the_loader_lets_the_real_environment_win(tmp_path: str) -> None:
    """`DATABASE_URL=... make migrate` must keep meaning what it reads.

    Run for real rather than asserted from the text: the loader is a hand-rolled
    shell fragment, and "process environment wins" is the property that makes
    every scratch-database command in this repository work.
    """
    env_file = REPO_ROOT / ".env.example"
    completed = subprocess.run(
        [
            "make",
            "-s",
            "env",
            f"DOTENV_FILE={env_file}",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "MAKEFLAGS": "",
            "DATABASE_URL": "postgresql://sentinel:hunter2@from-the-process/keystone",
            "PER_RUN_CAP_USD": "0.25",
        },
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "postgresql://sentinel:***@from-the-process/keystone" in completed.stdout, (
        f"the loader overrode an exported DATABASE_URL:\n{completed.stdout}"
    )
    assert "hunter2" not in completed.stdout, (
        f"`make env` printed the DSN password:\n{completed.stdout}"
    )
    assert "PER_RUN_CAP_USD                = 0.25" in completed.stdout, (
        f"the loader overrode an exported PER_RUN_CAP_USD:\n{completed.stdout}"
    )
    # And the variables the process did NOT set still come from the file.
    assert "DAILY_CAP_USD                  = 5.00" in completed.stdout, completed.stdout
    assert "TRIGGER_SECRET_SYNC      = set" in completed.stdout, completed.stdout


def test_the_loader_is_a_parser_and_not_a_shell(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A `.env` must not be able to run commands.

    `set -a; . ./.env` -- the obvious implementation, and the one most projects
    ship -- executes the file. This one reads `KEY=value` lines and ignores
    everything else, so a stray line in a copied `.env` is inert rather than a
    command substitution running as the operator.
    """
    hostile = tmp_path / "hostile.env"
    marker = tmp_path / "executed"
    hostile.write_text(
        f"touch {marker}\nDATABASE_URL=postgresql://parsed@only/keystone\nexport ESCAPED=1\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        ["make", "-s", "env", f"DOTENV_FILE={hostile}"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        env={k: v for k, v in os.environ.items() if k != "DATABASE_URL"} | {"MAKEFLAGS": ""},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not marker.exists(), "the .env loader executed a line instead of parsing it"
    assert "postgresql://parsed@only/keystone" in completed.stdout, completed.stdout
