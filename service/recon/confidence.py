"""R14's confidence model: the evaluator for the committed ``confidence.yaml``.

**This module holds no number.** Every base, every weight, every derivation
constant is read from ``confidence.yaml`` at the repository root, and a missing
or malformed entry raises :class:`ConfidenceModelError` rather than falling back
to a default. A fallback would make the committed file decorative -- the score
would keep being produced with the file deleted, and "the weights are committed"
would be a claim with nothing behind it.

Why that matters here specifically
----------------------------------
R14: *"Confidence SHALL be a [0,1] score computed by a committed deterministic
formula over inspectable signals (documented in ARCHITECTURE.md); same conflict +
same evidence => same score; partial/conflicting evidence lowers it. A hardcoded
constant or raw LLM-emitted number is a failure."*

Each clause is discharged by a mechanism, not by intent:

``committed``
    the numbers live in a versioned YAML file and
    ``tests/reconciler/test_confidence_yaml.py`` asserts every one of them
    verbatim, so changing a weight is a visible diff plus a red test rather than
    a silent behaviour change.

``deterministic, bit-for-bit``
    all arithmetic is :mod:`decimal`. Every value in the YAML is a **quoted
    string** and is parsed with ``Decimal(str)``, so no binary float is ever
    constructed and no result can vary with platform, libm or Python build. The
    terms are summed in the file's committed ``signal_order`` and the total is
    quantized to ``precision.decimal_places`` under ``precision.rounding``.
    Two processes on two machines produce the same digits.

``over inspectable signals``
    :class:`Score` carries the whole derivation -- the base, and for every signal
    its raw value, its weight and its contribution -- and the reconciler persists
    it in ``proposals.evidence``. A score whose inputs are not recorded is not
    inspectable, so the packet travels with the proposal rather than being
    recomputable-in-principle.

``partial/conflicting evidence lowers it``
    two negative signals do exactly that and nothing else does:
    ``partial_evidence`` (a degraded source, a null pinned observation, or a
    single-source packet) and ``disagreeing_field`` (one penalty per disagreeing
    COMPARISON ROW -- contract SS2.4 puts both endpoints of a row into
    ``disagreeing_fields``, so counting paths would double every penalty).

``never an LLM number``
    this module imports nothing from :mod:`recon.llm` and
    :func:`score` takes a :class:`Signals`, not text. ``tests/reconciler`` asserts
    the absence of that import, because "we did not do it" is a promise and an
    import graph is a fact.

What this module is NOT
-----------------------
It is not the sensitivity classifier. R15's classification is a pure function of
the target field path (contract SS6), evaluated **before** confidence and winning
over it at every score including 1.0 -- see :mod:`recon.sensitive`. Nothing here
takes a field path, and :func:`score` cannot hold or release a proposal.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import yaml

from recon.reference import COMPARED_FIELD_BY_PATH, CONFLICT_TYPES

__all__ = [
    "MODEL_FILENAME",
    "SIGNAL_KINDS",
    "ConfidenceModel",
    "ConfidenceModelError",
    "Score",
    "SignalTerm",
    "Signals",
    "clamp",
    "disagreeing_row_count",
    "load_model",
    "model_path",
    "score",
]

#: The committed model, at the repository root next to ``prices.yaml``.
MODEL_FILENAME: Final = "confidence.yaml"

#: The two shapes a signal value may take. ``boolean`` is 0 or 1; ``count`` is a
#: non-negative integer that multiplies its weight.
SIGNAL_KINDS: Final = frozenset({"boolean", "count"})

#: `decimal` precision for the sum. The values are two-decimal-place literals and
#: the counts are small integers, so 28 significant digits is enormous headroom --
#: it is pinned rather than inherited so a caller's `decimal` context cannot reach
#: in and change a graded number.
_PRECISION: Final = 28


class ConfidenceModelError(RuntimeError):
    """The committed model is missing, malformed, or incomplete.

    Always an error, never a default. A confidence model that degrades to
    hardcoded numbers when its file is unreadable is a hardcoded confidence
    model with extra steps, and R14 names that outcome a failure.
    """


def model_path() -> Path:
    """Absolute path to the committed ``confidence.yaml``.

    Resolved from this file's location (``service/recon/`` -> repository root),
    so it is the same file whatever the working directory is.
    """
    return Path(__file__).resolve().parents[2] / MODEL_FILENAME


def _decimal(raw: Any, *, where: str) -> Decimal:
    """Parse a committed numeric literal.

    Requires a **string**. A YAML float would already have been through binary
    floating point before this function ever saw it, and ``Decimal(0.35)`` is
    ``0.34999999999999997779...`` -- so the one place a float could enter the
    graded arithmetic is closed by refusing the type outright rather than by
    converting carefully.
    """
    if not isinstance(raw, str):
        raise ConfidenceModelError(
            f"{where}: numeric values must be quoted strings so they parse exactly "
            f"as Decimal, got {type(raw).__name__} {raw!r}"
        )
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ConfidenceModelError(f"{where}: {raw!r} is not a decimal literal") from exc


@dataclass(frozen=True, slots=True)
class SignalDef:
    """One committed signal: its weight, its shape, and its stated derivation."""

    name: str
    weight: Decimal
    kind: str
    sign: str
    derivation: str
    #: Only ``amount_date_corroboration`` uses it: type -> the pinned
    #: ``observed_values`` keys that must all be present and non-null.
    corroborating_keys: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: Only the three identity signals use it: the ``entity_link_candidates``
    #: key class this signal tests.
    key_class: str | None = None


@dataclass(frozen=True, slots=True)
class ConfidenceModel:
    """The parsed, validated ``confidence.yaml``."""

    version: int
    formula: str
    bases: Mapping[str, Decimal]
    base_notes: Mapping[str, str]
    signal_order: tuple[str, ...]
    signals: Mapping[str, SignalDef]
    decimal_places: int
    rounding: str
    clamp_min: Decimal
    clamp_max: Decimal
    source: Path

    @property
    def quantum(self) -> Decimal:
        """The exponent every score is quantized to (``0.0001`` at 4 places)."""
        return Decimal(1).scaleb(-self.decimal_places)

    def base(self, conflict_type: str) -> Decimal:
        try:
            return self.bases[conflict_type]
        except KeyError:
            raise ConfidenceModelError(
                f"{self.source.name} has no base for conflict type {conflict_type!r}"
            ) from None

    def signal(self, name: str) -> SignalDef:
        try:
            return self.signals[name]
        except KeyError:
            raise ConfidenceModelError(f"{self.source.name} has no signal named {name!r}") from None

    def key_class_signals(self) -> Mapping[str, str]:
        """``key_class -> signal name`` for the three identity signals."""
        return {
            definition.key_class: name
            for name, definition in self.signals.items()
            if definition.key_class is not None
        }

    def corroborating_keys(self, conflict_type: str) -> tuple[str, ...]:
        """The pinned ``observed_values`` keys that corroborate ``conflict_type``."""
        for definition in self.signals.values():
            if definition.corroborating_keys:
                return definition.corroborating_keys.get(conflict_type, ())
        return ()


def _require(mapping: Mapping[str, Any], key: str, *, where: str) -> Any:
    if key not in mapping:
        raise ConfidenceModelError(f"{where}: required key {key!r} is missing")
    return mapping[key]


def _parse(document: Any, source: Path) -> ConfidenceModel:
    if not isinstance(document, Mapping):
        raise ConfidenceModelError(f"{source}: the model must be a YAML mapping")

    version = _require(document, "version", where=str(source))
    if not isinstance(version, int):
        raise ConfidenceModelError(f"{source}: `version` must be an integer, got {version!r}")

    precision = _require(document, "precision", where=str(source))
    places = _require(precision, "decimal_places", where=f"{source}:precision")
    rounding = _require(precision, "rounding", where=f"{source}:precision")
    if not isinstance(places, int) or places < 0:
        raise ConfidenceModelError(f"{source}: precision.decimal_places must be a non-negative int")

    raw_bases = _require(document, "bases", where=str(source))
    bases: dict[str, Decimal] = {}
    notes: dict[str, str] = {}
    for conflict_type, entry in raw_bases.items():
        where = f"{source}:bases.{conflict_type}"
        bases[conflict_type] = _decimal(_require(entry, "value", where=where), where=where)
        notes[conflict_type] = str(_require(entry, "note", where=where))

    # Totality over the committed conflict catalogue. A type with no base would
    # otherwise surface as a KeyError at scoring time on whichever conflict
    # happened to arrive first -- i.e. in production, on one type, at random.
    missing = [conflict_type for conflict_type in CONFLICT_TYPES if conflict_type not in bases]
    if missing:
        raise ConfidenceModelError(f"{source}: no base for conflict type(s) {missing}")
    unknown = sorted(set(bases) - set(CONFLICT_TYPES))
    if unknown:
        raise ConfidenceModelError(f"{source}: base for unknown conflict type(s) {unknown}")

    raw_signals = _require(document, "signals", where=str(source))
    signals: dict[str, SignalDef] = {}
    for name, entry in raw_signals.items():
        where = f"{source}:signals.{name}"
        kind = str(_require(entry, "kind", where=where))
        if kind not in SIGNAL_KINDS:
            raise ConfidenceModelError(f"{where}: kind must be one of {sorted(SIGNAL_KINDS)}")
        corroborating = entry.get("corroborating_keys") or {}
        if corroborating:
            unknown_types = sorted(set(corroborating) - set(CONFLICT_TYPES))
            if unknown_types:
                raise ConfidenceModelError(
                    f"{where}: corroborating_keys names unknown conflict type(s) {unknown_types}"
                )
            absent = [
                conflict_type
                for conflict_type in CONFLICT_TYPES
                if conflict_type not in corroborating
            ]
            if absent:
                raise ConfidenceModelError(
                    f"{where}: corroborating_keys must name every conflict type; missing {absent}"
                )
        signals[name] = SignalDef(
            name=name,
            weight=_decimal(_require(entry, "weight", where=where), where=where),
            kind=kind,
            sign=str(_require(entry, "sign", where=where)),
            derivation=str(_require(entry, "derivation", where=where)),
            corroborating_keys={
                conflict_type: tuple(keys) for conflict_type, keys in corroborating.items()
            },
            key_class=entry.get("key_class"),
        )

    order = tuple(_require(document, "signal_order", where=str(source)))
    if set(order) != set(signals):
        raise ConfidenceModelError(
            f"{source}: signal_order {sorted(order)} does not cover exactly the "
            f"defined signals {sorted(signals)}"
        )
    if len(order) != len(set(order)):
        raise ConfidenceModelError(f"{source}: signal_order repeats a signal")

    for name, definition in signals.items():
        expected = "negative" if definition.weight < 0 else "positive"
        if definition.sign != expected:
            raise ConfidenceModelError(
                f"{source}:signals.{name}: sign says {definition.sign!r} but the "
                f"weight {definition.weight} is {expected}"
            )

    return ConfidenceModel(
        version=version,
        formula=str(_require(document, "formula", where=str(source))),
        bases=bases,
        base_notes=notes,
        signal_order=order,
        signals=signals,
        decimal_places=places,
        rounding=str(rounding),
        clamp_min=_decimal(
            _require(precision, "clamp_min", where=f"{source}:precision"),
            where=f"{source}:precision.clamp_min",
        ),
        clamp_max=_decimal(
            _require(precision, "clamp_max", where=f"{source}:precision"),
            where=f"{source}:precision.clamp_max",
        ),
        source=source,
    )


@lru_cache(maxsize=4)
def _load_cached(path: str, mtime_ns: int) -> ConfidenceModel:
    # `mtime_ns` is part of the key purely so an edited file is re-read; the model
    # is otherwise immutable for the life of the process.
    del mtime_ns
    source = Path(path)
    try:
        document = yaml.safe_load(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfidenceModelError(
            f"the committed confidence model is missing at {source}. There is no "
            "fallback: R14 requires the weights to be committed, and a model that "
            "keeps scoring without its file is a hardcoded model."
        ) from exc
    except yaml.YAMLError as exc:
        raise ConfidenceModelError(f"{source} is not valid YAML: {exc}") from exc
    return _parse(document, source)


def load_model(path: Path | str | None = None) -> ConfidenceModel:
    """Load and validate the committed model. Raises rather than defaulting."""
    source = Path(path) if path is not None else model_path()
    try:
        mtime_ns = source.stat().st_mtime_ns
    except FileNotFoundError as exc:
        raise ConfidenceModelError(
            f"the committed confidence model is missing at {source}. There is no "
            "fallback: R14 requires the weights to be committed, and a model that "
            "keeps scoring without its file is a hardcoded model."
        ) from exc
    return _load_cached(str(source), mtime_ns)


# =====================================================================================
# the evidence signals
# =====================================================================================
def disagreeing_row_count(paths: Iterable[str]) -> int:
    """How many COMPARED_FIELDS comparison **rows** ``paths`` covers.

    The unit of the ``disagreeing_field`` penalty, and the reason it is a
    function rather than ``len(paths)``: contract SS2.4 defines
    ``disagreeing_fields`` as "the sorted set of **both** source-qualified paths
    of every disagreeing comparison", so a grade-only C6 carries
    ``crm.contact.grade`` and ``appdb.student.grade`` -- two paths, **one**
    disagreement. Counting paths would charge every conflict twice and would
    charge a mixed C6 four times.

    A path outside the comparison vocabulary is counted as its own row rather
    than dropped: an unrecognised disagreeing path is still a disagreement, and
    silently scoring it as free is the wrong direction to fail in.
    """
    rows: set[str] = set()
    for path in paths:
        row = COMPARED_FIELD_BY_PATH.get(path)
        rows.add(row.logical if row is not None else f"?{path}")
    return len(rows)


@dataclass(frozen=True, slots=True)
class Signals:
    """The evaluated signal values for one conflict -- the packet's raw inputs.

    Built by :mod:`recon.reconciler` from durable tables; kept as a plain value
    object so a test can construct one directly and so the arithmetic can be
    checked without a database.
    """

    conflict_type: str
    hard_external_id_agreement: bool = False
    normalized_email_agreement: bool = False
    name_dob_exact: bool = False
    amount_date_corroboration: bool = False
    #: Number of disagreeing COMPARISON ROWS (see :func:`disagreeing_row_count`).
    disagreeing_field: int = 0
    partial_evidence: bool = False
    oscillation_observed: bool = False
    #: Named reasons behind ``partial_evidence``, carried into the packet so the
    #: penalty says WHY. Never an input to the arithmetic.
    partial_evidence_reasons: tuple[str, ...] = ()

    def value(self, name: str) -> int:
        """The signal's numeric value: 0/1 for booleans, the count otherwise."""
        raw = getattr(self, name)
        if isinstance(raw, bool):
            return 1 if raw else 0
        if isinstance(raw, int) and raw >= 0:
            return raw
        raise ConfidenceModelError(f"signal {name!r} has a non-scoreable value {raw!r}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "conflict_type": self.conflict_type,
            "hard_external_id_agreement": self.hard_external_id_agreement,
            "normalized_email_agreement": self.normalized_email_agreement,
            "name_dob_exact": self.name_dob_exact,
            "amount_date_corroboration": self.amount_date_corroboration,
            "disagreeing_field": self.disagreeing_field,
            "partial_evidence": self.partial_evidence,
            "oscillation_observed": self.oscillation_observed,
            "partial_evidence_reasons": list(self.partial_evidence_reasons),
        }


