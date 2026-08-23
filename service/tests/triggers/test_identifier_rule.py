"""ONE identifier rule, reached by every endpoint (R2, R19).

Same defect class as the two trigger-secret checks, one layer along: a
`run_id` was validated in **three** places by **three** different rules, and the
weakest one decided what the database was asked to store. Reproduced against a
real uvicorn server, not `TestClient`:

=========================  ============================  =========================
value                      ``/internal/ingest/records``  ``/internal/{sync,
                                                         reconcile}``
=========================  ============================  =========================
``"ctrl\\u0007char"``       422                           **200**, written to
                                                         ``audit_log.subject``
``"a\\u0000b"``             422                           **bare 500**
``"sur\\ud800rogate"``      **bare 500**                  422
``" "``                    accepted                      **200**
=========================  ============================  =========================

Two 5xx and one silently-accepted control character out of one requirement, and
the two endpoints of the *same job* disagreeing about the same value.

The rule now lives in `recon.adapters.identifiers` and nowhere else, and this
file proves that three independent ways -- because any one of them alone can be
satisfied by a second implementation that happens to agree today:

**behavioural** -- the matrix: every hostile identifier against every mutating
endpoint the app mounts. Each must be a 4xx, and each must be *the same* 4xx
everywhere. A divergence is a failure even when both answers are 4xx.

**routing** -- the endpoints are enumerated from the app object, so a route added
tomorrow that takes an identifier and does not reach the shared validator fails
here rather than quietly sitting outside the matrix.

**structural** -- the character rule exists in one module. A second
implementation has to spell a character class somewhere; the enumeration below
is what fails when a third spelling appears.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any, get_args, get_origin, get_type_hints
from urllib.parse import quote

import pytest
from fastapi import params as fastapi_params
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import Engine, text

from recon.adapters import identifiers as identifiers_module
from recon.adapters.identifiers import IDENTIFIER_MAX_LENGTH, IDENTIFIER_RULE
from recon.api import auth as auth_module
from recon.api.internal import TriggerRequest
from recon.ingest import RecordsRequest
from tests.budget.support import env_settings
from tests.triggers.test_single_trigger_guard import (
    API_KEY_ENDPOINTS,
    TRIGGER_ENDPOINTS,
    _app_with_every_trigger_route,
    mutating_routes,
)

RECON_ROOT = Path(identifiers_module.__file__).resolve().parents[1]
IDENTIFIERS_MODULE = "adapters/identifiers.py"

SYNC_SECRET = "sync-secret-for-the-identifier-matrix"
RECONCILE_SECRET = "reconcile-secret-for-the-identifier-matrix"

#: The generation the matrix posts into. Inside the range `tests/ingest`'s
#: teardown owns, and nothing is expected to land -- every case is a rejection.
MATRIX_GENERATION = 991


#: Every identifier the store or the domain refuses, with the reason it is on the
#: list. Spelled as JSON escapes where the value cannot be written literally.
HOSTILE_IDENTIFIERS: tuple[tuple[str, str, str], ...] = (
    ("empty", "", "identifies nothing"),
    ("space", " ", "whitespace-only: the shape a YAML quoting accident leaves"),
    ("spaces", "   ", "whitespace-only"),
    ("tab", "\t", "whitespace-only, and a control character"),
    ("newline", "\n", "whitespace-only, and a control character"),
    ("nul_leading", "\x00abc", "Postgres text cannot hold a NUL"),
    ("nul_middle", "a\x00b", "the reported 500 from claim_run's advisory-lock execute"),
    ("nul_trailing", "abc\x00", "Postgres text cannot hold a NUL"),
    ("bell", "ctrl\x07char", "control character: 200 on /internal/sync before the fix"),
    ("cr", "line\rreturn", "control character: forges a log record"),
    ("escape", "esc\x1bseq", "control character: rewrites a terminal"),
    ("unit_separator", "a\x1fb", "SS5.4 joins fingerprint elements with U+001F"),
    ("delete", "del\x7fchar", "DEL is Cc and was covered by one rule and not the other"),
    ("c1_nel", "nel\x85char", "C1 control character"),
    ("surrogate_high", "sur\ud800rogate", "unpaired surrogate: the 500 at the landing COPY"),
    ("surrogate_low", "sur\udfffrogate", "unpaired surrogate"),
    ("too_long", "x" * (IDENTIFIER_MAX_LENGTH + 1), "over-length for a concatenated load_id"),
)

#: Identifiers that must be ACCEPTED. Without these the matrix is satisfied by an
#: endpoint that refuses everything, which is not authentication or validation --
#: it is a closed door.
USABLE_IDENTIFIERS: tuple[str, ...] = (
    "run-2026-08-23T00-00-00Z",
    "  padded  ",
    "unicode-café-ok",
    "x" * IDENTIFIER_MAX_LENGTH,
)


@pytest.fixture
def matrix(owner_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The real app with both job secrets configured, and its rows swept after."""
    env_settings(
        monkeypatch,
        TRIGGER_SECRET_SYNC=SYNC_SECRET,
        TRIGGER_SECRET_RECONCILE=RECONCILE_SECRET,
        TRIGGER_SECRET=None,
    )
    app = _app_with_every_trigger_route()
    with TestClient(app) as client:
        yield client
    with owner_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM audit_log WHERE action LIKE 'trigger.%' AND subject LIKE :p"),
            {"p": "%idmatrix%"},
        )
        conn.execute(
            text("DELETE FROM budget_reservations WHERE scope LIKE :p"), {"p": "%idmatrix%"}
        )
        conn.execute(text("DELETE FROM budget_ledger WHERE scope LIKE :p"), {"p": "%idmatrix%"})
        for table in ("stg_crm_contact", "raw_records", "source_generations", "ingest_runs"):
            conn.execute(
                text(f"DELETE FROM {table} WHERE generation = :g"), {"g": MATRIX_GENERATION}
            )


