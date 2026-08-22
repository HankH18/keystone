"""Seed the committed demo API clients (hashes only, never plaintext).

Revision ID: 0003_seed_api_clients
Revises: 0002_roles_and_grants
Create Date: 2026-08-22

Two committed demo keys, per ``docs/DESIGN.md``: one ``client``-scoped key that
exists to demonstrate tenant isolation, and one ``admin``-scoped key the
dashboard uses for reviewer actions.

The database stores only ``sha256(f"{API_KEY_SALT}:{key}")`` in hex. The
plaintext keys live in ``.env.example`` and nowhere else in the repository --
they are demo credentials for a synthetic dataset, deliberately committed so
the demo is reproducible, and they must never be reused for anything real.

The salt and hashing are re-implemented here rather than imported from
``recon.db``. A migration is immutable history: if the application's hashing
helper ever changes, this revision must still describe what it actually wrote.
``tests/schema/test_api_clients_seed.py`` asserts the two agree today, so the
duplication cannot silently drift while both are in use.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_seed_api_clients"
down_revision: str | None = "0002_roles_and_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

API_KEY_SALT = "keystone-api-key-salt-v1"

DEMO_CLIENT_API_KEY = "keystone-demo-client-3f7a19c4e2b84d05"
DEMO_ADMIN_API_KEY = "keystone-demo-admin-8c25e0b71a94f36d"

DEMO_CLIENTS = (
    (DEMO_CLIENT_API_KEY, "client", "demo-client"),
    (DEMO_ADMIN_API_KEY, "admin", "demo-admin"),
)


def _key_hash(key: str) -> str:
    return hashlib.sha256(f"{API_KEY_SALT}:{key}".encode()).hexdigest()


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            INSERT INTO api_clients (key_hash, scope, label)
            VALUES (:key_hash, CAST(:scope AS api_client_scope), :label)
            ON CONFLICT (key_hash) DO NOTHING
            """
        ),
        [
            {"key_hash": _key_hash(key), "scope": scope, "label": label}
            for key, scope, label in DEMO_CLIENTS
        ],
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text("DELETE FROM api_clients WHERE key_hash IN :hashes").bindparams(
            sa.bindparam("hashes", expanding=True)
        ),
        {"hashes": [_key_hash(key) for key, _, _ in DEMO_CLIENTS]},
    )
