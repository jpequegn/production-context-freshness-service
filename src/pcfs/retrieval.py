"""Point-in-time retrieval of compact, provenance-backed context packets."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from enum import StrEnum

from pydantic import Field

from pcfs.models import (
    AccessContext,
    ContextState,
    Fact,
    FactType,
    RelationshipType,
    SourceSpan,
    StrictModel,
)
from pcfs.policy import PolicyDecision, PolicyEngine
from pcfs.store import Repository


class QueryMode(StrEnum):
    STRICT = "strict"
    DIAGNOSTIC = "diagnostic"


class ContextQuery(StrictModel):
    service: str
    question_time: datetime
    access: AccessContext
    environment: str = "production"
    fact_type: FactType | None = None
    predicate: str | None = None
    incident: str | None = None
    release: str | None = None
    text: str | None = None
    mode: QueryMode = QueryMode.STRICT
    limit: int = Field(default=20, ge=1, le=100)


class Citation(StrictModel):
    source_key: str
    source_id: str
    source_version: str
    uri: str
    content_digest: str
    source_span: SourceSpan | None
    recorded_at: datetime
    valid_from: datetime
    valid_to: datetime | None
    freshness_state: ContextState


class EvidenceItem(StrictModel):
    fact_id: str
    subject: str
    predicate: str
    value: str | int | float | bool | list[str] | dict[str, str]
    fact_type: FactType
    authority: int
    score: int
    included: bool
    state: ContextState
    citation: Citation
    reason_codes: tuple[str, ...]


class ContextPacket(StrictModel):
    query: ContextQuery
    state: ContextState
    evidence: tuple[EvidenceItem, ...]
    diagnostic_evidence: tuple[EvidenceItem, ...] = ()
    warnings: tuple[str, ...] = ()
    decisions: tuple[PolicyDecision, ...] = ()
    abstained: bool


class ContextRetriever:
    def __init__(self, repository: Repository, policy_engine: PolicyEngine | None = None) -> None:
        self.repository = repository
        self.policy_engine = policy_engine or PolicyEngine()

    def retrieve(
        self,
        query: ContextQuery,
        *,
        invalidated_fact_ids: frozenset[str] = frozenset(),
    ) -> ContextPacket:
        invalidated_fact_ids = (
            invalidated_fact_ids | self.repository.invalidated_fact_ids(query.question_time)
        )
        facts = self.repository.list_facts()
        relationships = self.repository.list_relationships()
        structural = tuple(fact for fact in facts if self._matches_structure(fact, query))
        warnings: list[str] = []
        if not structural:
            return ContextPacket(
                query=query,
                state=ContextState.UNKNOWN,
                evidence=(),
                warnings=("No facts match the requested operational scope.",),
                abstained=True,
            )

        knowable = tuple(fact for fact in structural if fact.recorded_at <= query.question_time)
        if len(knowable) < len(structural):
            warnings.append("Future-recorded evidence was withheld from this historical query.")
        accessible = tuple(fact for fact in knowable if self._can_access(fact, query.access))
        if len(accessible) < len(knowable):
            warnings.append("One or more facts were withheld by access policy.")
        if not accessible:
            return ContextPacket(
                query=query,
                state=ContextState.INSUFFICIENT_EVIDENCE,
                evidence=(),
                warnings=tuple(warnings or ["No evidence was knowable at the question time."]),
                abstained=True,
            )

        groups: dict[tuple[str, str], list[Fact]] = defaultdict(list)
        for fact in accessible:
            groups[(fact.predicate, fact.freshness_policy_id)].append(fact)

        decisions: list[PolicyDecision] = []
        selected_items: list[EvidenceItem] = []
        diagnostic_items: list[EvidenceItem] = []
        for group in groups.values():
            group_ids = {fact.fact_id for fact in group}
            superseded = {
                relationship.target_fact_id
                for relationship in relationships
                if relationship.relationship_type is RelationshipType.SUPERSEDES
                and relationship.recorded_at <= query.question_time
                and relationship.source_fact_id in group_ids
            }
            active = tuple(fact for fact in group if fact.fact_id not in superseded)
            active_relationships = tuple(
                relationship
                for relationship in relationships
                if relationship.source_fact_id in group_ids
                and relationship.target_fact_id in group_ids
            )
            decision = self.policy_engine.evaluate(
                active,
                question_time=query.question_time,
                relationships=active_relationships,
                invalidated_fact_ids=invalidated_fact_ids,
            )
            decisions.append(decision)
            selected_ids = set(decision.selected_fact_ids)
            reason_codes = tuple(reason.code for reason in decision.reasons)
            for fact in group:
                included = fact.fact_id in selected_ids and decision.state is ContextState.CURRENT
                item_state = (
                    ContextState.STALE if fact.fact_id in superseded else decision.state
                )
                item = self._item(fact, query, item_state, included, reason_codes)
                if included:
                    selected_items.append(item)
                if query.mode is QueryMode.DIAGNOSTIC:
                    diagnostic_items.append(item)

        state = _aggregate_state(tuple(decision.state for decision in decisions))
        if any(decision.state is not ContextState.CURRENT for decision in decisions):
            warnings.append("Some requested context is stale, disputed, or unavailable.")
        selected_items.sort(key=_item_sort_key)
        diagnostic_items.sort(key=_item_sort_key)
        evidence = tuple(selected_items[: query.limit])
        diagnostics = tuple(diagnostic_items[: query.limit])
        return ContextPacket(
            query=query,
            state=state,
            evidence=evidence,
            diagnostic_evidence=diagnostics,
            warnings=tuple(dict.fromkeys(warnings)),
            decisions=tuple(decisions),
            abstained=not evidence,
        )

    def _matches_structure(self, fact: Fact, query: ContextQuery) -> bool:
        return (
            fact.service == query.service
            and fact.environment == query.environment
            and (query.fact_type is None or fact.fact_type is query.fact_type)
            and (query.predicate is None or fact.predicate == query.predicate)
            and (query.incident is None or fact.incident_scope == query.incident)
            and (query.release is None or fact.release_scope == query.release)
        )

    def _can_access(self, fact: Fact, access: AccessContext) -> bool:
        if access.environment is not None and access.environment != fact.environment:
            return False
        return fact.required_scopes <= access.scopes

    def _item(
        self,
        fact: Fact,
        query: ContextQuery,
        state: ContextState,
        included: bool,
        reason_codes: tuple[str, ...],
    ) -> EvidenceItem:
        return EvidenceItem(
            fact_id=fact.fact_id,
            subject=fact.subject,
            predicate=fact.predicate,
            value=fact.value,
            fact_type=fact.fact_type,
            authority=int(fact.authority),
            score=_keyword_score(query.text, fact),
            included=included,
            state=state,
            citation=Citation(
                source_key=fact.source.key,
                source_id=fact.source.source_id,
                source_version=fact.source.version,
                uri=fact.source.uri,
                content_digest=fact.source.content_digest,
                source_span=fact.source_span,
                recorded_at=fact.recorded_at,
                valid_from=fact.valid_from,
                valid_to=fact.valid_to,
                freshness_state=state,
            ),
            reason_codes=reason_codes,
        )


def _keyword_score(text: str | None, fact: Fact) -> int:
    if not text:
        return 0
    query_tokens = set(re.findall(r"[a-z0-9_]+", text.lower()))
    fact_text = f"{fact.subject} {fact.predicate} {fact.value}".lower()
    fact_tokens = set(re.findall(r"[a-z0-9_]+", fact_text))
    return len(query_tokens & fact_tokens)


def _item_sort_key(item: EvidenceItem) -> tuple[int, str, str]:
    return (-item.score, item.predicate, item.fact_id)


def _aggregate_state(states: tuple[ContextState, ...]) -> ContextState:
    precedence = (
        ContextState.DISPUTED,
        ContextState.CURRENT,
        ContextState.STALE,
        ContextState.INSUFFICIENT_EVIDENCE,
        ContextState.UNKNOWN,
    )
    return next((state for state in precedence if state in states), ContextState.UNKNOWN)
