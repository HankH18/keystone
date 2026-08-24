"""Which rationale hook the production entrypoint actually asks for.

The defect: `recon.llm.generate_rationale` -- and behind it the whole R17
reserve -> call -> settle chain and the spend cap it enforces -- had **no
non-test caller anywhere in the repository**. `reconcile()` takes a `rationale`
hook, every caller used the default `no_rationale`, and so the reconciler had
never called a model outside a test. The cap was armed, correct, and never once
exercised by the service.

`reconcile_job` -- the body of `POST /internal/reconcile` -- now asks
`rationale_hook_for`. This module pins the two halves of that:

* under `LLM_PROVIDER=mock`, the default and every graded path, the hook is
  `no_rationale` **itself**, so nothing about a run changes. Identity is
  asserted, not equivalence: an equivalent wrapper that returned `None` would
  still build a provider, still render a prompt, and still be a behaviour change
  on the path determinism is graded on;
* under a live provider the hook is a real `generate_rationale` call.

Nothing here reaches the database. The ledger half -- that the hook really does
reserve, call and settle against real `budget_ledger` rows -- is
`tests/llm/test_reconciler_rationale_wiring.py`, which owns the live harness.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from recon.reconciler import (
    RATIONALE_KEY_PREFIX,
    ConflictRow,
    build_packet,
    no_rationale,
    rationale_hook_for,
    rationale_prompt,
)
from tests.budget.support import env_settings

FAKE_KEY = "sk-ant-not-a-real-key-0000000000"


@pytest.fixture(autouse=True)
def _settings_isolation() -> Iterator[None]:
    """No test here may leave `LLM_PROVIDER=anthropic` cached in `Settings`.

    `recon.config.get_settings` is `lru_cache`d and `env_settings` clears that
    cache; `monkeypatch` then restores the variables at teardown but nothing
    rebuilds the cached object, so a live-provider test would leave a cached
    `Settings` naming a live provider and a fake key for the rest of the pytest
    session. `tests/budget/support.py` has the same guard as a session fixture
    (`_settings_cache_isolation`) and `tests/llm/conftest.py` re-exports it;
    `tests/reconciler` does not, so this module brings its own.
    """
    from recon.config import get_settings

    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def _conflict() -> ConflictRow:
    return ConflictRow(
        id=1,
        fingerprint="fp-rationale-wiring",
        type="C6",
        rule_id="R-006",
        entity_refs=("appdb:student:s1", "crm:contact:c1"),
        sources_involved=("appdb", "crm"),
        disagreeing_fields=("appdb.student.grade", "crm.contact.grade"),
        observed_values={"appdb.student.grade": "7", "crm.contact.grade": "8"},
        oscillating=False,
        status="open",
        escalation_reason=None,
    )


def _packet(run_id: str = "wiring-run"):
    from recon.confidence import load_model
    from recon.reconciler import OSCILLATION_NO_INPUT

    return build_packet(
        _conflict(),
        run_id=run_id,
        generation=3,
        model=load_model(),
        candidates={},
        incomplete_sources=(),
        oscillating=False,
        oscillation_source=OSCILLATION_NO_INPUT,
        lineage_rows=0,
        lineage_generations=0,
    )


# ===========================================================================
# the graded path is unchanged
# ===========================================================================
def test_the_default_provider_yields_the_no_op_hook_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`LLM_PROVIDER=mock` must be byte-identical to having no hook at all."""
    env_settings(monkeypatch, LLM_PROVIDER="mock")
    assert rationale_hook_for("any-run") is no_rationale