@dataclass(frozen=True, slots=True)
class SignalTerm:
    """One line of the arithmetic, as it is shown to a reviewer."""

    name: str
    value: int
    weight: Decimal
    contribution: Decimal

    def as_dict(self) -> dict[str, Any]:
        return {
            "signal": self.name,
            "value": self.value,
            "weight": str(self.weight),
            "contribution": str(self.contribution),
        }


@dataclass(frozen=True, slots=True)
class Score:
    """A confidence score **and** the derivation that produced it.

    ``value`` is what lands in ``proposals.confidence``; ``as_dict()`` is what
    lands in ``proposals.evidence['confidence']``. They are produced together so
    a stored score can never exist without its inputs.
    """

    value: Decimal
    conflict_type: str
    base: Decimal
    terms: tuple[SignalTerm, ...]
    raw_total: Decimal
    clamped: bool
    model_version: int
    formula: str
    signals: Signals

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_version": self.model_version,
            "formula": self.formula,
            "conflict_type": self.conflict_type,
            "base": str(self.base),
            "terms": [term.as_dict() for term in self.terms],
            "raw_total": str(self.raw_total),
            "clamped": self.clamped,
            "confidence": str(self.value),
            "signals": self.signals.as_dict(),
        }


def clamp(total: Decimal, model: ConfidenceModel) -> tuple[Decimal, bool]:
    """Clamp ``total`` into the committed ``[clamp_min, clamp_max]`` window."""
    if total < model.clamp_min:
        return model.clamp_min, True
    if total > model.clamp_max:
        return model.clamp_max, True
    return total, False