def _secret_for(job: str) -> str:
    return SYNC_SECRET if job == auth_module.JOB_SYNC else RECONCILE_SECRET


def _body(path: str, run_id: str) -> bytes:
    """The request body for `path`, carrying `run_id`.

    Encoded with `json.dumps` so a lone surrogate is escaped onto the wire as
    `\\ud800` -- which is exactly how a real client sends one, and exactly what
    Python's parser hands back as an unpaired surrogate on the server side.
    """
    if path.endswith("/records"):
        payload = {
            "source": "crm",
            "entity_type": "contact",
            "generation": MATRIX_GENERATION,
            "records": [],
            "run_id": run_id,
            "persist": True,
        }
    else:
        payload = {"run_id": run_id}
    return json.dumps(payload).encode("utf-8")


def _fire(client: TestClient, path: str, job: str, run_id: str) -> tuple[int, dict]:
    response = client.post(
        path,
        content=_body(path, run_id),
        headers={
            "content-type": "application/json",
            auth_module.TRIGGER_SECRET_HEADER: _secret_for(job),
        },
    )
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {}


# ======================================================================================
# behavioural -- the matrix
# ======================================================================================


@pytest.mark.parametrize(
    ("case", "value", "why"), HOSTILE_IDENTIFIERS, ids=[c for c, _, _ in HOSTILE_IDENTIFIERS]
)
def test_every_endpoint_refuses_the_same_identifier_identically(
    matrix: TestClient, case: str, value: str, why: str
) -> None:
    """One value, three endpoints, one verdict -- and never a 5xx.

    The divergence is the defect, so agreement is the assertion: it is not enough
    that each endpoint refuses, they have to refuse with the same status. Two
    endpoints of one job answering 422 and 200 to the same string is how a
    control character reached `audit_log.subject`.
    """
    seen: dict[str, int] = {}
    for path, job in TRIGGER_ENDPOINTS:
        status, body = _fire(matrix, path, job, value)
        seen[path] = status
        assert status < 500, (
            f"{path} answered {status} for the {case} identifier ({why}); a value "
            "the store cannot hold is a 4xx about the request, never a server fault"
        )
        assert 400 <= status < 500, f"{path} accepted the {case} identifier with {status}"
        assert body.get("rule") == IDENTIFIER_RULE or body.get("type", "").endswith(
            "invalid_request"
        ), f"{path} refused the {case} identifier without stating the shared rule: {body}"
    assert len(set(seen.values())) == 1, (
        f"the {case} identifier ({why}) was judged differently by each endpoint: "
        f"{seen}. One rule means one verdict, whichever URL a caller used."
    )


