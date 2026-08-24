"""Find every environment variable the shipped code reads, mechanically.

`.env.example` calls itself "the authoritative list". Keeping that true by
reading it is how it stopped being true: a long tail of variables the code
really reads -- among them the three writer-role passwords and
`OPS_DATABASE_URL`, which `infra/render.yaml` declares -- was never written
down at all.
`Settings`'s `extra="ignore"` meant a documented-but-unknown name was dropped
in silence, so neither direction of that drift could ever fail anything.

Reading the source by hand is not a substitute, and this module exists because
a careful hand review of exactly this question got it backwards: it reported
`EMBEDDING_PROVIDER`, `VOYAGE_API_KEY` and `OPENAI_API_KEY` as "dead
everywhere -- zero code references" and recommended deleting them.
`recon.incidents.build_embedding_provider` reads all three, and deleting them
would have taken the embedding configuration with them.

This module is the mechanical half of the fix: it parses the source and returns
the variable names, so the contract test compares the file against the code
rather than against a second hand-maintained list that can drift in its own
right.

What it resolves
----------------
* ``os.environ.get(X)``, ``os.environ[X]``, ``os.environ.pop(X)``,
  ``os.getenv(X)`` -- where ``X`` is a string literal, or a module-level
  ``NAME = "STRING"`` constant defined anywhere in the scanned tree (so
  ``os.environ.get(DAILY_SCOPE_ENV)`` in one module resolves through the
  constant defined in another).
* the same members on a **copy** of the environment: ``env = dict(os.environ)``
  followed by ``env.get(X)``. `recon.suite.coverage` reads
  ``KEYSTONE_COVERAGE_DATABASE_URL`` exactly that way, and a scanner that only
  understood ``os.environ.get`` would have called the repository's most
  destructive undocumented variable "not read by any code".
* calls to the project's own env-reading helpers, by name and argument
  position -- currently ``cap_microusd_from_env(env_var, default)``.

Only **reads** count. ``os.environ[X] = value`` is the code deciding what a
child process gets, not something an operator configures, so a subscript in a
store context is skipped while ``.get``/``.pop``/``.setdefault`` -- which all
have an operator-supplied value to find -- are not.

What it cannot resolve, and says so
-----------------------------------
An expression rather than a name: ``os.environ.get(f"{role.upper()}_PASSWORD")``
and ``os.environ.get(env_var)`` where ``env_var`` is a parameter. Those are
returned as :class:`DynamicRead` entries, by file, and the contract test pins
the set of files that contain them. A new one in a new file fails the test and
has to be accounted for by hand -- which is the honest outcome, not a silent
gap.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ENV_READING_HELPERS",
    "DynamicRead",
    "EnvScan",
    "scan_env_reads",
    "seeded_scope_env_vars",
    "writer_role_password_vars",
]

#: Project helpers that take an environment-variable NAME as an argument.
#: Maps the function name to the position of that argument.
ENV_READING_HELPERS: dict[str, int] = {
    "cap_microusd_from_env": 0,
    "_cap_microusd": 0,
}

#: `os.environ` members that take a variable name as their first argument.
_ENVIRON_METHODS = frozenset({"get", "pop", "setdefault"})


@dataclass(frozen=True)
class DynamicRead:
    """An environment read whose variable name is computed, not written."""

    path: str
    lineno: int
    source: str


@dataclass(frozen=True)
class EnvScan:
    """What :func:`scan_env_reads` found."""

    #: Variable name -> the ``path:lineno`` sites that read it.
    names: dict[str, tuple[str, ...]]
    #: Reads whose name could not be resolved statically.
    dynamic: tuple[DynamicRead, ...]

    def dynamic_files(self) -> frozenset[str]:
        """The distinct files containing an unresolvable read."""
        return frozenset(read.path for read in self.dynamic)


def _python_files(roots: Iterable[Path]) -> Iterator[Path]:
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


def _is_os_environ(node: ast.expr) -> bool:
    """True for the expression ``os.environ``."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _is_os_getenv(node: ast.expr) -> bool:
    """True for the expression ``os.getenv``."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "getenv"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "STRING"`` / ``NAME: Final = "STRING"`` bindings."""
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
            value = node.value  # type: ignore[assignment]
        else:
            continue
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        for target in targets:
            found[target.id] = value.value
    return found


def _environ_copies(tree: ast.Module) -> frozenset[str]:
    """Names bound to ``dict(os.environ)`` or ``os.environ.copy()`` anywhere.

    Collected module-wide rather than per-scope on purpose: the failure this
    guards is an *undocumented* variable, so a name that is an environment copy
    in one function and something else in another costs at most one extra
    candidate to document -- never a missed one.
    """
    copies: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
            value = node.value  # type: ignore[assignment]
        else:
            continue
        if not isinstance(value, ast.Call):
            continue
        func = value.func
        is_copy = (
            isinstance(func, ast.Name)
            and func.id == "dict"
            and len(value.args) == 1
            and _is_os_environ(value.args[0])
        ) or (
            isinstance(func, ast.Attribute) and func.attr == "copy" and _is_os_environ(func.value)
        )
        if is_copy:
            copies.update(target.id for target in targets)
    return frozenset(copies)


