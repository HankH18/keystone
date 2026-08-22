"""`.env.example`'s demo keys are the keys the migration actually seeded.

The drift this closes: ``.env.example`` shipped ``demo-client-key-replace-me``
while migration 0003 seeded the hash of
``keystone-demo-client-3f7a19c4e2b84d05``. Every documented demo credential was
therefore rejected by the service, and nothing in the suite noticed -- the seed
test hard-coded the *correct* plaintext in its own module, so it agreed with
the migration and neither of them ever looked at the file a user copies.

This test closes the loop by reading the plaintext out of ``.env.example``
itself, hashing it with the application's own helper, and matching the result
against the row in ``api_clients``. There is no third copy of the key here to
drift: the file is the input, the database is the expected value.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from recon.db import api_key_hash

#: env var in .env.example -> (scope, label) of the row 0003 seeds for it.
DEMO_KEY_VARS = {
    "DEMO_CLIENT_API_KEY": ("client", "demo-client"),
    "DEMO_ADMIN_API_KEY": ("admin", "demo-admin"),
}

#: Vite inlines this into the client bundle; it must be the admin demo key,
#: because the dashboard performs reviewer actions.
VITE_KEY_VAR = "VITE_API_KEY"


def _env_example(service_root: Path) -> dict[str, str]:
    """Parse ``.env.example`` into a plain dict.

    Hand-rolled rather than via python-dotenv so the test reads exactly the
    bytes a developer copies, with no interpolation, expansion or defaulting
    that could paper over a wrong value.
    """
    path = service_root.parent / ".env.example"
    assert path.is_file(), f"{path} is missing"

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


@pytest.mark.parametrize(("variable", "expected"), sorted(DEMO_KEY_VARS.items()))
def test_the_documented_demo_key_hashes_to_the_seeded_row(
    owner_engine: Engine, service_root: Path, variable: str, expected: tuple[str, str]
) -> None:
    """Copy `.env.example`, use the key, get in. That is the whole claim."""
    plaintext = _env_example(service_root).get(variable, "")
    assert plaintext, f"{variable} is absent or empty in .env.example"

    with owner_engine.connect() as conn:
        row = conn.execute(
            text("SELECT scope, label FROM api_clients WHERE key_hash = :key_hash"),
            {"key_hash": api_key_hash(plaintext)},
        ).one_or_none()

    assert row is not None, (
        f".env.example's {variable}={plaintext!r} hashes to a key_hash that is not in "
        "api_clients, so the documented demo credential would be rejected. Either the "
        "file or migration 0003_seed_api_clients is wrong; they must agree."
    )
    assert (row.scope, row.label) == expected, (
        f"{variable} resolves to {(row.scope, row.label)}, expected {expected}"
    )


def test_the_dashboard_key_is_the_admin_demo_key(service_root: Path) -> None:
    """`VITE_API_KEY` is inlined into the bundle; a stale one breaks the demo UI."""
    values = _env_example(service_root)
    assert values.get(VITE_KEY_VAR) == values.get("DEMO_ADMIN_API_KEY"), (
        f"{VITE_KEY_VAR} must equal DEMO_ADMIN_API_KEY: the dashboard performs "
        "reviewer actions and needs the admin scope"
    )


@pytest.mark.parametrize("variable", sorted(DEMO_KEY_VARS))
def test_the_demo_keys_are_not_placeholders(service_root: Path, variable: str) -> None:
    """A placeholder is the exact failure mode this file exists to prevent.

    Kept separate from the hash check so the diagnosis is unambiguous: this
    fails with "you left a placeholder", not "the hash did not match".
    """
    value = _env_example(service_root)[variable]
    assert "replace-me" not in value.lower(), (
        f"{variable} is still a placeholder ({value!r}); the documented demo "
        "credential must be the real committed plaintext that 0003 seeded"
    )


def test_no_plaintext_demo_key_is_stored_in_the_database(
    owner_engine: Engine, service_root: Path
) -> None:
    """The file holds the plaintext; the database must only ever hold the hash."""
    values = _env_example(service_root)
    plaintexts = [values[variable] for variable in DEMO_KEY_VARS]

    with owner_engine.connect() as conn:
        hits = conn.execute(
            text(
                "SELECT count(*) FROM api_clients WHERE key_hash = ANY(:keys) OR label = ANY(:keys)"
            ),
            {"keys": plaintexts},
        ).scalar_one()
    assert hits == 0