@pytest.mark.parametrize("value", USABLE_IDENTIFIERS)
def test_every_endpoint_accepts_a_usable_identifier(matrix: TestClient, value: str) -> None:
    """The control. A validator that refuses everything proves nothing.

    The value is used verbatim, padding included: an idempotency key that the
    server silently trims is a key two different requests can share.
    """
    tagged = f"idmatrix-{value}"
    for path, job in TRIGGER_ENDPOINTS:
        status, body = _fire(matrix, path, job, tagged[:IDENTIFIER_MAX_LENGTH])
        assert status == 200, (
            f"{path} refused the usable identifier {value!r} with {status}: {body}"
        )
        assert body.get("run_id") == tagged[:IDENTIFIER_MAX_LENGTH], (
            "the identifier must come back unchanged; trimming it would accept a "
            f"key the client never sent: {body}"
        )


def test_a_wrong_typed_identifier_is_a_4xx_everywhere(matrix: TestClient) -> None:
    """`{"run_id": 7}` is a request problem, not a crash at the bind."""
    for path, job in TRIGGER_ENDPOINTS:
        payload = json.loads(_body(path, "placeholder"))
        payload["run_id"] = 7
        response = matrix.post(
            path,
            content=json.dumps(payload).encode(),
            headers={
                "content-type": "application/json",
                auth_module.TRIGGER_SECRET_HEADER: _secret_for(job),
            },
        )
        assert 400 <= response.status_code < 500, f"{path} answered {response.status_code}"


# ======================================================================================
# behavioural -- the matrix, second half: the identifier in the PATH
# ======================================================================================

#: `migrations/versions/0003_seed_api_clients.py`, and `.env.example`. Spelled out
#: rather than derived, exactly as `tests/api/conftest.py` spells it out: a value
#: computed by the same helper the service uses would agree with itself even if
#: both changed.
ADMIN_API_KEY = "keystone-demo-admin-8c25e0b71a94f36d"
ADMIN_HEADERS = {"X-Api-Key": ADMIN_API_KEY}

#: Ids that are well-formed and cannot name a row. The decision endpoints answer
#: 404 for these, which is what makes the control below a control: it proves the
#: matrix is judging the identifier and not just meeting a closed door -- and it
#: cannot decide a real proposal in the shared development database on its way
#: through, because there is no such proposal to decide.
UNUSED_PROPOSAL_IDS: tuple[str, ...] = (
    "9223372036854775807",  # max bigint
    "9" * 40,  # wider than bigint: still an integer, still nobody's id
)


def _path_segment(value: str) -> str:
    """`value` as a URL path segment, the way a real client would send it.

    `surrogatepass` so an unpaired surrogate is percent-encoded onto the wire
    rather than raising in the test -- the server side is what is under test.
    """
    return quote(value.encode("utf-8", "surrogatepass"), safe="")


def _fire_api_key(client: TestClient, path: str, value: str) -> tuple[int, dict]:
    url = path.replace("{proposal_id}", _path_segment(value))
    response = client.post(url, headers=ADMIN_HEADERS)
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {}


