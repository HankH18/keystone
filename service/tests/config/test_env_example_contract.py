"""`.env.example` and the code must not drift apart, in EITHER direction.

`README.md` calls `.env.example` "the authoritative list". It was not, and
nothing could tell you: `Settings`'s ``extra="ignore"`` drops an unrecognised
name in silence, so a documented variable no code reads and a variable read by
code that nothing documents are both invisible failures.

Both had happened. The file documented variables against a set of `Settings`
fields nobody compared it to, while a long tail the code really reads -- the
three writer-role passwords, ``OPS_DATABASE_URL``, ``KEYSTONE_REQUIRE_DB``,
``KEYSTONE_COVERAGE_DATABASE_URL``, ``KEYSTONE_CORS_ORIGINS``, the four path
overrides and the rest -- was documented nowhere, even though
`infra/render.yaml` declares four of them.

The durable fix is this test, not the edit that accompanied it. It closes the
loop mechanically:

* every `Settings` field must be documented (:func:`test_every_settings_field_is_documented`);
* every variable the shipped code reads must be documented
  (:func:`test_every_variable_the_code_reads_is_documented`), where "reads" is
  found by parsing the source, not by a second hand-written list;
* every documented variable must be real (:func:`test_no_documented_variable_is_dead`);
* the reads the parser cannot resolve are pinned by file, so a new dynamic read
  in a new file fails rather than opening a silent gap.

A worked example of why the scan has to be mechanical: a hand review of this
same question concluded that ``EMBEDDING_PROVIDER``, ``VOYAGE_API_KEY`` and
``OPENAI_API_KEY`` were "dead everywhere -- zero code references", and deleting
them would have broken `recon.incidents.build_embedding_provider`, which reads
all three. The parser finds them. Grep did not.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from recon.config import REPO_ROOT, SERVICE_ROOT, Settings
from recon.db import WRITER_ROLES
from tests.config.envscan import (
    EnvScan,
    scan_env_reads,
    seeded_scope_env_vars,
    writer_role_password_vars,
)

ENV_EXAMPLE = REPO_ROOT / ".env.example"
DASHBOARD_ENV_EXAMPLE = REPO_ROOT / "dashboard" / ".env.example"
DASHBOARD_SRC = REPO_ROOT / "dashboard" / "src"
MIGRATION_0005 = SERVICE_ROOT / "migrations" / "versions" / "0005_three_role_boundary.py"

#: The shipped code a grader configures. `service/tests/**` is deliberately out
#: of scope -- another ticket owns those files, and the two harness variables
#: they read are pinned by :data:`HARNESS_VARIABLES` instead.
SCANNED_ROOTS = (SERVICE_ROOT / "recon", SERVICE_ROOT / "migrations")

#: `KEY=value` on a live line.
_ACTIVE = re.compile(r"^(?P<name>[A-Z][A-Z0-9_]*)=(?P<value>.*)$")
#: `# KEY=value` -- a real variable with a working default, documented but not armed.
_COMMENTED = re.compile(r"^#\s*(?P<name>[A-Z][A-Z0-9_]*)=(?P<value>.*)$")

#: Variables the process sets for its own children. Not operator configuration,
#: so they are deliberately absent from `.env.example`: documenting them would
#: invite someone to set `PYTHONHASHSEED` to something the seed generator then
#: refuses to run under (SS8 / G30).
INTERNAL_ONLY = frozenset(
    {
        "PYTHONHASHSEED",
        "KEYSTONE_SEED_HASH_SEED_SET",
    }
)

#: Test-harness variables. They are not read by `recon`, so the source scan
#: cannot find them, but a grader has to know about them: the first is the
#: difference between a real `make test` and the false green that once let 76 of
#: 81 tests skip while CI reported success.
HARNESS_VARIABLES = {
    "KEYSTONE_REQUIRE_DB": "service/tests/schema/conftest.py",
    "KEYSTONE_SCRATCH_DB": "service/tests/schema/conftest.py",
}

#: Documented variables that no code reads *at runtime*, each with the thing
#: that makes it real anyway. `test_no_documented_variable_is_dead` checks the
#: named file actually mentions the variable, so an entry cannot outlive its
#: justification.
DOCUMENTED_WITHOUT_A_RUNTIME_READ = {
    "DEMO_CLIENT_API_KEY": "service/tests/schema/test_env_example_demo_keys.py",
    # Read by **libpq**, not by any Python in this repository, so no source scan
    # can ever find it -- and it is load-bearing anyway: `infra/render.yaml`
    # passes the `recon_writer` password through it precisely so a generated
    # base64 secret is never parsed as URL syntax, which means the deployed
    # service authenticates with a variable `.env.example` did not mention.
    "PGPASSWORD": "infra/render.yaml",
}


#: Files containing an environment read whose variable name is an expression the
#: parser cannot evaluate. Each entry says which variables that file can produce.
#: The KEYS are asserted against the scan -- a new dynamic read in a new file
#: fails this test -- and the VALUES are derived from the code rather than typed
#: out, so they cannot go stale either.
def _dynamic_read_files() -> dict[str, str]:
    return {
        "service/recon/db.py": "os.environ.get(f'{role.upper()}_PASSWORD')",
        "service/recon/budget.py": "os.environ.get(env_var) inside cap_microusd_from_env",
        "service/migrations/versions/0002_roles_and_grants.py": (
            "os.environ.get(f'{role.upper()}_PASSWORD')"
        ),
        "service/migrations/versions/0005_three_role_boundary.py": (
            "os.environ.get(f'{REVIEW_WRITER.upper()}_PASSWORD') and the SEEDED_SCOPES loop"
        ),
    }


@pytest.fixture(scope="module")
def scan() -> EnvScan:
    """Every environment read the shipped code makes, found by parsing it."""
    return scan_env_reads(SCANNED_ROOTS, REPO_ROOT)


@pytest.fixture(scope="module")
def dynamic_variables() -> frozenset[str]:
    """The variables the dynamic reads can produce, derived from the code."""
    return writer_role_password_vars(WRITER_ROLES) | seeded_scope_env_vars(MIGRATION_0005)


def _parse(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(active, commented)`` name -> value maps for an env example file.

    Hand-rolled rather than via python-dotenv, for the reason
    `tests/schema/test_env_example_demo_keys.py` gives: the test must read
    exactly the bytes a developer copies, with no interpolation or defaulting
    that could paper over a wrong value.
    """
    assert path.is_file(), f"{path} is missing"
    active: dict[str, str] = {}
    commented: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        match = _ACTIVE.match(stripped)
        if match:
            active[match.group("name")] = match.group("value").strip()
            continue
        match = _COMMENTED.match(stripped)
        if match:
            commented[match.group("name")] = match.group("value").strip()
    return active, commented


