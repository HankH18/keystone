"""`python -m recon.invariants` -- the runnable golden-diff check.

The ticket asks for "a runnable check comparing detected conflicts against
golden/conflicts.json". This is it, and its **exit status** is the contract: 0 only
when there are zero false negatives, zero false positives, zero SS5.4 field-exactness
mismatches and zero flagged clean-sample entities.
"""

from __future__ import annotations

import pytest

from recon.invariants.__main__ import main


def test_the_cli_runs_the_engine_and_reports_a_clean_diff(ingested_dsn, capsys) -> None:
    status = main(["--dsn", ingested_dsn, "--run-id", "t6-cli"])
    output = capsys.readouterr().out
    assert status == 0, output
    assert "FALSE NEGATIVES: 0 {}" in output
    assert "FALSE POSITIVES: 0 {}" in output
    assert "FLAGGED: 0" in output
    assert "R-000 stg_crm_deal" in output
    assert "wall clock:" in output


def test_the_cli_persists_when_asked(ingested_dsn, capsys) -> None:
    import psycopg

    status = main(["--dsn", ingested_dsn, "--run-id", "t6-cli-persist", "--persist"])
    assert status == 0, capsys.readouterr().out
    with psycopg.connect(ingested_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM invariant_results WHERE run_id = 't6-cli-persist'")
        assert cur.fetchone()[0] == 376_000
        cur.execute("SELECT count(*) FROM conflicts WHERE first_seen_run = 't6-cli-persist'")
        assert cur.fetchone()[0] == 3050
        cur.execute("DELETE FROM conflicts WHERE first_seen_run = 't6-cli-persist'")
        cur.execute("DELETE FROM invariant_results WHERE run_id = 't6-cli-persist'")
        conn.commit()


def test_the_cli_refuses_to_guess_a_database(monkeypatch) -> None:
    """`DATABASE_URL` drives everything; never hardcode a DSN (CLAUDE.md)."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        main([])
