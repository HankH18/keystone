"""`/internal/ingest/records` fails CLOSED (R19).

R19: "Scheduled jobs SHALL be triggered via HTTPS with a per-job shared-secret
header; requests without it are 401."

The endpoint used to read the configured secret and then skip the comparison
whenever it was falsy -- so in the default configuration (every secret `None`) an
anonymous caller could write to the append-only landing table. That is worse than
having no authentication at all: the route carries a secret header, the code
carries a comparison, and the check is absent only in exactly the deployment where
nobody noticed. It looks protected until it is used.

Every way of having no usable secret is therefore a 401 and is parametrized below:
unset, empty, whitespace-only. So are the wrong secret and the missing header. The
positive case is here too, because "always 401" would satisfy the rest of the file
and would be a different outage.

**Each unusable configuration is presented back to the endpoint.** That is not a
detail: this file used to parametrize ``configured="   "`` and then present only
``None``, ``""``, ``"anything"`` and the real secret -- never ``"   "``, the one
value a whitespace-only secret would have matched. So the case that mattered was
listed but never exercised, deleting the ``.strip()`` from the guard left the whole
suite green, and the file *read* as covering the hole it did not cover. A case is
covered when a caller presents the value that would exploit it.

Two properties beyond the status code:

* **the secret never appears anywhere** -- not in the response body, not in a log
  line. A 401 that echoes what it compared against is an oracle;
* **the comparison is constant time** (`hmac.compare_digest`). Not timeable in a
  test worth running, so it is asserted structurally, on the source of the one
  shared guard -- see `tests/triggers/test_single_trigger_guard.py` for the
  assertion that there is exactly one.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import recon.ingest as ingest_module
from recon.api import auth as auth_module
from recon.app import create_app
from recon.config import get_settings

SECRET = "the-real-ingest-trigger-secret"

#: The settings fields the sync job's secret may live in. Read from the one module
#: that owns them rather than restated here, so a field added there is configured
#: by this file's `_configure` automatically instead of silently escaping it.
TRIGGER_SECRET_FIELDS = auth_module.TRIGGER_SECRET_FIELDS[auth_module.JOB_SYNC]

#: Every string a caller could present. `"   "` and `"\t"` are here because they are
#: what an *unusable* whitespace-only configuration would compare equal to.
PRESENTABLE: tuple[str | None, ...] = (None, "", " ", "   ", "\t", "\n", "anything", SECRET)

RECORD = (
    '{"crm_id":"CRM-9800001","email":"auth@example.test","first_name":"Ada",'
    '"last_name":"Byron","lifecycle_stage":"lead","created_at":"2026-02-01T00:00:00Z",'
    '"updated_at":"2026-02-02T00:00:00Z","external_id":null,"dob":"2012-05-04",'
    '"grade":"4","state":"TX","marketing_consent":true}'
)


@pytest.fixture
def api(owner_engine) -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


@pytest.fixture(autouse=True)
def _clean_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _configure(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    """Set every field the endpoint may read, or delete them all."""
    for name in TRIGGER_SECRET_FIELDS:
        if value is None:
            monkeypatch.delenv(name.upper(), raising=False)
        else:
            monkeypatch.setenv(name.upper(), value)
    get_settings.cache_clear()


def _post(client: TestClient, secret: str | None, run_id: str):
    headers = {} if secret is None else {"X-Trigger-Secret": secret}
    return client.post(
        "/internal/ingest/records",
        json={
            "source": "crm",
            "entity_type": "contact",
            "generation": 980,
            "records": [RECORD],
            "run_id": run_id,
        },
        headers=headers,
    )


@pytest.mark.parametrize(
    ("case", "configured"),
    [
        ("unset", None),
        ("empty", ""),
        ("space", " "),
        ("whitespace", "   "),
        ("tab", "\t"),
        ("newline", "\n"),
        ("mixed-whitespace", " \t \n "),
    ],
)
def test_an_unusable_configured_secret_denies_every_caller(
    api: TestClient,
    owner_engine,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    configured: str | None,
) -> None:
    """No usable configured secret means deny -- it never means "skip the check".

    Every presented value in `PRESENTABLE` is tried against every unusable
    configuration, **including the configured value itself**. That last pairing is
    the whole test: for `configured="   "` the caller presenting `"   "` is the one
    a `if not configured` guard authenticates, and it was the one the previous
    version of this test never sent.
    """
    _configure(monkeypatch, configured)

    for presented in {*PRESENTABLE, configured}:
        response = _post(api, presented, run_id=f"auth-{case}")
        assert response.status_code == 401, (
            f"a {case} ({configured!r}) secret accepted presented={presented!r}: a "
            "mutating endpoint whose authentication disappears when it is "
            "misconfigured is worse than one with none"
        )
        assert response.json()["title"] == "unauthorized"

    with owner_engine.connect() as connection:
        landed = connection.execute(
            text("SELECT count(*) FROM raw_records WHERE run_id = :run_id"),
            {"run_id": f"auth-{case}"},
        ).scalar()
    assert landed == 0, "an unauthenticated caller must not reach the landing table"


def test_a_missing_header_is_401_even_with_a_secret_configured(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch, SECRET)
    assert _post(api, None, run_id="auth-missing").status_code == 401


@pytest.mark.parametrize(
    "presented",
    ["", "wrong", "the-real-ingest-trigger-secre", "the-real-ingest-trigger-secretx", " " + SECRET],
    ids=["empty", "wrong", "truncated", "extended", "padded"],
)
def test_a_wrong_secret_is_401(
    api: TestClient, monkeypatch: pytest.MonkeyPatch, presented: str
) -> None:
    _configure(monkeypatch, SECRET)
    assert _post(api, presented, run_id="auth-wrong").status_code == 401


def test_the_right_secret_is_accepted(api: TestClient, owner_engine, monkeypatch) -> None:
    """The positive control: this is authentication, not a closed door."""
    _configure(monkeypatch, SECRET)
    response = _post(api, SECRET, run_id="auth-right")
    assert response.status_code == 200, response.text
    assert response.json()["accepted"] == 1

    with owner_engine.connect() as connection:
        landed = connection.execute(
            text("SELECT count(*) FROM raw_records WHERE run_id = 'auth-right'")
        ).scalar()
    assert landed == 1


def test_the_deprecated_single_secret_still_works_while_it_exists(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment mid-rotation is not locked out -- but it still has to present one."""
    monkeypatch.delenv("TRIGGER_SECRET_SYNC", raising=False)
    monkeypatch.setenv("TRIGGER_SECRET", SECRET)
    get_settings.cache_clear()

    assert _post(api, None, run_id="auth-legacy-none").status_code == 401
    assert _post(api, "wrong", run_id="auth-legacy-wrong").status_code == 401
    assert _post(api, SECRET, run_id="auth-legacy-right").status_code == 200


