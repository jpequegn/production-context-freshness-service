"""Dependency-aware operational change propagation and impact reporting."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime

from pcfs.corpus import Corpus, load_corpus
from pcfs.identifiers import stable_id
from pcfs.models import (
    Fact,
    FactRelationship,
    FactType,
    InvalidationRecord,
    RelationshipType,
    StrictModel,
)
from pcfs.store import Repository


class ImpactReport(StrictModel):
    trigger: str
    service: str
    occurred_at: datetime
    incident: str | None = None
    affected_fact_ids: tuple[str, ...]
    affected_query_keys: tuple[str, ...]
    affected_question_ids: tuple[str, ...]
    traversed_relationship_ids: tuple[str, ...]
    records: tuple[InvalidationRecord, ...]
    applied: bool


class ChangePropagator:
    def __init__(self, repository: Repository, corpus: Corpus | None = None) -> None:
        self.repository = repository
        self.corpus = corpus or load_corpus()

    def propagate(
        self,
        *,
        trigger: str,
        service: str,
        occurred_at: datetime,
        incident: str | None = None,
        source_fact_id: str | None = None,
        apply: bool = False,
    ) -> ImpactReport:
        facts = self.repository.list_facts()
        relationships = self.repository.list_relationships()
        seeds = self._seed_facts(
            facts,
            trigger=trigger,
            service=service,
            occurred_at=occurred_at,
            incident=incident,
            source_fact_id=source_fact_id,
        )
        affected_ids, traversed = expand_dependents(
            frozenset(fact.fact_id for fact in seeds), relationships
        )
        by_id = {fact.fact_id: fact for fact in facts}
        records = tuple(
            InvalidationRecord(
                invalidation_id=stable_id(
                    "invalidate",
                    {
                        "trigger": trigger,
                        "target": fact_id,
                        "occurred_at": occurred_at,
                    },
                ),
                trigger=trigger,
                target_fact_id=fact_id,
                source_fact_id=source_fact_id,
                service=service,
                occurred_at=occurred_at,
                reason=_reason(trigger, by_id.get(fact_id)),
            )
            for fact_id in sorted(affected_ids)
        )
        if apply:
            self.repository.record_invalidations(records)
        question_ids = tuple(
            sorted(
                question.id
                for question in self.corpus.questions()
                if question.service == service and _question_affected(question.kind, trigger)
            )
        )
        query_keys = tuple(
            sorted(
                {
                    f"service:{service}:fact:{by_id[fact_id].fact_type.value}"
                    for fact_id in affected_ids
                    if fact_id in by_id
                }
            )
        )
        return ImpactReport(
            trigger=trigger,
            service=service,
            occurred_at=occurred_at,
            incident=incident,
            affected_fact_ids=tuple(sorted(affected_ids)),
            affected_query_keys=query_keys,
            affected_question_ids=question_ids,
            traversed_relationship_ids=tuple(sorted(traversed)),
            records=records,
            applied=apply,
        )

    def _seed_facts(
        self,
        facts: tuple[Fact, ...],
        *,
        trigger: str,
        service: str,
        occurred_at: datetime,
        incident: str | None,
        source_fact_id: str | None,
    ) -> tuple[Fact, ...]:
        eligible = tuple(
            fact
            for fact in facts
            if fact.service == service and fact.recorded_at <= occurred_at
        )
        if source_fact_id:
            return tuple(fact for fact in eligible if fact.fact_id == source_fact_id)
        rules = {
            "metric_renamed": {FactType.METRIC_NAME, FactType.PROCEDURE},
            "ownership_changed": {FactType.OWNERSHIP},
            "incident_closed": {FactType.TEMPORARY_MITIGATION, FactType.INCIDENT_OBSERVATION},
            "permanent_fix": {FactType.TEMPORARY_MITIGATION},
            "deploy_changed": {FactType.PROCEDURE, FactType.DEPENDENCY},
        }
        fact_types = rules.get(trigger)
        if fact_types is None:
            raise ValueError(f"unsupported invalidation trigger: {trigger}")
        return tuple(
            fact
            for fact in eligible
            if fact.fact_type in fact_types
            and (incident is None or fact.incident_scope == incident)
        )


def expand_dependents(
    seeds: frozenset[str], relationships: tuple[FactRelationship, ...]
) -> tuple[frozenset[str], frozenset[str]]:
    """Traverse reverse dependency edges without revisiting cycles."""
    reverse: dict[str, list[tuple[str, str]]] = defaultdict(list)
    traversable = {
        RelationshipType.DERIVED_FROM,
        RelationshipType.DEPENDS_ON,
        RelationshipType.SUPPORTS,
    }
    for relationship in relationships:
        if relationship.relationship_type in traversable:
            reverse[relationship.target_fact_id].append(
                (relationship.source_fact_id, relationship.relationship_id)
            )
    visited = set(seeds)
    traversed: set[str] = set()
    queue = deque(sorted(seeds))
    while queue:
        target = queue.popleft()
        for dependent, relationship_id in sorted(reverse.get(target, [])):
            traversed.add(relationship_id)
            if dependent not in visited:
                visited.add(dependent)
                queue.append(dependent)
    return frozenset(visited), frozenset(traversed)


def _question_affected(kind: str, trigger: str) -> bool:
    mapping = {
        "metric_renamed": {"metric_at_open", "mitigation_after_close"},
        "ownership_changed": {"owner_at_open"},
        "incident_closed": {"mitigation_after_close"},
        "permanent_fix": {"mitigation_after_close"},
        "deploy_changed": {"metric_at_open", "undocumented_objective"},
    }
    return kind in mapping.get(trigger, set())


def _reason(trigger: str, fact: Fact | None) -> str:
    suffix = f" for {fact.predicate}" if fact else ""
    return f"Invalidated by {trigger}{suffix}."
