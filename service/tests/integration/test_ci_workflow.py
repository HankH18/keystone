"""The two CI pipelines, parsed as YAML -- the seed step must not overwrite `golden/`.

`golden/` is **tracked**; `fixtures/` is not (`.gitignore`: `fixtures/*`, with an
explicit `!fixtures/.gitkeep`). Those two facts pull the CI seed step in opposite
directions and the pipeline has to satisfy both:

* without a seed, every T-5-derived suite errors in collection -- `tests/er/dataset.py`
  resolves `FIXTURES = REPO_ROOT / "fixtures"` from `__file__`, reads no environment
  variable, and hard-fails when `manifest.json` is absent. One run produced 865 errors
  from exactly that;
* **but** a bare `python -m recon.seed --profile full` writes BOTH trees at the
  repository root (`recon/seed/run.py`: `root = Path(__file__).resolve().parents[3]`),
  so it overwrites the tracked `golden/*.json` before pytest reads them. That silently
  disarms `tests/seed/test_committed_golden.py`, whose whole job is to prove the
  COMMITTED bytes are what the committed generator produces: after the overwrite it
  compares the generator against itself and passes for a stale or hand-edited golden
  set. A seed step that regenerates the grading contract before checking it has
  removed the check.

So the pipelines seed into a scratch tree with `--out`, stage ONLY `fixtures/` back
into the workspace, and leave `golden/` exactly as checked out. Nothing enforced any
of that: no test asserted the seed step existed, none asserted its shape, and
`.gitlab-ci.yml` -- the file for the GRADED remote -- was not referenced by any test
at all, so deleting the whole repair kept every suite green.

This module is that enforcement. It parses both files as YAML (never regex) and
asserts the properties, not the wording: the gates each job runs, their order, that
every `recon.seed` invocation anywhere in CI carries `--out`, that the generated
fixtures are staged into the workspace while the scratch `golden/` is discarded, and
that the GitLab pipeline still mirrors the GitHub one gate for gate.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
GITHUB_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
GITLAB_PIPELINE = REPO_ROOT / ".gitlab-ci.yml"

#: Shell tokens that name the workspace itself. `--out` may not expand to any of them:
#: an out-directory that IS the checkout is a bare seed run wearing a flag.
WORKSPACE_TOKENS = frozenset(
    {
        "",
        ".",
        "./",
        "..",
        "../",
        "$PWD",
        "${PWD}",
        "$CI_PROJECT_DIR",
        "${CI_PROJECT_DIR}",
        "$CI_PROJECT_DIR/",
        "$GITHUB_WORKSPACE",
        "${GITHUB_WORKSPACE}",
        "$GITHUB_WORKSPACE/",
    }
)

#: Commands that move a tree around. Their destinations are what this module polices.
COPY_COMMANDS = frozenset({"cp", "rsync", "mv", "install", "ditto"})


# --------------------------------------------------------------------------- loading


def _load(path: Path) -> dict[str, Any]:
    assert path.is_file(), (
        f"{path.relative_to(REPO_ROOT)} is missing. Both pipelines are deliverables: "
        "GitHub is the mirror, labs.gauntletai.com is the graded remote, and a repair "
        "made to only one of them is a repair the graded remote never runs."
    )
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{path} did not parse as a YAML mapping"
    return document


def _github_job(name: str) -> dict[str, Any]:
    jobs = _load(GITHUB_WORKFLOW)["jobs"]
    assert name in jobs, f"the GitHub workflow has no `{name}` job (jobs: {sorted(jobs)})"
    return jobs[name]


def _gitlab_job(name: str) -> dict[str, Any]:
    document = _load(GITLAB_PIPELINE)
    assert name in document, f"the GitLab pipeline has no `{name}` job"
    job = document[name]
    assert isinstance(job, dict), f"`{name}` in the GitLab pipeline is not a job mapping"
    return job


def _github_commands(name: str) -> list[str]:
    """Every shell command the job runs, in order. Action steps have no `run`."""
    steps = _github_job(name)["steps"]
    return [step["run"] for step in steps if isinstance(step.get("run"), str)]


def _gitlab_commands(name: str) -> list[str]:
    job = _gitlab_job(name)
    commands: list[str] = []
    for key in ("before_script", "script", "after_script"):
        commands.extend(entry for entry in (job.get(key) or []) if isinstance(entry, str))
    return commands


def _lines(commands: Iterable[str]) -> list[str]:
    """Flatten multi-line `run:` blocks to individual, comment-free shell lines."""
    out: list[str] = []
    for command in commands:
        for line in command.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                out.append(stripped)
    return out


def _tokens(line: str) -> list[str]:
    try:
        return shlex.split(line)
    except ValueError:  # an unbalanced quote in some unrelated line
        return []


# ----------------------------------------------------------------------- the gates

#: The ordered gates the service job must run. Same list for both pipelines: this IS
#: the mirroring contract. Each entry is (label, predicate over one shell line).
SERVICE_GATES: tuple[tuple[str, Callable[[str], bool]], ...] = (
    ("uv sync --locked", lambda line: "uv sync --locked" in line),
    ("ruff check", lambda line: "ruff check" in line),
    ("ruff format --check", lambda line: "ruff format --check" in line),
    ("alembic upgrade head", lambda line: "alembic upgrade head" in line),
    ("seed the fixture tree", lambda line: "recon.seed" in line),
    ("pytest", lambda line: "uv run pytest" in line),
)

#: The dashboard job's gates, likewise ordered.
DASHBOARD_GATES: tuple[tuple[str, Callable[[str], bool]], ...] = (
    ("pnpm install --frozen-lockfile", lambda line: "pnpm install --frozen-lockfile" in line),
    ("pnpm lint", lambda line: line.startswith("pnpm lint")),
    ("pnpm test", lambda line: line.startswith("pnpm test") and "a11y" not in line),
    ("pnpm build", lambda line: line.startswith("pnpm build")),
    ("playwright install chromium", lambda line: "playwright install" in line),
    ("pnpm test:a11y", lambda line: "pnpm test:a11y" in line),
)

#: Environment every service job must carry, or a green proves less than it looks.
REQUIRED_SERVICE_ENV = {
    "KEYSTONE_REQUIRE_DB": "1",
    "LOG_MODE": "safe",
    "LLM_PROVIDER": "mock",
    "EMBEDDING_PROVIDER": "mock",
}


def _gate_positions(
    lines: list[str], gates: tuple[tuple[str, Callable[[str], bool]], ...], where: str
) -> list[int]:
    positions = []
    for label, predicate in gates:
        matches = [index for index, line in enumerate(lines) if predicate(line)]
        assert matches, f"{where} runs no `{label}` gate. Lines were:\n  " + "\n  ".join(lines)
        positions.append(matches[0])
    return positions


def _assert_ordered(
    positions: list[int], gates: tuple[tuple[str, Callable[[str], bool]], ...], where: str
) -> None:
    labels = [label for label, _ in gates]
    ordered = sorted(range(len(positions)), key=lambda index: positions[index])
    assert ordered == list(range(len(positions))), (
        f"{where} runs its gates out of order. Expected {labels}, found them at "
        f"{dict(zip(labels, positions, strict=True))}"
    )


# ------------------------------------------------------------------ the seed shape


def _seed_lines(commands: Iterable[str]) -> list[str]:
    return [line for line in _lines(commands) if "recon.seed" in line]


def _out_argument(line: str, where: str) -> str:
    tokens = _tokens(line)
    assert "--out" in tokens, (
        f"{where} runs the seed WITHOUT `--out`:\n    {line}\n"
        "A bare `python -m recon.seed` writes fixtures/ AND golden/ at the repository "
        "root, overwriting the tracked grading contract before pytest reads it -- which "
        "makes tests/seed/test_committed_golden.py compare the generator against itself "
        "and pass for a stale committed golden set. Seed into a scratch tree with "
        "`--out` and stage only fixtures/ back."
    )
    index = tokens.index("--out")
    assert index + 1 < len(tokens), f"{where}: `--out` has no argument:\n    {line}"
    value = tokens[index + 1]
    assert not value.startswith("-"), f"{where}: `--out` took a flag as its argument: {value}"
    return value


def _expand(value: str, environment: dict[str, str]) -> str:
    """Resolve one level of `$NAME` / `${NAME}` against a job's declared environment."""
    for key, resolved in environment.items():
        for form in (f"${{{key}}}", f"${key}"):
            if value == form:
                return str(resolved)
    return value


