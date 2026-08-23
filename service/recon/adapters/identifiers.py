"""The **one** rule for a client-supplied identifier that lands in a text column.

Three modules used to answer "is this `run_id` acceptable?" and they answered it
three different ways. That is the same defect class as the two trigger-secret
checks: not a style problem, a correctness one, because the *weakest* answer is
the one that decides what the database is asked to store.

What the divergence actually produced, all reproduced against a real uvicorn
server rather than `TestClient`:

======================================  ======================  =================
value                                   ``/internal/ingest/records``  ``/internal/{sync,reconcile}``
======================================  ======================  =================
``"ctrl\\u0007char"``                    422 (refused)           **200, written to
                                                                ``audit_log.subject``**
``"a\\u0000b"`` (NUL mid-string)         422 (refused)           **bare 500** inside
                                                                ``claim_run``
``"sur\\ud800rogate"`` (lone surrogate)  **bare 500** at the     422 (refused)
                                        landing ``COPY``
``" "`` (whitespace only)                accepted                **200**, and the
                                                                claim key is a space
======================================  ======================  =================

Two 5xx and one silently-accepted control character out of one requirement.

The rule is the **column's** rule, not a taste
----------------------------------------------
`raw_records.run_id`, `raw_records.load_id`, `ingest_runs.run_id` and
`audit_log.subject` are all Postgres ``text``. So the rule is derived from what
that column, and the wire in front of it, can actually carry:

``NUL (U+0000)``
    Postgres ``text`` cannot hold it at all. psycopg refuses the parameter with a
    plain ``ValueError`` -- *not* a ``psycopg.Error`` -- which is why the handler
    that catches driver errors still let it out as a 500.
``unpaired surrogate``
    Not encodable as UTF-8, so it cannot be put on the wire. ``str.encode``
    raises ``UnicodeEncodeError``, again not a driver error.
``control characters`` (Unicode category ``Cc``: ``U+0000``-``U+001F``,
``U+007F``-``U+009F``)
    Storable, but an identifier is echoed into every structured log line, into
    ``load_id``, and into ``audit_log.subject``; a raw ``\\r`` or ``\\x1b`` there
    forges log records and corrupts a terminal. SS5.4 already refuses one in a
    natural key for the neighbouring reason (a ref is joined with ``\\x1f``).
``empty`` / ``whitespace-only``
    An identifier that identifies nothing. ``" "`` is the exact shape that made
    the trigger-secret check dangerous, and it is a misconfiguration here for the
    same reason: it is what a here-doc or a YAML quoting accident leaves behind.
``over-length``
    Bounded because every one of these values is *also* concatenated into
    ``load_id`` and into an advisory-lock key; unbounded input there is a request
    for an index-size failure at the write rather than a verdict at the door.

Everything on that list is a **4xx**, identically, on every endpoint. Nothing on
it is a 5xx, because none of it is our fault -- and nothing off it is refused,
because inventing a stricter rule (an allow-list of characters, say) would start
rejecting run ids that deployments legitimately use.

Total, like `validate_payload`
------------------------------
:func:`identifier_fault` returns a reason string or ``None`` and raises nothing:
every one of its own checks is inside a guard, so a value that breaks the
*checker* is a rejection rather than an escape. That matters here specifically --
``value.encode("utf-8")`` is one of the checks, and it is the operation that
throws.
"""

from __future__ import annotations

import unicodedata

__all__ = [
    "IDENTIFIER_MAX_LENGTH",
    "IDENTIFIER_RULE",
    "IdentifierError",
    "identifier_fault",
    "validate_identifier",
]

#: The longest identifier accepted. `run_id` is concatenated into `load_id`
#: (``{run_id}:{source}:{entity_type}:g{generation}``) and into the advisory-lock
#: key, so the bound is on the input rather than on the derived string.
IDENTIFIER_MAX_LENGTH: int = 200

#: The rule, stated once, quoted by every rejection so a client is told the same
#: thing whichever endpoint refused it.
IDENTIFIER_RULE: str = (
    "an identifier is stored in a Postgres text column: it may not be empty or "
    "whitespace-only, may not contain a NUL (U+0000), an unpaired surrogate or "
    "any control character (Unicode Cc), and may not exceed "
    f"{IDENTIFIER_MAX_LENGTH} characters"
)


class IdentifierError(ValueError):
    """A client-supplied identifier the store or the domain refuses.

    A ``ValueError`` so a pydantic ``AfterValidator`` turns it into an ordinary
    422 without any endpoint needing to catch it; the endpoints that validate
    outside pydantic catch it explicitly and render the same problem document.
    """

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field} is not a usable identifier: {reason}")
        self.field = field
        self.reason = reason


def _control_characters(value: str) -> str | None:
    """The first control character in ``value``, described, or ``None``.

    ``unicodedata.category`` rather than a hand-written range: ``Cc`` is exactly
    C0 (``U+0000``-``U+001F``), ``DEL`` and C1 (``U+0080``-``U+009F``), and a
    hand-written range is how ``U+007F`` came to be covered in one module and not
    in the other.
    """
    for index, char in enumerate(value):
        if unicodedata.category(char) == "Cc":
            return f"control character U+{ord(char):04X} at position {index}"
    return None


def identifier_fault(
    value: object,
    *,
    max_length: int | None = IDENTIFIER_MAX_LENGTH,
) -> str | None:
    """Why ``value`` is not a storable identifier, or ``None`` when it is.

    Never raises. The checks are ordered so the cheapest and most specific
    verdict wins, and so the check that can itself throw (``encode``) runs inside
    the guard rather than around it.
    """
    try:
        if not isinstance(value, str):
            return f"expected a string, got {type(value).__name__}"
        if not value:
            return "it is empty"
        if not value.strip():
            return "it is whitespace-only"
        if max_length is not None and len(value) > max_length:
            return f"it is {len(value)} characters; the limit is {max_length}"
        if "\x00" in value:
            return (
                f"it contains a NUL (U+0000) at position {value.index(chr(0))}; "
                "Postgres text cannot hold one"
            )
        control = _control_characters(value)
        if control is not None:
            return control
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return (
                "it contains an unpaired surrogate, so it is not encodable as "
                "UTF-8 and cannot be put on the wire at all"
            )
    except Exception as exc:  # pragma: no cover - the guard, not a branch
        # A value that breaks the checker is a rejection, never an escape: this
        # function's whole job is to be the thing that cannot 500.
        return f"it could not be checked ({type(exc).__name__})"
    return None


def validate_identifier(
    value: object,
    *,
    field: str = "run_id",
    max_length: int | None = IDENTIFIER_MAX_LENGTH,
) -> str:
    """Return ``value`` unchanged, or raise :class:`IdentifierError`.

    **Unchanged, never trimmed.** The identifier is an idempotency key: silently
    rewriting it would make two different requests claim the same load, which is
    the failure the key exists to prevent.
    """
    reason = identifier_fault(value, max_length=max_length)
    if reason is not None:
        raise IdentifierError(field, f"{reason}. {IDENTIFIER_RULE}")
    assert isinstance(value, str)
    return value
