"""The result type every suite check returns, and the honest-failure exception.

A scorecard is only worth reading if a green row means the check *ran and
passed*. Two rules follow from that and are enforced here rather than left to
each check's author:

* there is no ``SKIP`` status. A check that cannot run has not passed, and a
  harness that can report "not applicable" will eventually report it for the
  one thing that mattered;
* work that is not built yet raises :class:`NotYetImplemented`, which the
  runner turns into a **FAIL** carrying the reason. A check that passes
  vacuously -- because its subject does not exist, because a query matched no
  rows, because an import failed -- is indistinguishable from a check that
  passes because the system is correct, and that is the precise failure mode
  this package exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["FAIL", "PASS", "CheckResult", "NotYetImplemented"]

PASS = "PASS"
FAIL = "FAIL"


class NotYetImplemented(RuntimeError):
    """Raised by a check whose subject has not been built yet.

    Deliberately an error and not a skip. The runner reports it as ``FAIL`` with
    the message attached, so an unfinished check is loud in the scorecard and in
    the process exit code instead of quietly counting as evidence.
    """


@dataclass(frozen=True)
class CheckResult:
    """One row of the scorecard."""

    name: str
    status: str
    detail: str

    def __post_init__(self) -> None:
        if self.status not in {PASS, FAIL}:
            raise ValueError(f"a check result is {PASS} or {FAIL}, not {self.status!r}")

    @property
    def ok(self) -> bool:
        return self.status == PASS

    def row(self) -> str:
        """Render as a scorecard line."""
        return f"{self.name:<40} {self.status:<8} {self.detail}"

    @classmethod
    def passed(cls, name: str, detail: str) -> CheckResult:
        return cls(name=name, status=PASS, detail=detail)

    @classmethod
    def failed(cls, name: str, detail: str) -> CheckResult:
        return cls(name=name, status=FAIL, detail=detail)