def _github_seed_step() -> dict[str, Any]:
    steps = _github_job("service")["steps"]
    seeding = [step for step in steps if "recon.seed" in str(step.get("run", ""))]
    assert len(seeding) == 1, (
        "the GitHub service job must have exactly one seed step; found "
        f"{len(seeding)}. Without it 865 tests error on the missing fixture tree."
    )
    return seeding[0]


def _staging_line(block: str, out_token: str) -> str | None:
    """The line that copies the scratch `fixtures/` into the workspace, if any."""
    for line in _lines([block]):
        tokens = _tokens(line)
        if not tokens or tokens[0] not in COPY_COMMANDS:
            continue
        operands = [token for token in tokens[1:] if not token.startswith("-")]
        if len(operands) < 2:
            continue
        source, destination = operands[0], operands[-1]
        if (
            "fixtures" in source
            and "fixtures" in destination
            and out_token in source
            and out_token not in destination
        ):
            return line
    return None


# ------------------------------------------------------------------------- premise


def test_the_premise_holds_golden_is_tracked_and_fixtures_are_generated() -> None:
    """Everything below follows from `.gitignore`. If that flips, re-read this file."""
    ignored = [line.strip() for line in (REPO_ROOT / ".gitignore").read_text().splitlines()]
    assert "fixtures/*" in ignored, (
        "fixtures/ is no longer gitignored. The CI seed step exists only because a "
        "fresh checkout has no fixture tree; re-derive the step if that changed."
    )
    assert not any(entry.lstrip("/").startswith("golden") for entry in ignored if entry), (
        "golden/ has been gitignored. It is the COMMITTED grading contract; "
        "tests/seed/test_committed_golden.py compares the committed bytes against a "
        "fresh generation and has nothing to compare if the tree stops being tracked."
    )
    for name in ("conflicts.json", "clean-sample.json", "expected-views.json"):
        assert (REPO_ROOT / "golden" / name).is_file(), f"golden/{name} is missing"


