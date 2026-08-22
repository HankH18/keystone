"""The committed demo API clients are seeded as hashes, never as plaintext."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text

from recon.db import API_KEY_SALT, api_key_hash

# Plaintext lives in .env.example; the database only ever sees the hash.
DEMO_CLIENT_API_KEY = "keystone-demo-client-3f7a19c4e2b84d05"
DEMO_ADMIN_API_KEY = "keystone-demo-admin-8c25e0b71a94f36d"

EXPECTED = {
    api_key_hash(DEMO_CLIENT_API_KEY): ("client", "demo-client"),
    api_key_hash(DEMO_ADMIN_API_KEY): ("admin", "demo-admin"),
}


def test_demo_clients_are_seeded_with_the_expected_scopes(owner_engine: Engine) -> None:
    with owner_engine.connect() as conn:
        rows = conn.execute(text("SELECT key_hash, scope, label FROM api_clients")).all()
    seeded = {key_hash: (scope, label) for key_hash, scope, label in rows}
    for key_hash, expected in EXPECTED.items():
        assert seeded.get(key_hash) == expected, f"missing or wrong seed row for {key_hash}"


@pytest.mark.parametrize("key", [DEMO_CLIENT_API_KEY, DEMO_ADMIN_API_KEY])
def test_no_plaintext_key_is_stored(owner_engine: Engine, key: str) -> None:
    with owner_engine.connect() as conn:
        hits = conn.execute(
            text("SELECT count(*) FROM api_clients WHERE key_hash = :key OR label = :key"),
            {"key": key},
        ).scalar_one()
    assert hits == 0


@pytest.mark.parametrize("key", [DEMO_CLIENT_API_KEY, DEMO_ADMIN_API_KEY])
def test_python_and_pgcrypto_agree_on_the_hash(owner_engine: Engine, key: str) -> None:
    """The migration re-implements the hash by design (migrations are immutable
    history). This is the test that stops the two definitions drifting."""
    with owner_engine.connect() as conn:
        in_sql = conn.execute(
            text("SELECT encode(digest(:salt || ':' || :key, 'sha256'), 'hex')"),
            {"salt": API_KEY_SALT, "key": key},
        ).scalar_one()
    assert in_sql == api_key_hash(key)