@pytest.fixture(scope="module")
def root_env() -> tuple[dict[str, str], dict[str, str]]:
    return _parse(ENV_EXAMPLE)


@pytest.fixture(scope="module")
def documented(root_env: tuple[dict[str, str], dict[str, str]]) -> frozenset[str]:
    """Every variable `.env.example` names, armed or merely described."""
    active, commented = root_env
    return frozenset(active) | frozenset(commented)


def _settings_env_names() -> frozenset[str]:
    """The environment variable each `Settings` field is populated from."""
    prefix = Settings.model_config.get("env_prefix") or ""
    return frozenset(f"{prefix}{name}".upper() for name in Settings.model_fields)


# ===========================================================================
# the two directions of drift


def test_every_settings_field_is_documented(documented: frozenset[str]) -> None:
    """A `Settings` field nobody documents is a control the operator cannot find."""
    missing = sorted(_settings_env_names() - documented)
    assert not missing, (
        f"{ENV_EXAMPLE.name} does not document these Settings fields: {missing}. "
        "Every field on recon.config.Settings is an environment variable; add a "
        "line for each (commented out if it has a working default)."
    )


def test_every_variable_the_code_reads_is_documented(
    scan: EnvScan, dynamic_variables: frozenset[str], documented: frozenset[str]
) -> None:
    """The other direction: a variable the code reads that nothing writes down."""
    required = (frozenset(scan.names) | dynamic_variables | frozenset(HARNESS_VARIABLES)) - (
        INTERNAL_ONLY
    )
    missing = sorted(required - documented)
    sites = {name: scan.names.get(name, ("(dynamic or harness)",)) for name in missing}
    assert not missing, (
        f"{ENV_EXAMPLE.name} does not document these variables, which the code reads: "
        f"{sites}. Document each one (commented out if it has a working default), or "
        "add it to INTERNAL_ONLY if it is something the process sets for its own children."
    )


