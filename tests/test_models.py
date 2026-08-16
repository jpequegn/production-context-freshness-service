from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from pcfs.identifiers import canonical_json, content_digest, stable_id
from pcfs.models import (
    AuthorityLevel,
    Fact,
    FactRelationship,
    FactType,
    FreshnessMode,
    FreshnessPolicy,
    RelationshipType,
    Sensitivity,
    SourceClass,
    SourceSpan,
    SourceVersion,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def source() -> SourceVersion:
    return SourceVersion(
        source_id="catalog/services",
        version="v1",
        source_class=SourceClass.SERVICE_CATALOG,
        uri="fixtures/catalog.yaml",
        content_digest=content_digest({"services": ["checkout"]}),
        observed_at=NOW,
        recorded_at=NOW,
    )


def fact(**overrides: object) -> Fact:
    values: dict[str, object] = {
        "subject": "checkout",
        "predicate": "owned_by",
        "value": "team-orchid",
        "fact_type": FactType.OWNERSHIP,
        "source": source(),
        "observed_at": NOW,
        "valid_from": NOW,
        "recorded_at": NOW,
        "authority": AuthorityLevel.AUTHORITATIVE,
        "owner": "team-orchid",
        "environment": "production",
        "service": "checkout",
        "freshness_policy_id": "ownership-v1",
    }
    values.update(overrides)
    return Fact.model_validate(values)


def test_contracts_serialize_deterministically() -> None:
    first = fact()
    second = fact()

    assert canonical_json(first) == canonical_json(second)
    assert first.fact_id == second.fact_id
    assert first.source.key == second.source.key


@given(st.text(min_size=1), st.integers(), st.booleans())
def test_stable_ids_ignore_mapping_order(text: str, number: int, flag: bool) -> None:
    left = {"text": text, "number": number, "flag": flag}
    right = {"flag": flag, "number": number, "text": text}

    assert stable_id("test", left) == stable_id("test", right)


def test_invalid_temporal_intervals_are_rejected() -> None:
    with pytest.raises(ValidationError, match="valid_to must be after valid_from"):
        fact(valid_to=NOW - timedelta(seconds=1))

    with pytest.raises(ValidationError, match="timezone-aware"):
        fact(recorded_at=datetime(2026, 1, 1))


def test_restricted_facts_require_access_scope() -> None:
    with pytest.raises(ValidationError, match="restricted facts require"):
        fact(sensitivity=Sensitivity.RESTRICTED)


def test_confidence_is_bounded_and_does_not_change_authority() -> None:
    low_authority = fact(authority=AuthorityLevel.UNVERIFIED, confidence=1.0)
    assert low_authority.authority is AuthorityLevel.UNVERIFIED

    with pytest.raises(ValidationError):
        fact(confidence=1.1)


def test_source_spans_and_relationships_are_validated() -> None:
    with pytest.raises(ValidationError):
        SourceSpan(line_start=4, line_end=3)

    current = fact()
    with pytest.raises(ValidationError, match="same fact"):
        FactRelationship(
            source_fact_id=current.fact_id,
            target_fact_id=current.fact_id,
            relationship_type=RelationshipType.SUPPORTS,
            source=source(),
            recorded_at=NOW,
        )


def test_freshness_policy_requires_mode_configuration() -> None:
    with pytest.raises(ValidationError, match="soft review"):
        FreshnessPolicy(
            policy_id="ownership-v1",
            version="1",
            fact_type=FactType.OWNERSHIP,
            modes={FreshnessMode.SOFT_REVIEW},
        )

    policy = FreshnessPolicy(
        policy_id="ownership-v1",
        version="1",
        fact_type=FactType.OWNERSHIP,
        modes={FreshnessMode.SOFT_REVIEW},
        soft_review_after=timedelta(days=30),
    )
    assert policy.minimum_authority is AuthorityLevel.INFORMAL


def test_knowable_and_valid_are_independent() -> None:
    delayed = fact(valid_from=NOW, recorded_at=NOW + timedelta(days=2))
    question_time = NOW + timedelta(days=1)

    assert delayed.was_valid_at(question_time)
    assert not delayed.was_knowable_at(question_time)
