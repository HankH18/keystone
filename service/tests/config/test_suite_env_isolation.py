"""The test suite's verdict must not depend on an untracked local `.env`.

`README.md` step 2 is ``cp .env.example .env``. Doing exactly that turned four
tests red -- the ones asserting R19's fail-closed rule, that an *unconfigured*
trigger secret returns 401 rather than disabling the check:

    tests/triggers/test_single_trigger_guard.py::test_the_deprecated_single_secret_is_the_sync_jobs_alone
    tests/triggers/test_single_trigger_guard.py::test_a_whitespace_only_deprecated_secret_is_also_unusable
    tests/triggers/test_internal_endpoints.py::test_an_unconfigured_secret_fails_closed
    tests/ingest/test_trigger_auth.py::test_the_deprecated_single_secret_still_works_while_it_exists

They build the unconfigured state with ``monkeypatch.delenv``, which clears
`os.environ` and nothing else. `recon.config`'s repo-root `.env` still answered
underneath, supplying the placeholder
``TRIGGER_SECRET_SYNC=replace-me-sync-trigger-secret`` -- so the premise
evaporated and the safety assertions were made against a *configured*
deployment. The green those four reported was green for having skipped the
README, which is worse than no test at all.

`tests/conftest.py`'s session fixture is the repair. This module is what keeps
it: it asserts the isolation against the developer's real `.env`, and it
asserts -- in a subprocess, where the fixture does not reach -- that the file is
still read by everything that is not the test suite. Both halves are needed. An
isolation that also broke `make serve` would be a worse bug than the one it fixed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from recon import config as config_module
from recon.config import REPO_ROOT, SERVICE_ROOT, Settings, get_settings

#: `.env` is gitignored, so a machine that has not run `cp .env.example .env`
#: -- CI, a fresh clone -- has nothing for these tests to isolate *from*.
REPO_ENV_FILE = REPO_ROOT / ".env"

_NO_ENV_FILE = f"{REPO_ENV_FILE} does not exist; there is no ambient file to be isolated from"


def _declared_in_the_env_file() -> dict[str, str]:
    """The repo-root `.env` as ``{KEY: value}``, comments and blanks dropped."""
    parsed: dict[str, str] = {}
    for line in REPO_ENV_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        parsed[key.strip()] = value.strip().strip("\"'")
    return parsed


def _fields_only_the_file_supplies() -> dict[str, str]:
    """`Settings` fields the file sets to a non-default value, and the environment does not.

    A key that is *also* in `os.environ` is excluded: the process environment
    outranks the file, so the file is not what the test would be observing. A
    key whose file value equals the field's default is excluded too -- it cannot
    distinguish "the file was ignored" from "the file was read".
    """
    interesting: dict[str, str] = {}
    for key, value in _declared_in_the_env_file().items():
        field = Settings.model_fields.get(key.lower())
        if field is None or key in os.environ:
            continue
        if str(field.default) == value:
            continue
        interesting[key.lower()] = value
    return interesting


@pytest.mark.skipif(not REPO_ENV_FILE.exists(), reason=_NO_ENV_FILE)
def test_the_suite_does_not_read_the_developers_repo_root_env_file() -> None:
    """Every `Settings` field the file alone would supply reads as its default.

    Asserted against the real file rather than a fixture copy: the failure being
    pinned was *caused* by the real file, and a synthetic one would not have
    caught it.
    """
    supplied = _fields_only_the_file_supplies()
    assert supplied, (
        f"{REPO_ENV_FILE} sets no Settings field that the process environment does not "
        "already set to the same value, so this test cannot observe the isolation it "
        "claims to test. Do not delete it -- it is vacuous on this machine only."
    )

    settings = get_settings()
    for field, file_value in sorted(supplied.items()):
        default = Settings.model_fields[field].default
        assert getattr(settings, field) == default, (
            f"Settings.{field} came from {REPO_ENV_FILE} (it is {file_value!r} there), not "
            "from the process environment. The suite's result now moves with an untracked "
            "file, which is how the fail-closed trigger tests came to pass only on machines "
            "that had NOT followed README step 2."
        )


@pytest.mark.skipif(not REPO_ENV_FILE.exists(), reason=_NO_ENV_FILE)
def test_deleting_a_variable_genuinely_unconfigures_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact idiom the four tests use, asserted directly.

    ``monkeypatch.delenv`` empties `os.environ`. This is the claim that nothing
    else answers afterwards -- which is the whole of the defect, stated once,
    where a future change to the `.env` chain will trip over it.
    """
    assert "trigger_secret_sync" in _fields_only_the_file_supplies(), (
        f"{REPO_ENV_FILE} no longer sets TRIGGER_SECRET_SYNC to a non-default value, so "
        "this test would pass without the isolation doing anything"
    )

    monkeypatch.delenv("TRIGGER_SECRET_SYNC", raising=False)
    get_settings.cache_clear()

    assert get_settings().trigger_secret_sync is None, (
        "delenv left the setting configured: an 'unconfigured secret' test cannot "
        "reach the unconfigured state it exists to assert"
    )


