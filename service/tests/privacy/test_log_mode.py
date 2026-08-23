"""LOG_MODE behaviour: `safe` is the default, `full` is env-gated (SPEC R21)."""

from __future__ import annotations

import io
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from recon.config import Settings, get_settings
from recon.logging import (
    FULL_MODE_WARNING,
    LOG_MODE_FULL,
    LOG_MODE_SAFE,
    _jsonable,
    audit_detail,
    audit_row,
    body_sha256,
    configure_logging,
    get_logger,
    is_safe_mode,
    log_mode,
    redaction_processor,
    resolve_mode,
)
from recon.privacy import PII_KEYS, canonical_json, is_token

ENV_KEYS = ("LOG_MODE", "log_mode")


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A process environment that says nothing about LOG_MODE."""
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def emitted() -> io.StringIO:
    return io.StringIO()


# ---------------------------------------------------------------------------
# the default
# ---------------------------------------------------------------------------


def test_safe_is_the_default_with_no_environment(clean_env: None) -> None:
    """Nothing in the environment => safe. `full` is never the fallback."""
    assert Settings(_env_file=None).log_mode == LOG_MODE_SAFE
    assert log_mode() == LOG_MODE_SAFE
    assert is_safe_mode() is True
    assert resolve_mode(None) == LOG_MODE_SAFE


def test_full_requires_an_explicit_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`full` is reachable only by setting LOG_MODE in the environment."""
    monkeypatch.setenv("LOG_MODE", LOG_MODE_FULL)
    get_settings.cache_clear()
    try:
        assert log_mode() == LOG_MODE_FULL
        assert is_safe_mode() is False
    finally:
        get_settings.cache_clear()


def test_an_unknown_mode_is_rejected_rather_than_defaulted() -> None:
    """A typo must not silently fall back to either mode."""
    with pytest.raises(ValueError, match="LOG_MODE must be one of"):
        resolve_mode("verbose")


def test_env_example_ships_safe() -> None:
    """The documented deployment default matches the code default."""
    from tests.privacy.conftest import SERVICE_ROOT

    text = (SERVICE_ROOT.parent / ".env.example").read_text()
    assert "LOG_MODE=safe" in text
    assert "LOG_MODE=full" not in text


# ---------------------------------------------------------------------------
# emitted events
# ---------------------------------------------------------------------------


def _emit(stream: io.StringIO, mode: str, **event: Any) -> dict[str, Any]:
    configure_logging(mode=mode, stream=stream, cache=False)
    stream.truncate(0)
    stream.seek(0)
    get_logger("tests.privacy").info("conflict.detected", **event)
    return json.loads(stream.getvalue().strip())


def test_safe_mode_redacts_nested_event_values(
    emitted: io.StringIO, dev_records: dict[str, list[dict[str, Any]]]
) -> None:
    """The PII lives inside the jsonb, so that is where redaction has to reach."""
    student = dev_records["appdb.student"][0]
    line = _emit(
        emitted,
        LOG_MODE_SAFE,
        run_id="run-1",
        evidence={"record": student, "observed": [student["guardian_email"]]},
    )
    assert line["run_id"] == "run-1"
    assert is_token(line["evidence"]["record"]["guardian_email"])
    assert is_token(line["evidence"]["observed"][0])
    assert student["guardian_email"] not in json.dumps(line)


def test_full_mode_stores_the_raw_body(
    emitted: io.StringIO, dev_records: dict[str, list[dict[str, Any]]]
) -> None:
    """The dev-only branch really is unredacted -- which is why it is env-gated."""
    student = dev_records["appdb.student"][0]
    line = _emit(emitted, LOG_MODE_FULL, evidence={"record": student})
    assert line["evidence"]["record"]["guardian_email"] == student["guardian_email"]


def test_full_mode_configuration_warns_that_it_is_development_only(
    emitted: io.StringIO,
) -> None:
    """Turning raw logging on cannot be done quietly."""
    configure_logging(mode=LOG_MODE_FULL, stream=emitted, cache=False)
    warning = json.loads(emitted.getvalue().strip().splitlines()[0])
    assert warning["event"] == FULL_MODE_WARNING
    assert warning["level"] == "warning"
    assert "development only" in warning["note"]


def test_safe_mode_configuration_emits_nothing(emitted: io.StringIO) -> None:
    """The default path is silent: no warning, no noise."""
    configure_logging(mode=LOG_MODE_SAFE, stream=emitted, cache=False)
    assert emitted.getvalue() == ""


def test_bound_context_is_redacted_too(
    emitted: io.StringIO, dev_records: dict[str, list[dict[str, Any]]]
) -> None:
    """Redaction runs last, so values bound onto the logger are covered."""
    student = dev_records["appdb.student"][0]
    configure_logging(mode=LOG_MODE_SAFE, stream=emitted, cache=False)
    get_logger("tests.privacy").bind(guardian_email=student["guardian_email"]).info("bound")
    line = json.loads(emitted.getvalue().strip())
    assert is_token(line["guardian_email"])


def test_rendered_json_is_the_project_spelling(
    emitted: io.StringIO,
) -> None:
    """Sorted keys, ASCII only -- the same JSON rule the rest of the repo uses."""
    line = _emit(emitted, LOG_MODE_SAFE, zeta=1, alpha=2, name="Fáirbank-Mead")
    rendered = emitted.getvalue().strip()
    assert rendered == canonical_json(line)
    assert rendered.isascii()


# ---------------------------------------------------------------------------
# audit_log.detail -- DESIGN's "hash + preview"
# ---------------------------------------------------------------------------


