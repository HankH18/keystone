"""PII redaction and time-based retention (SPEC R21, R26).

Two things live here, because they are two halves of one promise: *personal data
in Keystone's own stores is minimised at write time and removed on a schedule.*

``Redactor``
    A deterministic, structure-preserving redactor over the PII shapes this
    dataset actually produces (``docs/invariant-contract.md`` §1). Same input =>
    same token, so a redacted log stays correlatable; the token carries a
    truncated salted digest plus a *character-class shape*, which is what makes
    a redacted evidence packet still debuggable.

``run_purge`` / ``RETENTION``
    The retention sweep. One rule per table, each with an explicit window, an
    explicit disposition (purge / anonymize / retain) and the reason, mirrored
    one-for-one by ``docs/retention-policy.md``.

Design of the redactor
======================

**Default-deny, not deny-list.** A deny-list of field names leaks the day
somebody adds a field. So a leaf is emitted verbatim only if its key is on a
committed allow-list of operational, non-identifying keys (:data:`SAFE_KEYS`
plus :data:`SAFE_KEY_PATTERNS`); everything else becomes a token. **Default-deny
applies to all three leaf positions**, not just to a mapping value:

* *values* -- the allow-list above;
* *mapping keys* -- a key is a leaf too (an evidence packet keyed by observed
  value, a per-household roll-up), so a key survives only if it is on a
  committed vocabulary of field NAMES (see :meth:`Redactor._redact_key`);
* *sequence elements* -- an element has no key, so it inherits none, except
  under a :data:`PII_KEYS` key or a :data:`SEQUENCE_SAFE_KEYS` key (see
  :func:`_element_key`).

The dataset carries no ``phone`` field -- contract §1 defines the field set
as ``email``, ``guardian_email``, ``guardian2_email``, ``payer_email``,
``billing_owner_email``, ``first_name``/``last_name`` (and the payments
``metadata.student_*_name`` pair), ``dob``, ``student_number``,
``household_id`` and the two consent flags -- and no pattern is invented for
one. A ``phone`` key would be redacted anyway, by the default-deny rule, which
is exactly the point of making the default deny.

**The redactor sees the final string form of every value.** A value with no
JSON spelling -- an exception, a ``UUID``, a ``Decimal``, a ``datetime`` -- is
stringified *inside* :meth:`Redactor._leaf`, before any decision is taken about
it, and the renderer receives only what the redactor produced. The ordering is
load-bearing: while the renderer did the stringifying, the redactor inspected an
*object*, decided it was not a string and passed it through, and the renderer
then wrote ``str(obj)`` into the log after redaction had finished. That is not
theoretical -- ``log.error(..., error=ValueError(f"cannot land {record}"))`` is
what the ingest path does on a rejection, and it emitted the whole record.

**Precedence** for a leaf, in order:

1. ``None`` stays ``None``. Nullity is *not* redacted: whether an optional
   field was present is treated as schema, not as personal data.
2. An existing token is returned unchanged, so ``redact(redact(x)) ==
   redact(x)`` and a second retention sweep rewrites nothing.
3. :data:`PII_KEYS` -- the committed key -> kind map. Wins over everything.
4. *The value is reduced to its rendered string* if it is not already a JSON
   scalar. Every rule below judges that string, not the object.
5. :data:`TEXT_KEYS` -- free text, emitted *scrubbed* (see the limitation
   below), because an error message with the value removed is not debuggable.
6. Value-shape detection -- :data:`SHAPE_DETECTORS`, and **only** those four
   kinds: an email address, an ``S-000000`` student number, a bare
   ``YYYY-MM-DD`` date of birth and an ``HH-000000`` household id. Those four
   are caught wherever they appear, including under a key nobody predicted and
   under an allow-listed one. The kinds with no detector are named, with the
   reason, in :data:`SHAPELESS_KINDS`; ``name`` is the one that matters, and
   the paragraph below says what closes it instead.
7. :data:`SAFE_KEYS` / :data:`SAFE_KEY_PATTERNS` -- verbatim (strings still get
   the substring scrub, which costs nothing and closes the case where an
   allow-listed value quotes an address).
8. Anything else -- token, kind ``opaque``.

**A personal NAME has no shape, and nothing here pretends otherwise.** A name
is a word: no regular expression separates ``Fairbank`` from ``Applied``, or
``Amriyo Fairbank`` from ``Lower School``. This module used to claim that
"value-shape detection catches PII that arrives under a key nobody predicted",
which was true of ``email`` and ``student_number`` and of nothing else -- so a
name, a dob or a household id under an allow-listed key, in an event name or in
prose was emitted verbatim. Two of those three now have detectors, because a
date and an ``HH-`` id do have shapes. The name is closed a different way, in
four layers, none of which is a shape:

* the **key vocabulary** -- :data:`PII_KEYS` tokenises a value under a name key,
  and :meth:`Redactor._redact_key` tokenises a name used *as* a key;
* the **keyed forms** :meth:`Redactor.scrub_text` reads, which recognise a name
  by the field name written next to it in four spellings;
* the **sibling pass** (:meth:`Redactor.redact`), which removes a value the same
  event already carries under a PII key from that event's free text;
* a **narrower allow-list**. ``natural_key`` and ``source_key`` -- the two keys
  whose content is chosen by the *source* rather than by Keystone -- are no
  longer allow-listed. They were, and a duplicate-primary-key rejection put a
  surname, a household id and a date of birth on the terminal of the running
  service in default safe mode: the key was allow-listed, so it was only
  scrubbed, and ``scrub_text`` cannot see a name.

What is left over is stated in the honest limits below and in
``docs/retention-policy.md`` §4.1, and it is the true residue: a bare name in
genuinely unstructured prose, under no key, appearing nowhere else in the event.

**The digest canonicalises, the shape does not.** ``digest`` is taken over
``recon.normalize``'s canonical form, so ``"Brenmar-.Fairbank-Mead+school@Gmail.com"``
and ``brenmar-fairbank-mead@gmail.com`` share a digest -- one mailbox, one
pseudonym -- while ``shape`` is derived from the *raw* value, so the C4
email-variance conflict this project is graded on is still visible in the log.

**The shape contains no character of the value.** Letters become ``a``, digits
become ``9``, a small punctuation set survives, everything else becomes ``?``.
That is deliberate: a preview built from the first few characters of an address
is a leak, and "grep the emitted log for a raw dataset value" is a test this
module has to pass.

Honest limits
-------------

* A truncated salted SHA-256 of an email is a **pseudonym, not anonymisation**.
  The salt is a committed constant (determinism is graded here), so an attacker
  who can guess an address can confirm it. Low-cardinality values are worse:
  hashing a boolean consent flag reveals it outright, which is why
  :data:`KIND_FLAG` tokens carry no digest at all. ``dob`` and
  ``student_number`` are enumerable in the same way; their tokens correlate
  records, they do not protect the value against a guessing attacker.
  ``docs/retention-policy.md`` says so too, because a retention policy that
  called this "anonymised" would be wrong.
* Free-text scrubbing recognises **three things: literals the same event
  already knows are personal, adjacent keys, and shapes** -- in that order. The
  shape detectors catch an address, an ``S-000000``, a bare ``YYYY-MM-DD`` and
  an ``HH-000000``; the keyed forms catch a *name*, which has no shape of its
  own, in four spellings, with the quoting put back as it was found: JSON
  (``"first_name": "..."``), Python ``repr`` (``'first_name': 'Amriyo'`` --
  what every f-string, ``%s``, ``repr()`` and traceback of a dict actually
  produces, and therefore the common case), Postgres'
  ``Key (first_name)=(...)``, and a bare ``first_name=...``. **What it cannot
  catch is a bare name in genuinely unstructured prose** -- ``"Zedail could not
  be matched"`` names nobody's field, has no shape, and if the event carries it
  nowhere else there is nothing to match it against -- or a multi-word value in
  the bare ``key=value`` form, which stops at whitespace. Callers must
  therefore put source values in the structured ``detail`` mapping rather than
  interpolating them into a sentence; :func:`recon.logging.audit_detail` is the
  supported path.
* A ``dob`` is recognised by shape **only as a bare ``YYYY-MM-DD``** -- the one
  spelling :func:`recon.normalize.norm_dob` accepts, and therefore the only one
  a dob has once it is inside this system. A date carrying a time component is
  read as an operational timestamp and left alone. That is the deliberate
  trade: the alternative tokenises every ``created_at`` in every log line
  without removing one personal value.
* Surrogate keys (``student.id``, ``external_id``, ``external_ref``,
  ``canonical_id``, ``crm_id``, ``payment_id``) are emitted verbatim. Contract
  §1.3 pins ``student.id`` as ``uuid5`` of a generator sequence index, *never*
  derived from identity fields, so they carry no personal data themselves --
  and a log that can name no record at all cannot be debugged.
* **A ref embeds the source's natural key.** ``crm:contact:<natural_key>`` is
  still emitted verbatim under ``record_ref`` / ``source_ref`` / ``entity_refs``
  and their siblings, because the evidence model is unreadable without them.
  ``natural_key`` itself is tokenised, and the sibling pass removes it from a
  ref in the *same* event -- but an event carrying a ref and not the key it was
  built from shows whatever the source put in its primary key. Contract SS3
  pins this dataset's primary keys as surrogate, so it is a property of the
  input rather than a guarantee of this module.

Design of the retention sweep
=============================

**Which principal purges: the schema owner named by ``DATABASE_URL`` -- the
same ops/migration principal that runs alembic -- and no grant is widened to
make that convenient.** Migrations 0001-**0009** hand DELETE to exactly one
non-owner grantee: ``recon_writer`` on the five ``stg_*`` tables (a
re-materialisable cache). The count matters, because 0009 adds
``source_generations``, which this schedule anonymises: it grants
``recon_writer`` INSERT and UPDATE on that table and *no* DELETE, so the claim
still holds after it. No writer role holds DELETE on ``raw_records``,
``field_lineage``, ``audit_log``, ``invariant_results``, ``conflicts``,
``proposals``, ``proposal_events`` or ``source_generations``, which is correct
-- the detection path must not be able to erase its own evidence -- and it means
the sweep simply is not something ``recon_writer`` can run. :func:`assert_purge_principal` refuses
to run as any of the three writer roles rather than deleting zero rows quietly.

**Purge vs anonymize is decided by what the write boundary permits, not by
taste.** ``proposals.evidence`` cannot be rewritten in place *by anyone*: the
0005 immutability trigger raises ``KS005`` for the owner too (verified against
the live schema). So a proposal is purged whole, never anonymised. ``conflicts``
by contrast has no UPDATE trigger, and is referenced by ``proposals``, so its
``observed_values`` are anonymised first and the row is purged later once no
proposal references it.

**Parents wait for their children.** ``DELETE FROM conflicts`` while a proposal
references it raises ``23503``; every purge rule that has dependents therefore
carries a ``NOT EXISTS`` guard, and a parent whose child is still inside its own
window is retained until the child ages out.

**One consequence worth naming:** ``entity_links`` may only be UPDATEd while its
landing record exists (trigger ``entity_links_require_raw_record``, ``KS009``).
Once ``raw_records`` is purged, those rows are effectively immutable, and the
provenance floor is verifiable only inside the landing window.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from recon.normalize import QUOTE_CHARS, norm_dob, norm_email, norm_name

__all__ = [
    "KIND_DOB",
    "KIND_EMAIL",
    "KIND_FLAG",
    "KIND_HOUSEHOLD",
    "KIND_NAME",
    "KIND_OPAQUE",
    "KIND_STUDENT_NUMBER",
    "KIND_TEXT",
    "PII_KEYS",
    "PURGE_ACTION",
    "PURGE_ACTOR",
    "RETENTION",
    "SAFE_KEYS",
    "SAFE_KEY_PATTERNS",
    "SEQUENCE_SAFE_KEYS",
    "SHAPELESS_KINDS",
    "SHAPE_DETECTORS",
    "STRUCTURAL_KEYS",
    "TEXT_KEYS",
    "UNRENDERABLE",
    "PurgeNotPermitted",
    "PurgeResult",
    "Redactor",
    "RetentionRule",
    "assert_purge_principal",
    "canonical_json",
    "default_redactor",
    "is_token",
    "known_values",
    "main",
    "redact",
    "render_sweep",
    "render_target",
    "retention_rule",
    "run_purge",
    "scrub_text",
]

# ---------------------------------------------------------------------------
# kinds
# ---------------------------------------------------------------------------

KIND_EMAIL: Final = "email"
KIND_NAME: Final = "name"
KIND_DOB: Final = "dob"
KIND_STUDENT_NUMBER: Final = "student_number"
KIND_HOUSEHOLD: Final = "household"
#: Consent/opt-out booleans. A two-valued domain cannot be hashed usefully, so
#: these tokens carry no digest -- a digest would *be* the value.
KIND_FLAG: Final = "flag"
#: Free text emitted after substring scrubbing.
KIND_TEXT: Final = "text"
#: Default-deny: a leaf under a key that is neither known-PII nor allow-listed.
KIND_OPAQUE: Final = "opaque"

# ---------------------------------------------------------------------------
# the committed key vocabularies
# ---------------------------------------------------------------------------

#: Key -> kind. Every entry is a real column or JSON key in this project:
#: ``docs/invariant-contract.md`` §1 for the source records, plus the ``stg_*``
#: columns the ingest path materialises (the ``*_norm`` twins are *still* the
#: personal value, normalised).
PII_KEYS: Final[Mapping[str, str]] = {
    # --- emails (contract §1.1, §1.3, §1.4, §1.5) ---------------------------
    "email": KIND_EMAIL,
    "email_norm": KIND_EMAIL,
    "guardian_email": KIND_EMAIL,
    "guardian2_email": KIND_EMAIL,
    "guardian2_email_norm": KIND_EMAIL,
    "payer_email": KIND_EMAIL,
    "payer_email_norm": KIND_EMAIL,
    "billing_owner_email": KIND_EMAIL,
    "billing_owner_email_norm": KIND_EMAIL,
    # the SS5.4 evidence-packet spellings (`recon.reference.OBSERVED_VALUE_KEYS`)
    "contact_email_norm": KIND_EMAIL,
    "student_guardian_email_norms": KIND_EMAIL,
    # --- names --------------------------------------------------------------
    "first_name": KIND_NAME,
    "last_name": KIND_NAME,
    "first_norm": KIND_NAME,
    "last_norm": KIND_NAME,
    "payer_name": KIND_NAME,
    "payer_first_norm": KIND_NAME,
    "payer_last_norm": KIND_NAME,
    # payments metadata carries the two name parts as SEPARATE fields (§1.5)
    "student_first_name": KIND_NAME,
    "student_last_name": KIND_NAME,
    "student_name_first_norm": KIND_NAME,
    "student_name_last_norm": KIND_NAME,
    # `crm.deal.name` is "<household surname> <word> <year>" -- a family name
    "name": KIND_NAME,
    # --- date of birth ------------------------------------------------------
    "dob": KIND_DOB,
    "dob_norm": KIND_DOB,
    "dob_norm_a": KIND_DOB,
    "dob_norm_b": KIND_DOB,
    # --- government / household identifiers ---------------------------------
    "student_number": KIND_STUDENT_NUMBER,
    "household_id": KIND_HOUSEHOLD,
    "household_key": KIND_HOUSEHOLD,
    # --- source-controlled primary keys -------------------------------------
    # NOT allow-listed, and that is the fix for a real leak: `natural_key` is
    # whatever the SOURCE calls its primary key, so its content is chosen
    # upstream and cannot be safe by construction. A duplicate-primary-key
    # rejection put `Fairbank-Mead|HH-004821|2015-12-16` on the terminal of the
    # running service in default safe mode, because the key was allow-listed and
    # a surname has no shape for `scrub_text` to find. The kind is `opaque`
    # rather than a personal kind because the key may be a surrogate id, a
    # composite, or a person's name -- the redactor cannot tell, so it does not
    # guess. The token is still deterministic, so two rows that share a natural
    # key still visibly share one.
    "natural_key": KIND_OPAQUE,
    "source_key": KIND_OPAQUE,
    # --- consent flags (contract §6 SENSITIVE_FIELDS) -----------------------
    "marketing_consent": KIND_FLAG,
    "communication_opt_out": KIND_FLAG,
}

#: Free-text keys: emitted scrubbed rather than tokenised, because an error
#: message with its content removed cannot be debugged. Shape-based scrubbing
#: cannot see a bare personal name -- see the module docstring.
TEXT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "body",
        "description",
        "detail",
        "error",
        "error_detail",
        "escalation_reason",
        "event",
        "exception",
        "message",
        "msg",
        "note",
        "prompt",
        "rationale",
        "reason",
        "response",
        "text",
        "traceback",
    }
)

#: Operational and non-identifying keys, emitted verbatim. Everything here is a
#: real column of the Keystone schema or a structlog housekeeping key. Note
#: what is deliberately present: surrogate keys, which name a record without
#: describing a person, and ``decided_by``/``actor``, which name the *staff*
#: member accountable for a decision -- the accountability record R15 exists
#: for. ``docs/retention-policy.md`` covers their window.
SAFE_KEYS: Final[frozenset[str]] = frozenset(
    {
        # surrogate / source primary and foreign keys
        "associated_contact_ids",
        "canonical_id",
        "conflict_id",
        "crm_deal_id",
        "crm_id",
        "deal_id",
        "enrollment_id",
        "deal_person_refs",
        "enrollment_ref",
        "entity_refs",
        "ext_resolved_ref",
        "external_id",
        "external_ref",
        "id",
        "namedob_resolved_ref",
        "paid_payment_refs",
        "incident_id",
        "key_hash",
        "payment_id",
        "proposal_id",
        "raw_record_id",
        "record_ref",
        "resolved_ref",
        "source_ref",
        "student_id",
        "target_canonical_id",
        # attributes compared by the rule set, none of them identifying
        "amount",
        "amount_cents",
        "amount_microusd",
        "amount_raw",
        "currency",
        "enrollment_year",
        "grade",
        "grade_norm",
        "grade_ord",
        "lifecycle_norm",
        "lifecycle_stage",
        "pipeline",
        "pipeline_norm",
        "program",
        "program_norm",
        "stage",
        "stage_funnel",
        "stage_funnel_ord",
        "states",
        "status_funnel",
        "stage_norm",
        "state",
        "state_norm",
        "status",
        "status_compare",
        "status_norm",
        "type",
        "type_norm",
        # operational / pipeline bookkeeping
        "accepted",
        "action",
        "actor",
        "after_digest",
        "alert",
        "attempt",
        "attempts",
        "auto",
        "before_digest",
        # `RollbackResult.byte_identical`: a bool saying the reversal restored the
        # digest the apply captured. A claim about two hashes, never a value --
        # the digests themselves are already here as `before_digest`/`after_digest`.
        "byte_identical",
        # Two integer counters from the request-size bound in `recon/ingest.py`:
        # `bytes_read` is how many bytes of a body had been consumed when the limit
        # was hit (it sits beside `limit_bytes`/`declared_bytes`, which the `_bytes`
        # suffix already covers), and `value_length` is `len()` of the *environment
        # variable* `MAX_BODY_BYTES` when it will not parse. Neither is derived from
        # a record. Added deliberately rather than by widening `SAFE_KEY_SUFFIXES`
        # with `_read`/`_length`, which would allow-list any future key ending that
        # way sight unseen.
        "bytes_read",
        "complete",
        "confidence",
        "conflict_type",
        "count",
        "created_run",
        "current_status",
        "deal_present_gen3",
        "decision",
        "disposition",
        "dropped_source",
        "decided_by",
        "disagreeing_fields",
        "distance",
        "embedding_dim",
        "embedding_model",
        "entity_type",
        "entry",  # a manifest's `<source>.<entity>` key, not a datum
        "env_var",
        "env_vars",
        "event_id",
        "expected",
        "failed",
        "field",
        "form",
        "found",
        "field_path",
        "fingerprint",
        "first_seen_run",
        "generation",
        "idempotency_key",
        "job",
        "key_class",
        "kind",
        "label",
        "last_seen_run",
        "level",
        "line",
        "lineage",
        "link_method",
        "load_id",
        "loaded",
        "logger",
        "metadata_name_pair_present",
        "method",
        "mode",
        "model",
        "oscillating",
        "outcome",
        "param",
        "path",
        "persist",
        "presented",
        "price_table_version",
        "profile",
        "reached_provider",
        "reclaimed",
        "records_ok",
        "records_read",
        "records_rejected",
        "rejected",
        "rejections",
        "restored_digest",
        "returned",
        "rows",
        "rule",
        "rule_id",
        "rule_version",
        "run_id",
        "scope",
        "scope_provisioned",
        "scopes",
        "seed",
        "sensitive",
        "sink",
        "source",
        "source_id",
        "sources",
        "sqlstate",
        "subject",
        "swept",
        "table",
        "tables",
        "timestamp",
        "title",
        "tokens_in",
        "tokens_out",
        "total",
        "unchecked_fields",
        "upstream_status",
        "usage",
        # see `bytes_read` above: `len()` of an unparseable MAX_BODY_BYTES setting
        "value_length",
        "verdict",
        "version",
        "wanted_status",
        "window_days",
    }
)

#: Suffixes and prefixes that make an unlisted key safe. Kept narrow and
#: shape-driven: timestamps, durations, counters and hashes. ``_id`` is
#: deliberately absent -- ``household_id`` is personal data, and a rule that
#: made every ``*_id`` safe would be one added column away from leaking.
SAFE_KEY_SUFFIXES: Final[tuple[str, ...]] = (
    "_at",
    "_bytes",
    "_code",
    "_count",
    "_hash",
    "_microusd",
    "_ms",
    "_ord",
    "_seconds",
    "_sha256",
    "_tokens",
    "_ts",
)
SAFE_KEY_PREFIXES: Final[tuple[str, ...]] = ("count_", "has_", "is_", "n_", "num_", "total_")
SAFE_KEY_PATTERNS: Final[tuple[tuple[str, ...], tuple[str, ...]]] = (
    SAFE_KEY_PREFIXES,
    SAFE_KEY_SUFFIXES,
)

#: Keys allow-listed **in key position only, never for a value**. A mapping key
#: is itself a leaf that can carry personal data -- an evidence packet keyed by
#: observed value, a per-household roll-up -- so keys are default-deny too (see
#: the module docstring). These are the words that name a *source*, an *entity
#: type* or a *log structure*; they are field names, not data. They are
#: deliberately NOT in :data:`SAFE_KEYS`, because ``{"student": "Amriyo
#: Fairbank"}`` must still redact its value.
STRUCTURAL_KEYS: Final[frozenset[str]] = frozenset(
    {
        # source ids and entity types (`recon.reference.SOURCE_IDS`, `_REF_SOURCE`)
        "appdb",
        "contact",
        "crm",
        "deal",
        "enrollment",
        "payment",
        "payments",
        "student",
        # Keystone's own table / row names, as structure words
        "batch",
        "batches",
        "candidates",
        "conflict",
        "conflicts",
        "entities",
        "entity",
        "incident",
        "incidents",
        "proposal",
        "proposals",
        "results",
        "rules",
        "run",
        "runs",
        # log / evidence structure
        "after",
        "args",
        "before",
        "by_source",
        "by_type",
        "context",
        "counts",
        "data",
        "deep",
        "evidence",
        "extra",
        "fields",
        "items",
        "kwargs",
        "left",
        "meta",
        "metadata",
        "nested",
        "new",
        "observed",
        "observed_values",
        "old",
        "params",
        "payload",
        "record",
        "records",
        "request",
        "result",
        "right",
        "set",
        "summary",
        "supporting",
        "totals",
        "values",
    }
)

#: Keys whose *sequence elements* keep the key. A list element has no key of its
#: own, so under default-deny it inherits none -- otherwise an element under a
#: free-text or allow-listed key is judged as free text, and a name, a dob or a
#: household id has no shape to catch. These are the keys whose elements come
#: from a committed non-personal vocabulary (field names, refs, table names).
#: Every entry must also be in :data:`SAFE_KEYS`; the assertion below pins that.
SEQUENCE_SAFE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "associated_contact_ids",
        "deal_person_refs",
        "disagreeing_fields",
        "entity_refs",
        "env_vars",
        "failed",  # `recon.apply`: the names of R24's failed gate checks
        "paid_payment_refs",
        "rejections",
        "scopes",
        "sources",
        "states",
        "tables",
        "unchecked_fields",
    }
)

assert SEQUENCE_SAFE_KEYS <= SAFE_KEYS, sorted(SEQUENCE_SAFE_KEYS - SAFE_KEYS)
assert not (STRUCTURAL_KEYS & SAFE_KEYS), sorted(STRUCTURAL_KEYS & SAFE_KEYS)

# ---------------------------------------------------------------------------
# token shape
# ---------------------------------------------------------------------------

#: A pseudonymisation salt, NOT a secret. It is committed so that two processes
#: (and two test runs) produce the same token for the same value -- determinism
#: is graded in this project, and a per-deployment random salt would make the
#: audit log uncorrelatable across restarts. Because it is committed, a token is
#: a pseudonym and nothing stronger; see the module docstring.
DEFAULT_SALT: Final = "keystone/pii/v1"

_DIGEST_CHARS: Final = 12
_SHAPE_LIMIT: Final = 24
_SHAPE_TRUNCATED: Final = "~"
_MAX_DEPTH: Final = 12

#: Punctuation that survives into a shape. Chosen so an email, a DOB and a
#: source ref stay recognisable, and so a token can never contain ``]``.
_SHAPE_KEEP: Final = "@.-_+:/ "

_EMAIL_PATTERN: Final = r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}"
_STUDENT_NUMBER_PATTERN: Final = r"S-\d{6}"

#: A bare ISO **calendar date** -- the one spelling `recon.normalize.norm_dob`
#: accepts and therefore the only spelling a `dob` has once it is in this system
#: (``_DOB_SHAPE`` there is the same ``^\d{4}-\d{2}-\d{2}$``).
#:
#: The lookarounds are what keep it from eating an operational timestamp:
#: ``2026-02-01T00:00:00Z`` is a `created_at`, not a birthday, and a rule that
#: tokenised every `*_at` column would make the log unreadable without removing
#: any personal data. So a date immediately touching a digit, a ``T``, a ``:``
#: or another ``-`` is left alone, and what remains is a date that stands by
#: itself. The residual gap is stated in the honest limits: a dob deliberately
#: written with a time component is not recognised by shape.
_DOB_PATTERN: Final = r"(?<![\dT:\-])\d{4}-\d{2}-\d{2}(?![\dT:\-])"

#: A household identifier. ``docs/invariant-contract.md`` §1.2 pins the
#: generator's spelling as ``HH-`` plus a six-digit sequence
#: (``recon/seed/build.py``: ``f"HH-{household.index + 1:06d}"``), and
#: ``household_key`` carries the same value.
_HOUSEHOLD_PATTERN: Final = r"(?<![0-9A-Za-z])HH-\d{6}(?![0-9])"

_TOKEN_PATTERN: Final = r"\[pii:[a-z_]+(?::[^\]]*)?\]"

#: Literals a keyed form leaves alone / treats as a flag, in both spellings.
_NULLISH: Final[frozenset[str]] = frozenset({"", "null", "None"})
_BOOLISH: Final[frozenset[str]] = frozenset({"true", "false", "True", "False"})

_EMAIL_RE: Final = re.compile(_EMAIL_PATTERN)
_STUDENT_NUMBER_RE: Final = re.compile(_STUDENT_NUMBER_PATTERN)
_DOB_RE: Final = re.compile(_DOB_PATTERN)
_HOUSEHOLD_RE: Final = re.compile(_HOUSEHOLD_PATTERN)
_TOKEN_RE: Final = re.compile(_TOKEN_PATTERN)

#: ``kind -> compiled shape``, in match order. **This tuple is the whole of
#: value-shape detection**, and the kinds NOT in it are the point: ``name`` has
#: no entry and cannot have one (see the module docstring), and ``flag`` has
#: none because ``true``/``false`` is not a shape. Everything that walks the
#: kinds -- the module docstring, the policy document and
#: ``tests/privacy/test_redaction.py`` -- reads this tuple rather than a prose
#: list, so the claim and the code cannot drift.
SHAPE_DETECTORS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (KIND_EMAIL, _EMAIL_RE),
    (KIND_STUDENT_NUMBER, _STUDENT_NUMBER_RE),
    (KIND_DOB, _DOB_RE),
    (KIND_HOUSEHOLD, _HOUSEHOLD_RE),
)

#: Kinds with no shape detector, and why. ``name`` is the one that matters: a
#: personal name is a word, and no regular expression separates ``Fairbank``
#: from ``Applied`` or ``Lower School``. It is closed by the *key* vocabulary
#: (:data:`PII_KEYS`), by the keyed forms :meth:`Redactor.scrub_text` reads, by
#: the sibling scrub (:meth:`Redactor.redact`), and by keeping source-controlled
#: keys off :data:`SAFE_KEYS` -- never by shape.
SHAPELESS_KINDS: Final[Mapping[str, str]] = {
    KIND_NAME: (
        "a personal name has no shape: it is a word, and no pattern distinguishes "
        "it from an enum label, a pipeline name or an English sentence"
    ),
    KIND_FLAG: "a two-valued domain is not a shape; a bare `true` is no evidence of a consent flag",
    KIND_TEXT: "free text is the position, not the value",
    KIND_OPAQUE: "the default-deny outcome, which is what a value with no recognised shape gets",
}

#: Alternation over the committed PII key vocabulary. Every entry is a plain
#: identifier, so nothing here is a regex metacharacter.
_PII_KEY_ALT: Final = "|".join(sorted(PII_KEYS, key=len, reverse=True))

#: A quoted string literal in EITHER spelling. Python's ``repr`` -- what every
#: f-string, ``%s`` and traceback of a dict produces -- writes ``'first_name':
#: 'Amriyo'``, so a pattern that only knew JSON's double quotes matched nothing
#: in the single most common way a record reaches a log line.
_QUOTED_STRING: Final = r'"(?:[^"\\]|\\.)*"' r"|'(?:[^'\\]|\\.)*'"

#: ``"guardian_email": "..."`` / ``'guardian_email': '...'`` inside a serialised
#: or repr'd record. A name cannot be recognised by its own shape, but it CAN be
#: recognised by the key next to it, and that is how a personal name reaches a
#: log in practice: a rejected row rendered into an error, a repr of a payload, a
#: Postgres detail line. ``null``/``None`` are deliberately excluded -- a null is
#: not personal data. The closing quote is a backreference, so ``'x": y'`` is not
#: mistaken for a pair.
_JSON_PAIR_PATTERN: Final = (
    rf'(?P<jq>["\'])(?P<jkey>{_PII_KEY_ALT})(?P=jq)(?P<jsep>\s*:\s*)'
    rf"(?P<jval>{_QUOTED_STRING}|true|false|True|False|-?\d+(?:\.\d+)?)"
)

#: ``Key (email)=(someone@example.test)`` -- the Postgres unique-violation form.
_PG_PAIR_PATTERN: Final = rf"\((?P<pkey>{_PII_KEY_ALT})\)=\((?P<pval>[^)]*)\)"

#: ``first_name=Zedail`` / ``first_name='Zedail'`` -- the bare form a repr, a
#: dataclass repr or an f-string produces. ``\b`` keeps ``email`` from matching
#: inside ``guardian_email`` (``_`` is a word character, so there is no boundary
#: there). The quoted alternative comes FIRST and is what keeps scrubbing
#: idempotent for this form: a token is re-rendered inside the quotes it was
#: found in, and the unquoted character class stops at ``]``, so a second pass
#: would otherwise chop ``'[pii:name:...]'`` in half and tokenise the pieces.
#:
#: ``&`` and the quote characters terminate the value too, and that matters now
#: that uvicorn's access line goes through here: a URL query string is
#: ``key=value&key=value``, so without ``&`` the first parameter's rule
#: swallowed the whole rest of the request line into one token -- safe, but it
#: destroyed the line. Ending the value at ``&`` narrows the token to one
#: parameter and lets each following parameter be judged by its own rule, which
#: removes strictly more and keeps strictly more.
_BARE_PAIR_PATTERN: Final = (
    rf"\b(?P<bkey>{_PII_KEY_ALT})(?P<bsep>\s*=\s*)"
    rf'(?P<bval>{_QUOTED_STRING}|(?!\[pii:)[^\s,;)\]}}&"\']+)'
)

#: Token alternative comes FIRST so an already-redacted string is left alone and
#: scrubbing stays idempotent (a token's shape can itself look like an address).
#: The keyed forms come before the bare shape detectors so a value is tokenised
#: under the kind its key implies rather than as ``opaque``.
_SCRUB_RE: Final = re.compile(
    rf"(?P<token>{_TOKEN_PATTERN})"
    rf"|(?P<json_pair>{_JSON_PAIR_PATTERN})"
    rf"|(?P<pg_pair>{_PG_PAIR_PATTERN})"
    rf"|(?P<bare_pair>{_BARE_PAIR_PATTERN})"
    rf"|(?P<{KIND_EMAIL}>{_EMAIL_PATTERN})"
    rf"|(?P<{KIND_STUDENT_NUMBER}>{_STUDENT_NUMBER_PATTERN})"
    rf"|(?P<{KIND_DOB}>{_DOB_PATTERN})"
    rf"|(?P<{KIND_HOUSEHOLD}>{_HOUSEHOLD_PATTERN})"
)


#: ``(key group, key-quote group, separator group, value group, template)`` for
#: each keyed form :data:`_SCRUB_RE` recognises. The template puts the key and
#: its original quoting back exactly as they were written, so the scrubbed text
#: still says WHICH field was removed and still reads as JSON or as a repr.
_KEYED_FORMS: Final[tuple[tuple[str, str | None, str | None, str, str], ...]] = (
    ("jkey", "jq", "jsep", "jval", "{q}{key}{q}{sep}{body}"),
    ("pkey", None, None, "pval", "({key})=({body})"),
    ("bkey", None, "bsep", "bval", "{key}{sep}{body}"),
)


#: ``(literal value, kind)`` pairs a redaction pass already knows are personal.
KnownValues = tuple[tuple[str, str], ...]

#: Below this length a literal is too short to be worth removing and too likely
#: to appear inside an unrelated word.
_KNOWN_MIN_CHARS: Final = 4

#: Cap on how many sibling literals one pass carries. A pathological structure
#: must not turn one log line into thousands of regex substitutions; the cap is
#: far above any real event (the widest evidence packet this project builds
#: carries well under a hundred distinct personal values).
_KNOWN_LIMIT: Final = 128


def _boundary_re(value: str) -> re.Pattern[str]:
    """``value`` as a literal, matched only at non-alphanumeric boundaries."""
    return re.compile(rf"(?<![0-9A-Za-z]){re.escape(value)}(?![0-9A-Za-z])")


def known_values(obj: Any) -> KnownValues:
    """Literal personal values ``obj`` carries under a :data:`PII_KEYS` key.

    Longest first, then alphabetical -- a total order, because the replacement
    they drive has to be deterministic and a composite has to be replaced before
    any of its own parts. Booleans, nullish spellings and existing tokens are
    excluded: they are not values worth removing, and a flag is two-valued.
    """
    found: dict[str, str] = {}

    def visit(node: Any, key: str | None, depth: int, seen: frozenset[int]) -> None:
        if len(found) >= _KNOWN_LIMIT or depth >= _MAX_DEPTH:
            return
        if isinstance(node, Mapping):
            if id(node) in seen:
                return
            for name, item in node.items():
                visit(item, str(name), depth + 1, seen | {id(node)})
            return
        if isinstance(node, list | tuple | set | frozenset):
            if id(node) in seen:
                return
            for item in node:
                visit(item, key, depth + 1, seen | {id(node)})
            return
        if key is None or node is None or isinstance(node, bool):
            return
        kind = _pii_kind_for_key(key)
        if kind is None or kind == KIND_FLAG:
            return
        text = _stringify(node)
        if len(text) < _KNOWN_MIN_CHARS or text in _NULLISH or is_token(text):
            return
        found.setdefault(text, kind)

    visit(obj, None, 0, frozenset())
    return tuple(sorted(found.items(), key=lambda pair: (-len(pair[0]), pair[0])))


def _unquote(raw: str) -> tuple[str, str]:
    """``("Zedail", "'")`` for a quoted literal, ``(raw, "")`` otherwise.

    The quote character is returned rather than a flag so the replacement can be
    written back in the spelling it was found in -- ``repr`` uses ``'``, JSON
    uses ``"``, and rewriting one as the other would corrupt the surrounding
    text.
    """
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        if raw[0] == '"':
            try:
                return json.loads(raw), '"'
            except ValueError:
                return raw[1:-1], '"'
        return raw[1:-1], "'"
    return raw, ""


def is_token(value: object) -> bool:
    """True when ``value`` is already a redaction token."""
    return isinstance(value, str) and _TOKEN_RE.fullmatch(value) is not None


def _shape(raw: str) -> str:
    """Character-class signature of ``raw`` carrying none of its characters."""
    out: list[str] = []
    for char in raw[: _SHAPE_LIMIT + 1]:
        if char in _SHAPE_KEEP:
            out.append(char)
        elif char.isascii() and char.isalpha():
            out.append("a")
        elif char.isdigit():
            out.append("9")
        else:
            out.append("?")
    if len(out) > _SHAPE_LIMIT:
        return "".join(out[:_SHAPE_LIMIT]) + _SHAPE_TRUNCATED
    return "".join(out)


def _canonical(value: str, kind: str) -> str:
    """The form the digest is taken over: one mailbox / one person, one token."""
    if kind == KIND_EMAIL:
        return norm_email(value) or value.strip().casefold()
    if kind == KIND_NAME:
        return norm_name(value) or value.strip().casefold()
    if kind == KIND_DOB:
        return norm_dob(value) or value.strip()
    if kind in (KIND_STUDENT_NUMBER, KIND_HOUSEHOLD):
        return value.strip()
    # Opaque and free-text values are NOT canonicalised: two values that differ
    # only in whitespace are two values, and collapsing them would make two
    # distinct records share one pseudonym.
    return value


@dataclass(frozen=True)
class Redactor:
    """Deterministic PII redactor. Same input => same token."""

    salt: str = DEFAULT_SALT
    digest_chars: int = _DIGEST_CHARS

    # -- tokens -----------------------------------------------------------
    def digest(self, value: object, kind: str) -> str:
        """Truncated salted SHA-256 over the canonical form of ``value``."""
        canonical = _canonical(_stringify(value), kind)
        material = f"{self.salt}\x1f{kind}\x1f{canonical}".encode()
        return hashlib.sha256(material).hexdigest()[: self.digest_chars]

    def token(self, value: object, kind: str) -> str:
        """The replacement for one PII value.

        ``[pii:<kind>:<digest>:<shape>]`` -- or ``[pii:flag:redacted]`` for a
        two-valued domain, where a digest would be the value itself.
        """
        if kind == KIND_FLAG:
            return f"[pii:{KIND_FLAG}:redacted]"
        raw = _stringify(value)
        return f"[pii:{kind}:{self.digest(value, kind)}:{_shape(raw)}]"

    # -- free text --------------------------------------------------------
    def scrub_text(self, text: str, known: KnownValues = ()) -> str:
        """Remove embedded personal data from prose, leaving the prose intact.

        Three layers, in order:

        1. ``known`` -- literal values this same event already knows to be
           personal, because they appear under a :data:`PII_KEYS` key elsewhere
           in the structure being redacted. This is the layer that catches a
           **name**, which has no shape and may carry no adjacent key: a
           duplicate-primary-key rejection writes
           ``natural_key='Fairbank-Mead|HH-004821|2015-12-16'`` in one field and
           interpolates the same string bare into ``detail``, and only the first
           of those is recognisable on its own. See :meth:`redact`.
        2. three keyed forms (``"key": "value"``, Postgres' ``(key)=(value)``
           and a bare ``key=value``), which catch a name by the key beside it;
        3. the :data:`SHAPE_DETECTORS` -- address, student number, bare ISO
           date, household id.

        Idempotent: an existing token is matched first and returned unchanged.
        """
        return _SCRUB_RE.sub(self._scrub_match, self._replace_known(text, known))

    def _replace_known(self, text: str, known: KnownValues) -> str:
        """Replace literal ``known`` values with their tokens, longest first.

        Longest first so a composite (``Fairbank-Mead|HH-004821|2015-12-16``) is
        replaced whole rather than being chopped up by one of its own parts, and
        with alphanumeric boundaries so a short value cannot corrupt an
        unrelated word.
        """
        for value, kind in known:
            text = _boundary_re(value).sub(self.token(value, kind), text)
        return text

    def _scrub_match(self, match: re.Match[str]) -> str:
        if match.group("token") is not None:
            return match.group(0)
        for key_group, quote_group, sep_group, value_group, template in _KEYED_FORMS:
            key = match.group(key_group)
            if key is None:
                continue
            raw = match.group(value_group)
            inner, quote = _unquote(raw)
            if inner in _NULLISH or is_token(inner):
                return match.group(0)
            kind = KIND_FLAG if inner in _BOOLISH else PII_KEYS.get(key, KIND_OPAQUE)
            body = self.token(inner, kind)
            return template.format(
                key=key,
                q=match.group(quote_group) if quote_group else "",
                sep=match.group(sep_group) if sep_group else "",
                body=f"{quote}{body}{quote}",
            )
        for kind, _ in SHAPE_DETECTORS:
            if match.group(kind) is not None:
                return self.token(match.group(0), kind)
        raise AssertionError(f"_SCRUB_RE matched no known group: {match.group(0)!r}")

    # -- structures -------------------------------------------------------
    def redact(self, obj: Any, *, key: str | None = None) -> Any:
        """Redact ``obj`` recursively.

        Nested mappings and sequences are walked to the leaves, which is where
        the PII in a jsonb evidence packet, an action payload or an error detail
        actually lives.

        **One pre-pass first.** Before anything is walked, the values sitting
        under a :data:`PII_KEYS` key anywhere in ``obj`` are collected
        (:func:`known_values`) and carried down, so that the *same* literal
        appearing in a free-text or allow-listed sibling is removed too. That
        closes the position a shape detector cannot reach and a key detector
        does not see: a value interpolated bare into a sentence by the very
        event that also carries it as a field. It is not a substitute for the
        honest limit -- a name that appears ONLY in prose, under no key, in no
        other field, is still not findable.
        """
        return self._walk(obj, key, depth=0, seen=frozenset(), known=known_values(obj))

    def _walk(
        self,
        obj: Any,
        key: str | None,
        *,
        depth: int,
        seen: frozenset[int],
        known: KnownValues,
    ) -> Any:
        if obj is None:
            return None
        if isinstance(obj, Mapping | list | tuple | set | frozenset):
            if depth >= _MAX_DEPTH:
                return f"[pii:{KIND_OPAQUE}:depth-limit]"
            if id(obj) in seen:
                return f"[pii:{KIND_OPAQUE}:cycle]"
            nested = seen | {id(obj)}
            if isinstance(obj, Mapping):
                return {
                    self._redact_key(str(name)): self._walk(
                        item, str(name), depth=depth + 1, seen=nested, known=known
                    )
                    for name, item in obj.items()
                }
            element_key = _element_key(key)
            walked = [
                self._walk(item, element_key, depth=depth + 1, seen=nested, known=known)
                for item in obj
            ]
            if isinstance(obj, set | frozenset):
                # Set iteration order is never allowed to reach an output here.
                return sorted(walked, key=repr)
            return walked
        return self._leaf(obj, key, known)

    def _redact_key(self, name: str) -> str:
        """Redact a mapping key, default-deny like every other leaf.

        A key is a leaf too. Evidence packets are keyed by field path *and* by
        observed value, and a per-household roll-up is keyed by the household --
        so a rule that tokenised a key only when it had a recognisable SHAPE
        emitted every personal name, dob and household id used as a key
        verbatim, because none of those has a shape. A key is therefore kept
        only when it is on a committed vocabulary: :data:`PII_KEYS`,
        :data:`TEXT_KEYS`, :data:`SAFE_KEYS` / :data:`SAFE_KEY_PATTERNS` (as a
        field *name*, which is not personal data) or :data:`STRUCTURAL_KEYS`.
        Anything else is tokenised.

        The failure mode of a key the vocabulary has not met is a token where a
        readable name should be -- loud, safe, and one line in :data:`SAFE_KEYS`
        to fix. The failure mode of the old rule was a leaked name.
        """
        if is_token(name):
            return name
        detected = _detect_kind(name)
        if detected is not None:
            return self.token(name, detected)
        if _is_known_key(name):
            return name
        return self.token(name, KIND_OPAQUE)

    def _leaf(self, value: Any, key: str | None, known: KnownValues = ()) -> Any:
        """Redact one leaf. **Nothing reaches the renderer without passing here.**

        The value is reduced to the exact string the log will show *before* any
        decision is taken about it. That ordering is the whole point: while the
        renderer did the stringifying, an object was inspected by the redactor
        and a string was handed to the log -- so ``error=ValueError(f"cannot
        land {record}")`` wrote the whole record, names and addresses included,
        after the only thing that could have cleaned it had already run.
        """
        if is_token(value):
            return value
        if key is not None:
            kind = _pii_kind_for_key(key)
            if kind is not None:
                return self.token(value, KIND_FLAG if isinstance(value, bool) else kind)
        # `bool` is checked before `int` because it is a subclass of it.
        rendered: Any = value if isinstance(value, str | bool | int | float) else _stringify(value)
        if key is not None and _is_text_key(key):
            return self.scrub_text(rendered, known) if isinstance(rendered, str) else rendered
        if isinstance(rendered, str):
            detected = _detect_kind(rendered)
            if detected is not None:
                return self.token(rendered, detected)
        if key is not None and _is_safe_key(key):
            # Allow-listed values are scrubbed too -- an `*_ref` embeds the
            # source natural key, and the sibling pass is what removes it.
            return self.scrub_text(rendered, known) if isinstance(rendered, str) else rendered
        if isinstance(rendered, bool):
            return self.token(rendered, KIND_FLAG)
        return self.token(rendered, KIND_OPAQUE)


#: What a value whose ``__str__`` raises is rendered as.
UNRENDERABLE: Final = "<unrenderable>"


def _stringify(value: object) -> str:
    """The exact string a value would show as -- and never an exception.

    ``str(value)`` is arbitrary user code. A ``__str__`` that raises used to
    take the redactor down *inside the logging call*, and Python then printed
    the escaping exception's own message -- which for a model, a row wrapper or
    an ORM object routinely quotes the record -- straight to stderr, outside the
    chain. A redactor that can be made to crash by the value it is redacting is
    a redactor that can be made to leak, so failure is contained here: the value
    becomes :data:`UNRENDERABLE` plus its type, which names nobody.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat()
    try:
        return str(value)
    except Exception:  # `__str__` is arbitrary code; it must not escape the redactor
        return f"{UNRENDERABLE}:{type(value).__name__}"


def _detect_kind(text: str) -> str | None:
    """Kind implied by a value's *shape*, independent of its key.

    :data:`SHAPE_DETECTORS` is the whole vocabulary, and :data:`SHAPELESS_KINDS`
    says what is deliberately not in it -- above all ``name``, which has no
    shape and is closed by the key vocabulary, the keyed forms and the sibling
    pass instead.

    Surrounding A.3 quote dirt is stripped before matching (contract §2.1), so a
    quoted address is still recognised as one.
    """
    candidate = text.strip().strip(QUOTE_CHARS).strip()
    for kind, pattern in SHAPE_DETECTORS:
        if pattern.fullmatch(candidate):
            return kind
    return None


def _leaf_name(key: str) -> str:
    """The final segment of a source-qualified field path.

    Evidence packets and ``proposals.action`` are keyed by the ``COMPARED_FIELDS``
    vocabulary -- ``crm.contact.email``, ``appdb.student.guardian_email`` -- so
    the key vocabularies above are matched against the last segment as well as
    the whole key. Without this a dotted path falls to default-deny, which is
    still safe but loses the kind (and so the correlation) for no reason.
    """
    return key.rsplit(".", 1)[-1] if "." in key else key


def _pii_kind_for_key(key: str) -> str | None:
    return PII_KEYS.get(key) or PII_KEYS.get(_leaf_name(key))


def _is_text_key(key: str) -> bool:
    return key in TEXT_KEYS or _leaf_name(key) in TEXT_KEYS


def _is_safe_key(key: str) -> bool:
    name = _leaf_name(key)
    if key in SAFE_KEYS or name in SAFE_KEYS:
        return True
    return name.startswith(SAFE_KEY_PREFIXES) or name.endswith(SAFE_KEY_SUFFIXES)


def _is_known_key(key: str) -> bool:
    """True when ``key`` is a committed *field name* rather than a datum.

    Used for mapping keys only. It unions the four committed vocabularies: a
    PII key name (``first_name`` names a field; it is not itself a name), a
    free-text key, an allow-listed operational key, and
    :data:`STRUCTURAL_KEYS`.
    """
    name = _leaf_name(key)
    if _pii_kind_for_key(key) is not None or _is_text_key(key) or _is_safe_key(key):
        return True
    return key in STRUCTURAL_KEYS or name in STRUCTURAL_KEYS


def _element_key(key: str | None) -> str | None:
    """The key a SEQUENCE element inherits from its parent -- usually none.

    An element has no key of its own, so default-deny gives it none. Two
    committed exceptions:

    * a :data:`PII_KEYS` key -- every element of ``{"first_name": [...]}`` *is*
      a first name, and tokenising it under that kind beats ``opaque``;
    * :data:`SEQUENCE_SAFE_KEYS` -- elements drawn from a committed
      non-personal vocabulary (field names, refs, table names).

    A free-text or otherwise allow-listed key does **not** propagate. It used
    to, and that is what made ``redact({"note": ("Amriyo", "2015-12-16")})``
    return both values untouched: they were judged as free text, and free text
    is recognised by shape, which a name and a dob do not have.
    """
    if key is None:
        return None
    if _pii_kind_for_key(key) is not None:
        return key
    if key in SEQUENCE_SAFE_KEYS or _leaf_name(key) in SEQUENCE_SAFE_KEYS:
        return key
    return None


#: The process-wide redactor. Frozen and stateless, so sharing it is safe.
default_redactor: Final = Redactor()


def redact(obj: Any, *, key: str | None = None) -> Any:
    """Redact ``obj`` with the committed default redactor."""
    return default_redactor.redact(obj, key=key)


def scrub_text(text: str) -> str:
    """Scrub embedded PII out of free text with the default redactor."""
    return default_redactor.scrub_text(text)


def canonical_json(obj: Any) -> str:
    """The project's one JSON spelling: sorted keys, ASCII, no incidental space."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------

Disposition = Literal["purge", "anonymize", "retain"]

#: ``(child_table, child_column, parent_column)`` -- a dependent that blocks the
#: parent's DELETE until it has aged out of its own window.
Dependent = tuple[str, str, str]


class PurgeNotPermitted(RuntimeError):
    """Raised when the connected principal must not run the retention sweep."""


@dataclass(frozen=True)
class RetentionRule:
    """One table's retention decision. Mirrored by ``docs/retention-policy.md``."""

    table: str
    disposition: Disposition
    reason: str
    ts_column: str | None = None
    window_days: int | None = None
    #: Anonymize only: the columns rewritten in place through the redactor.
    columns: tuple[str, ...] = ()
    #: Anonymize only: the primary key, used to batch and order the rewrite.
    #: A tuple because ``ingest_runs`` and ``source_generations`` are keyed on
    #: composites, not on a surrogate ``id``.
    pk_columns: tuple[str, ...] = ("id",)
    #: Purge only: dependents whose surviving rows block the DELETE.
    dependents: tuple[Dependent, ...] = ()

    def cutoff(self, now: datetime) -> datetime | None:
        """Rows strictly older than this are in scope; ``None`` when retained."""
        if self.window_days is None or self.ts_column is None:
            return None
        return now - timedelta(days=self.window_days)


_STAGING = ("stg_crm_contact", "stg_crm_deal", "stg_student", "stg_enrollment", "stg_payment")

#: The committed retention schedule. Order is execution order: every dependent
#: is swept before the parent it would otherwise block.
RETENTION: Final[tuple[RetentionRule, ...]] = (
    *(
        RetentionRule(
            table=table,
            disposition="purge",
            ts_column="materialized_at",
            window_days=30,
            reason=(
                "Derived, re-materialisable cache of normalised source values. It is "
                "the one PII copy that can be rebuilt on demand from the landing "
                "table, so it gets the shortest window. It is also the only table any "
                "writer role may DELETE (recon_writer, migration 0002)."
            ),
        )
        for table in _STAGING
    ),
    RetentionRule(
        table="entity_link_candidates",
        disposition="purge",
        ts_column="created_at",
        window_days=90,
        reason=(
            "Rejected and accepted ER candidates with their scoring detail. Useful "
            "only while the generation that produced them is still explainable; that "
            "is the landing window."
        ),
    ),
    RetentionRule(
        table="raw_records",
        disposition="purge",
        ts_column="ingest_ts",
        window_days=90,
        reason=(
            "Verbatim source payloads -- the largest raw-PII store in the system. "
            "Held only long enough to re-materialise staging and re-run the three "
            "ingested generations (contract §9.2), which is one quarter. Purging it "
            "makes the entity_links provenance floor verifiable only inside this "
            "window, and makes those rows un-UPDATEable (KS009)."
        ),
        dependents=tuple((child, "raw_record_id", "id") for child in _STAGING),
    ),
    RetentionRule(
        table="ingest_runs",
        disposition="anonymize",
        ts_column="started_at",
        window_days=90,
        columns=("error_detail",),
        pk_columns=("run_id", "source_id"),
        reason=(
            "The row is load-bearing, not logging: absence-style invariants are "
            "skipped for a source whose run is not 'ok'. The row therefore survives "
            "forever and only `error_detail`, which quotes rejected records, is "
            "redacted -- on the same clock as the landing table it describes."
        ),
    ),
    RetentionRule(
        table="source_generations",
        disposition="anonymize",
        ts_column="updated_at",
        window_days=90,
        columns=("error_detail",),
        pk_columns=("source_id", "generation", "entity_type"),
        reason=(
            "The per-source completeness ledger (migration 0009). The counts and the "
            "`complete` flag decide whether an absence-style invariant may run at "
            "all, so the row is permanent; `error_detail` quotes rejected records and "
            "is redacted on the landing table's clock, exactly like `ingest_runs`."
        ),
    ),
    RetentionRule(
        table="field_lineage",
        disposition="purge",
        ts_column="observed_ts",
        window_days=180,
        reason=(
            "`value_text` holds per-field personal values across generations. Two "
            "quarters covers the A,B,A oscillation window scan with margin; nothing "
            "reads lineage older than that."
        ),
    ),
    RetentionRule(
        table="invariant_results",
        disposition="anonymize",
        ts_column="created_at",
        window_days=180,
        columns=("detail",),
        reason=(
            "Per-record verdicts are the grading contract and are never overwritten "
            "per run, so the row is kept forever; only the `detail` packet, which "
            "quotes the values that failed the rule, is redacted in place."
        ),
    ),
    RetentionRule(
        table="proposal_events",
        disposition="purge",
        ts_column="ts",
        window_days=365,
        reason=(
            "`before`/`after` are full canonical snapshots. They exist to make a "
            "rollback possible; a year past the apply, the reversal is no longer a "
            "live operation. Purged before its parent proposal."
        ),
    ),
    RetentionRule(
        table="proposals",
        disposition="purge",
        ts_column="created_at",
        window_days=365,
        reason=(
            "`evidence`, `action` and `rationale` all quote personal values, and "
            "`evidence` CANNOT be anonymised in place: migration 0005's immutability "
            "trigger raises KS005 for the schema owner too. The only disposition the "
            "write boundary permits is deleting the row whole. The redacted audit_log "
            "entry written at decision time is what survives as the decision record."
        ),
        dependents=(("proposal_events", "proposal_id", "id"),),
    ),
    RetentionRule(
        table="conflicts",
        disposition="anonymize",
        ts_column="created_at",
        window_days=365,
        columns=("entity_refs", "observed_values"),
        reason=(
            "`observed_values` holds the disagreeing personal values themselves. A "
            "conflict is referenced by its proposals, so it cannot be deleted while "
            "they survive -- it is anonymised in place at one year instead, keeping "
            "the fingerprint, type and status that make re-detection idempotent."
        ),
    ),
    RetentionRule(
        table="conflict_incidents",
        disposition="purge",
        ts_column="created_at",
        window_days=730,
        pk_columns=("incident_id", "conflict_id"),
        reason=(
            "Membership edge: two ids and a distance, no personal data of its own. "
            "It is purged on the same clock as the conflict it points at, and BEFORE "
            "it -- otherwise it would block that conflict's DELETE forever."
        ),
    ),
    RetentionRule(
        table="conflicts",
        disposition="purge",
        ts_column="created_at",
        window_days=730,
        reason=(
            "Second stage: once no proposal and no incident membership references it, "
            "the (already anonymised) conflict row itself goes."
        ),
        dependents=(
            ("proposals", "conflict_id", "id"),
            ("conflict_incidents", "conflict_id", "id"),
        ),
    ),
    RetentionRule(
        table="audit_log",
        disposition="purge",
        ts_column="ts",
        window_days=730,
        reason=(
            "The accountability record, and under the default LOG_MODE=safe already "
            "redacted at write time -- so it is the lowest-risk copy and gets the "
            "longest window. It outlives the proposals it describes on purpose."
        ),
    ),
    # --- retained: no time-based window ------------------------------------
    RetentionRule(
        table="entities",
        disposition="retain",
        reason=(
            "The canonical record is the product, not a log; ageing it out would "
            "delete the service's output. `current` is also UPDATE-blocked outside "
            "the cited-apply path (KS001). Erasure of a named subject is a "
            "rights-request operation, NOT a time-based sweep, and is not built here."
        ),
    ),
    RetentionRule(
        table="entity_links",
        disposition="retain",
        reason=(
            "`source_key` is the source's natural key (it is joined to "
            "raw_records.natural_key by trigger KS009), i.e. a surrogate identifier, "
            "not personal data. Kept as long as the canonical rows it justifies."
        ),
    ),
    RetentionRule(
        table="budget_model_prices",
        disposition="retain",
        reason=(
            "Reference data, not a record of anything that happened: the committed "
            "per-token rates from prices.yaml (migration 0010), keyed by model name. "
            "It carries no personal data and it is owner-only for a reason -- "
            "migration 0010's reserve and settle triggers derive the worst case and "
            "the settled amount by reading this table, so a row that aged out would "
            "turn every reservation for that model into KS0xx 'model is not in "
            "budget_model_prices' rather than free anything. Rows change by "
            "migration, never by clock."
        ),
    ),
    *(
        RetentionRule(
            table=table,
            disposition="retain",
            reason="No personal data: identifiers, money and cluster geometry only.",
        )
        for table in ("api_clients", "budget_ledger", "budget_reservations", "incidents")
    ),
)


def retention_rule(table: str, disposition: Disposition | None = None) -> RetentionRule:
    """The rule for ``table`` (optionally the one with ``disposition``)."""
    for rule in RETENTION:
        if rule.table == table and (disposition is None or rule.disposition == disposition):
            return rule
    raise KeyError(f"no retention rule for table {table!r}")


@dataclass(frozen=True)
class PurgeResult:
    """What one rule did (or, under ``dry_run``, would have done)."""

    table: str
    disposition: Disposition
    window_days: int | None
    cutoff: datetime | None
    rows: int
    dry_run: bool = False
    details: tuple[str, ...] = ()


#: The three application roles. None of them may run the sweep.
_WRITER_ROLES: Final = ("recon_writer", "review_writer", "apply_writer")

#: Actor the sweep attributes its own audit row to. Matches the ``^system:``
#: convention migration 0004 pins for machine principals.
PURGE_ACTOR: Final = "system:retention"

#: ``audit_log.action`` for the sweep's own row.
PURGE_ACTION: Final = "retention.purge"


def assert_purge_principal(conn: Any) -> str:
    """Return ``current_user``, refusing to sweep as an application writer role.

    Migrations 0001-0009 grant DELETE to exactly one non-owner grantee --
    ``recon_writer`` on the five ``stg_*`` tables (0009's ``source_generations``
    included: it grants INSERT and UPDATE, never DELETE). Every other retention-bearing
    table can be deleted only by the schema owner (the ops/migration principal
    named by ``DATABASE_URL``). Failing here, loudly and early, beats issuing a
    DELETE that a grant refuses halfway through the schedule; and widening a
    grant so the detection path could erase its own evidence would break the
    write boundary this project is graded on.
    """
    from sqlalchemy import text  # local: keeps the redactor importable without a DB

    who = str(conn.execute(text("SELECT current_user")).scalar_one())
    if who in _WRITER_ROLES:
        raise PurgeNotPermitted(
            f"the retention sweep must not run as {who}: migrations 0001-0009 give the "
            f"application roles DELETE on the stg_* cache only, and no grant is widened "
            f"to make purging convenient. Run it as the ops/migration principal named "
            f"by DATABASE_URL (the schema owner)."
        )
    return who


def _in_scope_predicate(rule: RetentionRule) -> str:
    """SQL predicate selecting the rows this rule may act on."""
    clauses = [f"t.{rule.ts_column} < :cutoff"]
    for child, child_column, parent_column in rule.dependents:
        clauses.append(
            f"NOT EXISTS (SELECT 1 FROM {child} c WHERE c.{child_column} = t.{parent_column})"
        )
    return " AND ".join(clauses)


def _purge(conn: Any, rule: RetentionRule, cutoff: datetime, *, dry_run: bool) -> int:
    """DELETE (or, under ``dry_run``, count) the rows this rule owns.

    Table and column names come from the committed :data:`RETENTION` tuple, never
    from a caller, and the only value interpolated is the bound ``:cutoff``.
    """
    from sqlalchemy import text

    predicate = _in_scope_predicate(rule)
    if dry_run:
        sql = f"SELECT count(*) FROM {rule.table} t WHERE {predicate}"
        return int(conn.execute(text(sql), {"cutoff": cutoff}).scalar_one())
    sql = f"DELETE FROM {rule.table} t WHERE {predicate}"
    return int(conn.execute(text(sql), {"cutoff": cutoff}).rowcount)


def _anonymize(
    conn: Any,
    rule: RetentionRule,
    cutoff: datetime,
    *,
    redactor: Redactor,
    dry_run: bool,
    batch_size: int,
) -> int:
    """Rewrite ``rule.columns`` in place through the redactor, in pk batches.

    Only rows whose redaction differs from what is stored are written, and
    redaction is idempotent, so a second sweep over the same rows is a no-op.
    The batch cursor is a row-value comparison over ``rule.pk_columns``, because
    ``ingest_runs`` is keyed on a pair and a single-column cursor would skip rows.
    """
    from sqlalchemy import text

    pk = rule.pk_columns
    pk_select = ", ".join(f"t.{name}" for name in pk)
    pk_tuple = "(" + ", ".join(f"t.{name}" for name in pk) + ")"
    pk_params = "(" + ", ".join(f":after_{name}" for name in pk) + ")"
    value_select = ", ".join(f"t.{name}" for name in rule.columns)

    first_sql = text(
        f"SELECT {pk_select}, {value_select} FROM {rule.table} t "
        f"WHERE t.{rule.ts_column} < :cutoff ORDER BY {pk_select} LIMIT :limit"
    )
    next_sql = text(
        f"SELECT {pk_select}, {value_select} FROM {rule.table} t "
        f"WHERE t.{rule.ts_column} < :cutoff AND {pk_tuple} > {pk_params} "
        f"ORDER BY {pk_select} LIMIT :limit"
    )
    # Every anonymised column in RETENTION is jsonb, so the rewritten value is
    # bound as canonical JSON text and CAST -- psycopg will not adapt a bare dict.
    assignments = ", ".join(f"{name} = CAST(:{name} AS jsonb)" for name in rule.columns)
    where = " AND ".join(f"{name} = :pk_{name}" for name in pk)
    update_sql = text(f"UPDATE {rule.table} SET {assignments} WHERE {where}")

    changed = 0
    cursor: dict[str, Any] | None = None
    while True:
        params: dict[str, Any] = {"cutoff": cutoff, "limit": batch_size}
        if cursor is None:
            rows = conn.execute(first_sql, params).mappings().all()
        else:
            params.update({f"after_{name}": cursor[name] for name in pk})
            rows = conn.execute(next_sql, params).mappings().all()
        if not rows:
            return changed
        for row in rows:
            cursor = {name: row[name] for name in pk}
            updates = {
                name: redactor.redact(row[name], key=name)
                for name in rule.columns
                if row[name] is not None
            }
            if not updates or all(updates[name] == row[name] for name in updates):
                continue
            changed += 1
            if not dry_run:
                write: dict[str, Any] = {f"pk_{name}": row[name] for name in pk}
                for name in rule.columns:
                    value = updates.get(name, row[name])
                    write[name] = None if value is None else canonical_json(value)
                conn.execute(update_sql, write)


def run_purge(
    conn: Any,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    redactor: Redactor | None = None,
    rules: Iterable[RetentionRule] = RETENTION,
    batch_size: int = 1000,
    audit: bool = True,
) -> tuple[PurgeResult, ...]:
    """Run the retention schedule against ``conn`` and report per-rule counts.

    ``conn`` is a SQLAlchemy ``Connection``. The caller owns the transaction:
    nothing here commits, so a sweep can be inspected and rolled back.

    Refuses to run as any application writer role (see
    :func:`assert_purge_principal`). Rules execute in the committed order, which
    puts every dependent ahead of the parent it would otherwise block with a
    foreign-key violation.

    ``dry_run`` counts against the database **as it is now**, so for a table with
    dependents it is a *lower bound*: a parent still blocked by a child that this
    same sweep would delete is reported as not-in-scope. That is deliberate --
    the alternative is a second predicate that can drift from the one the real
    DELETE uses, and a dry run that over-reports is worse than one that
    under-reports. Leaf tables are exact.
    """
    from sqlalchemy import text

    principal = assert_purge_principal(conn)
    moment = now or datetime.now(UTC)
    active = redactor or default_redactor
    results: list[PurgeResult] = []

    for rule in rules:
        cutoff = rule.cutoff(moment)
        if rule.disposition == "retain" or cutoff is None:
            results.append(
                PurgeResult(
                    table=rule.table,
                    disposition=rule.disposition,
                    window_days=rule.window_days,
                    cutoff=None,
                    rows=0,
                    dry_run=dry_run,
                )
            )
            continue
        if rule.disposition == "purge":
            rows = _purge(conn, rule, cutoff, dry_run=dry_run)
        else:
            rows = _anonymize(
                conn,
                rule,
                cutoff,
                redactor=active,
                dry_run=dry_run,
                batch_size=batch_size,
            )
        results.append(
            PurgeResult(
                table=rule.table,
                disposition=rule.disposition,
                window_days=rule.window_days,
                cutoff=cutoff,
                rows=rows,
                dry_run=dry_run,
                details=rule.columns,
            )
        )

    if audit and not dry_run:
        # The sweep's own audit row goes through the SAME sink every other
        # audit row goes through (`recon.logging.audit_row`), so it carries the
        # documented `{mode, body_sha256, body}` detail shape instead of a
        # second, hand-rolled one. The import is local because `recon.logging`
        # imports this module.
        from recon.logging import audit_row

        params = audit_row(
            actor=PURGE_ACTOR,
            action=PURGE_ACTION,
            subject=f"principal:{principal}",
            body=_audit_body(moment, results),
        )
        conn.execute(
            text(
                "INSERT INTO audit_log (actor, action, subject, detail) "
                "VALUES (:actor, :action, :subject, CAST(:detail AS jsonb))"
            ),
            {name: params[name] for name in ("actor", "action", "subject", "detail")},
        )
    return tuple(results)


def _audit_body(moment: datetime, results: Sequence[PurgeResult]) -> dict[str, Any]:
    """Summary of a sweep. Counts only -- never a purged value.

    Returned as a structure, not as JSON text, because it is handed to
    :func:`recon.logging.audit_row`, which is the one place that decides how an
    ``audit_log.detail`` body is spelled and redacted.
    """
    return {
        "ran_at": moment.isoformat(),
        "tables": [
            {
                "table": r.table,
                "disposition": r.disposition,
                "window_days": r.window_days,
                "rows": r.rows,
            }
            for r in results
            if r.disposition != "retain"
        ],
    }


# ---------------------------------------------------------------------------
# the entry point: `python -m recon.privacy`
# ---------------------------------------------------------------------------


def render_sweep(results: Sequence[PurgeResult], *, dry_run: bool = False) -> str:
    """The sweep report, one line per rule, in execution order.

    Counts only. A retention report that quoted a purged value would be a copy of
    the thing the sweep just removed -- so a line names the table, the disposition,
    the window and how many rows, and nothing else. It still goes through
    :func:`recon.logging.console` at the call site, because "no value can appear
    here" is a property of this function and the chokepoint is what makes it a
    property of the *output*.
    """
    header = "would sweep (dry run)" if dry_run else "swept"
    lines = [f"retention {header}: {len(results)} rules"]
    for result in results:
        window = "--" if result.window_days is None else f"{result.window_days}d"
        columns = f"  [{', '.join(result.details)}]" if result.details else ""
        lines.append(
            (
                f"  {result.table:<24} {result.disposition:<9} {window:>5} "
                f"rows={result.rows:<8}{columns}"
            ).rstrip()
        )
    total = sum(result.rows for result in results)
    lines.append(f"  {'total':<24} {'':<9} {'':>5} rows={total}")
    return "\n".join(lines)


def render_target(url: Any, *, apply: bool) -> str:
    """Name the database this run is pointed at, **before** it touches it.

    A destructive tool that does not say what it is pointed at is how a retention
    sweep deleted 37,498 rows out of this project's shared development database:
    the process that ran it had no ``DATABASE_URL`` of its own, inherited the
    repository's ``.env``, and printed a report that named tables and row counts
    and never once named the database they came out of. So every run prints this,
    dry or not, and prints it before the first statement rather than beside the
    result.

    **No password, ever** -- the fields are read off the URL one at a time and
    ``url.password`` is not one of them, which is stronger than rendering the DSN
    with a masking flag that a later refactor could drop.

    **The ``key=value`` spelling is load-bearing, not cosmetic.** This line goes
    through :func:`recon.logging.console`, so :func:`scrub_text` reads it: the
    obvious ``user@host:port/database`` rendering is *email-shaped* on any real
    deployment host, and comes out as ``[pii:email:...]`` with the hostname and
    the database name inside the token. A report that redacts the identity of the
    database it is about to empty is worse than no report. Written as
    ``host=<host>``, the same string survives the scrubber intact, because
    ``host``/``database``/``user`` are not PII key names.
    """
    mode = "APPLY -- rows WILL be deleted and rewritten" if apply else "dry run -- writes nothing"
    return (
        f"retention target: database={url.database} host={url.host or '-'} "
        f"port={url.port or '-'} user={url.username or '-'} mode={mode}"
    )


def _confirm(target: str) -> bool:
    """Ask an interactive operator to confirm a destructive sweep.

    Only on a terminal. ``--apply`` is the gate that matters, and it is the whole
    gate for a cron entry or a CI job, where there is nobody to ask and a prompt
    would hang; ``sys.stdin.isatty()`` is what tells the two apart. A human at a
    keyboard gets one more chance to read which database is named on the line
    above -- which is the failure mode this whole change exists to close.
    """
    from recon.logging import console  # local: `recon.logging` imports THIS module

    if not sys.stdin.isatty():
        return True
    # The prompt itself goes through `console`, and `input` is called bare, so the
    # only text this function puts on the terminal is on the chokepoint
    # `tests/privacy/test_sinks.py` enumerates.
    console(
        f"about to APPLY the retention schedule to: {target}\n"
        "type 'apply' to delete and rewrite rows, anything else to abort:"
    )
    answer = input()
    return answer.strip() == "apply"


def main(argv: list[str] | None = None) -> int:
    """``python -m recon.privacy`` -- report, and on request run, the retention schedule.

    This is ``docs/retention-policy.md`` §3.2's body with a transaction, an exit
    status and a mode flag around it, and nothing else: the schedule, the windows,
    the dispositions and the order all come from :data:`RETENTION`, which the
    policy document mirrors row for row. **It is not a scheduler.** §3.3 puts the
    sweep on the ops/migration principal named by ``DATABASE_URL`` -- the same one
    that runs ``alembic upgrade`` -- precisely because no application role holds
    DELETE on a retention-bearing table, and :func:`assert_purge_principal`
    refuses rather than issuing DELETEs a grant will reject halfway through.

    **Counting is the default; deleting is opt-in.** ``main([])`` -- the CLI with
    no arguments, which is what a stray ``python -m recon.privacy``, a test, or a
    misconfigured cron entry actually runs -- names the target, reports every row
    it *would* remove, rolls back and exits 0. Nothing is deleted without
    ``--apply``. That is not defensiveness: the previous default was to sweep, and
    it emptied 180 days out of this project's shared development database, from a
    process that thought it was unconfigured (see :func:`render_target`). A
    destructive default turns every mistake about *which database* into permanent
    data loss, and the schedule is not urgent enough to be worth that.

    **An apply says what it is about to do first.** ``--apply`` runs the counting
    pass and prints it, then runs the real schedule, both inside one transaction
    and against one pinned ``now`` so the two agree. The preview costs a second
    pass of ``SELECT count(*)`` and buys a report that exists even if the DELETEs
    then fail.

    The caller owns the transaction, which is what §3.2 means by "a sweep can be
    inspected and rolled back": a refusal, an abort or an exception rolls back the
    whole schedule rather than leaving it half-applied.

    Exit status: ``0`` swept, or counted; ``1`` refused, because the connected
    principal is an application writer role; ``2`` misconfigured (argparse, or no
    ``DATABASE_URL``); ``3`` aborted by the operator at the confirmation prompt.
    """
    # One of the ways a Keystone process starts (`recon.logging.ENTRY_POINTS`), so
    # the redaction chain is installed before anything can be emitted -- and the
    # report below goes through `console`, the same chokepoint the scorecard uses.
    # Both imports are local: `recon.logging` imports THIS module, and `recon.db`
    # must not be imported by a process that only wants the redactor.
    from recon.logging import configure_logging_once, console

    configure_logging_once()

    parser = argparse.ArgumentParser(
        prog="recon.privacy",
        description=(
            "Report the committed retention schedule against the database DATABASE_URL "
            "names (docs/retention-policy.md §2). COUNTS ONLY unless --apply is given. "
            "Connects as the principal named by DATABASE_URL, which must be the "
            "ops/migration principal, not an application writer role."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "THE DEFAULT, and accepted so it can be written down explicitly: count "
            "the rows in scope and write nothing. For a table with dependents this "
            "is a LOWER bound (policy §3.2) -- a parent still blocked by a child "
            "this same sweep would delete is reported as not-in-scope."
        ),
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help=(
            "actually DELETE and rewrite the rows outside their windows, and commit. "
            "Irreversible. The target database is named on the first line of output, "
            "and the rows that will go are reported before they go."
        ),
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="with --apply, skip the confirmation prompt shown on an interactive terminal.",
    )
    args = parser.parse_args(argv)
    # Anything that is not an explicit --apply counts and rolls back. `--dry-run`
    # sets no separate mode; it is the name of the default, so that a script that
    # spells it out and a script that forgets to behave identically.
    applying: bool = args.apply

    from recon.db import DatabaseNotConfigured, database_url, get_engine

    try:
        url = database_url()
        # `get_engine` is process-wide and lru_cached, so it is not disposed here:
        # the sweep is a one-shot job and the process owns the pool's lifetime.
        engine = get_engine()
    except DatabaseNotConfigured as failure:
        parser.error(str(failure))

    target = render_target(url, apply=applying)
    console(target)

    # One moment for both passes: a preview taken against a cutoff a few
    # milliseconds older than the DELETE's would report counts the sweep then
    # disagrees with, and the report is evidence or it is decoration.
    moment = datetime.now(UTC)

    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            preview = run_purge(conn, now=moment, dry_run=True, audit=False)
        except PurgeNotPermitted as refusal:
            transaction.rollback()
            console(f"retention sweep refused: {refusal}", stream=sys.stderr)
            return 1
        except BaseException:
            transaction.rollback()
            raise
        console(render_sweep(preview, dry_run=True))

        if not applying:
            transaction.rollback()
            console(
                "retention: nothing was written. Re-run with --apply to delete and "
                "rewrite the rows counted above."
            )
            return 0

        if not (args.yes or _confirm(target)):
            transaction.rollback()
            console("retention sweep aborted: nothing was written.", stream=sys.stderr)
            return 3

        try:
            results = run_purge(conn, now=moment, dry_run=False)
        except PurgeNotPermitted as refusal:  # pragma: no cover - the preview refuses first
            transaction.rollback()
            console(f"retention sweep refused: {refusal}", stream=sys.stderr)
            return 1
        except BaseException:
            # A schedule that stopped halfway would leave the dependents of a rule
            # that has not run yet already deleted. It is all or nothing.
            transaction.rollback()
            raise
        transaction.commit()

    console(render_sweep(results, dry_run=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