def test_the_declared_env_file_chain_is_untouched_while_it_is_disabled() -> None:
    """Disabling the chain drops a *source*; it never rewrites `model_config`.

    `model_config` is class state that other modules and
    `tests/config/test_env_file_resolution.py` read. If the isolation worked by
    editing it, the declared configuration would be a lie for the duration and
    the two test modules would contradict each other.
    """
    assert config_module._ENV_FILE_CHAIN_ENABLED is False, (
        "the session fixture in tests/conftest.py is not holding the chain open"
    )
    declared = Settings.model_config.get("env_file")
    assert declared is not None, "env_file was removed from model_config, not merely disabled"
    # The third entry is `$PWD/.env` and is deliberately cwd-dependent; the two
    # repository-anchored files are the ones `test_env_file_resolution.py` pins.
    assert tuple(declared)[:2] == (REPO_ROOT / ".env", SERVICE_ROOT / ".env"), (
        f"env_file was mutated while the chain was disabled: {declared}"
    )


def test_a_process_that_is_not_the_test_suite_still_reads_the_env_file() -> None:
    """`make serve` must keep working. The isolation is the suite's, not the module's.

    Run in a subprocess with a hand-built environment, because that is the only
    place `tests/conftest.py`'s session fixture does not reach -- and because an
    in-process assertion would be asserting on the very flag it wants to prove
    is not stuck. `TRIGGER_SECRET_SYNC` is absent from that environment, so the
    value printed can only have come from the file.
    """
    if not REPO_ENV_FILE.exists():
        pytest.skip(_NO_ENV_FILE)
    expected = _declared_in_the_env_file().get("TRIGGER_SECRET_SYNC")
    if not expected:
        pytest.skip(f"{REPO_ENV_FILE} does not set TRIGGER_SECRET_SYNC")

    program = textwrap.dedent(
        """
        from recon import config
        print("enabled:", config._ENV_FILE_CHAIN_ENABLED)
        print("sync:", config.Settings().trigger_secret_sync)
        with config.env_file_chain_disabled():
            print("disabled_sync:", config.Settings().trigger_secret_sync)
        print("restored_sync:", config.Settings().trigger_secret_sync)
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=str(SERVICE_ROOT),
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(SERVICE_ROOT), "PYTHONPATH": str(SERVICE_ROOT)},
    )
    assert completed.returncode == 0, completed.stderr

    printed = dict(
        (key, value)
        for key, _, value in (line.partition(": ") for line in completed.stdout.splitlines())
    )
    assert printed["enabled"] == "True", (
        "a deployed process starts with the .env chain OFF -- `cp .env.example .env` would "
        "configure nothing and `make serve` would 401 on every trigger"
    )
    assert printed["sync"] == expected, (
        f"the repo-root .env was not read by a plain process: {completed.stdout}"
    )
    assert printed["disabled_sync"] == "None", "env_file_chain_disabled() did not disable it"
    assert printed["restored_sync"] == expected, (
        "env_file_chain_disabled() did not restore the chain on exit -- a test using it "
        "would silently unconfigure every test that ran after it"
    )
