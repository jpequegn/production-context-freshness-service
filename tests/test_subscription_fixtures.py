import json
from collections import Counter
from datetime import timedelta

import pytest
from pydantic import ValidationError

import pcfs.subscriptions.fixtures as fixtures
from pcfs.identifiers import canonical_json, content_digest
from pcfs.models import Sensitivity
from pcfs.subscriptions.fixtures import (
    PINNED_TIME,
    Scenario,
    demo_scenario,
    evaluation_scenarios,
    evaluation_truth,
    offline_cache,
)
from pcfs.subscriptions.models import AssessmentInput, EvidenceSnapshot


def test_demo_has_six_original_facts_three_independent_conclusions_and_four_deliveries() -> None:
    scenario = demo_scenario()
    assert len(scenario.facts) == 6
    assert len(scenario.subscriptions) == 3
    assert len({item.conclusion_id for item in scenario.subscriptions}) == 3
    assert len(scenario.events) == 4
    editorial, reversal, ownership, replay = scenario.events
    assert reversal.old == editorial.new
    assert editorial.old == scenario.facts[0]
    assert ownership.old == scenario.facts[1]
    assert ownership.old.fact.source.source_id != reversal.old.fact.source.source_id
    assert (editorial.sequence, reversal.sequence, ownership.sequence) == (1, 2, 1)
    assert ownership.observed_at == PINNED_TIME + timedelta(seconds=30)
    assert replay == reversal
    assert replay.key == reversal.key
    assert len({item.event_id for item in scenario.events}) == 3
    assert {item.fact.fact_id for item in scenario.facts} == {
        dependency for item in scenario.subscriptions for dependency in item.dependencies
    }
    dependencies = [set(item.dependencies) for item in scenario.subscriptions]
    assert all(len(items) == 2 for items in dependencies)
    assert not dependencies[0] & dependencies[1]
    assert not dependencies[0] & dependencies[2]
    assert not dependencies[1] & dependencies[2]


def test_demo_material_changes_share_one_review_episode_within_debounce() -> None:
    scenario = demo_scenario()
    truth = evaluation_truth()
    material_pairs = {
        (event.event_id, subscription.subscription_id)
        for event in scenario.events
        for subscription in scenario.subscriptions
        if truth[event.event_id, subscription.subscription_id]
    }
    assert material_pairs == {
        ("demo/procedure-reversal", "demo/restart"),
        ("demo/ownership", "demo/restart"),
    }
    assert {
        (subscription.subscription_id, subscription.revision)
        for subscription in scenario.subscriptions
        if any(key[1] == subscription.subscription_id for key in material_pairs)
    } == {("demo/restart", 1)}
    assert scenario.events[2].available_at - scenario.events[1].available_at < timedelta(
        seconds=scenario.subscriptions[0].debounce_seconds
    )
    cache = offline_cache(scenario)
    assert [
        cache[AssessmentInput(subscription=scenario.subscriptions[0], event=event).key].materiality
        for event in scenario.events
    ] == ["unchanged", "material", "material", "material"]
    for subscription in scenario.subscriptions[1:]:
        assert all(
            cache[AssessmentInput(subscription=subscription, event=event).key].relevance
            == "irrelevant"
            for event in scenario.events
        )


def test_evaluation_exact_counts_and_family_level_heldout_split() -> None:
    scenarios = evaluation_scenarios()
    assert len(scenarios) == 28
    assert len({item.sequence_id for item in scenarios}) == 28
    assert sum(len(item.events) for item in scenarios) == 58
    assert len({event.event_id for item in scenarios for event in item.events}) == 57
    assert Counter(item.split for item in scenarios) == {"dev": 19, "heldout": 9}
    families = Counter(item.sequence_id.split("/")[0] for item in scenarios[1:])
    assert families == {
        family: 3
        for family in (
            "editorial",
            "procedure-reversal",
            "ownership",
            "conflict",
            "access-revocation",
            "hard-expiry",
            "metric-rename",
            "late-event",
            "provider-failure",
        )
    }
    dev_families = {item.sequence_id.split("/")[0] for item in scenarios if item.split == "dev"}
    heldout_families = {
        item.sequence_id.split("/")[0] for item in scenarios if item.split == "heldout"
    }
    assert heldout_families == {"metric-rename", "late-event", "provider-failure"}
    assert not dev_families & heldout_families
    dev_documents = {
        event.new.document for item in scenarios if item.split == "dev" for event in item.events
    }
    heldout_documents = {
        event.new.document for item in scenarios if item.split == "heldout" for event in item.events
    }
    assert not dev_documents & heldout_documents


def test_fixtures_and_truth_are_deterministic_and_fresh_per_call() -> None:
    assert canonical_json(evaluation_scenarios()) == canonical_json(evaluation_scenarios())
    assert demo_scenario().model_dump_json() == demo_scenario().model_dump_json()
    assert evaluation_truth() == evaluation_truth()
    assert offline_cache(demo_scenario()) == offline_cache(demo_scenario())
    truth = evaluation_truth()
    assert len(truth) == 63
    assert sum(truth.values()) == 38
    assert all(type(value) is bool for value in truth.values())
    truth.clear()
    assert len(evaluation_truth()) == 63