def test_no_documented_variable_is_dead(
    scan: EnvScan, dynamic_variables: frozenset[str], documented: frozenset[str]
) -> None:
    """A documented variable nothing reads is worse than an admitted gap.

    `EMBEDDING_PROVIDER` and its two keys nearly got deleted on a hand review
    that called them dead; they are read by `recon.incidents`. This test is the
    one that would have caught the deletion.
    """
    vite = {name for name in documented if name.startswith("VITE_")}
    real = (
        frozenset(scan.names)
        | dynamic_variables
        | frozenset(HARNESS_VARIABLES)
        | frozenset(DOCUMENTED_WITHOUT_A_RUNTIME_READ)
        | _settings_env_names()
        | vite
    )
    dead = sorted(documented - real)
    assert not dead, (
        f"{ENV_EXAMPLE.name} documents variables nothing reads: {dead}. Delete them -- "
        "a documented control that does not exist is worse than an admitted gap -- or, "
        "if one is real in a way the source scan cannot see, register it in "
        "DOCUMENTED_WITHOUT_A_RUNTIME_READ with the file that makes it real."
    )


@pytest.mark.parametrize(
    ("variable", "justified_by"), sorted(DOCUMENTED_WITHOUT_A_RUNTIME_READ.items())
)
def test_a_variable_without_a_runtime_read_still_has_a_binding(
    variable: str, justified_by: str
) -> None:
    """The registry above cannot outlive the file it points at."""
    path = REPO_ROOT / justified_by
    assert path.is_file(), f"{variable} cites {justified_by}, which does not exist"
    assert variable in path.read_text(encoding="utf-8"), (
        f"{variable} cites {justified_by}, which no longer mentions it; the variable is "
        "now undocumented-and-unread, so either restore the binding or delete the line "
        f"from {ENV_EXAMPLE.name}"
    )


@pytest.mark.parametrize("variable", sorted(HARNESS_VARIABLES))
def test_a_harness_variable_is_still_read_where_it_claims(variable: str) -> None:
    """`HARNESS_VARIABLES` is hand-written; this keeps it honest."""
    path = REPO_ROOT / HARNESS_VARIABLES[variable]
    assert path.is_file(), f"{variable} cites {HARNESS_VARIABLES[variable]}, which does not exist"
    assert variable in path.read_text(encoding="utf-8"), (
        f"{variable} is documented as read by {HARNESS_VARIABLES[variable]}, which no "
        "longer mentions it"
    )


def test_the_unresolvable_reads_are_the_known_ones(scan: EnvScan) -> None:
    """A new computed variable name must be accounted for, not silently missed.

    The parser resolves a literal and a module-level constant. It cannot
    evaluate ``f"{role.upper()}_PASSWORD"`` or a name that is a function
    parameter, and pretending otherwise is how a scanner becomes a rubber stamp.
    So the FILES that contain such a read are pinned here: a new one fails this
    test until someone decides which variables it can produce and documents them.
    """
    expected = _dynamic_read_files()
    found = scan.dynamic_files()
    unexpected = sorted(found - frozenset(expected))
    assert not unexpected, (
        f"new unresolvable environment reads in {unexpected}. The scanner cannot tell "
        "which variables they produce, so add each file to _dynamic_read_files() with the "
        "variables it can read, and document those variables in .env.example."
    )
    vanished = sorted(frozenset(expected) - found)
    assert not vanished, (
        f"{vanished} no longer contain an unresolvable environment read. Drop them from "
        "_dynamic_read_files() so the pin keeps meaning something."
    )


def test_the_derived_dynamic_variables_are_not_empty(dynamic_variables: frozenset[str]) -> None:
    """Guard the guard: an empty derivation would make the check above vacuous."""
    assert writer_role_password_vars(WRITER_ROLES) == {
        "RECON_WRITER_PASSWORD",
        "REVIEW_WRITER_PASSWORD",
        "APPLY_WRITER_PASSWORD",
    }
    assert "DAILY_CAP_USD" in dynamic_variables
    assert "PER_RUN_CAP_USD" in dynamic_variables


# ===========================================================================
# the dashboard's half


