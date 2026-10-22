import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from pcfs.identifiers import content_digest
from pcfs.models import AuthorityLevel, Fact, FactType, Sensitivity, SourceClass, StrictModel
from pcfs.subscriptions import (
    Assessment,
    AssessmentInput,
    ChangeEvent,
    EvidenceSnapshot,
    GuardResult,
    Receipt,
    ReviewDisposition,
    Subscription,
)

NOW = datetime(2026, 10, 22, tzinfo=UTC)


def snapshot(
    document: str = "Checkout uses a ten-second timeout.",
    version: str = "v1",
    **fact_overrides: object,
) -> EvidenceSnapshot:
    values: dict[str, object] = {
        "subject": "checkout",
        "predicate": "timeout_seconds",
        "value": 10,
        "fact_type": FactType.PROCEDURE,
        "source": {
            "source_id": "runbook/checkout",
            "version": version,
            "source_class": SourceClass.RUNBOOK,
            "uri": "synthetic://checkout/timeout",
            "content_digest": content_digest({"text": document}),
            "observed_at": NOW,
            "recorded_at": NOW,
        },
        "observed_at": NOW,
        "valid_from": NOW,
        "recorded_at": NOW,
        "authority": AuthorityLevel.MAINTAINER,
        "owner": "checkout-team",
        "environment": "production",
        "service": "checkout",
        "freshness_policy_id": "procedure-v1",
    }
    values.update(fact_overrides)
    return EvidenceSnapshot(fact=Fact.model_validate(values), document=document)


def subscription(**overrides: object) -> Subscription:
    old = snapshot()
    values: dict[str, object] = {
        "subscription_id": "subscription-1",
        "conclusion_id": "conclusion-1",
        "revision": 1,
        "assumption": "Checkout uses a ten-second timeout.",
        "assumption_version": "v1",
        "dependencies": (old.fact.fact_id,),
        "source_versions": {old.fact.fact_id: old.fact.source.key},
        "service": "checkout",
        "environment": "production",
        "reviewer": "reviewer-1",
        "deadline": NOW + timedelta(days=1),
    }
    values.update(overrides)
    return Subscription.model_validate(values)


def event(**overrides: object) -> ChangeEvent:
    values: dict[str, object] = {
        "event_id": "event-1",
        "old": snapshot(),
        "new": snapshot("Checkout uses a five-second timeout.", "v2", value=5),
        "observed_at": NOW,
        "available_at": NOW + timedelta(seconds=1),
        "sequence": 1,
    }
    values.update(overrides)
    return ChangeEvent.model_validate(values)


@pytest.fixture
def contracts() -> tuple[StrictModel, ...]:
    bound = AssessmentInput(subscription=subscription(), event=event())
    assessment = Assessment(
        input_key=bound.key,
        model_version="synthetic-v1",
        rubric_version="rubric-v1",
        relevance="relevant",
        contradiction="contradicts",
        materiality="unknown",
    )
    guard = GuardResult(
        mandatory=True,
        reasons=("source_changed",),
        accessible=True,
        affected_fact_ids=bound.subscription.dependencies,
        graph_fingerprint=content_digest({"facts": bound.subscription.dependencies}),
        traversal_truncated=False,
    )
    receipt = Receipt(
        receipt_id="receipt-1",
        subscription_id="subscription-1",
        revision=1,
        event_id="event-1",
        input_key=bound.key,
        guard=guard,
        assessment=assessment,
        action="review",
        created_at=NOW + timedelta(seconds=2),
    )
    disposition = ReviewDisposition(
        command_id="command-1",
        receipt_id="receipt-1",
        reviewer="reviewer-1",
        decision="deferred",
        rationale="Waiting for the rollout to finish.",
        at=NOW + timedelta(minutes=1),
    )
    return (
        bound.event.old,
        bound.subscription,
        bound.event,
        bound,
        assessment,
        guard,
        receipt,
        disposition,
    )


@pytest.mark.parametrize("index", range(8))
def test_contract_roundtrip_identity_and_immutability(
    contracts: tuple[StrictModel, ...], index: int
) -> None:
    contract = contracts[index]
    restored = type(contract).model_validate_json(contract.model_dump_json())
    assert restored == contract
    assert restored.key == contract.key
    assert len(contract.key) == 64
    assert contract.model_dump()["schema_version"] == 1
    with pytest.raises(ValidationError, match="extra_forbidden"):
        type(contract).model_validate({**contract.model_dump(), "unexpected": True})
    with pytest.raises(ValidationError, match="frozen_instance"):
        contract.schema_version = 1
    with pytest.raises(ValidationError, match="literal_error"):
        type(contract).model_validate({**contract.model_dump(), "schema_version": 2})


