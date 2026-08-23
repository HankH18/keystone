"""The graded verification suite: `python -m recon.suite` (T-14).

Ten checks and six benchmarks, one row each. The process exits **non-zero** if
any of them failed, the scorecard is written to `docs/scorecard.txt` (and, in the
shape `dashboard/src/lib/contract.ts` A4 pins, to `docs/scorecard.json`, which
`GET /api/scorecard` serves), and the whole thing runs with **no provider key**:
`LLM_PROVIDER` defaults to `mock` and the graded spend-cap burst drives the real
reservation ledger through `recon.llm.MockProvider`.

There is no ``SKIP``: see :mod:`recon.suite.checks` for why a check that cannot
run is a failure, and :mod:`recon.suite.pipeline` for the precondition that stops
an empty database from producing a small green.

Module map
-----------
``__main__``      the registry, the CLI, and the exit code
``pipeline``      the ONE graded pass every check reads, and its precondition
``golden``        golden-diff, clean-sample, join-check
``proposals``     proposal-safety, oscillation-dedup
``determinism``   two seeded generator runs + two detection passes
``manifest``      the generator's 47 self-checks and Appendix A's minimums
``coverage``      the >=80% gate, measured by really running pytest
``burst``         the 120-thread spend-cap burst (and its one cached outcome)
``mirror``        the landing/staging content digest
``report``        the two renderings: the human table and the A4 JSON body
``probe``         the in-process HTTP client the join check and two benchmarks use
"""