@pytest.mark.parametrize("spelling", ["mock", "MOCK", "  Mock  "])
def test_the_provider_name_is_normalised_before_it_is_judged(
    monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    """The same case/whitespace handling `recon.llm.build_provider` applies."""
    env_settings(monkeypatch, LLM_PROVIDER=spelling)
    assert rationale_hook_for("any-run") is no_rationale


def test_an_unset_provider_yields_the_no_op_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """No `LLM_PROVIDER` at all is the offline default, not an error."""
    env_settings(monkeypatch, LLM_PROVIDER=None)
    assert rationale_hook_for("any-run") is no_rationale


# ===========================================================================
# a live provider, and the two ways it can be unusable
# ===========================================================================
def test_a_live_provider_without_a_key_degrades_to_the_no_op_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misconfigured deploy proposes with rationale NULL; it does not 500.

    `build_provider` raises `ProviderNotConfigured` rather than falling back to
    the mock -- deliberately, so nobody believes they are calling a model when
    they are not. The cron must still reconcile.
    """
    env_settings(monkeypatch, LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY=None)
    assert rationale_hook_for("any-run") is no_rationale


def test_an_unknown_provider_degrades_to_the_no_op_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_settings(monkeypatch, LLM_PROVIDER="not-a-provider")
    assert rationale_hook_for("any-run") is no_rationale


def test_a_configured_live_provider_yields_a_real_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider is built from the settings, and it is the live one.

    No call is made: the assertion is about which object the wiring produced.
    """
    from recon.llm import AnthropicProvider, build_provider

    env_settings(monkeypatch, LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY=FAKE_KEY)
    assert isinstance(build_provider(), AnthropicProvider)
    assert rationale_hook_for("any-run") is not no_rationale


# ===========================================================================
# the endpoint's body asks for the hook
# ===========================================================================
def test_reconcile_job_passes_the_production_hook_into_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`reconcile_job` is what `POST /internal/reconcile` runs, and it must ask.

    Captured off the real call rather than read out of the source, so a
    refactor that stops threading the hook is caught by behaviour.
    """
    import recon.reconciler as module

    seen: dict[str, object] = {}

    class _Report:
        def as_dict(self) -> dict[str, object]:
            return {"ok": True}

    def _capture(*, run_id: str, rationale: object) -> _Report:
        seen["run_id"] = run_id
        seen["rationale"] = rationale
        return _Report()

    monkeypatch.setattr(module, "reconcile", _capture)

    env_settings(monkeypatch, LLM_PROVIDER="mock")
    assert module.reconcile_job("run-mock") == {"ok": True}
    assert seen["run_id"] == "run-mock"
    assert seen["rationale"] is no_rationale

    env_settings(monkeypatch, LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY=FAKE_KEY)
    assert module.reconcile_job("run-live") == {"ok": True}
    assert seen["run_id"] == "run-live"
    assert seen["rationale"] is not no_rationale


def test_the_suite_entrypoint_does_not_ask_for_a_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run_once` is `recon.suite`'s entrypoint; the graded pass calls no model.

    Even with a live provider configured, the offline harness must reserve
    nothing -- otherwise `python -m recon.suite` would spend money and stop
    being reproducible.
    """
    import recon.reconciler as module

    seen: dict[str, object] = {}

    def _capture(**kwargs: object) -> object:
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(module, "reconcile", _capture)
    env_settings(monkeypatch, LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY=FAKE_KEY)
    module.run_once()
    assert seen == {}, (
        f"run_once() called reconcile with {sorted(seen)}; it must pass nothing, so "
        "the graded pass takes reconcile()'s own `no_rationale` default"
    )


# ===========================================================================
# the prompt, and the idempotency key derived from the run
# ===========================================================================
def test_the_prompt_is_the_canonical_evidence_packet() -> None:
    """Deterministic, and the same spelling `proposals.evidence` is written in."""
    from recon.privacy import canonical_json

    packet = _packet()
    rendered = rationale_prompt(packet)

    assert rendered == canonical_json(packet.as_dict())
    assert rendered == rationale_prompt(_packet()), "the prompt must be byte-stable"
    assert '"confidence"' in rendered and '"conflict"' in rendered


def test_the_idempotency_key_is_derived_from_the_run_and_the_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No uuid4, no clock: a replayed run replays the reservation, not the spend.

    The key is captured by standing in for `generate_rationale`, which is the
    only consumer of it.
    """
    import recon.llm as llm_module

    env_settings(monkeypatch, LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY=FAKE_KEY)
    captured: dict[str, object] = {}

    class _Outcome:
        ok = True
        text = "rationale text"
        status = "ok"
        sqlstate = None

    def _fake(request: object, *, run_id: str, idempotency_key: str, provider: object) -> _Outcome:
        captured["subject"] = request.subject
        captured["prompt"] = request.prompt
        captured["run_id"] = run_id
        captured["key"] = idempotency_key
        return _Outcome()

    monkeypatch.setattr(llm_module, "generate_rationale", _fake)

    hook = rationale_hook_for("run-42")
    packet = _packet()
    assert hook(packet) == "rationale text"

    assert captured["subject"] == packet.conflict.fingerprint
    assert captured["prompt"] == rationale_prompt(packet)
    assert captured["run_id"] == "run-42"
    assert captured["key"] == f"{RATIONALE_KEY_PREFIX}:run-42:{packet.conflict.fingerprint}"
    # Same inputs, same key -- so a re-fired run under the same id replays.
    assert hook(_packet()) == "rationale text"
    assert captured["key"] == f"{RATIONALE_KEY_PREFIX}:run-42:{packet.conflict.fingerprint}"


def test_a_refused_or_failed_call_returns_none_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cap hit, provider error, halted scope: the proposal still lands, NULL.

    `_rationale` also wraps the hook in try/except, but relying on that would
    make this hook's own contract untested -- and `reconcile` records
    `rationale_attached` from the return value either way.
    """
    import recon.llm as llm_module

    env_settings(monkeypatch, LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY=FAKE_KEY)

    class _Outcome:
        def __init__(self, status: str, text: str | None) -> None:
            self.status = status
            self.text = text
            self.sqlstate = "KS006" if status == "cap_hit" else None

        @property
        def ok(self) -> bool:
            return self.status == "ok"

    for status, text_value in [
        ("cap_hit", None),
        ("provider_error", None),
        ("scope_halted", None),
        ("internal_error", None),
        # `ok` with empty text is a provider that answered with nothing; it is a
        # NULL rationale, not the empty string written into the column.
        ("ok", ""),
    ]:
        monkeypatch.setattr(
            llm_module,
            "generate_rationale",
            lambda *a, _s=status, _t=text_value, **k: _Outcome(_s, _t),
        )
        assert rationale_hook_for("run-x")(_packet()) is None