def test_snapshot_binds_exact_text_and_fact_metadata() -> None:
    evidence = snapshot("abc")
    assert evidence.digest == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert evidence.key != evidence.digest
    assert evidence.key != snapshot("abc", owner="another-team").key
    with pytest.raises(ValidationError, match="exact text envelope"):
        EvidenceSnapshot(fact=evidence.fact, document="abc\n")
    legacy_source = evidence.fact.source.model_dump()
    legacy_source["content_digest"] = content_digest({"document": "abc"})
    with pytest.raises(ValidationError, match="exact text envelope"):
        snapshot("abc", source=legacy_source)


def test_snapshot_raw_digest_preserves_utf8_and_newlines() -> None:
    document = "\u00e9\r\n"
    evidence = snapshot(document)
    assert evidence.document == document
    assert evidence.digest == hashlib.sha256(b"\xc3\xa9\r\n").hexdigest()
    assert evidence.key != snapshot("\u00e9\n").key


def test_snapshot_size_limit_counts_utf8_bytes() -> None:
    assert len(snapshot("a" * 65536).document) == 65536
    assert len(snapshot("\u00e9" * 32768).document.encode("utf-8")) == 65536
    for document in ("a" * 65537, "\u00e9" * 32769):
        with pytest.raises(ValidationError, match="64 KiB"):
            snapshot(document)


@pytest.mark.parametrize(
    "overrides",
    [
        {"dependencies": ()},
        {"dependencies": ("fact-1", "fact-1"), "source_versions": {"fact-1": "src-1"}},
        {"source_versions": {}},
        {"source_versions": {"unrelated": "src-1"}},
        {"dependencies": ("",), "source_versions": {"": "src-1"}},
        {"dependencies": ("fact-1",), "source_versions": {"fact-1": ""}},
        {"revision": 0},
        {"revision": True},
        {"debounce_seconds": -1},
        {"debounce_seconds": 3601},
        {"debounce_seconds": 1.5},
        {"assumption": ""},
        {"reviewer": ""},
    ],
)
def test_subscription_rejects_invalid_dependencies_and_bounds(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        subscription(**overrides)


def test_subscription_defaults_and_debounce_boundaries() -> None:
    assert subscription().debounce_seconds == 60
    assert subscription(debounce_seconds=0).debounce_seconds == 0
    assert subscription(debounce_seconds=3600).debounce_seconds == 3600
    current = subscription()
    with pytest.raises(ValidationError, match="exactly"):
        subscription(source_versions={**current.source_versions, "extra": "src-extra"})


@pytest.mark.parametrize("name", ["subject", "predicate", "service", "environment"])
def test_event_rejects_changed_fact_identity(name: str) -> None:
    with pytest.raises(ValidationError, match=f"same {name}"):
        event(new=snapshot("New passage.", "v2", **{name: "different"}))


def test_event_rejects_changed_source_identity_and_reused_version() -> None:
    other_source = snapshot().fact.source.model_dump()
    other_source["source_id"] = "runbook/another"
    with pytest.raises(ValidationError, match="same source_id"):
        event(new=snapshot(source=other_source))
    with pytest.raises(ValidationError, match="cannot change document bytes"):
        event(new=snapshot("Different passage.", "v1"))
    assert event(new=snapshot()).old.digest == event(new=snapshot()).new.digest


@pytest.mark.parametrize(
    ("old_level", "new_level", "expected"),
    [
        (Sensitivity.PUBLIC, Sensitivity.PUBLIC, Sensitivity.PUBLIC),
        (Sensitivity.PUBLIC, Sensitivity.INTERNAL, Sensitivity.INTERNAL),
        (Sensitivity.RESTRICTED, Sensitivity.PUBLIC, Sensitivity.RESTRICTED),
        (Sensitivity.PUBLIC, Sensitivity.RESTRICTED, Sensitivity.RESTRICTED),
    ],
)
def test_event_sensitivity_cannot_be_downgraded(
    old_level: Sensitivity, new_level: Sensitivity, expected: Sensitivity
) -> None:
    changed = event(
        old=snapshot(sensitivity=old_level, required_scopes={"ops"}),
        new=snapshot("New passage.", "v2", sensitivity=new_level, required_scopes={"ops"}),
    )
    assert changed.sensitivity is expected
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ChangeEvent.model_validate({**changed.model_dump(), "sensitivity": "public"})


@pytest.mark.parametrize("overrides", [{"sequence": 0}, {"sequence": True}, {"trigger": ""}])
def test_event_rejects_invalid_sequence_and_trigger(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        event(**overrides)


def test_event_availability_cannot_precede_observation() -> None:
    with pytest.raises(ValidationError, match="cannot precede"):
        event(available_at=NOW - timedelta(seconds=1))


@pytest.mark.parametrize(
    ("index", "field"),
    [(1, "deadline"), (2, "observed_at"), (2, "available_at"), (6, "created_at"), (7, "at")],
)
def test_contract_timestamps_require_timezone(
    contracts: tuple[StrictModel, ...], index: int, field: str
) -> None:
    contract = contracts[index]
    with pytest.raises(ValidationError, match="timezone_aware"):
        type(contract).model_validate({**contract.model_dump(), field: NOW.replace(tzinfo=None)})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("subscription_id", "subscription-2"),
        ("conclusion_id", "conclusion-2"),
        ("revision", 2),
        ("assumption", "A revised operational assumption."),
        ("assumption_version", "v2"),
        ("reviewer", "reviewer-2"),
        ("deadline", NOW + timedelta(days=2)),
        ("debounce_seconds", 120),
    ],
)
def test_assessment_input_identity_binds_subscription(field: str, value: object) -> None:
    original = AssessmentInput(subscription=subscription(), event=event())
    revised = AssessmentInput(subscription=subscription(**{field: value}), event=event())
    assert original.key != revised.key


