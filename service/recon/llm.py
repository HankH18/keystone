"""Rationale text from a model — and nothing else (SPEC R17; DESIGN §Decisions).

**The LLM produces rationale text only.** It never detects a conflict, never
computes a confidence number, and never writes to any table. DESIGN pins that as
a decision, not a phase: LLMs are non-deterministic even at temperature 0,
determinism is graded, and a raw LLM confidence number is disqualified by the
brief. Everything in this module returns a string or ``None``; there is no code
path from here into ``conflicts``, ``proposals.confidence`` or ``entities``.

Which means the failure story is simple, and is the point:

    **The rationale is a nicety. The proposal is the product.**

If the provider errors, times out, is unconfigured, or the spend cap refuses the
call, :func:`generate_rationale` returns ``text=None`` with a status and logs a
warning. It does not raise. The reconciler lands the proposal with
``rationale = NULL`` and the run continues.

Money leaves this module only as EVIDENCE
-----------------------------------------
"The rationale is a nicety" is a statement about the *proposal*, never about the
*money*. A provider that timed out after generating did the work and will bill
for it, so releasing its reservation in full is the application refunding money
it actually spent -- a cap bypass that needs no database access at all, and one
a red team used to bill unbounded money against a ledger reading zero.

:mod:`recon.budget` closes that structurally: a reservation is closed against a
typed :class:`~recon.budget.SpendEvidence` value and nothing else. This module's
whole job on the money side is therefore to pick the right one, and there are
exactly three outcomes it can pick from:

* the call succeeded **and reported usage that is present and non-degenerate**
  -> :class:`~recon.budget.ProviderReportedUsage`. Charge what the tokens cost;
* the call failed and the request **provably** never left this process
  -> :class:`~recon.budget.NeverSent`, carrying the
  :class:`~recon.budget.PreSendProof` that :func:`_pre_send_proof` classified
  the transport's own exception as. Charge zero. :class:`ProviderNotSent` is
  raised only for that case, and the classification is a whitelist: an
  unrecognised exception is post-send, because the safe default when you cannot
  prove where a failure happened is that the money is gone. The proof is derived
  from the exception here and is a closed-vocabulary member the settle trigger
  also holds -- the one construction that grants a 100% refund used to accept
  ``NeverSent("trust me bro")``;
* **anything else** -> :class:`~recon.budget.OutcomeUnknown`. Charge the full
  reservation. That includes the case a red team found next: a call that
  returns real text with an absent or zeroed usage block. ``Usage()`` prices at
  zero, so 100 successful, text-returning, billed calls were charged nothing.
  Text with no usage is not a cost of zero -- it is an UNKNOWN, and unknown
  charges the worst case.

Two consequences are worth stating plainly, because both look like bugs and
neither is:

* **a retry after a post-send failure pays the full worst case twice.** That is
  the honest price of not knowing, and it is why a failure storm now walks into
  the cap and halts instead of looping forever against a ledger that never moves;
* **an overspend halts the scope, not just the call.** ``settle_capped`` records
  a durable halt, so the next :func:`recon.budget.reserve` on that scope is
  refused (:data:`STATUS_SCOPE_HALTED`) rather than proceeding against a ledger
  that is known to under-count.

There is no ``scopes`` argument
-------------------------------
:func:`generate_rationale` used to take one, and it reached
:func:`recon.budget.reserve` unchanged -- so "spend without the mandated daily
cap" was one keyword away from every caller of this module, guarded only by a
type whose constructor inspected the call stack. R17 mandates the daily cap, so
applying it is :func:`recon.budget.reserve`'s job now and nothing on this path
can express its absence. This module supplies ``run_id`` and the reservation
lands on both mandated scopes, always.

The provider is env-selected and **mock is the default**
--------------------------------------------------------
``LLM_PROVIDER=mock`` (the default in :class:`recon.config.Settings` and in
``.env.example``) needs no key and no network, so the whole suite -- including
the graded burst test -- runs keyless. The mock is not a stub around the ledger:
it returns deterministic text **and deterministic provider-usage numbers**, and
those numbers go through the same :func:`recon.budget.cost_microusd`, the same
reservation and the same settlement as a live call. The burst test therefore
exercises the real cap rather than a simulation of one.

Selecting ``anthropic`` without a key fails **loudly**. It does not fall back to
the mock: a deployment that believes it is calling a model and is quietly
getting canned text is a worse outcome than a visible error.

Every attempt reserves
----------------------
A retry is a fresh :func:`recon.budget.reserve` with a fresh idempotency key --
never a reuse of a reservation that was already refused, and never a call
without one. When the cap refuses, the attempt loop stops: a cap hit is
terminal, and retrying it would only produce more ``KS006``.

PII
---
Prompts carry personal data (names, emails, DOBs from the evidence packet). Every
prompt and response that is logged goes through :func:`recon.logging.audit_detail`
first, which redacts to hash+preview in ``safe`` mode. Redaction is applied
**here**, to the body, *as well as* by the structlog processor chain that every
entry point now installs (:func:`recon.logging.configure_logging_once`), so a
payload built by this module is safe even in a process that never configured a
logger -- an embedded interpreter, a REPL, a test.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, Protocol

from sqlalchemy.exc import DBAPIError

from recon.budget import (
    DEFAULT_LEASE_SECONDS,
    KS_CAP_EXCEEDED,
    BudgetCapExceeded,
    BudgetError,
    BudgetOverspend,
    BudgetScopeHalted,
    NeverSent,
    OutcomeUnknown,
    PreSendProof,
    ProviderReportedUsage,
    Reservation,
    SettlementRefused,
    SpendEvidence,
    UnknownModelError,
    Usage,
    degenerate_usage_reason,
    price_table,
    reserve,
    settle_capped,
    settle_failed_call,
    worst_case_input_tokens,
)
from recon.config import get_settings
from recon.logging import audit_detail, get_logger

__all__ = [
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "MOCK_MODEL_ID",
    "STATUS_BUDGET_ERROR",
    "STATUS_CAP_HIT",
    "STATUS_INTERNAL_ERROR",
    "STATUS_OK",
    "STATUS_OVERSPEND",
    "STATUS_PROVIDER_ERROR",
    "STATUS_REPLAYED",
    "STATUS_SCOPE_HALTED",
    "STATUS_UNPRICED",
    "SYSTEM_PROMPT",
    "AnthropicProvider",
    "MockProvider",
    "ProviderError",
    "ProviderNotConfigured",
    "ProviderNotSent",
    "ProviderResult",
    "RationaleOutcome",
    "RationaleProvider",
    "RationaleRequest",
    "build_provider",
    "generate_rationale",
]

log = get_logger("recon.llm")

#: The offline provider's model id. It is priced in `prices.yaml` at the
#: production rate, so mock runs move real money through the real ledger.
MOCK_MODEL_ID: Final = "mock-rationale-v1"

#: Rationale is one short paragraph. The reservation is worst-case on this
#: number, so it is a cost lever as well as a length limit.
DEFAULT_MAX_OUTPUT_TOKENS: Final = 384

STATUS_OK: Final = "ok"
STATUS_CAP_HIT: Final = "cap_hit"
STATUS_PROVIDER_ERROR: Final = "provider_error"
STATUS_UNPRICED: Final = "unpriced_model"
#: A budget refusal that is NOT the cap -- today, a scope nobody provisioned.
#: Kept distinct so "the cap fired" stays a claim about the cap.
STATUS_BUDGET_ERROR: Final = "budget_error"
#: The provider reported more spend than the reservation could hold. Terminal,
#: and deliberately NOT `ok`: a run that spent more than it reserved has an
#: under-counting ledger, so continuing would spend against a wrong number.
STATUS_OVERSPEND: Final = "overspend"
#: The idempotency key had already reserved. Nothing was charged again and the
#: paid call is NOT repeated -- repeating it would be a call the cap never saw.
STATUS_REPLAYED: Final = "replayed"
#: The scope has already overspent and now refuses every reservation. Terminal
#: and durable: only ops lifts it, after reconciling the ledger.
STATUS_SCOPE_HALTED: Final = "scope_halted"
#: Something this module did not anticipate escaped. `generate_rationale` is
#: documented never to raise, so the last resort is a status and not a traceback
#: through the reconciler -- see :func:`generate_rationale`.
STATUS_INTERNAL_ERROR: Final = "internal_error"

#: Slack added to a provider's own timeout to get the reservation's lease. Wide
#: on purpose: an early lease expiry lets the sweeper reclaim a LIVE
#: reservation, which loses that call's cost from the ledger permanently, while
#: a late one merely holds budget a little longer.
LEASE_MARGIN_SECONDS: Final = 120

#: Frozen system prompt. Frozen on purpose: prompt caching is a **prefix** match,
#: so any byte that changes here invalidates the cache for every call. It also
#: states the one job the model has, because "explain, do not decide" is a
#: property we want the model to reinforce rather than merely rely on the code
#: for.
SYSTEM_PROMPT: Final = (
    "You write one short paragraph explaining, to a human reviewer, why two "
    "systems disagree about a record and why the proposed fix is the likely "
    "correction. You are describing a decision that has already been made by "
    "deterministic rules. Do not decide anything, do not assign a confidence, "
    "do not recommend applying or rejecting, and do not invent facts that are "
    "not in the evidence. Plain prose, no preamble, no bullet points, at most "
    "four sentences."
)

#: Models whose API removed `temperature`/`top_p`/`top_k` -- sending any of them
#: is a 400. The ticket asks for temperature 0; on these models "as
#: deterministic as the API allows" is expressed by sending no sampling
#: parameter at all, which is why this set exists rather than a bare
#: `temperature=0` that would break the configured default model.
_SAMPLING_REMOVED: Final = frozenset(
    {
        "claude-fable-5",
        "claude-mythos-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-sonnet-5",
    }
)

#: Models that accept `output_config.effort`. Rationale is a four-sentence
#: explanation of a decision already made, so it runs at the cheapest depth.
_EFFORT_MODELS: Final = _SAMPLING_REMOVED | {"claude-opus-4-6", "claude-sonnet-4-6"}


# ===========================================================================
# provider interface
# ===========================================================================
class ProviderError(RuntimeError):
    """The provider call failed. Recoverable: the proposal still lands.

    **Financially this is the expensive class.** A bare ``ProviderError`` says
    the call failed and says nothing about *where*, so it is treated as
    post-send and its reservation is charged in full. Raise
    :class:`ProviderNotSent` instead -- and only -- when the request provably
    never left this process.
    """


class ProviderNotSent(ProviderError):
    """The request PROVABLY never reached the provider, so nothing was billed.

    The only failure class that settles at zero, which is why the bar for
    raising it is evidence and not optimism: a connection that was refused, a
    hostname that did not resolve, a request the client rejected before sending,
    an authentication rejection at the edge. Anything that happened after bytes
    went out -- a timeout, a read error, a 5xx, a cancelled stream -- is not
    this, because the provider may have generated the response and will bill for
    it whether or not it reached us.

    If you are unsure which side of the send a failure is on, it is not this.

    ``proof`` names *which* pre-send failure this is, from
    :class:`~recon.budget.PreSendProof` -- the same closed vocabulary the
    database holds. It defaults to
    :attr:`~recon.budget.PreSendProof.CLIENT_REJECTED_REQUEST`, which is what a
    transport asserting "I did not send this" is claiming. There is deliberately
    no way to attach free text as the justification: a full release is granted on
    a classified transport failure, never on a sentence.
    """

    proof: PreSendProof = PreSendProof.CLIENT_REJECTED_REQUEST

    def __init__(self, *args: object, proof: PreSendProof | None = None) -> None:
        super().__init__(*args)
        if proof is not None:
            self.proof = proof


class ProviderNotConfigured(ProviderNotSent):
    """A live provider was selected without the credentials it needs.

    Pre-send by construction: the request cannot be built, let alone sent.
    """

    proof: PreSendProof = PreSendProof.CLIENT_REJECTED_REQUEST


@dataclass(frozen=True)
class RationaleRequest:
    """One rationale ask.

    ``subject`` is a conflict fingerprint or proposal id -- an identifier, never
    personal data, because it lands in ``audit_log.subject`` unredacted.
    ``prompt`` is the evidence packet and **does** carry personal data.
    """

    subject: str
    prompt: str
    system: str = SYSTEM_PROMPT

    def prompt_bytes(self) -> int:
        return len((self.system + self.prompt).encode("utf-8"))


@dataclass(frozen=True)
class ProviderResult:
    """What a provider returns: text plus the usage **it reported**."""

    text: str
    usage: Usage
    model: str


class RationaleProvider(Protocol):
    """The seam every provider implements. Read-only, one method, no writes."""

    model: str

    def complete(self, request: RationaleRequest, *, max_output_tokens: int) -> ProviderResult:
        """Return rationale text and provider-reported usage, or raise."""


def _mock_tokens(text: str) -> int:
    """A deterministic token count for the mock: ~4 bytes per token, never 0."""
    return len(text.encode("utf-8")) // 4 + 1


@dataclass
class MockProvider:
    """Deterministic offline provider. The default, and the graded one.

    Same prompt in, byte-identical text out, identical usage numbers out. That
    determinism is what lets the burst test assert an **exact** spend figure
    against the real ledger instead of a range.

    ``on_call`` is a test seam and nothing else: the burst test uses it to hold
    a call open at a barrier so every contender's reservation is in flight at
    the same instant. Production leaves it ``None``.
    """

    model: str = MOCK_MODEL_ID
    on_call: Callable[[RationaleRequest], None] | None = None

    def complete(self, request: RationaleRequest, *, max_output_tokens: int) -> ProviderResult:
        if self.on_call is not None:
            self.on_call(request)
        digest = hashlib.sha256(request.prompt.encode("utf-8")).hexdigest()
        text = (
            "The two sources disagree because one of them is holding a value "
            "that the other has since replaced. The proposed fix keeps the "
            "value the more recently loaded source asserts, which the evidence "
            f"above supports. (deterministic mock rationale {digest[:12]})"
        )
        output_tokens = min(_mock_tokens(text), max_output_tokens)
        return ProviderResult(
            text=text,
            usage=Usage(
                input_tokens=_mock_tokens(request.prompt),
                output_tokens=output_tokens,
                # The frozen system prompt is the cached prefix, exactly as the
                # live path arranges it -- so the mock exercises the cache-read
                # rate in the committed price table rather than skipping it.
                cache_read_tokens=_mock_tokens(request.system),
                cache_write_tokens=0,
            ),
            model=self.model,
        )


@dataclass
class AnthropicProvider:
    """Live provider. Temperature 0 where the model still accepts it, caching on.

    Two API facts shape this and are worth stating, because both contradict the
    obvious code:

    * **`temperature=0` is a 400 on the configured default model.** Sampling
      parameters were removed on Opus 5 and the rest of the 5/4.7/4.8 family, so
      the request omits them there and sends ``temperature=0`` only on models
      that still take it. See ``_SAMPLING_REMOVED``.
    * **Prompt caching is a prefix match**, so the ``cache_control`` breakpoint
      goes on the frozen system prompt and the volatile evidence packet goes
      after it. Reversing that would cache nothing and report
      ``cache_read_input_tokens == 0`` forever.

    Usage comes back from ``response.usage`` and is passed to the ledger
    verbatim: cost is computed from provider-reported numbers, never estimated.
    """

    model: str
    api_key: str
    timeout_seconds: float = 30.0
    max_retries: int = 0
    _client: Any = field(default=None, repr=False, compare=False)

    def _client_or_build(self) -> Any:
        if self._client is None:
            import anthropic

            # max_retries=0: the SDK's own retry would repeat a call this module
            # has already reserved and settled for. Retries live in
            # `generate_rationale`, where every attempt re-reserves.
            self._client = anthropic.Anthropic(
                api_key=self.api_key,
                timeout=self.timeout_seconds,
                max_retries=self.max_retries,
            )
        return self._client

    def _extra_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.model not in _SAMPLING_REMOVED:
            params["temperature"] = 0
        if self.model in _EFFORT_MODELS:
            params["output_config"] = {"effort": "low"}
        return params

    def complete(self, request: RationaleRequest, *, max_output_tokens: int) -> ProviderResult:
        client = self._client_or_build()
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=max_output_tokens,
                system=[
                    {
                        "type": "text",
                        "text": request.system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": request.prompt}],
                **self._extra_params(),
            )
        except Exception as exc:  # SDK raises a family of typed errors
            # Deliberately NOT classified here. The wrapper preserves the SDK
            # exception as `__cause__` and `_reached_provider` reads it, so the
            # pre-send whitelist lives in exactly one place instead of being
            # re-guessed by every provider that wraps an error.
            raise ProviderError(f"{type(exc).__name__}: {exc}") from exc

        usage = response.usage
        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        return ProviderResult(
            text=text,
            usage=Usage(
                input_tokens=getattr(usage, "input_tokens", 0) or 0,
                output_tokens=getattr(usage, "output_tokens", 0) or 0,
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            ),
            model=self.model,
        )


def build_provider(name: str | None = None) -> RationaleProvider:
    """Build the configured provider. ``mock`` unless the environment says otherwise.

    Selecting ``anthropic`` without ``ANTHROPIC_API_KEY`` raises
    :class:`ProviderNotConfigured` rather than falling back to the mock. A
    silent fallback would let a deployment believe it is calling a model while
    it serves canned text -- and would make "the suite passes keyless" a claim
    about a fallback rather than about the default.

    The key is **stripped before it is judged**. ``if not key`` is False for
    ``"   "``, so a whitespace value -- exactly what a mis-pasted dashboard
    secret or a shell-quoted empty variable produces -- used to build a live
    provider that failed later, at the first call, as an opaque 401. That is the
    silent-misconfiguration failure this branch exists to prevent, so it fails
    here, at build time, as documented.
    """
    settings = get_settings()
    resolved = (name or settings.llm_provider or "mock").strip().lower()
    if resolved == "mock":
        return MockProvider()
    if resolved == "anthropic":
        api_key = (settings.anthropic_api_key or "").strip()
        if not api_key:
            raise ProviderNotConfigured(
                "LLM_PROVIDER=anthropic needs a non-blank ANTHROPIC_API_KEY (a "
                "whitespace-only value is treated as absent). Set it, or leave "
                "LLM_PROVIDER=mock (the default) to run offline -- this does not "
                "silently fall back to the mock."
            )
        return AnthropicProvider(model=settings.llm_model, api_key=api_key)
    raise ProviderNotConfigured(
        f"unknown LLM_PROVIDER {resolved!r}; expected 'mock' or 'anthropic'"
    )


# ===========================================================================
# where did the failure happen? -- the money question
# ===========================================================================
#: Exception class names that PROVE a request never left this process, or was
#: rejected by the provider before any generation happened. A whitelist, not a
#: blacklist: an unrecognised error is post-send, because the safe default when
#: you cannot prove where a failure occurred is that the money is gone.
#:
#: Note what is deliberately absent. ``ConnectionResetError`` and
#: ``httpx.ReadTimeout``/``ReadError`` happen *after* bytes went out.
#: ``APITimeoutError`` subclasses the SDK's connection error and is the exact
#: case this whole classification exists for -- a provider that generated a
#: response and could not deliver it still bills for it.
#:
#: It is a *mapping* rather than a set because the proof a release is granted on
#: is now a :class:`~recon.budget.PreSendProof` member and not a string a caller
#: writes: the transport's own exception class is what classifies it, and the
#: settle trigger holds the same closed vocabulary. ``NeverSent("trust me bro")``
#: does not typecheck on either side of the boundary any more.
_PRE_SEND_EXCEPTIONS: Final[Mapping[str, PreSendProof]] = MappingProxyType(
    {
        "ConnectionRefusedError": PreSendProof.CONNECTION_REFUSED,
        "ConnectError": PreSendProof.CONNECTION_REFUSED,
        "ConnectTimeout": PreSendProof.CONNECTION_REFUSED,
        "ConnectTimeoutError": PreSendProof.CONNECTION_REFUSED,
        "ProxyError": PreSendProof.CONNECTION_REFUSED,
        "gaierror": PreSendProof.DNS_FAILURE,
        "NameResolutionError": PreSendProof.DNS_FAILURE,
        "SSLError": PreSendProof.TLS_HANDSHAKE_FAILED,
        "SSLCertVerificationError": PreSendProof.TLS_HANDSHAKE_FAILED,
        "UnsupportedProtocol": PreSendProof.CLIENT_REJECTED_REQUEST,
        "InvalidURL": PreSendProof.CLIENT_REJECTED_REQUEST,
        "AuthenticationError": PreSendProof.AUTH_REJECTED_AT_EDGE,
        "PermissionDeniedError": PreSendProof.AUTH_REJECTED_AT_EDGE,
    }
)

#: HTTP statuses that mean the provider refused the request rather than serving
#: it: nothing was generated, so nothing was billed.
_PRE_SEND_STATUS_CODES: Final = frozenset({401, 403})

#: How far up a ``__cause__`` chain to look before giving up (and charging).
_CAUSE_DEPTH: Final = 5


def _is_pre_send(exc: BaseException) -> PreSendProof | None:
    """Which pre-send proof this exact exception establishes, or ``None``."""
    proof = _PRE_SEND_EXCEPTIONS.get(type(exc).__name__)
    if proof is not None:
        return proof
    if getattr(exc, "status_code", None) in _PRE_SEND_STATUS_CODES:
        return PreSendProof.AUTH_REJECTED_AT_EDGE
    return None


def _pre_send_proof(exc: BaseException, *, depth: int = 0) -> PreSendProof | None:
    """The proof that this failure is pre-send, or ``None``. Fails closed.

    ``None`` means "charge the full reservation", so every proof returned below
    is one the transport itself established:

    * :class:`ProviderNotSent` -- the provider asserted it, and carries the
      classification it is asserting;
    * a whitelisted exception class or a 401/403, anywhere in the ``__cause__``
      chain that a wrapper preserved.

    Everything else -- an unrecognised class, a bare :class:`ProviderError`, a
    timeout, a 5xx, a cancellation -- is post-send and gets nothing.
    """
    if isinstance(exc, ProviderNotSent):
        return _is_pre_send(exc) or exc.proof
    proof = _is_pre_send(exc)
    if proof is not None:
        return proof
    cause = exc.__cause__
    if cause is not None and depth < _CAUSE_DEPTH:
        return _pre_send_proof(cause, depth=depth + 1)
    return None


def _reached_provider(exc: BaseException) -> bool:
    """``True`` unless the failure is provably pre-send. Fails closed."""
    return _pre_send_proof(exc) is None


def _failure_evidence(exc: BaseException) -> SpendEvidence:
    """Which evidence value a provider failure is worth. Fails closed.

    Two answers, and the classification in :func:`_pre_send_proof` decides
    between them: a proven pre-send failure is
    :class:`~recon.budget.NeverSent` carrying the proof the transport
    established, and releases the reservation; everything else is
    :class:`~recon.budget.OutcomeUnknown` and charges it in full.

    The proof is *derived here*, from the exception, and is a closed-vocabulary
    member -- so the one construction that grants a 100% refund cannot be talked
    into existence by a caller with a persuasive string.
    """
    detail = f"{type(exc).__name__}: {exc}"
    proof = _pre_send_proof(exc)
    if proof is None:
        return OutcomeUnknown(f"the provider call failed after the request went out ({detail})")
    return NeverSent(proof, detail)


def _success_evidence(result: ProviderResult) -> SpendEvidence:
    """Which evidence value a SUCCESSFUL call is worth.

    A success is not automatically priceable. ``cost_microusd(model, Usage())``
    is zero, so a provider that returns real text with an absent or zeroed usage
    block used to settle at zero: 100 successful, text-returning, billed calls
    charged nothing, and no trigger could see it because the ledger cannot tell
    an honest zero from a fabricated one.

    So usage is evidence only when it is **present and non-degenerate**
    (:func:`recon.budget.degenerate_usage_reason`). When it is not, the actual
    cost of this call is UNKNOWN -- and unknown charges the full reservation.
    """
    degenerate = degenerate_usage_reason(result.usage)
    if degenerate is None:
        return ProviderReportedUsage(result.usage)
    log.error(
        "llm.usage_not_evidence",
        model=result.model,
        reason=degenerate,
        text_bytes=len(result.text.encode("utf-8")),
        detail=(
            "the provider returned a response but did not report what it billed; "
            "charging the full reservation because the actual is unknown"
        ),
    )
    return OutcomeUnknown(
        f"the call returned {len(result.text.encode('utf-8'))} bytes of text but "
        f"its usage is not evidence of a cost: {degenerate}"
    )


def _lease_seconds_for(provider: RationaleProvider) -> int:
    """How long this provider's call may credibly stay in flight.

    Taken from the provider's own timeout when it declares one, so the lease
    that entitles the sweeper to reclaim a reservation is derived from the same
    number that bounds the call rather than guessed independently of it.
    """
    timeout = getattr(provider, "timeout_seconds", None)
    if isinstance(timeout, int | float) and timeout > 0:
        return int(timeout) + LEASE_MARGIN_SECONDS
    return DEFAULT_LEASE_SECONDS


# ===========================================================================
# the one public entry point
# ===========================================================================
@dataclass(frozen=True)
class RationaleOutcome:
    """Result of a rationale attempt. ``text is None`` is a normal outcome."""

    text: str | None
    status: str
    attempts: int
    model: str
    cost_microusd: int = 0
    usage: Usage | None = None
    detail: str | None = None
    #: SQLSTATE of the database refusal, when there was one. `KS006` and only
    #: `KS006` means "the cap refused this call" -- so a dropped connection, a
    #: deadlock or a bug cannot masquerade as the cap holding.
    sqlstate: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


def generate_rationale(
    request: RationaleRequest,
    *,
    run_id: str,
    idempotency_key: str,
    provider: RationaleProvider | None = None,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    max_attempts: int = 2,
) -> RationaleOutcome:
    """Reserve, call, settle. Return rationale text, or ``None`` and a reason.

    **Never raises. Totally.** Not "never raises for the failures we thought of":
    the previous version raised ``ValueError`` on ``run_id=""`` -- a value
    ``/internal/reconcile`` supplies -- and propagated any ``DBAPIError`` from
    ``reserve`` that was not ``KS006``/``23505``: a pool timeout, a connection
    reset, a deadlock, a statement timeout. Callers written against "never
    raises" do not have an ``except`` clause, so each of those took down the run
    that the whole "the rationale is a nicety" design exists to keep alive.

    So this function is a total wrapper around :func:`_attempt_rationale` and the
    last resort is :data:`STATUS_INTERNAL_ERROR`. Note the money direction when
    the last resort fires *after* a reservation exists: the reservation stays
    ``open`` and fully charged until the sweeper closes it, which is the
    fail-closed direction and is why this can be quiet.
    """
    try:
        return _attempt_rationale(
            request,
            run_id=run_id,
            idempotency_key=idempotency_key,
            provider=provider,
            max_output_tokens=max_output_tokens,
            max_attempts=max_attempts,
        )
    except Exception as exc:  # the contract is totality; see the docstring
        log.error(
            "llm.rationale_internal_error",
            subject=request.subject,
            error=f"{type(exc).__name__}: {exc}",
            detail=(
                "generate_rationale is documented never to raise; any reservation "
                "already taken stays open and fully charged until its lease expires"
            ),
            exc_info=True,
        )
        return RationaleOutcome(
            text=None,
            status=STATUS_INTERNAL_ERROR,
            attempts=0,
            model="unknown",
            detail=f"{type(exc).__name__}: {exc}",
        )


def _attempt_rationale(
    request: RationaleRequest,
    *,
    run_id: str,
    idempotency_key: str,
    provider: RationaleProvider | None,
    max_output_tokens: int,
    max_attempts: int,
) -> RationaleOutcome:
    """The attempt loop. See :func:`generate_rationale` for the contract.

    The order is fixed and is the R17 requirement: the worst-case cost is
    reserved **before** the call, and the call's cost is settled **after** it
    against a typed evidence value. Nothing calls a provider without a live
    reservation.

    Retries re-reserve. Attempt *n* uses idempotency key ``<key>#attempt<n>``,
    so it is a new row through the same ``BEFORE INSERT`` trigger. A cap hit ends
    the loop -- it is terminal, not transient -- and so does a halted scope.

    **A failed attempt is not a free attempt.** Its reservation is closed through
    :func:`recon.budget.settle_failed_call` with the evidence from
    :func:`_failure_evidence`: released only when the request provably never
    reached the provider, charged in full otherwise. So a storm of timeouts
    consumes budget at the worst-case rate and walks into the cap, instead of
    refunding itself and retrying for ever.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    try:
        active = provider if provider is not None else build_provider()
    except ProviderError as exc:
        log.warning("llm.provider_unavailable", subject=request.subject, error=str(exc))
        return RationaleOutcome(
            text=None, status=STATUS_PROVIDER_ERROR, attempts=0, model="none", detail=str(exc)
        )

    model = active.model
    if not run_id:
        # `/internal/reconcile` supplies this, and `run_scope("")` used to raise
        # straight through a function documented never to raise.
        log.error("llm.missing_run_id", subject=request.subject)
        return RationaleOutcome(
            text=None,
            status=STATUS_BUDGET_ERROR,
            attempts=0,
            model=model,
            detail=(
                "run_id is empty, so there is no per-run ledger scope to reserve "
                "against; R17 mandates a per-run cap as well as the daily one"
            ),
        )

    input_bound = worst_case_input_tokens(request.system + request.prompt)
    lease_seconds = _lease_seconds_for(active)
    _log_prompt(request, model=model)

    last_detail: str | None = None
    for attempt in range(1, max_attempts + 1):
        attempt_key = f"{idempotency_key}#attempt{attempt}"
        try:
            reservation = reserve(
                idempotency_key=attempt_key,
                model=model,
                max_output_tokens=max_output_tokens,
                max_input_tokens=input_bound,
                run_id=run_id,
                lease_seconds=lease_seconds,
            )
        except BudgetCapExceeded as exc:
            # Terminal. `reserve` has already written the `cap_hit` audit row
            # and fired the alert; the run halts here rather than retrying into
            # the same trigger.
            log.warning(
                "llm.rationale_skipped_cap",
                subject=request.subject,
                scope=exc.scope,
                attempt=attempt,
            )
            return RationaleOutcome(
                text=None,
                status=STATUS_CAP_HIT,
                attempts=attempt,
                model=model,
                detail=exc.detail,
                sqlstate=exc.sqlstate or KS_CAP_EXCEEDED,
            )
        except BudgetScopeHalted as exc:
            # THE OVERSPEND HALT, consumed. This scope's ledger is known to
            # under-count real spend, so there is nothing to retry into: every
            # further reservation on it is refused until ops resumes it.
            log.error(
                "llm.rationale_scope_halted",
                subject=request.subject,
                scope=exc.scope,
                attempt=attempt,
            )
            return RationaleOutcome(
                text=None,
                status=STATUS_SCOPE_HALTED,
                attempts=attempt,
                model=model,
                detail=str(exc),
            )
        except UnknownModelError as exc:
            log.error("llm.model_not_priced", subject=request.subject, model=model)
            return RationaleOutcome(
                text=None,
                status=STATUS_UNPRICED,
                attempts=attempt,
                model=model,
                detail=str(exc),
            )
        except BudgetError as exc:
            # Anything else the ledger refuses -- an unprovisioned scope, most
            # likely. Still not fatal: the proposal lands without a rationale.
            log.error(
                "llm.budget_refused",
                subject=request.subject,
                attempt=attempt,
                error=f"{type(exc).__name__}: {exc}",
            )
            return RationaleOutcome(
                text=None,
                status=STATUS_BUDGET_ERROR,
                attempts=attempt,
                model=model,
                detail=str(exc),
            )
        except DBAPIError as exc:
            # A pool timeout, a reset connection, a deadlock, a statement
            # timeout. None of them is the cap, none of them is a bug in the
            # caller, and every one of them used to propagate out of a function
            # documented never to raise. Nothing was reserved: the transaction
            # rolled back with the error.
            log.error(
                "llm.budget_unavailable",
                subject=request.subject,
                attempt=attempt,
                sqlstate=_dbapi_sqlstate(exc),
                error=f"{type(exc).__name__}: {exc}",
            )
            return RationaleOutcome(
                text=None,
                status=STATUS_BUDGET_ERROR,
                attempts=attempt,
                model=model,
                detail=f"{type(exc).__name__}: {exc}",
                sqlstate=_dbapi_sqlstate(exc),
            )

        if reservation.replayed:
            # The key already reserved. Calling the provider now would be a paid
            # call against a reservation this attempt did not make -- the exact
            # shape of "a call the cap never saw". Idempotent no-op instead.
            log.warning(
                "llm.rationale_replayed",
                subject=request.subject,
                idempotency_key=attempt_key,
                attempt=attempt,
            )
            return RationaleOutcome(
                text=None,
                status=STATUS_REPLAYED,
                attempts=attempt,
                model=model,
                detail=(
                    f"idempotency key {attempt_key!r} has already reserved; the call it "
                    "covers is not repeated and nothing was charged again"
                ),
            )

        try:
            result = active.complete(request, max_output_tokens=max_output_tokens)
        except Exception as exc:
            # THE REFUND RULE. A failed call is released only when the request
            # provably never reached the provider. A timeout after generation is
            # work the provider did and will bill for, so its reservation is
            # charged in full -- refunding it is the application handing back
            # money it actually spent, which no database trigger can catch.
            last_detail = f"{type(exc).__name__}: {exc}"
            evidence = _failure_evidence(exc)
            _settle_failure(reservation, subject=request.subject, evidence=evidence)
            log.warning(
                "llm.call_failed",
                subject=request.subject,
                attempt=attempt,
                error=last_detail,
                evidence=evidence.kind,
                charged_microusd=(0 if evidence.releases else reservation.reserve_microusd),
            )
            continue

        try:
            settlement = settle_capped(reservation, _success_evidence(result))
        except BudgetOverspend as exc:
            # The reservation was settled at its cap, the shortfall audited and
            # alerted, and every scope it touched HALTED by `settle_capped`. The
            # run halts: the ledger is now known to under-count real spend, so
            # `ok` would be a false claim -- and, unlike the version this
            # replaces, the halt does not depend on anybody reading this status.
            log.error(
                "llm.settle_overspend",
                subject=request.subject,
                attempt=attempt,
                shortfall_microusd=exc.shortfall_microusd,
                reported_microusd=exc.settlement.reported_microusd,
                reserve_microusd=exc.settlement.reserve_microusd,
            )
            return RationaleOutcome(
                text=None,
                status=STATUS_OVERSPEND,
                attempts=attempt,
                model=model,
                cost_microusd=exc.settlement.actual_microusd,
                usage=result.usage,
                detail=str(exc),
            )
        except SettlementRefused as exc:
            # The reservation is gone (closed by the sweeper, or already
            # settled) and the money for this call cannot be recorded against
            # it. Loud, and NOT ok: an unrecordable cost is exactly what the
            # lease exists to prevent, so it must never look like a successful
            # cheap call.
            log.error(
                "llm.settle_refused",
                subject=request.subject,
                attempt=attempt,
                idempotency_key=reservation.idempotency_key,
                error=str(exc),
            )
            return RationaleOutcome(
                text=None,
                status=STATUS_BUDGET_ERROR,
                attempts=attempt,
                model=model,
                detail=str(exc),
                sqlstate=exc.sqlstate,
            )

        _log_response(result, subject=request.subject, cost_microusd=settlement.actual_microusd)
        return RationaleOutcome(
            text=result.text,
            status=STATUS_OK,
            attempts=attempt,
            model=result.model,
            cost_microusd=settlement.actual_microusd,
            usage=result.usage,
        )

    log.warning(
        "llm.rationale_unavailable",
        subject=request.subject,
        attempts=max_attempts,
        error=last_detail,
    )
    return RationaleOutcome(
        text=None,
        status=STATUS_PROVIDER_ERROR,
        attempts=max_attempts,
        model=model,
        detail=last_detail,
    )


