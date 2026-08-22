-- Runs once on first boot of an empty data volume (docker-entrypoint-initdb.d).
-- Extensions only. All tables/indexes/roles are owned by alembic migrations (T-3).
--
-- vector   : pgvector, for the incident-similarity index (stretch #9).
-- pgcrypto : gen_random_uuid() + digest() for API-key hashing.
--            NOTE: uuid generation here is for non-graded rows only; deterministic
--            graded paths derive ids from the seeded PRNG, never from the database.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