def score(signals: Signals, *, model: ConfidenceModel | None = None) -> Score:
    """``clamp01(base[type] + sum(w_i * s_i))`` -- the committed formula, evaluated.

    Deterministic bit-for-bit: :mod:`decimal` throughout, a pinned context, terms
    summed in the file's ``signal_order``, and one quantization at the end under
    the file's ``rounding``. No float is constructed at any point, so nothing in
    this function can vary between two processes reading the same YAML.
    """
    active = model or load_model()
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        base = active.base(signals.conflict_type)
        total = base
        terms: list[SignalTerm] = []
        for name in active.signal_order:
            definition = active.signal(name)
            value = signals.value(name)
            if definition.kind == "boolean" and value not in (0, 1):
                raise ConfidenceModelError(
                    f"signal {name!r} is committed as boolean but scored {value!r}"
                )
            contribution = definition.weight * value
            total += contribution
            terms.append(
                SignalTerm(
                    name=name, value=value, weight=definition.weight, contribution=contribution
                )
            )
        bounded, clamped = clamp(total, active)
        value = bounded.quantize(active.quantum, rounding=active.rounding)

    return Score(
        value=value,
        conflict_type=signals.conflict_type,
        base=base,
        terms=tuple(terms),
        raw_total=total,
        clamped=clamped,
        model_version=active.version,
        formula=active.formula,
        signals=signals,
    )


