import itertools
import json
import socket
from datetime import timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from test_subscription_models import NOW, event, snapshot, subscription

from pcfs.subscriptions.advice import (
    MAX_CACHE_BYTES,
    MODEL_VERSION,
    RUBRIC_VERSION,
    OfflineAssessor,
    plan_receipt,
    unknown_assessment,
)
from pcfs.subscriptions.models import Assessment, AssessmentInput, GuardResult


@pytest.fixture
def bound() -> AssessmentInput:
    return AssessmentInput(subscription=subscription(), event=event())


def advice(bound: AssessmentInput, **overrides: object) -> Assessment:
    return Assessment.model_validate(
        {
            "input_key": bound.key,
            "model_version": MODEL_VERSION,
            "rubric_version": RUBRIC_VERSION,
            "relevance": "irrelevant",
            "contradiction": "supports",
            "materiality": "unchanged",
            "uncertainty": "Synthetic authored advice, not live inference.",
            **overrides,
        }
    )


def guard(**overrides: object) -> GuardResult:
    return GuardResult.model_validate(
        {
            "mandatory": False,
            "reasons": (),
            "accessible": True,
            "affected_fact_ids": (),
            "graph_fingerprint": "synthetic-graph",
            "traversal_truncated": False,
            **overrides,
        }
    )


def assert_unknown(result: Assessment, bound: AssessmentInput) -> None:
    assert result.input_key == bound.key
    assert (result.relevance, result.contradiction, result.materiality) == (
        "unknown",
        "unknown",
        "unknown",
    )
    assert result.provenance == "synthetic_cached"
    assert result.uncertainty.strip()


def test_exact_cache_hit_and_fixture_defaults(bound: AssessmentInput) -> None:
    cached = advice(bound, input_tokens=17, output_tokens=9)
    result = OfflineAssessor({bound.key: cached}).assess(bound)
    assert result is cached
    assert result.model_version == "synthetic-authored-text-pairs-v1"
    assert result.rubric_version == "synthetic-review-rubric-v1"
    assert (result.input_tokens, result.output_tokens) == (17, 9)


@pytest.mark.parametrize("field", ["input_key", "model_version", "rubric_version"])
def test_bad_cache_identity_is_unknown(bound: AssessmentInput, field: str) -> None:
    cached = advice(bound, **{field: "wrong-identity"})
    result = OfflineAssessor({bound.key: cached}).assess(bound)
    assert_unknown(result, bound)
    assert "does not match" in result.uncertainty
    assert result.model_version == MODEL_VERSION
    assert result.rubric_version == RUBRIC_VERSION


def test_cache_identity_is_checked_after_mutation(bound: AssessmentInput) -> None:
    cache = {bound.key: advice(bound)}
    assessor = OfflineAssessor(cache)
    assert assessor.assess(bound) is cache[bound.key]
    cache[bound.key] = advice(bound, rubric_version="stale")
    assert_unknown(assessor.assess(bound), bound)


def test_custom_versions_are_bound_even_for_unknown(bound: AssessmentInput) -> None:
    cached = advice(bound, model_version="custom-model", rubric_version="custom-rubric")
    assessor = OfflineAssessor({bound.key: cached}, "custom-model", "custom-rubric")
    assert assessor.assess(bound) is cached
    unknown = OfflineAssessor({}, "custom-model", "custom-rubric").assess(bound)
    assert_unknown(unknown, bound)
    assert (unknown.model_version, unknown.rubric_version) == ("custom-model", "custom-rubric")


def test_missing_and_failed_cache_are_unknown(bound: AssessmentInput) -> None:
    assert_unknown(OfflineAssessor({}).assess(bound), bound)

    class BrokenCache(dict[str, Assessment]):
        def get(self, key: str) -> Assessment | None:
            raise RuntimeError("private cache failure details")

    failed = OfflineAssessor(BrokenCache()).assess(bound)
    assert_unknown(failed, bound)
    assert "lookup failed" in failed.uncertainty
    assert "private" not in failed.uncertainty
    explicit = unknown_assessment(bound, "Synthetic fixture unavailable.")
    assert_unknown(explicit, bound)
    assert explicit.uncertainty == "Synthetic fixture unavailable."


@pytest.mark.parametrize(
    "changed",
    [
        {"subscription_id": "subscription-2"},
        {"revision": 2},
        {"assumption": "Changed assumption."},
        {"assumption_version": "v2"},
    ],
)
def test_subscription_changes_miss_cache_and_change_receipt(
    bound: AssessmentInput, changed: dict[str, object]
) -> None:
    revised = AssessmentInput(subscription=subscription(**changed), event=bound.event)
    assert revised.key != bound.key
    assert_unknown(OfflineAssessor({bound.key: advice(bound)}).assess(revised), revised)
    assert (
        plan_receipt(revised, guard(), None, NOW).receipt_id
        != plan_receipt(bound, guard(), None, NOW).receipt_id
    )


@pytest.mark.parametrize(
    "changed",
    [
        {"event_id": "event-2"},
        {"sequence": 2},
        {"new": snapshot("Checkout uses a five-second timeout.\n", "v2", value=5)},
        {"old": snapshot("Checkout uses a ten-second timeout.\n")},
    ],
)
def test_event_changes_miss_cache_and_change_receipt(
    bound: AssessmentInput, changed: dict[str, object]
) -> None:
    revised = AssessmentInput(subscription=bound.subscription, event=event(**changed))
    assert revised.key != bound.key
    assert_unknown(OfflineAssessor({bound.key: advice(bound)}).assess(revised), revised)
    assert (
        plan_receipt(revised, guard(), None, NOW).receipt_id
        != plan_receipt(bound, guard(), None, NOW).receipt_id
    )