def _key_nodes(tree: ast.Module) -> Iterator[ast.expr]:
    """Every expression used as an environment-variable name in ``tree``."""
    copies = _environ_copies(tree)

    def _is_environ_like(node: ast.expr) -> bool:
        return _is_os_environ(node) or (isinstance(node, ast.Name) and node.id in copies)

    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _is_environ_like(node.value):
            # A store is the code configuring a child, not an operator's input.
            if isinstance(node.ctx, ast.Load):
                yield node.slice
        elif isinstance(node, ast.Call):
            func = node.func
            takes_a_name = (
                isinstance(func, ast.Attribute)
                and func.attr in _ENVIRON_METHODS
                and _is_environ_like(func.value)
            ) or _is_os_getenv(func)
            if takes_a_name and node.args:
                yield node.args[0]
            elif isinstance(func, ast.Name) and func.id in ENV_READING_HELPERS:
                index = ENV_READING_HELPERS[func.id]
                if len(node.args) > index:
                    yield node.args[index]


def scan_env_reads(roots: Iterable[Path], repo_root: Path) -> EnvScan:
    """Scan ``roots`` for environment reads. Paths are reported repo-relative."""
    paths = list(_python_files(roots))
    trees: dict[Path, ast.Module] = {}
    constants: dict[str, set[str]] = {}

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        trees[path] = tree
        for name, value in _module_constants(tree).items():
            constants.setdefault(name, set()).add(value)

    #: A name bound to two different literals cannot be resolved unambiguously.
    unique = {name: next(iter(values)) for name, values in constants.items() if len(values) == 1}

    names: dict[str, list[str]] = {}
    dynamic: list[DynamicRead] = []
    for path, tree in trees.items():
        relative = path.relative_to(repo_root).as_posix()
        source_lines = path.read_text(encoding="utf-8").splitlines()
        for key in _key_nodes(tree):
            resolved: str | None = None
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                resolved = key.value
            elif isinstance(key, ast.Name):
                resolved = unique.get(key.id)
            if resolved is None:
                inside = key.lineno <= len(source_lines)
                line = source_lines[key.lineno - 1].strip() if inside else ""
                dynamic.append(DynamicRead(relative, key.lineno, line))
            else:
                names.setdefault(resolved, []).append(f"{relative}:{key.lineno}")

    return EnvScan(
        names={name: tuple(sorted(sites)) for name, sites in sorted(names.items())},
        dynamic=tuple(sorted(dynamic, key=lambda read: (read.path, read.lineno))),
    )


def writer_role_password_vars(roles: Iterable[str]) -> frozenset[str]:
    """``<ROLE>_PASSWORD`` for each role, exactly as `recon.db.role_password` builds it.

    Derived from `recon.db.WRITER_ROLES` rather than typed out, so a fourth
    least-privilege role automatically demands a fourth documented variable.
    """
    return frozenset(f"{role.upper()}_PASSWORD" for role in roles)


def seeded_scope_env_vars(migration: Path) -> frozenset[str]:
    """The cap variables migration 0005's ``SEEDED_SCOPES`` reads.

    ``SEEDED_SCOPES`` is a tuple of ``(scope, env_var, default)`` triples fed to
    a helper that reads ``os.environ`` through a loop variable, so no static
    resolution reaches it. Reading the literal out of the tuple keeps the answer
    derived from the migration instead of copied from it: adding a scope adds a
    variable this test then demands documentation for.
    """
    tree = ast.parse(migration.read_text(encoding="utf-8"), filename=str(migration))
    for node in tree.body:
        targets: list[ast.expr]
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value  # type: ignore[assignment]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "SEEDED_SCOPES" for t in targets):
            continue
        if not isinstance(value, ast.Tuple | ast.List):
            break
        found: set[str] = set()
        for element in value.elts:
            if not isinstance(element, ast.Tuple | ast.List) or len(element.elts) < 2:
                continue
            env_var = element.elts[1]
            if isinstance(env_var, ast.Constant) and isinstance(env_var.value, str):
                found.add(env_var.value)
        if found:
            return frozenset(found)
        break
    raise AssertionError(
        f"{migration.name} no longer defines SEEDED_SCOPES as a tuple of "
        "(scope, env_var, default) literals; this helper can no longer derive the "
        "cap variables it documents, so it must be updated rather than deleted."
    )