def observed_value_is_null(value: Any) -> bool:
    """The committed emptiness test behind ``partial_evidence``'s clause (b).

    ``None``, the empty string and the empty sequence all mean "the predicate
    named this fact and it was not observed". ``False`` and ``0`` do NOT: a
    ``metadata_name_pair_present: false`` is an observation, and treating it as a
    hole would penalise C2 for the very fact that defines it.
    """
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, (str, bytes, list, tuple, dict, set)):
        return len(value) == 0
    return False


def partial_evidence_reasons(
    *,
    incomplete_sources: Sequence[str],
    observed_values: Mapping[str, Any],
    sources_involved: Sequence[str],
    contradictory_match_keys: Sequence[str] = (),
) -> tuple[str, ...]:
    """The named reasons for ``partial_evidence``; empty means the signal is 0.

    Four clauses, three of them "partial" and one of them "conflicting" -- R14
    names both and weights them the same:

    ``incomplete_sources``
        ``source_generations`` reports a generation-3 source as not complete, so
        the run is degraded (contract SS5.3) and a rule may have read a partial
        snapshot.
    ``null_observed_values``
        a fact the predicate names was not actually observed.
    ``single_source``
        fewer than two systems appear in the packet, so nothing corroborates
        anything.
    ``contradictory_match_keys``
        two match-key classes resolve ONE source record to two DIFFERENT
        entities. This is the "conflicting evidence" half of R14 at the identity
        level rather than the field level, and it is what a C10
        (merge-collapsed record) is made of.

    Returned as names rather than a bare bool so the persisted packet says which
    clause fired. Sorted, so the packet is byte-stable across runs.
    """
    reasons: list[str] = []
    if incomplete_sources:
        reasons.append("incomplete_sources:" + ",".join(sorted(incomplete_sources)))
    holes = sorted(key for key, value in observed_values.items() if observed_value_is_null(value))
    if holes:
        reasons.append("null_observed_values:" + ",".join(holes))
    if len(set(sources_involved)) < 2:
        reasons.append("single_source:" + ",".join(sorted(set(sources_involved))))
    if contradictory_match_keys:
        reasons.append("contradictory_match_keys:" + ",".join(sorted(contradictory_match_keys)))
    return tuple(reasons)
