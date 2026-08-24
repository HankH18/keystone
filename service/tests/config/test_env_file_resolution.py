"""`cp .env.example .env` at the repository root must actually configure the service.

The bug this pins was silent, which is what made it expensive. `env_file` was
the bare relative string ``".env"``; pydantic-settings resolves that against the
**process working directory**, and every application entry point runs with the
working directory set to ``service/`` -- `Makefile`'s ``UV := uv --directory
service``, and the documented ``cd service && uv run ...``. So the repo-root
`.env` the README tells you to create was opened by nothing, there is no upward
search, and `create_app()` touches no database: `make serve` came up looking
healthy and then answered 503 on ``/health`` and **401** on
``POST /internal/sync``, because the trigger secret the operator had just pasted
was not in the process. Nothing anywhere said "your `.env` was ignored".

The two halves of the fix are tested in two places. This module covers
`recon.config`: the file chain is absolute, anchored to the repository, and the
same wherever the process was started from. The Makefile's half -- exporting the
file into the environment, which is the only way the `os.environ`-only variables
and the `VITE_*` values are ever reachable -- is covered by
`tests/config/test_make_targets.py`.

The end-to-end test here does not write into the real repository: it builds a
throwaway skeleton in `tmp_path` out of the **committed bytes of config.py**,
drops a `.env` at its root, and runs a subprocess from the same working
directory `make serve` uses. That is the original failure, reproduced, against
the real module.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from recon.config import REPO_ROOT, SERVICE_ROOT, Settings

CONFIG_SOURCE = SERVICE_ROOT / "recon" / "config.py"

#: A value no default and no committed file could produce.
SENTINEL_DSN = "postgresql://sentinel:sentinel@env-file-proof:1/keystone_root_env"
SENTINEL_SECRET = "sentinel-root-env-trigger-secret"


def _env_files() -> tuple[Path, ...]:
    raw = Settings.model_config.get("env_file")
    assert raw is not None, "Settings no longer declares env_file at all"
    return tuple(Path(entry) for entry in (raw if isinstance(raw, tuple | list) else (raw,)))


def test_the_env_file_chain_is_absolute_and_anchored_to_the_repository() -> None:
    """A relative `env_file` is the bug; every entry must be absolute."""
    files = _env_files()
    assert all(path.is_absolute() for path in files), (
        f"env_file must not contain a relative path (it resolves against the working "
        f"directory, which is `service/` for every entry point): {files}"
    )
    assert files[0] == REPO_ROOT / ".env", (
        f"the repo-root .env must be first (lowest precedence): {files}"
    )
    assert files[1] == SERVICE_ROOT / ".env", (
        f"service/.env must be second, so it overrides the repo-root file: {files}"
    )


def test_the_chain_has_no_duplicates() -> None:
    """Running from the repository root must not promote the root file."""
    files = _env_files()
    resolved = [str(path) for path in files]
    assert len(resolved) == len(set(resolved)), f"duplicate entries in env_file: {files}"


@pytest.mark.parametrize("cwd", ["repo_root", "service", "tmp"])
def test_the_chain_does_not_depend_on_the_working_directory(cwd: str, tmp_path: Path) -> None:
    """The whole point: `uv --directory service` must not change the answer."""
    directories = {"repo_root": REPO_ROOT, "service": SERVICE_ROOT, "tmp": tmp_path}
    program = textwrap.dedent(
        """
        from recon.config import Settings
        files = Settings.model_config["env_file"]
        print("\\n".join(str(path) for path in files[:2]))
        """
    )
    # `recon` is importable from `service/` alone, and this test deliberately
    # runs from elsewhere -- so the path is supplied explicitly rather than
    # inherited from the working directory the test is trying to vary.
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(SERVICE_ROOT)
    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=str(directories[cwd]),
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == [
        str(REPO_ROOT / ".env"),
        str(SERVICE_ROOT / ".env"),
    ], f"working directory {cwd} changed the env_file chain:\n{completed.stdout}"


# ===========================================================================
# end to end, against a throwaway copy of the real module


def _skeleton(tmp_path: Path) -> Path:
    """A `<repo>/service/recon/config.py` skeleton built from the committed file."""
    package = tmp_path / "repo" / "service" / "recon"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy(CONFIG_SOURCE, package / "config.py")
    return tmp_path / "repo"


_PROBE = textwrap.dedent(
    """
    import sys
    from pydantic_settings import BaseSettings, SettingsConfigDict
    sys.path.insert(0, sys.argv[1])
    from recon.config import Settings

    settings = Settings()
    print("database_url:", settings.database_url)
    print("trigger_secret_sync:", settings.trigger_secret_sync)

    class Old(BaseSettings):
        model_config = SettingsConfigDict(env_file=".env", extra="ignore")
        database_url: str | None = None

    print("old_relative_env_file:", Old().database_url)
    """
)


def _probe(repo: Path, cwd: Path) -> dict[str, str]:
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE, str(repo / "service")],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(cwd)},
    )
    assert completed.returncode == 0, completed.stderr
    parsed: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        key, _, value = line.partition(": ")
        parsed[key] = value
    return parsed


def test_a_repo_root_env_file_configures_the_service_from_the_service_directory(
    tmp_path: Path,
) -> None:
    """The original failure, reproduced against the committed module.

    Working directory ``service/`` is what ``uv --directory service`` gives every
    application target, so this is literally what `make serve` sees. The
    ``old_relative_env_file`` line is the anti-vacuous half: a `Settings` built
    the way this one used to be finds nothing from the same directory, so the
    test cannot pass because the environment happened to carry the value.
    """
    repo = _skeleton(tmp_path)
    (repo / ".env").write_text(
        f"DATABASE_URL={SENTINEL_DSN}\nTRIGGER_SECRET_SYNC={SENTINEL_SECRET}\n",
        encoding="utf-8",
    )

    result = _probe(repo, cwd=repo / "service")

    assert result["database_url"] == SENTINEL_DSN, (
        "the repo-root .env was not read from the service/ working directory -- this is "
        "the exact failure `cp .env.example .env` used to produce"
    )
    assert result["trigger_secret_sync"] == SENTINEL_SECRET
    assert result["old_relative_env_file"] == "None", (
        "a relative env_file found the repo-root file from service/, so this test would "
        "have passed before the fix and proves nothing"
    )


def test_a_repo_root_env_file_configures_the_service_from_an_unrelated_directory(
    tmp_path: Path,
) -> None:
    """Not merely "one directory up": the anchor is the repository, not the cwd."""
    repo = _skeleton(tmp_path)
    (repo / ".env").write_text(f"DATABASE_URL={SENTINEL_DSN}\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    result = _probe(repo, cwd=elsewhere)

    assert result["database_url"] == SENTINEL_DSN
    assert result["old_relative_env_file"] == "None"


def test_a_service_local_env_file_overrides_the_repo_root_one(tmp_path: Path) -> None:
    """Documented precedence, so the two files cannot both silently apply."""
    repo = _skeleton(tmp_path)
    (repo / ".env").write_text(f"DATABASE_URL={SENTINEL_DSN}\n", encoding="utf-8")
    (repo / "service" / ".env").write_text(
        "DATABASE_URL=postgresql://sentinel@service-local/keystone\n", encoding="utf-8"
    )

    result = _probe(repo, cwd=repo / "service")

    assert result["database_url"] == "postgresql://sentinel@service-local/keystone"


def test_the_real_environment_still_outranks_every_file(tmp_path: Path) -> None:
    """`DATABASE_URL=... make migrate` has to keep meaning what it reads."""
    repo = _skeleton(tmp_path)
    (repo / ".env").write_text(f"DATABASE_URL={SENTINEL_DSN}\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "-c", _PROBE, str(repo / "service")],
        cwd=str(repo / "service"),
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "DATABASE_URL": "postgresql://sentinel@from-the-process/keystone",
        },
    )
    assert completed.returncode == 0, completed.stderr
    assert "database_url: postgresql://sentinel@from-the-process/keystone" in completed.stdout


def test_a_missing_env_file_is_not_an_error(tmp_path: Path) -> None:
    """A container with no `.env` configures from the real environment alone."""
    repo = _skeleton(tmp_path)

    result = _probe(repo, cwd=repo / "service")

    assert result["database_url"] == "None"
    assert result["trigger_secret_sync"] == "None"