@pytest.mark.parametrize(
    ("case", "value", "why"), HOSTILE_IDENTIFIERS, ids=[c for c, _, _ in HOSTILE_IDENTIFIERS]
)
def test_every_api_key_route_refuses_the_same_hostile_path_identically(
    matrix: TestClient, case: str, value: str, why: str
) -> None:
    """The other three mutating routes, run through the same hostile list.

    T-11 added a second class of mutating route, and its identifier arrives in
    the **path** rather than the body. Listing those routes in the coverage
    assertion without ever posting a hostile value at them would have made the
    coverage a lie, so they are fired at here: same list of values, same "never a
    5xx", same "one rule, one verdict, whichever URL a caller used".

    An identifier that cannot even *form* a path segment (the empty string) is
    refused by the router before routing resolves; everything else reaches the
    parser, which refuses it as an unprocessable request. Both are 4xx, and both
    are the same 4xx on all three routes -- the divergence is the defect.
    """
    segment = _path_segment(value)
    seen: dict[str, int] = {}
    for path, _scope in API_KEY_ENDPOINTS:
        status, body = _fire_api_key(matrix, path, value)
        seen[path] = status
        assert status < 500, (
            f"{path} answered {status} for the {case} identifier ({why}); a value "
            "the store cannot hold is a 4xx about the request, never a server fault"
        )
        assert 400 <= status < 500, f"{path} accepted the {case} identifier with {status}"
        if segment == "":
            assert status == 404, (
                f"{path} answered {status} for an identifier that cannot form a path "
                "segment at all; the route must simply not resolve"
            )
        else:
            assert status == 422, (
                f"{path} answered {status} for the {case} identifier ({why}), not 422: {body}"
            )
            assert body.get("rule") == IDENTIFIER_RULE or body.get("type", "").endswith(
                "invalid_request"
            ), f"{path} refused the {case} identifier without stating the shared rule: {body}"
    assert len(set(seen.values())) == 1, (
        f"the {case} identifier ({why}) was judged differently by each endpoint: "
        f"{seen}. One rule means one verdict, whichever URL a caller used."
    )


@pytest.mark.parametrize("value", UNUSED_PROPOSAL_IDS)
def test_every_api_key_route_accepts_a_usable_path_identifier(
    matrix: TestClient, value: str
) -> None:
    """The control, for the path half. A door that is always shut proves nothing.

    A well-formed id must get past the identifier verdict and be *looked up* --
    404, because no proposal carries it. A 422 here would mean the route refuses
    every identifier, which would satisfy the hostile matrix above while
    validating nothing; a 401/403 would mean the matrix has been measuring the
    credential rather than the identifier.
    """
    for path, _scope in API_KEY_ENDPOINTS:
        status, body = _fire_api_key(matrix, path, value)
        assert status == 404, (
            f"{path} answered {status} for the usable identifier {value!r}, not 404: "
            f"{body}. The matrix above would then be measuring a closed door."
        )
        assert body.get("type", "").endswith("proposal-not-found"), body


# ======================================================================================
# routing -- enumerated from the app, never from memory
# ======================================================================================


def test_every_mutating_route_the_app_mounts_is_in_the_identifier_matrix() -> None:
    """A new mutating route must join the matrix rather than escape it.

    The matrix has two halves because the application has two classes of mutating
    route (T-11), and each half is *fired*, not merely listed:
    :data:`TRIGGER_ENDPOINTS` carry their identifier in the request body and are
    driven by `test_every_endpoint_refuses_the_same_identifier_identically`;
    :data:`API_KEY_ENDPOINTS` carry it in the path segment and are driven by
    `test_every_api_key_route_refuses_the_same_hostile_path_identically`. A route
    on neither list is a route no hostile identifier is ever posted at.
    """
    app = _app_with_every_trigger_route()
    covered = {path for path, _ in TRIGGER_ENDPOINTS} | {path for path, _ in API_KEY_ENDPOINTS}
    mounted = {route.path for route in mutating_routes(app)}
    assert mounted, "no mutating route was enumerated at all"
    assert mounted <= covered, (
        "the application mounts a mutating route the identifier matrix does not "
        f"cover: {sorted(mounted - covered)}"
    )


def _mentions_str(annotation: object) -> bool:
    """True when ``annotation`` can bind a `str` -- ``str``, ``str | None``, ``list[str]``."""
    if annotation is str:
        return True
    return any(_mentions_str(arg) for arg in get_args(annotation))


def _caller_supplied_text(endpoint: Any) -> dict[str, str]:
    """``name -> where`` for every **text** value ``endpoint`` binds from the caller.

    Read off the endpoint's own annotations (and, for a body model, that model's
    fields), so it describes the route the app is serving right now rather than a
    list somebody maintained. Dependency-injected parameters are excluded: a
    ``Principal`` comes from the auth dependency, not from the request body.
    """
    hints = get_type_hints(endpoint, include_extras=True)
    found: dict[str, str] = {}
    for name, annotation in hints.items():
        if name == "return":
            continue
        base = annotation
        if get_origin(annotation) is Annotated:
            args = get_args(annotation)
            base = args[0]
            if any(isinstance(meta, fastapi_params.Depends) for meta in args[1:]):
                continue
        if _mentions_str(base):
            found[name] = "parameter"
            continue
        if isinstance(base, type) and issubclass(base, BaseModel):
            for field_name, field in base.model_fields.items():
                if _mentions_str(field.annotation):
                    found[f"{name}.{field_name}"] = f"field of {base.__name__}"
    return found


