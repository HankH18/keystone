"""The graded verification suite: `python -m recon.suite` (T-14).

The scorecard prints one row per registered check and the process exits
non-zero if any of them failed. There is no ``SKIP``: see
:mod:`recon.suite.checks` for why a check that cannot run is a failure.
"""