def _dbapi_sqlstate(exc: BaseException) -> str | None:
    """SQLSTATE of a driver error, when the driver reported one."""
    return getattr(getattr(exc, "orig", None), "sqlstate", None)


def _settle_failure(
    reservation: Reservation,
    *,
    subject: str,
    evidence: SpendEvidence,
) -> None:
    """Close a reservation whose call failed, per the refund rule.

    Failing to settle must not mask the provider failure that got us here, so
    the exception is logged rather than raised -- but note which direction that
    leaves the money in: an unsettled reservation stays ``open`` and keeps its
    whole worst-case amount charged to the ledger until its lease expires, at
    which point the sweeper charges it in full as well. That is the fail-closed
    direction, and it is why this can be quiet.
    """
    try:
        settle_failed_call(reservation, evidence)
    except Exception as exc:  # pragma: no cover - the lease sweeper is the backstop
        log.error(
            "llm.settle_failed",
            subject=subject,
            idempotency_key=reservation.idempotency_key,
            evidence=evidence.kind,
            error=f"{type(exc).__name__}: {exc}",
            detail="the reservation stays open and fully charged until its lease expires",
        )


def _log_prompt(request: RationaleRequest, *, model: str) -> None:
    """Log the prompt through the redactor. Prompts carry personal data."""
    log.info(
        "llm.request",
        subject=request.subject,
        model=model,
        price_table_version=price_table().version,
        prompt=audit_detail({"prompt": request.prompt}),
    )


def _log_response(result: ProviderResult, *, subject: str, cost_microusd: int) -> None:
    """Log the response through the redactor. Rationale quotes the evidence."""
    log.info(
        "llm.response",
        subject=subject,
        model=result.model,
        cost_microusd=cost_microusd,
        usage=result.usage.as_dict(),
        response=audit_detail({"text": result.text}),
    )