@pytest.mark.parametrize("mandatory", [False, True])
@pytest.mark.parametrize(
    ("relevance", "contradiction", "materiality"),
    itertools.product(
        ["relevant", "irrelevant", "unknown"],
        ["contradicts", "supports", "unknown"],
        ["material", "unchanged", "unknown"],
    ),
)
def test_review_decision_matrix(
    bound: AssessmentInput,
    mandatory: bool,
    relevance: str,
    contradiction: str,
    materiality: str,
) -> None:
    cached = advice(
        bound, relevance=relevance, contradiction=contradiction, materiality=materiality
    )
    checked = guard(mandatory=mandatory, reasons=("source_changed",) if mandatory else ())
    receipt = plan_receipt(bound, checked, cached, NOW)
    expected = (
        "none"
        if (
            not mandatory
            and relevance != "unknown"
            and (contradiction, materiality) == ("supports", "unchanged")
        )
        else "review"
    )
    assert receipt.action == expected
    assert receipt.assessment is cached
    assert receipt.guard is checked


def test_absent_or_wrong_input_advice_cannot_suppress_review(bound: AssessmentInput) -> None:
    assert plan_receipt(bound, guard(), None, NOW).action == "review"
    receipt = plan_receipt(bound, guard(), advice(bound, input_key="other-input"), NOW)
    assert receipt.action == "review"
    assert receipt.assessment is not None
    assert_unknown(receipt.assessment, bound)


@pytest.mark.parametrize("mandatory", [False, True])
def test_inaccessible_guard_drops_advice(bound: AssessmentInput, mandatory: bool) -> None:
    inaccessible = guard(
        accessible=False, mandatory=mandatory, reasons=("access_denied",) if mandatory else ()
    )
    receipt = plan_receipt(bound, inaccessible, advice(bound), NOW)
    assert receipt.assessment is None
    assert receipt.action == "review"


def test_receipt_identity_excludes_time_guard_and_advice(bound: AssessmentInput) -> None:
    first = plan_receipt(bound, guard(), advice(bound), NOW)
    later = plan_receipt(bound, guard(), advice(bound), NOW + timedelta(days=5000))
    changed = plan_receipt(
        bound,
        guard(mandatory=True, reasons=("deadline",)),
        advice(bound, materiality="material"),
        NOW,
    )
    assert first.receipt_id == later.receipt_id == changed.receipt_id
    assert len({first.key, later.key, changed.key}) == 3
    assert changed.assessment is not None
    assert changed.assessment.materiality == "material"
    assert first.assessment is not None
    assert first.assessment.materiality == "unchanged"


def test_future_aware_receipt_dates_and_naive_rejection(bound: AssessmentInput) -> None:
    future = (NOW + timedelta(days=5000)).astimezone(timezone(timedelta(hours=9)))
    assert plan_receipt(bound, guard(), None, future).created_at == future
    with pytest.raises(ValidationError, match="timezone_aware"):
        plan_receipt(bound, guard(), None, future.replace(tzinfo=None))


def test_cache_file_roundtrip(bound: AssessmentInput, tmp_path: Path) -> None:
    cached = advice(bound)
    path = tmp_path / "advice.json"
    path.write_text(json.dumps({bound.key: cached.model_dump(mode="json")}), encoding="utf-8")
    assert OfflineAssessor.from_file(path).assess(bound) == cached


@pytest.mark.parametrize(
    "data",
    [b"not json", b"[]", b'{"key": {}}', b'{"key": {"schema_version": 2}}'],
)
def test_cache_parse_errors_stay_at_file_boundary(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "invalid.json"
    path.write_bytes(data)
    with pytest.raises(ValueError):
        OfflineAssessor.from_file(path)


def test_file_rejects_live_provenance(bound: AssessmentInput, tmp_path: Path) -> None:
    path = tmp_path / "live.json"
    path.write_text(
        json.dumps({bound.key: {**advice(bound).model_dump(), "provenance": "live"}}),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="synthetic_cached"):
        OfflineAssessor.from_file(path)


def test_cache_file_size_is_bounded(tmp_path: Path) -> None:
    path = tmp_path / "large.json"
    path.write_bytes(b" " * (MAX_CACHE_BYTES + 1))
    with pytest.raises(ValueError, match="exceeds"):
        OfflineAssessor.from_file(path)
    path.write_bytes(b"{}" + b" " * (MAX_CACHE_BYTES - 2))
    assert OfflineAssessor.from_file(path).cache == {}


def test_advice_never_calls_network(
    bound: AssessmentInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    network = Mock(side_effect=AssertionError("network access is forbidden"))
    monkeypatch.setattr(socket, "socket", network)
    monkeypatch.setattr(socket, "create_connection", network)
    for cache in ({}, {bound.key: advice(bound)}):
        result = OfflineAssessor(cache).assess(bound)
        plan_receipt(bound, guard(), result, NOW)
        plan_receipt(bound, guard(accessible=False), result, NOW)
    network.assert_not_called()