def _vite_variables_read_by_the_dashboard() -> frozenset[str]:
    """Every ``import.meta.env.VITE_*`` the dashboard source reads."""
    assert DASHBOARD_SRC.is_dir(), f"{DASHBOARD_SRC} is missing"
    pattern = re.compile(r"import\.meta\.env\.(VITE_[A-Z0-9_]+)")
    found: set[str] = set()
    for path in sorted(DASHBOARD_SRC.rglob("*.ts")):
        found.update(pattern.findall(path.read_text(encoding="utf-8")))
    for path in sorted(DASHBOARD_SRC.rglob("*.tsx")):
        found.update(pattern.findall(path.read_text(encoding="utf-8")))
    return frozenset(found)


def test_every_vite_variable_is_documented_in_both_example_files(
    documented: frozenset[str],
) -> None:
    """Vite reads `dashboard/.env.local`; `make dash` feeds it the repo-root file.

    Both are documented paths, so both example files have to name every
    variable. ``VITE_USE_MOCK_API`` was in the dashboard's file and absent from
    the root one, which is the shape of the gap this closes.
    """
    read = _vite_variables_read_by_the_dashboard()
    assert read, "found no import.meta.env.VITE_* reads; the scan regex has gone stale"

    dashboard_active, dashboard_commented = _parse(DASHBOARD_ENV_EXAMPLE)
    dashboard_documented = frozenset(dashboard_active) | frozenset(dashboard_commented)

    assert not sorted(read - documented), (
        f"{ENV_EXAMPLE.name} does not document {sorted(read - documented)}, which the "
        "dashboard reads and `make dash` supplies from this file"
    )
    assert not sorted(read - dashboard_documented), (
        f"dashboard/.env.example does not document {sorted(read - dashboard_documented)}"
    )


def test_the_dashboard_example_ships_the_admin_demo_key(
    root_env: tuple[dict[str, str], dict[str, str]],
) -> None:
    """It shipped EMPTY, so a grader who copied it authenticated as nobody.

    `apiKey()` returned `''`, the client raised `ApiConfigError`, and no request
    was ever sent. `tests/schema/test_env_example_demo_keys.py` pinned the ROOT
    file's `VITE_API_KEY` to `DEMO_ADMIN_API_KEY` and never looked at this one,
    which is exactly how the empty value survived.
    """
    active_root, _ = root_env
    dashboard_active, _ = _parse(DASHBOARD_ENV_EXAMPLE)

    admin_key = active_root["DEMO_ADMIN_API_KEY"]
    assert admin_key, "DEMO_ADMIN_API_KEY is empty in .env.example"
    assert dashboard_active.get("VITE_API_KEY") == admin_key, (
        "dashboard/.env.example must ship VITE_API_KEY=<DEMO_ADMIN_API_KEY>; an empty "
        "value authenticates the dashboard as nobody"
    )


def test_the_two_example_files_agree_on_every_shared_variable(
    root_env: tuple[dict[str, str], dict[str, str]],
) -> None:
    """`make dash` exports the root file over `dashboard/.env.local`.

    Vite ranks the process environment above its own files, so a disagreement
    between the two examples is a dashboard that behaves differently under
    `make dash` than under `pnpm --dir dashboard dev` -- with nothing on screen
    to say which file won.
    """
    active_root, _ = root_env
    dashboard_active, _ = _parse(DASHBOARD_ENV_EXAMPLE)

    shared = sorted(set(active_root) & set(dashboard_active))
    assert shared, "the two example files share no armed variable; one of them is wrong"
    disagreements = {
        name: (active_root[name], dashboard_active[name])
        for name in shared
        if active_root[name] != dashboard_active[name]
    }
    assert not disagreements, (
        f"the two .env.example files disagree (root, dashboard): {disagreements}"
    )


# ===========================================================================
# the file has to survive being copied


def test_the_example_file_is_safe_to_load(
    root_env: tuple[dict[str, str], dict[str, str]],
) -> None:
    """`cp .env.example .env` must not smuggle shell into the Makefile's loader.

    The loader is a parser, not `source`, but a value carrying a quote or a
    `$VAR` would still land in the environment verbatim and mean something other
    than it reads -- so the committed file must not contain any.
    """
    active, _ = root_env
    for name, value in sorted(active.items()):
        assert "$" not in value, f"{name} contains `$`; the loader does not expand variables"
        assert not value.startswith(('"', "'")), (
            f"{name} is quoted; the loader takes values verbatim, quotes included"
        )
        assert value == value.strip(), f"{name} has surrounding whitespace"