# --------------------------------------------------------------- the GitHub pipeline


def test_the_github_service_job_runs_every_gate_in_order() -> None:
    lines = _lines(_github_commands("service"))
    positions = _gate_positions(lines, SERVICE_GATES, "the GitHub service job")
    _assert_ordered(positions, SERVICE_GATES, "the GitHub service job")


def test_the_github_dashboard_job_runs_every_gate_in_order() -> None:
    lines = _lines(_github_commands("dashboard"))
    positions = _gate_positions(lines, DASHBOARD_GATES, "the GitHub dashboard job")
    _assert_ordered(positions, DASHBOARD_GATES, "the GitHub dashboard job")


def test_the_github_seed_step_writes_to_a_scratch_tree_not_the_workspace() -> None:
    step = _github_seed_step()
    environment = {**_github_job("service").get("env", {}), **step.get("env", {})}
    seeds = _seed_lines([step["run"]])
    assert len(seeds) == 1, f"expected one seed invocation, found {seeds}"
    raw = _out_argument(seeds[0], "the GitHub seed step")
    expanded = _expand(raw, {key: str(value) for key, value in environment.items()})
    assert expanded not in WORKSPACE_TOKENS, (
        f"the GitHub seed step's `--out` resolves to the workspace itself ({raw!r} -> "
        f"{expanded!r}), which writes golden/ exactly as a bare seed run would."
    )
    assert "--profile full" in seeds[0], (
        "the seed must run `--profile full`: recon/seed/run.py refuses any other "
        "profile for the repository tree, and tests/apply/store.py asserts the graded "
        "3,050-conflict distribution from golden/conflicts.json."
    )


def test_the_github_seed_step_stages_the_fixture_tree_into_the_workspace() -> None:
    step = _github_seed_step()
    out_token = _out_argument(_seed_lines([step["run"]])[0], "the GitHub seed step")
    staging = _staging_line(step["run"], out_token)
    assert staging is not None, (
        "the GitHub seed step writes its fixtures to a scratch tree and never stages "
        "them into the workspace. tests/er/dataset.py computes REPO_ROOT / 'fixtures' "
        "from __file__ and reads no environment variable (KEYSTONE_FIXTURES_DIR steers "
        "only recon.adapters.jsonl), so a tree left in the scratch directory leaves 865 "
        "tests erroring on a missing fixture tree."
    )


def test_the_github_seed_step_guards_the_committed_golden_tree() -> None:
    """The step asserts, in CI, the thing this module asserts statically."""
    step = _github_seed_step()
    guards = [
        line
        for line in _lines([step["run"]])
        if _tokens(line)[:1] == ["git"]
        and "diff" in _tokens(line)
        and any(token in _tokens(line) for token in ("--exit-code", "--quiet"))
        and any("golden" in token for token in _tokens(line))
    ]
    assert guards, (
        "the GitHub seed step has no `git diff --exit-code -- golden/` guard. The guard "
        "is what turns 'CI must not rewrite the grading contract' into a red build "
        "instead of a comment, on the day someone drops `--out` again."
    )


# --------------------------------------------------------------- the GitLab pipeline