def test_every_mutating_route_resolves_to_the_shared_identifier_validator() -> None:
    """Its module must call `validate_identifier`; it may not roll its own.

    Two branches, and **which branch a route takes is derived from the route**,
    not from an exemption list a reader has to trust:

    * a route that binds any **text** from the caller can be handed a control
      character, an unpaired surrogate or a NUL, so its module has to reach the
      one rule in `recon.adapters.identifiers`;
    * a route whose caller-supplied values are all *parsed* types -- the reviewer
      decisions take ``proposal_id: int`` -- cannot be handed one at all: the
      framework's parser refuses ``a\x00b`` before any handler code runs, which
      `test_every_api_key_route_refuses_the_same_hostile_path_identically` proves
      against the real app rather than assuming. Asserting the *absence* of a
      text parameter is what keeps that branch honest: retyping ``proposal_id``
      as ``str`` moves the route into the first branch and turns this red on the
      commit that does it.
    """
    import inspect

    app = _app_with_every_trigger_route()
    routes = mutating_routes(app)
    checked = 0
    exempt: list[str] = []
    for route in routes:
        module = inspect.getmodule(route.endpoint)
        assert module is not None
        source = inspect.getsource(module)
        checked += 1
        text_inputs = _caller_supplied_text(route.endpoint)
        if not text_inputs:
            exempt.append(route.path)
            continue
        assert "validate_identifier" in source, (
            f"{route.path} accepts a client-supplied identifier "
            f"({sorted(text_inputs)}) but its module never calls "
            "recon.adapters.identifiers.validate_identifier"
        )
    assert checked == len(routes)
    assert checked >= 6, (
        "expected the three trigger endpoints and the three reviewer decisions, "
        f"enumerated {checked}"
    )
    assert sorted(exempt) == sorted(path for path, _ in API_KEY_ENDPOINTS), (
        "the set of routes that take NO text from the caller has changed: "
        f"{sorted(exempt)}. Every other mutating route must reach the shared rule."
    )


def test_the_payload_validator_reaches_the_same_rule() -> None:
    """A natural key is an identifier too, and lands in a text column too.

    `raw_records.natural_key` is the same kind of value in the same kind of
    column, so it is judged by the same function -- not by a second control-
    character set living in the payload validator.
    """
    import inspect

    from recon.adapters import validation as validation_module

    source = inspect.getsource(validation_module)
    assert "identifier_fault(" in source, (
        "the payload validator no longer routes the natural key through the shared identifier rule"
    )


# ======================================================================================
# structural -- one module owns the character rule
# ======================================================================================

#: The modules allowed to spell a character class, and why. Published as data --
#: not as prose in a docstring -- so a third one fails the test that reads it,
#: the same shape `recon.logging.AUDIT_WRITERS` uses for `audit_log` writers.
#:
#: `recon/reference.py` keeps its own control-character set on purpose: it guards
#: the SS5.4 **fingerprint**, whose elements are joined with `\x1f`, and it runs
#: *after* `identifier_fault` in `validate_payload`. It is a strictly narrower
#: second guard on an already-checked value, which its docstring says outright
#: ("the first of two independent guards, not the only one") -- not a second
#: answer to "may this identifier be stored".
CHARACTER_RULE_MODULES: dict[str, str] = {
    "adapters/identifiers.py": "the rule itself",
    "reference.py": (
        "SS5.4 fingerprint safety: a ref may not carry the U+001F its section "
        "separator uses. Runs after the shared rule, never instead of it."
    ),
}

_TELLS = (
    re.compile(r"range\(0x20\)"),
    re.compile(r"range\(32\)"),
    re.compile(r"unicodedata\.category"),
    re.compile(r"\.isprintable\(\)"),
)