def test_the_secret_is_never_echoed(
    api: TestClient, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Neither the configured secret nor the presented one may leave the process."""
    _configure(monkeypatch, SECRET)
    capsys.readouterr()

    response = _post(api, "a-guess-that-is-wrong", run_id="auth-echo")
    captured = capsys.readouterr()

    assert response.status_code == 401
    assert SECRET not in response.text
    assert "a-guess-that-is-wrong" not in response.text
    assert SECRET not in captured.out + captured.err, "the configured secret reached a log line"
    assert "a-guess-that-is-wrong" not in captured.out + captured.err


def test_the_comparison_is_constant_time() -> None:
    """Structural, because a timing assertion in a unit test proves nothing.

    The source inspected is `recon.api.auth.verify_trigger_secret`: this endpoint no
    longer holds a comparison of its own, and asserting on a copy that no longer
    exists would be asserting on nothing.
    """
    source = inspect.getsource(auth_module.verify_trigger_secret)
    assert "hmac.compare_digest" in source
    assert "==" not in source.replace("!=", ""), (
        "an equality comparison on a secret leaks its prefix through timing"
    )


def test_this_endpoint_holds_no_comparison_of_its_own() -> None:
    """`_authorize` delegates; it does not re-implement.

    Two implementations of one security check are not redundancy -- they are a
    coin flip over which one a given endpoint got. `recon.ingest` and
    `recon.api.auth` did diverge here, and the copy that lost was the one that
    treated `"   "` as a usable secret.
    """
    source = inspect.getsource(ingest_module._authorize)
    assert "compare_digest" not in source
    assert "trigger_guard" in source
    assert not hasattr(ingest_module, "_configured_trigger_secret"), (
        "the second implementation of 'is this configured secret usable' is back"
    )
    assert not hasattr(ingest_module, "_TRIGGER_SECRET_FIELDS"), (
        "the second copy of the trigger-secret field list is back; "
        "recon.api.auth.TRIGGER_SECRET_FIELDS is the one table"
    )