def test_the_gitlab_pipeline_defines_the_same_jobs_as_github() -> None:
    github_jobs = set(_load(GITHUB_WORKFLOW)["jobs"])
    document = _load(GITLAB_PIPELINE)
    gitlab_jobs = {
        name
        for name, body in document.items()
        if isinstance(body, dict) and ("script" in body) and not name.startswith(".")
    }
    assert github_jobs <= gitlab_jobs, (
        f"the GitLab pipeline is missing {sorted(github_jobs - gitlab_jobs)}. "
        "labs.gauntletai.com is the graded remote; a gate that runs only on the GitHub "
        "mirror does not run on the repository that is read."
    )


def test_the_gitlab_service_job_mirrors_the_github_service_gates() -> None:
    lines = _lines(_gitlab_commands("service"))
    positions = _gate_positions(lines, SERVICE_GATES, "the GitLab service job")
    _assert_ordered(positions, SERVICE_GATES, "the GitLab service job")


def test_the_gitlab_dashboard_job_mirrors_the_github_dashboard_gates() -> None:
    lines = _lines(_gitlab_commands("dashboard"))
    positions = _gate_positions(lines, DASHBOARD_GATES, "the GitLab dashboard job")
    _assert_ordered(positions, DASHBOARD_GATES, "the GitLab dashboard job")


def test_the_gitlab_seed_step_writes_to_a_scratch_tree_and_stages_fixtures() -> None:
    job = _gitlab_job("service")
    blocks = [entry for entry in (job.get("script") or []) if "recon.seed" in entry]
    assert len(blocks) == 1, f"expected one seed command in the GitLab service job, got {blocks}"
    block = blocks[0]
    raw = _out_argument(_seed_lines([block])[0], "the GitLab seed step")
    variables = {key: str(value) for key, value in (job.get("variables") or {}).items()}
    expanded = _expand(raw, variables)
    assert expanded not in WORKSPACE_TOKENS, (
        f"the GitLab seed step's `--out` resolves to the workspace itself ({raw!r})."
    )
    assert _staging_line(block, raw) is not None, (
        "the GitLab seed step never stages its scratch fixtures/ into the workspace."
    )


def test_both_pipelines_pin_the_same_service_environment() -> None:
    github_env = {key: str(value) for key, value in _github_job("service").get("env", {}).items()}
    gitlab_env = {
        key: str(value) for key, value in (_gitlab_job("service").get("variables") or {}).items()
    }
    for key, expected in REQUIRED_SERVICE_ENV.items():
        assert github_env.get(key) == expected, f"GitHub service job: {key} must be {expected!r}"
        assert gitlab_env.get(key) == expected, f"GitLab service job: {key} must be {expected!r}"


# ------------------------------------------------- neither pipeline may write golden


@pytest.mark.parametrize("path", [GITHUB_WORKFLOW, GITLAB_PIPELINE], ids=["github", "gitlab"])
def test_no_ci_command_anywhere_seeds_without_an_out_directory(path: Path) -> None:
    """Belt and braces: not just the known step -- ANY `recon.seed` line in the file."""
    document = _load(path)
    jobs = document["jobs"].values() if path == GITHUB_WORKFLOW else document.values()
    commands: list[str] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                commands.append(step["run"])
        for key in ("before_script", "script", "after_script"):
            commands.extend(entry for entry in (job.get(key) or []) if isinstance(entry, str))
    seeds = _seed_lines(commands)
    assert seeds, f"{path.name} runs no seed at all; the fixture tree would never exist"
    for line in seeds:
        _out_argument(line, path.name)


@pytest.mark.parametrize("path", [GITHUB_WORKFLOW, GITLAB_PIPELINE], ids=["github", "gitlab"])
def test_no_ci_command_anywhere_copies_anything_into_golden(path: Path) -> None:
    document = _load(path)
    jobs = document["jobs"].values() if path == GITHUB_WORKFLOW else document.values()
    commands: list[str] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                commands.append(step["run"])
        for key in ("before_script", "script", "after_script"):
            commands.extend(entry for entry in (job.get(key) or []) if isinstance(entry, str))
    for line in _lines(commands):
        tokens = _tokens(line)
        if not tokens or tokens[0] not in COPY_COMMANDS:
            continue
        operands = [token for token in tokens[1:] if not token.startswith("-")]
        if len(operands) < 2:
            continue
        assert "golden" not in operands[-1], (
            f"{path.name} writes into the committed grading contract:\n    {line}\n"
            "golden/ is what the pipeline is checked AGAINST; CI may never produce it."
        )
