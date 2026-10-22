"""Dependency-level policy checks before optional semantic advice."""

from datetime import datetime

from pcfs.identifiers import content_digest
from pcfs.models import AccessContext, ContextState, Fact
from pcfs.policy import PolicyEngine
from pcfs.retrieval import ContextQuery, ContextRetriever
from pcfs.store import Repository
from pcfs.subscriptions.models import ChangeEvent, GuardResult, Subscription


def can_read(fact: Fact, access: AccessContext) -> bool:
    return fact.required_scopes <= access.scopes and (
        access.environment is None or access.environment == fact.environment
    )


def affected_dependencies(
    repository: Repository, subscription: Subscription, event: ChangeEvent
) -> tuple[str, ...]:
    facts = {fact.fact_id: fact for fact in repository.list_facts()}
    return tuple(
        dependency
        for dependency in subscription.dependencies
        if dependency in facts
        and facts[dependency].source.source_id == event.old.fact.source.source_id
    )


def evaluate_guards(
    repository: Repository,
    subscription: Subscription,
    event: ChangeEvent,
    access: AccessContext,
    at: datetime,
    *,
    projected_ids: tuple[str, ...] = (),
    graph_fingerprint: str = "flat-v1",
    traversal_truncated: bool = False,
) -> GuardResult:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("evaluation time must be timezone-aware")
    facts = {fact.fact_id: fact for fact in repository.list_facts()}
    engine = PolicyEngine()
    retriever = ContextRetriever(repository, engine)
    affected = set(affected_dependencies(repository, subscription, event))
    affected.update(set(subscription.dependencies) & set(projected_ids))
    reasons: list[str] = []
    accessible = True
    if event.available_at > at or event.observed_at > at:
        reasons.append("future_event")
    if subscription.deadline <= at:
        reasons.append("review_deadline")
    if traversal_truncated:
        reasons.append("impact_unknown_truncated")
    for snapshot in (event.old, event.new):
        stored = facts.get(snapshot.fact.fact_id)
        if stored != snapshot.fact:
            reasons.append("unverified_snapshot")
        if not can_read(snapshot.fact, access):
            accessible = False
            reasons.append("access_denied")
        if any(
            value > at
            for value in (
                snapshot.fact.recorded_at,
                snapshot.fact.observed_at,
                snapshot.fact.source.recorded_at,
                snapshot.fact.source.observed_at,
            )
        ):
            reasons.append("future_evidence")
    for dependency in subscription.dependencies:
        fact = facts.get(dependency)
        if fact is None:
            reasons.append("missing_dependency")
            continue
        if (fact.service, fact.environment) != (subscription.service, subscription.environment):
            reasons.append("scope_mismatch")
        if fact.source.key != subscription.source_versions[dependency]:
            reasons.append("source_binding_mismatch")
        if not can_read(fact, access):
            accessible = False
            reasons.append("access_denied")
            continue
        packet = retriever.retrieve(
            ContextQuery(
                service=fact.service,
                environment=fact.environment,
                fact_type=fact.fact_type,
                predicate=fact.predicate,
                question_time=at,
                access=access,
            )
        )
        # A packet can contain current evidence while another decision is stale.
        if not packet.decisions or any(
            decision.state is not ContextState.CURRENT for decision in packet.decisions
        ):
            reasons.append("dependency_not_current")
        if fact.fact_id not in {item.fact_id for item in packet.evidence}:
            reasons.append("pinned_evidence_not_current")
    if affected and event.old.fact.source.key != event.new.fact.source.key:
        reasons.append("source_version_changed")
    if affected and event.trigger is not None:
        reasons.append("structural_change")
    return GuardResult(
        mandatory=bool(reasons),
        reasons=tuple(dict.fromkeys(reasons)),
        accessible=accessible,
        affected_fact_ids=tuple(sorted(affected)),
        graph_fingerprint=content_digest(
            {
                "projection": graph_fingerprint,
                "policies": [policy.model_dump(mode="json") for policy in engine.registry.all()],
                "evaluated_at": at,
                "access": access,
            }
        ),
        traversal_truncated=traversal_truncated,
    )
