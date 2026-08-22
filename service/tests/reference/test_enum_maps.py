"""SS2.3 crosswalk maps -- total over their declared domain, raising on a missing key.

Totality is tested the way the contract states it: iterate the declared domain and
assert no `KeyError`, then assert that an *undeclared* key is refused rather than
silently defaulting.
"""

from __future__ import annotations

import pytest

from recon.normalize import (
    DEAL_STAGE_VALUES,
    LIFECYCLE_VALUES,
    STAGE_FUNNEL_VALUES,
    STATUS_VALUES,
    norm_enum,
)
from recon.reference import (
    DEAL_STAGE_TO_FUNNEL,
    FEE_SCHEDULE,
    FUNNEL_VALUES,
    GRADE_ORDER,
    LIFECYCLE_TO_FUNNEL,
    OBSERVED_VALUE_KEYS,
    STATUS_TO_FUNNEL,
    TotalMap,
)

TOTAL_MAPS = [
    (DEAL_STAGE_TO_FUNNEL, DEAL_STAGE_VALUES),
    (STATUS_TO_FUNNEL, STATUS_VALUES),
    (LIFECYCLE_TO_FUNNEL, LIFECYCLE_VALUES),
    (GRADE_ORDER, None),
    (FEE_SCHEDULE, None),
    (OBSERVED_VALUE_KEYS, None),
]


@pytest.mark.parametrize(("mapping", "domain"), TOTAL_MAPS, ids=lambda x: getattr(x, "name", ""))
def test_declared_domain_never_raises(mapping: TotalMap, domain: tuple[str, ...] | None) -> None:
    for key in domain or mapping.domain:
        mapping[key]
    assert set(mapping) == set(mapping.domain)
    assert len(mapping) == len(mapping.domain)


@pytest.mark.parametrize(("mapping", "_domain"), TOTAL_MAPS, ids=lambda x: getattr(x, "name", ""))
def test_undeclared_key_raises(mapping: TotalMap, _domain: object) -> None:
    """An undeclared key is a caller bug; dirty data goes through `norm_enum`."""
    with pytest.raises(KeyError):
        mapping["definitely-not-in-the-domain"]


def test_a_map_missing_a_declared_key_raises_at_construction() -> None:
    """This is what "raises at import" means: the module cannot load half-total."""
    with pytest.raises(ValueError, match="not total over its declared domain"):
        TotalMap("EXAMPLE", ("a", "b"), {"a": 1})


def test_a_map_with_an_extra_key_raises_at_construction() -> None:
    with pytest.raises(ValueError, match="not total over its declared domain"):
        TotalMap("EXAMPLE", ("a",), {"a": 1, "b": 2})


def test_deal_stage_to_funnel_is_the_committed_bijection() -> None:
    assert dict(DEAL_STAGE_TO_FUNNEL) == {
        "New Lead": "prospect",
        "Application Submitted": "applied",
        "Waitlisted": "waitlisted",
        "Deposit Received": "deposit_paid",
        "Closed Won": "enrolled",
        "Closed Lost": "withdrawn",
        "Refunded": "refunded",
    }
    assert sorted(DEAL_STAGE_TO_FUNNEL.values()) == sorted(FUNNEL_VALUES)
    assert len(set(DEAL_STAGE_TO_FUNNEL.values())) == len(FUNNEL_VALUES) == 7


def test_status_to_funnel_is_the_committed_map() -> None:
    assert dict(STATUS_TO_FUNNEL) == {
        "prospect": "prospect",
        "applied": "applied",
        "enrolled": "enrolled",
        "active": "enrolled",
        "withdrawn": "withdrawn",
    }
    assert STATUS_TO_FUNNEL["active"] == STATUS_TO_FUNNEL["enrolled"] == "enrolled"


def test_lifecycle_to_funnel_is_the_committed_map() -> None:
    assert dict(LIFECYCLE_TO_FUNNEL) == {
        "subscriber": None,
        "lead": "prospect",
        "marketingqualifiedlead": "prospect",
        "MQL": "prospect",
        "salesqualifiedlead": "applied",
        "SQL": "applied",
        "opportunity": "applied",
        "customer": "enrolled",
        "evangelist": None,
        "other": None,
    }


def test_no_lifecycle_value_maps_to_withdrawn() -> None:
    """`G18`: a `withdrawn` student is represented on the CRM side by the
    `None`-mapping subset, so the comparison is `unchecked`, not a disagreement."""
    assert "withdrawn" not in set(LIFECYCLE_TO_FUNNEL.values())
    none_mapping = {key for key, value in LIFECYCLE_TO_FUNNEL.items() if value is None}
    assert none_mapping == {"subscriber", "evangelist", "other"}


def test_every_funnel_value_is_reachable_from_the_appdb_side() -> None:
    assert set(STATUS_TO_FUNNEL.values()) <= set(FUNNEL_VALUES)
    assert set(STAGE_FUNNEL_VALUES) == set(FUNNEL_VALUES)


@pytest.mark.parametrize("value", DEAL_STAGE_VALUES)
def test_dirty_deal_stage_variants_reach_the_map(value: str) -> None:
    """`norm_enum` -> crosswalk is the whole path; SQL rules may not normalize."""
    for dirty in (value.upper().replace(" ", "_"), value.lower(), f"  {value}  "):
        canonical = norm_enum("deal_stage", dirty)
        assert canonical == value
        assert DEAL_STAGE_TO_FUNNEL[canonical] == DEAL_STAGE_TO_FUNNEL[value]


@pytest.mark.parametrize("value", LIFECYCLE_VALUES)
def test_lifecycle_domain_round_trips_through_norm_enum(value: str) -> None:
    assert norm_enum("lifecycle_stage", value) == value
    LIFECYCLE_TO_FUNNEL[value]


def test_unmappable_enum_value_never_reaches_a_total_map() -> None:
    """SS5.1/SS5.8: the `None` short-circuits into `unchecked`, never a `KeyError`."""
    assert norm_enum("lifecycle_stage", "not-a-stage") is None
    assert norm_enum("deal_stage", "not-a-stage") is None
    assert norm_enum("status", "not-a-status") is None