def test_audit_detail_safe_is_hash_plus_redacted_preview(
    dev_records: dict[str, list[dict[str, Any]]],
) -> None:
    student = dev_records["appdb.student"][0]
    detail = audit_detail(student, mode=LOG_MODE_SAFE)
    assert detail["mode"] == LOG_MODE_SAFE
    assert detail["body_sha256"] == body_sha256(student)
    assert len(detail["body_sha256"]) == 64
    assert is_token(detail["body"]["guardian_email"])
    assert set(detail["body"]) == set(student)
    assert student["guardian_email"] not in canonical_json(detail)


def test_audit_detail_hash_is_over_the_raw_body(
    dev_records: dict[str, list[dict[str, Any]]],
) -> None:
    """Two rows describing the same body prove it without storing the body."""
    student = dev_records["appdb.student"][0]
    other = dev_records["appdb.student"][1]
    assert audit_detail(student)["body_sha256"] == audit_detail(dict(student))["body_sha256"]
    assert audit_detail(student)["body_sha256"] != audit_detail(other)["body_sha256"]


def test_audit_detail_full_is_raw_and_labelled(
    dev_records: dict[str, list[dict[str, Any]]],
) -> None:
    student = dev_records["appdb.student"][0]
    detail = audit_detail(student, mode=LOG_MODE_FULL)
    assert detail["mode"] == LOG_MODE_FULL
    assert detail["body"]["guardian_email"] == student["guardian_email"]


def test_audit_detail_defaults_to_safe(clean_env: None) -> None:
    assert audit_detail({"guardian_email": "a@keystone.test"})["mode"] == LOG_MODE_SAFE


def test_audit_row_prepares_canonical_json_detail(
    dev_records: dict[str, list[dict[str, Any]]],
) -> None:
    """The helper binds parameters only -- it opens nothing and writes nothing."""
    student = dev_records["appdb.student"][0]
    row = audit_row(actor="system:reconciler", action="conflict.detected", body=student)
    assert row["actor"] == "system:reconciler"
    assert row["subject"] is None
    parsed = json.loads(row["detail"])
    assert parsed["mode"] == LOG_MODE_SAFE
    assert student["guardian_email"] not in row["detail"]
    assert audit_row(actor="a", action="b")["detail"] is None


# ---------------------------------------------------------------------------
# every field of an audit row, and nothing left for the renderer to stringify
# ---------------------------------------------------------------------------


def test_audit_row_redacts_every_field_not_only_detail() -> None:
    """`subject` routinely carries an entity reference, and used to go straight
    through to the bound parameter untouched -- `detail` was the only redacted
    field. Every field now goes through the redactor under its own key."""
    row = audit_row(
        actor="system:reconciler",
        action="proposal.decided",
        subject="student:guardian@keystone.test",
        body={"first_name": "Amriyo"},
    )
    assert "guardian@keystone.test" not in canonical_json(row)
    assert row["subject"].startswith("student:")
    assert "[pii:email:" in row["subject"]
    assert "Amriyo" not in row["detail"]


def test_audit_row_keeps_the_actor_the_database_trigger_matches_on() -> None:
    """KS003 matches `actor` against `^system:`; tokenising it would break the
    write boundary, so an allow-listed field is scrubbed rather than tokenised."""
    row = audit_row(actor="system:retention", action="retention.purge", subject="principal:owner")
    assert row["actor"] == "system:retention"
    assert row["action"] == "retention.purge"
    assert row["subject"] == "principal:owner"


def test_audit_row_scrubs_a_value_smuggled_into_a_free_field() -> None:
    """A caller that interpolates a record into `action` does not get a pass."""
    row = audit_row(actor="system:ingest", action="rejected first_name=Amriyo", subject=None)
    assert "Amriyo" not in canonical_json(row)
    assert row["subject"] is None


def test_audit_row_counters_survive_as_numbers() -> None:
    """Allow-listed counters and money stay usable, or the ledger stops adding up."""
    row = audit_row(actor="system:llm", action="llm.called", tokens_in=11, cost_microusd=25)
    assert row["tokens_in"] == 11
    assert row["cost_microusd"] == 25


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(ValueError("cannot land {'first_name': 'Amriyo'}"), id="exception"),
        pytest.param(uuid.UUID("00000000-0000-0000-0000-000000000002"), id="uuid"),
        pytest.param(Decimal("3.50"), id="decimal"),
        pytest.param(datetime(2015, 12, 16, tzinfo=UTC), id="datetime"),
    ],
)
def test_the_renderer_has_nothing_left_to_stringify_in_safe_mode(value: Any) -> None:
    """`_jsonable` must be an identity over a redacted event.

    That is the property the blocker came down to: while the renderer did the
    stringifying, the redactor inspected an *object*, passed it through, and the
    renderer then wrote `str(obj)` into the log after redaction had finished.
    """
    event = {"event": "ingest.record_rejected", "error": value, "surprise": value}
    redacted = redaction_processor(None, "error", dict(event))
    assert _jsonable(redacted) == redacted
    assert "Amriyo" not in canonical_json(redacted)


def test_an_exception_argument_survives_the_whole_emitted_pipeline(
    emitted: io.StringIO, dev_records: dict[str, list[dict[str, Any]]]
) -> None:
    """End to end through the real chain, not through `redact()` directly."""
    student = dev_records["appdb.student"][0]
    configure_logging(mode=LOG_MODE_SAFE, stream=emitted, cache=False)
    get_logger("tests.privacy").error(
        "ingest.record_rejected", error=ValueError(f"cannot land {student!r}")
    )
    rendered = emitted.getvalue()
    for key, value in student.items():
        if key in PII_KEYS and isinstance(value, str) and len(value) >= 4:
            assert value not in rendered, f"{key} reached the log through an exception"