def _recon_sources() -> list[Path]:
    return sorted(p for p in RECON_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def test_the_character_rule_is_spelled_in_exactly_the_declared_modules() -> None:
    """A second implementation has to spell a character class. This is where it is caught."""
    found: dict[str, list[str]] = {}
    for path in _recon_sources():
        relative = str(path.relative_to(RECON_ROOT))
        hits = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if any(tell.search(line) for tell in _TELLS)
            and not line.lstrip().startswith(("#", "#:"))
            and "``" not in line
        ]
        if hits:
            found[relative] = hits
    assert set(found) <= set(CHARACTER_RULE_MODULES), (
        "a character class is spelled outside the declared modules, which is how "
        f"two answers to 'may this identifier be stored' come to exist again: "
        f"{ {k: v for k, v in found.items() if k not in CHARACTER_RULE_MODULES} }"
    )
    assert IDENTIFIERS_MODULE in found, (
        "the shared rule stopped spelling a character class at all; it has been hollowed out"
    )


def test_no_module_declares_its_own_identifier_length_bound() -> None:
    """`max_length=200` on a model field is half a rule, and half a rule diverges.

    `RecordsRequest.run_id` carried `max_length=200` while `TriggerRequest.run_id`
    carried it too and neither carried anything else -- so both endpoints agreed
    about length and disagreed about every other property of the same value.
    """
    offenders: dict[str, list[str]] = {}
    for path in _recon_sources():
        relative = str(path.relative_to(RECON_ROOT))
        if relative == IDENTIFIERS_MODULE:
            continue
        hits = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if "max_length" in line and not line.lstrip().startswith(("#", "#:"))
        ]
        # calling the shared rule with an explicit bound is delegation, not a rule
        hits = [line for line in hits if "identifier_fault" not in line]
        if hits:
            offenders[relative] = hits
    assert not offenders, f"an identifier length bound is declared outside the rule: {offenders}"


@pytest.mark.parametrize("model", [RecordsRequest, TriggerRequest], ids=["records", "trigger"])
def test_the_request_models_state_no_identifier_rule_of_their_own(model: type) -> None:
    """`run_id` is a plain optional string in every envelope.

    The rule is applied by the handler through the one validator. A pydantic
    annotation that restates part of it is a second implementation wearing a type
    hint, and it is what made the two envelopes disagree.
    """
    field = model.model_fields["run_id"]
    assert field.metadata == [], (
        f"{model.__name__}.run_id carries its own constraints {field.metadata}; the "
        "identifier rule lives in recon.adapters.identifiers"
    )
    assert field.default is None


def test_the_rule_text_is_one_string() -> None:
    """Every rejection quotes the same sentence, so a client is told one thing."""
    assert "NUL" in IDENTIFIER_RULE
    assert "unpaired surrogate" in IDENTIFIER_RULE
    assert "whitespace-only" in IDENTIFIER_RULE
    assert str(IDENTIFIER_MAX_LENGTH) in IDENTIFIER_RULE


# ======================================================================================
# the validator itself is total
# ======================================================================================


@pytest.mark.parametrize(
    "value",
    [None, 7, 1.5, b"bytes", [], {}, object()],
    ids=["none", "int", "float", "bytes", "list", "dict", "object"],
)
def test_identifier_fault_never_raises_whatever_it_is_handed(value: object) -> None:
    """A value that breaks the checker must be a rejection, not an escape.

    The check that decides storability is itself `value.encode("utf-8")`, which
    is the operation that throws -- so totality here is not a nicety, it is the
    difference between a 422 and the 500 this module exists to remove.
    """
    reason = identifiers_module.identifier_fault(value)
    assert isinstance(reason, str) and reason, f"{value!r} was accepted as an identifier"


def test_a_usable_identifier_is_returned_unchanged() -> None:
    assert identifiers_module.validate_identifier("  keep  ") == "  keep  "
    assert identifiers_module.identifier_fault("plain-run-id") is None


def test_every_hostile_identifier_is_refused_by_the_validator_directly() -> None:
    """The matrix goes through HTTP; this pins the function itself."""
    for case, value, why in HOSTILE_IDENTIFIERS:
        assert identifiers_module.identifier_fault(value) is not None, (
            f"the {case} identifier ({why}) was accepted by the shared rule"
        )