def test_assessment_input_identity_binds_evidence_and_source_versions() -> None:
    original = AssessmentInput(subscription=subscription(), event=event())
    revised_event = event(new=snapshot("A new exact passage.", "v3", value=3))
    revised_versions = {key: "another-source-key" for key in subscription().dependencies}
    variants = [
        AssessmentInput(subscription=subscription(), event=revised_event),
        AssessmentInput(subscription=subscription(source_versions=revised_versions), event=event()),
        AssessmentInput(subscription=subscription(), event=event(sequence=2)),
        AssessmentInput(subscription=subscription(), event=event(trigger="deployment")),
        AssessmentInput(subscription=subscription(), event=event(event_id="event-2")),
        AssessmentInput(subscription=subscription(), event=event(old=snapshot(owner="other-team"))),
    ]
    assert all(original.key != variant.key for variant in variants)


@pytest.mark.parametrize("field", ["service", "environment"])
def test_assessment_input_rejects_cross_scope_event(field: str) -> None:
    with pytest.raises(ValidationError, match=f"same {field}"):
        AssessmentInput(subscription=subscription(**{field: "another"}), event=event())


def test_assessment_unknown_has_visible_uncertainty(contracts: tuple[StrictModel, ...]) -> None:
    assessment = Assessment.model_validate(contracts[4].model_dump())
    assert assessment.materiality == "unknown"
    assert assessment.uncertainty == "Not assessed."
    assert assessment.input_tokens is None
    assert assessment.output_tokens is None
    cached = Assessment.model_validate(
        {
            **assessment.model_dump(),
            "input_tokens": 0,
            "output_tokens": 12,
            "uncertainty": "The rollout status is unavailable.",
        }
    )
    assert (cached.input_tokens, cached.output_tokens) == (0, 12)
    assert cached.uncertainty == "The rollout status is unavailable."
    assert cached.key != assessment.key


@pytest.mark.parametrize(
    "overrides",
    [
        {"relevance": "probably"},
        {"contradiction": "irrelevant"},
        {"materiality": "high"},
        {"provenance": "live"},
        {"input_tokens": -1},
        {"output_tokens": True},
        {"input_tokens": "12"},
        {"uncertainty": ""},
        {"confidence": 0.9},
    ],
)
def test_assessment_rejects_invalid_contract(
    contracts: tuple[StrictModel, ...], overrides: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        Assessment.model_validate({**contracts[4].model_dump(), **overrides})


def test_guard_and_receipt_reject_inconsistent_decisions(
    contracts: tuple[StrictModel, ...],
) -> None:
    guard = contracts[5].model_dump()
    with pytest.raises(ValidationError, match="at least one reason"):
        GuardResult.model_validate({**guard, "reasons": ()})
    with pytest.raises(ValidationError, match="duplicates"):
        GuardResult.model_validate({**guard, "affected_fact_ids": ("fact-1", "fact-1")})
    receipt = contracts[6].model_dump()
    with pytest.raises(ValidationError, match="require review"):
        Receipt.model_validate({**receipt, "action": "none"})
    with pytest.raises(ValidationError, match="input_key must match"):
        Receipt.model_validate({**receipt, "input_key": "another-input"})
    with pytest.raises(ValidationError, match="inaccessible"):
        Receipt.model_validate({**receipt, "guard": {**guard, "accessible": False}})
    inaccessible = Receipt.model_validate(
        {**receipt, "guard": {**guard, "accessible": False}, "assessment": None}
    )
    assert inaccessible.action == "review"
    assert inaccessible.assessment is None
    no_action = Receipt.model_validate(
        {
            **receipt,
            "guard": {**guard, "mandatory": False, "reasons": ()},
            "assessment": None,
            "action": "none",
        }
    )
    assert no_action.action == "none"


@pytest.mark.parametrize("decision", ["confirmed", "dismissed", "deferred"])
def test_review_disposition_decisions(contracts: tuple[StrictModel, ...], decision: str) -> None:
    disposition = ReviewDisposition.model_validate(
        {**contracts[7].model_dump(), "decision": decision}
    )
    assert disposition.decision == decision


@pytest.mark.parametrize("rationale", ["", " \n\t"])
def test_review_requires_nonblank_rationale(
    contracts: tuple[StrictModel, ...], rationale: str
) -> None:
    with pytest.raises(ValidationError):
        ReviewDisposition.model_validate({**contracts[7].model_dump(), "rationale": rationale})