def test_runtime_serialization_has_no_truth_labels() -> None:
    assert set(Scenario.model_fields) == {
        "sequence_id",
        "split",
        "subscriptions",
        "facts",
        "events",
    }
    for scenario in evaluation_scenarios():
        payload = scenario.model_dump_json()
        assert Scenario.model_validate_json(payload) == scenario
        assert set(json.loads(payload)) == set(Scenario.model_fields)
        assert not any(
            label in payload for label in ("expected_material", "expectedmaterial", "truth")
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        Scenario.model_validate({**demo_scenario().model_dump(), "truth": {}})


def test_snapshots_pin_synthetic_provenance_text_envelopes_and_aware_times() -> None:
    for scenario in evaluation_scenarios():
        snapshots = (
            *scenario.facts,
            *(snapshot for event in scenario.events for snapshot in (event.old, event.new)),
        )
        for snapshot in snapshots:
            fact = snapshot.fact
            assert EvidenceSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
            assert fact.source.content_digest == content_digest({"text": snapshot.document})
            assert fact.source.uri.startswith("synthetic://subscriptions/")
            assert fact.environment == "fixture"
            assert fact.service.startswith("synthetic-")
            assert fact.source_span.line_start == fact.source_span.line_end == 1
            for at in (
                fact.observed_at,
                fact.valid_from,
                fact.recorded_at,
                fact.source.observed_at,
                fact.source.recorded_at,
            ):
                assert at >= PINNED_TIME
                assert at.utcoffset() == timedelta(0)
            assert fact.source.recorded_at == fact.recorded_at
            if fact.source.version != "v1":
                assert fact.recorded_at > PINNED_TIME
        for subscription in scenario.subscriptions:
            originals = {item.fact.fact_id: item for item in scenario.facts}
            assert subscription.source_versions == {
                dependency: originals[dependency].fact.source.key
                for dependency in subscription.dependencies
            }
            assert subscription.deadline.utcoffset() == timedelta(0)
        for event in scenario.events:
            assert event.observed_at.utcoffset() == event.available_at.utcoffset() == timedelta(0)


def test_cache_is_exact_input_bound_and_clearly_synthetic() -> None:
    scenario = demo_scenario()
    cache = offline_cache(scenario)
    inputs = {
        AssessmentInput(subscription=subscription, event=event).key
        for subscription in scenario.subscriptions
        for event in scenario.events
    }
    assert set(cache) == inputs
    assert len(cache) == 9
    for key, assessment in cache.items():
        assert assessment.input_key == key
        assert assessment.provenance == "synthetic_cached"
        assert assessment.model_version == "synthetic-authored-text-pairs-v1"
        assert assessment.rubric_version == "synthetic-review-rubric-v1"
        assert "not real model output" in assessment.uncertainty
        assert assessment.input_tokens is None
        assert assessment.output_tokens is None
    revised = scenario.subscriptions[0].model_copy(update={"revision": 2})
    assert AssessmentInput(subscription=revised, event=scenario.events[0]).key not in cache


def test_provider_failure_and_unrecognized_text_pairs_are_cache_misses() -> None:
    failures = [
        item for item in evaluation_scenarios() if item.sequence_id.startswith("provider-failure/")
    ]
    assert len(failures) == 3
    assert all(offline_cache(item) == {} for item in failures)
    scenario = demo_scenario()
    event = scenario.events[0]
    document = "Synthetic unknown interpretation."
    source = event.new.fact.source.model_copy(
        update={"content_digest": content_digest({"text": document})}
    )
    snapshot = EvidenceSnapshot(
        document=document, fact=event.new.fact.model_copy(update={"source": source})
    )
    unknown = event.model_copy(update={"new": snapshot})
    assert offline_cache(scenario.model_copy(update={"events": (unknown,)})) == {}


def test_runtime_cache_does_not_consult_evaluation_truth(monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = demo_scenario()
    expected = offline_cache(scenario)

    def forbidden() -> None:
        pytest.fail("runtime advice consulted evaluator truth")

    monkeypatch.setattr(fixtures, "evaluation_truth", forbidden)
    monkeypatch.setattr(fixtures, "_evaluation", forbidden)
    assert offline_cache(Scenario.model_validate_json(scenario.model_dump_json())) == expected


def test_structural_cases_are_real_metadata_changes_not_text_labels() -> None:
    scenarios = {item.sequence_id: item for item in evaluation_scenarios()}
    access = scenarios["access-revocation/larch"].events[0]
    assert access.old.document == access.new.document
    assert access.old.fact.sensitivity is Sensitivity.INTERNAL
    assert access.new.fact.sensitivity is Sensitivity.RESTRICTED
    assert access.new.fact.required_scopes == frozenset({"synthetic:restricted"})
    expiry = scenarios["hard-expiry/larch"].events[0]
    assert expiry.new.fact.valid_to <= expiry.available_at
    late = scenarios["late-event/larch"].events
    assert [event.sequence for event in late] == [2, 1]
    assert late[0].available_at < late[1].available_at
    assert late[0].old == late[1].new
    conflict = scenarios["conflict/larch"]
    assert conflict.facts[0].fact.predicate == conflict.facts[1].fact.predicate
    assert conflict.facts[0].fact.authority == conflict.facts[1].fact.authority
    assert conflict.events[0].new.fact.value != conflict.facts[1].fact.value
    for family in ("access-revocation", "hard-expiry"):
        scenario = scenarios[f"{family}/larch"]
        assert all(item.materiality == "unchanged" for item in offline_cache(scenario).values())
        assert all(
            evaluation_truth()[event.event_id, scenario.subscriptions[0].subscription_id]
            for event in scenario.events
        )
