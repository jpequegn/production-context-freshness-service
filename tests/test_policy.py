from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from pcfs.corpus import load_corpus
from pcfs.ingestion import build_corpus_bundles
from pcfs.models import AuthorityLevel, ContextState, FactType
from pcfs.policy import PolicyEngine, PolicyRegistry

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def sample_fact(fact_type: FactType = FactType.OWNERSHIP):
    facts = [
        fact
        for bundle in build_corpus_bundles(load_corpus())
        for fact in bundle.facts
        if fact.fact_type is fact_type
    ]
    return facts[0]


def test_default_registry_covers_every_fact_type() -> None:
    registry = PolicyRegistry.defaults()
    assert {policy.fact_type for policy in registry.all()} == set(FactType)


def test_current_fact_is_selected_with_versioned_policy() -> None:
    fact = sample_fact()
    decision = PolicyEngine().evaluate((fact,), question_time=fact.observed_at + timedelta(days=1))

    assert decision.state is ContextState.CURRENT
    assert decision.selected_fact_ids == (fact.fact_id,)
    assert decision.policy_id == "ownership-v1"
    assert decision.policy_version == "1"


@given(st.integers(min_value=30, max_value=179))
def test_soft_review_boundary_marks_ownership_stale(days: int) -> None:
    fact = sample_fact()
    decision = PolicyEngine().evaluate(
        (fact,), question_time=fact.observed_at + timedelta(days=days)
    )
    assert decision.state is ContextState.STALE


def test_expired_validity_and_event_invalidation_are_stale() -> None:
    mitigation = sample_fact(FactType.TEMPORARY_MITIGATION)
    after_close = mitigation.valid_to + timedelta(seconds=1)

    expired = PolicyEngine().evaluate((mitigation,), question_time=after_close)
    invalidated = PolicyEngine().evaluate(
        (mitigation,),
        question_time=mitigation.observed_at + timedelta(minutes=1),
        invalidated_fact_ids=frozenset({mitigation.fact_id}),
    )

    assert expired.state is ContextState.STALE
    assert invalidated.state is ContextState.STALE


def test_equal_authority_conflict_is_disputed() -> None:
    fact = sample_fact()
    conflicting = fact.model_copy(update={"fact_id": "fact_conflict", "value": "team-ruby"})

    decision = PolicyEngine().evaluate(
        (fact, conflicting), question_time=fact.observed_at + timedelta(days=1)
    )
    assert decision.state is ContextState.DISPUTED
    assert set(decision.selected_fact_ids) == {fact.fact_id, conflicting.fact_id}


def test_higher_authority_wins_even_when_lower_confidence_is_one() -> None:
    fact = sample_fact()
    weak = fact.model_copy(
        update={
            "fact_id": "fact_weak",
            "value": "team-ruby",
            "authority": AuthorityLevel.INFORMAL,
            "confidence": 1.0,
        }
    )
    strong = fact.model_copy(update={"confidence": 0.1})

    decision = PolicyEngine().evaluate(
        (weak, strong), question_time=fact.observed_at + timedelta(days=1)
    )
    assert decision.state is ContextState.CURRENT
    assert decision.selected_fact_ids == (strong.fact_id,)


def test_future_recorded_fact_cannot_leak_into_historical_decision() -> None:
    fact = sample_fact()
    future = fact.model_copy(
        update={
            "recorded_at": fact.recorded_at + timedelta(days=2),
            "fact_id": "fact_future",
        }
    )

    decision = PolicyEngine().evaluate(
        (future,), question_time=fact.recorded_at + timedelta(days=1)
    )
    assert decision.state is ContextState.INSUFFICIENT_EVIDENCE
    assert decision.selected_fact_ids == ()


def test_missing_policy_returns_unknown_instead_of_guessing() -> None:
    fact = sample_fact().model_copy(update={"freshness_policy_id": "missing-v1"})
    decision = PolicyEngine().evaluate((fact,), question_time=NOW + timedelta(days=1))

    assert decision.state is ContextState.UNKNOWN
    assert decision.reasons[0].code == "missing_policy"
