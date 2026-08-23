"""Invariant engine: versioned cross-source rules from `rules/` (T-6).

Public surface:

* :func:`recon.invariants.runner.run_invariants` -- execute every rule against
  generation 3, stamp `invariant_results`, materialize the surviving conflicts.
* :func:`recon.invariants.grading.grade_run` -- the golden diff: false negatives and
  false positives against `golden/conflicts.json`, plus the `golden/clean-sample.json`
  intersection probe.
"""

from recon.invariants.context import CURRENT_GENERATION, InvariantContext, build_context
from recon.invariants.rules import RuleSpec, load_rules
from recon.invariants.runner import DetectedConflict, InvariantRun, persist_run, run_invariants

__all__ = [
    "CURRENT_GENERATION",
    "DetectedConflict",
    "InvariantContext",
    "InvariantRun",
    "RuleSpec",
    "build_context",
    "load_rules",
    "persist_run",
    "run_invariants",
]
