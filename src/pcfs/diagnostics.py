"""Database attention diagnostics for operators and automation."""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from pcfs.models import AccessContext, ContextState, FactType, StrictModel
from pcfs.retrieval import ContextQuery, ContextRetriever
from pcfs.store import Repository


class StatusReport(StrictModel):
    as_of: datetime
    source_count: int
    fact_count: int
    invalidation_count: int
    service_count: int
    state_counts: dict[str, int]
    missing_context: tuple[str, ...]
    needs_attention: bool
    reasons: tuple[str, ...]


def inspect_status(repository: Repository, *, as_of: datetime) -> StatusReport:
    facts = repository.list_facts()
    services = sorted({fact.service for fact in facts})
    required_types = (
        FactType.OWNERSHIP,
        FactType.SLO,
        FactType.METRIC_NAME,
        FactType.PROCEDURE,
        FactType.FLAG,
    )
    all_scopes = frozenset(scope for fact in facts for scope in fact.required_scopes)
    states: Counter[str] = Counter()
    missing: list[str] = []
    retriever = ContextRetriever(repository)
    for service in services:
        for fact_type in required_types:
            packet = retriever.retrieve(
                ContextQuery(
                    service=service,
                    question_time=as_of,
                    fact_type=fact_type,
                    access=AccessContext(principal="status-inspector", scopes=all_scopes),
                )
            )
            states[packet.state.value] += 1
            if packet.state in {ContextState.UNKNOWN, ContextState.INSUFFICIENT_EVIDENCE}:
                missing.append(f"{service}:{fact_type.value}")
    invalidations = repository.count("invalidations")
    reasons: list[str] = []
    if not facts:
        reasons.append("database contains no facts")
    if states[ContextState.STALE.value]:
        reasons.append(f"{states[ContextState.STALE.value]} context groups are stale")
    if states[ContextState.DISPUTED.value]:
        reasons.append(f"{states[ContextState.DISPUTED.value]} context groups are disputed")
    if missing:
        reasons.append(f"{len(missing)} required context groups are missing")
    if invalidations:
        reasons.append(f"{invalidations} invalidation records require downstream review")
    return StatusReport(
        as_of=as_of,
        source_count=repository.count("source_versions"),
        fact_count=len(facts),
        invalidation_count=invalidations,
        service_count=len(services),
        state_counts=dict(sorted(states.items())),
        missing_context=tuple(missing),
        needs_attention=bool(reasons),
        reasons=tuple(reasons),
    )
