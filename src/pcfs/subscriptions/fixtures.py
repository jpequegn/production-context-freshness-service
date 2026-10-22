"""Fictional, offline-only change sequences. No model accuracy is implied."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from pcfs.identifiers import content_digest
from pcfs.models import (
    AuthorityLevel,
    Fact,
    FactType,
    ReviewState,
    Sensitivity,
    SourceClass,
    SourceSpan,
    SourceVersion,
    StrictModel,
)
from pcfs.subscriptions.models import (
    Assessment,
    AssessmentInput,
    ChangeEvent,
    EvidenceSnapshot,
    Subscription,
)

PINNED_TIME = datetime(2026, 6, 1, 12, tzinfo=UTC)
type Truth = dict[tuple[str, str], bool]
type Interpretation = tuple[
    Literal["contradicts", "supports", "unknown"],
    Literal["material", "unchanged", "unknown"],
]


class Scenario(StrictModel):
    sequence_id: str
    split: Literal["dev", "heldout"]
    subscriptions: tuple[Subscription, ...]
    facts: tuple[EvidenceSnapshot, ...]
    events: tuple[ChangeEvent, ...]


@dataclass(frozen=True)
class _Case:
    fact_type: FactType
    documents: tuple[str, str, str]
    values: tuple[str, str, str]
    interpretations: tuple[Interpretation | None, Interpretation | None]


_CASES = {
    "editorial": _Case(
        FactType.PROCEDURE,
        (
            "{entity}: drain before restart.",
            "{entity}: Drain before restart.",
            "{entity}: Drain before restart. Typo corrected.",
        ),
        ("drain first", "drain first", "drain first"),
        (("supports", "unchanged"), ("supports", "unchanged")),
    ),
    "procedure-reversal": _Case(
        FactType.PROCEDURE,
        (
            "{entity}: drain before restart.",
            "{entity}: restart without draining.",
            "{entity}: drain before restarting; bypass is withdrawn.",
        ),
        ("drain first", "restart immediately", "drain first"),
        (("contradicts", "material"), ("contradicts", "material")),
    ),
    "ownership": _Case(
        FactType.OWNERSHIP,
        (
            "{entity}: team-fern owns escalation.",
            "{entity}: team-iris owns escalation.",
            "{entity}: Team-iris owns escalation.",
        ),
        ("team-fern", "team-iris", "team-iris"),
        (("contradicts", "material"), ("supports", "unchanged")),
    ),
    "conflict": _Case(
        FactType.PROCEDURE,
        (
            "{entity}: the approved restart requires draining.",
            "{entity}: the approved restart forbids draining.",
            "{entity}: both draining and bypass remain approved by different maintainers.",
        ),
        ("drain first", "do not drain", "unresolved procedures"),
        (("contradicts", "material"), ("unknown", "material")),
    ),
    "access-revocation": _Case(
        FactType.PROCEDURE,
        ("{entity}: restricted restart instructions.",) * 3,
        ("drain first",) * 3,
        (("supports", "unchanged"), ("supports", "unchanged")),
    ),
    "hard-expiry": _Case(
        FactType.TEMPORARY_MITIGATION,
        ("{entity}: temporary bypass expires at 12:00:30 UTC.",) * 3,
        ("temporary bypass",) * 3,
        (("supports", "unchanged"), ("supports", "unchanged")),
    ),
    "metric-rename": _Case(
        FactType.METRIC_NAME,
        (
            "{entity}: alert on queue_depth.",
            "{entity}: queue_depth is now backlog_items.",
            "{entity}: backlog_items measures queued items, not queued bytes.",
        ),
        ("queue_depth", "backlog_items", "backlog_items"),
        (("contradicts", "material"), ("supports", "unchanged")),
    ),
    "late-event": _Case(
        FactType.PROCEDURE,
        (
            "{entity}: acknowledge before failover.",
            "{entity}: fail over before acknowledgement.",
            "{entity}: fail over first; acknowledgement may follow.",
        ),
        ("acknowledge first", "fail over first", "fail over first"),
        (("contradicts", "material"), ("supports", "unchanged")),
    ),
    "provider-failure": _Case(
        FactType.PROCEDURE,
        (
            "{entity}: use the primary recovery endpoint.",
            "{entity}: the primary endpoint is retired; use the standby.",
            "{entity}: use the standby recovery endpoint. Formatting corrected.",
        ),
        ("primary endpoint", "standby endpoint", "standby endpoint"),
        (None, None),
    ),
}
_ENTITIES = ("larch", "mistral", "vesper")
_HELDOUT = frozenset({"metric-rename", "late-event", "provider-failure"})
_TEXT_INTERPRETATIONS = {
    (
        case.documents[index].format(entity=entity),
        case.documents[index + 1].format(entity=entity),
    ): interpretation
    for case in _CASES.values()
    for entity in _ENTITIES
    for index, interpretation in enumerate(case.interpretations)
    if interpretation is not None
}
_DEMO_DOCUMENTS = (
    "Synthetic checkout: drain before restart.",
    "Synthetic checkout: Drain before restart. Heading corrected.",
    "Synthetic checkout: restart without draining; draining is prohibited.",
    "Synthetic checkout: team-fern owns escalation.",
    "Synthetic checkout: team-iris owns escalation.",
)
_TEXT_INTERPRETATIONS.update(
    {
        (_DEMO_DOCUMENTS[0], _DEMO_DOCUMENTS[1]): ("supports", "unchanged"),
        (_DEMO_DOCUMENTS[1], _DEMO_DOCUMENTS[2]): ("contradicts", "material"),
        (_DEMO_DOCUMENTS[3], _DEMO_DOCUMENTS[4]): ("contradicts", "material"),
    }
)


def _snapshot(
    sequence_id: str,
    slot: str,
    version: int,
    document: str,
    value: str,
    fact_type: FactType = FactType.PROCEDURE,
    *,
    restricted: bool = False,
    expires: bool = False,
    predicate: str | None = None,
) -> EvidenceSnapshot:
    source = SourceVersion(
        source_id=f"synthetic/{sequence_id}/{slot}",
        version=f"v{version}",
        source_class=(
            SourceClass.SERVICE_CATALOG if fact_type is FactType.OWNERSHIP else SourceClass.RUNBOOK
        ),
        uri=f"synthetic://subscriptions/{sequence_id}/{slot}",
        content_digest=content_digest({"text": document}),
        observed_at=PINNED_TIME,
        recorded_at=PINNED_TIME,
    )
    return EvidenceSnapshot(
        document=document,
        fact=Fact(
            subject=f"synthetic-{sequence_id}",
            predicate=predicate or f"{slot}-{fact_type.value}",
            value=value,
            fact_type=fact_type,
            source=source,
            source_span=SourceSpan(line_start=1, line_end=1, heading="Synthetic fixture"),
            observed_at=PINNED_TIME,
            valid_from=PINNED_TIME,
            valid_to=PINNED_TIME + timedelta(seconds=30) if expires else None,
            recorded_at=PINNED_TIME,
            authority=AuthorityLevel.AUTHORITATIVE,
            owner="synthetic-maintainers",
            review_state=ReviewState.REVIEWED,
            environment="fixture",
            service=f"synthetic-{sequence_id}",
            sensitivity=Sensitivity.RESTRICTED if restricted else Sensitivity.INTERNAL,
            required_scopes=frozenset({"synthetic:restricted"}) if restricted else frozenset(),
            freshness_policy_id=f"{fact_type.value}-v1",
        ),
    )


def _subscription(
    sequence_id: str,
    name: str,
    dependencies: tuple[EvidenceSnapshot, ...],
    assumption: str,
) -> Subscription:
    return Subscription(
        subscription_id=f"{sequence_id}/{name}",
        conclusion_id=f"synthetic-conclusion/{sequence_id}/{name}",
        revision=1,
        assumption=assumption,
        assumption_version="synthetic-v1",
        dependencies=tuple(item.fact.fact_id for item in dependencies),
        source_versions={item.fact.fact_id: item.fact.source.key for item in dependencies},
        service=f"synthetic-{sequence_id}",
        environment="fixture",
        reviewer="synthetic-reviewer",
        deadline=PINNED_TIME + timedelta(days=1),
        debounce_seconds=120,
    )


def demo_scenario() -> Scenario:
    """Four deliveries, one affected conclusion, no external delivery endpoint."""
    sequence_id = "demo"
    procedure = _snapshot(sequence_id, "procedure", 1, _DEMO_DOCUMENTS[0], "drain first")
    editorial = _snapshot(sequence_id, "procedure", 2, _DEMO_DOCUMENTS[1], "drain first")
    reversal = _snapshot(sequence_id, "procedure", 3, _DEMO_DOCUMENTS[2], "restart immediately")
    owner = _snapshot(sequence_id, "owner", 1, _DEMO_DOCUMENTS[3], "team-fern", FactType.OWNERSHIP)
    new_owner = _snapshot(
        sequence_id, "owner", 2, _DEMO_DOCUMENTS[4], "team-iris", FactType.OWNERSHIP
    )
    originals = (procedure, owner) + tuple(
        _snapshot(sequence_id, name, 1, f"Synthetic {name}: {value}.", value, fact_type)
        for name, value, fact_type in (
            ("latency", "p95 below 200ms", FactType.SLO),
            ("latency-metric", "latency_ms", FactType.METRIC_NAME),
            ("archive", "retain 30 days", FactType.PROCEDURE),
            ("archive-owner", "team-elm", FactType.OWNERSHIP),
        )
    )
    subscriptions = (
        _subscription(
            sequence_id, "restart", originals[:2], "Drain before restart and escalate to team-fern."
        ),
        _subscription(
            sequence_id,
            "latency",
            originals[2:4],
            "Latency stays below the stated objective using latency_ms.",
        ),
        _subscription(
            sequence_id, "archive", originals[4:], "Team-elm retains archives for 30 days."
        ),
    )
    events = tuple(
        ChangeEvent(
            event_id=f"demo/{name}",
            old=old,
            new=new,
            sequence=1 if name == "ownership" else index,
            observed_at=PINNED_TIME + timedelta(seconds=10 * index),
            available_at=PINNED_TIME + timedelta(seconds=10 * index),
            trigger=trigger,
        )
        for index, (name, old, new, trigger) in enumerate(
            (
                ("editorial", procedure, editorial, None),
                ("procedure-reversal", editorial, reversal, None),
                ("ownership", owner, new_owner, "ownership_changed"),
            ),
            start=1,
        )
    )
    return Scenario(
        sequence_id=sequence_id,
        split="dev",
        subscriptions=subscriptions,
        facts=originals,
        events=(*events, events[1]),
    )


def _evaluation_sequence(
    family: str, entity: str, expected: tuple[bool, bool]
) -> tuple[Scenario, Truth]:
    case = _CASES[family]
    sequence_id = f"{family}/{entity}"
    snapshots = tuple(
        _snapshot(
            sequence_id,
            "evidence",
            index + 1,
            document.format(entity=entity),
            value,
            case.fact_type,
            restricted=family == "access-revocation" and index > 0,
            expires=family == "hard-expiry",
        )
        for index, (document, value) in enumerate(zip(case.documents, case.values, strict=True))
    )
    originals = (snapshots[0],)
    if family == "conflict":
        corroboration = _snapshot(
            sequence_id,
            "corroboration",
            1,
            f"{entity}: independent maintainer requires draining.",
            "drain first",
            predicate=snapshots[0].fact.predicate,
        )
        originals += (corroboration,)
    subscription = _subscription(
        sequence_id, "conclusion", originals, case.documents[0].format(entity=entity)
    )
    trigger = {"ownership": "ownership_changed", "metric-rename": "metric_renamed"}.get(family)
    events = tuple(
        ChangeEvent(
            event_id=f"{sequence_id}/{index}",
            old=snapshots[index - 1],
            new=snapshots[index],
            sequence=index,
            observed_at=PINNED_TIME + timedelta(seconds=60 + index * 10),
            available_at=PINNED_TIME
            + timedelta(seconds=180 if family == "late-event" and index == 1 else 60 + index * 10),
            trigger=trigger,
        )
        for index in (1, 2)
    )
    if family == "late-event":
        events = tuple(reversed(events))
    scenario = Scenario(
        sequence_id=sequence_id,
        split="heldout" if family in _HELDOUT else "dev",
        subscriptions=(subscription,),
        facts=originals,
        events=events,
    )
    truth = {
        (event.event_id, subscription.subscription_id): expected[event.sequence - 1]
        for event in events
    }
    return scenario, truth


def _evaluation() -> tuple[tuple[Scenario, ...], Truth]:
    demo = demo_scenario()
    truth = {
        (event.event_id, subscription.subscription_id): (
            subscription.subscription_id == "demo/restart"
            and event.event_id in {"demo/procedure-reversal", "demo/ownership"}
        )
        for event in demo.events
        for subscription in demo.subscriptions
    }
    scenarios = [demo]
    # Authored review labels include mandatory structural changes, not just text advice.
    expected = {
        "editorial": (False, False),
        "procedure-reversal": (True, True),
        "ownership": (True, False),
        "conflict": (True, True),
        "access-revocation": (True, True),
        "hard-expiry": (True, True),
        "metric-rename": (True, False),
        "late-event": (True, False),
        "provider-failure": (True, False),
    }
    for family, labels in expected.items():
        for entity in _ENTITIES:
            scenario, labels_by_pair = _evaluation_sequence(family, entity, labels)
            scenarios.append(scenario)
            truth.update(labels_by_pair)
    return tuple(scenarios), truth


def evaluation_scenarios() -> tuple[Scenario, ...]:
    return _evaluation()[0]


def evaluation_truth() -> Truth:
    """Return evaluator-only (event_id, subscription_id) material-review labels."""
    return _evaluation()[1]


def offline_cache(scenario: Scenario) -> dict[str, Assessment]:
    """Exact-input, authored synthetic advice. Unknown text pairs are cache misses."""
    cache = {}
    for subscription in scenario.subscriptions:
        dependency_sources = {
            item.fact.source.source_id
            for item in scenario.facts
            if item.fact.fact_id in subscription.dependencies
        }
        for event in scenario.events:
            if (subscription.service, subscription.environment) != (
                event.new.fact.service,
                event.new.fact.environment,
            ):
                continue
            interpretation = _TEXT_INTERPRETATIONS.get((event.old.document, event.new.document))
            if interpretation is None:
                continue
            relevant = event.new.fact.source.source_id in dependency_sources
            contradiction, materiality = interpretation if relevant else ("unknown", "unchanged")
            assessment_input = AssessmentInput(subscription=subscription, event=event)
            cache[assessment_input.key] = Assessment(
                input_key=assessment_input.key,
                model_version="synthetic-authored-text-pairs-v1",
                rubric_version="synthetic-review-rubric-v1",
                relevance="relevant" if relevant else "irrelevant",
                contradiction=contradiction,
                materiality=materiality,
                provenance="synthetic_cached",
                uncertainty="Authored synthetic interpretation, not real model output or accuracy.",
            )
    return cache
