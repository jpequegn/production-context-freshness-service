from datetime import UTC, datetime, timedelta
from pathlib import Path

from pcfs.corpus import load_corpus
from pcfs.ingestion import build_corpus_bundles
from pcfs.models import (
    AccessContext,
    FactRelationship,
    FactType,
    RelationshipType,
)
from pcfs.propagation import ChangePropagator, expand_dependents
from pcfs.retrieval import ContextQuery, ContextRetriever
from pcfs.store import Repository


def populated_repository(path: Path) -> Repository:
    repository = Repository(path)
    for bundle in build_corpus_bundles(load_corpus()):
        repository.ingest(bundle)
    return repository


def test_metric_rename_invalidates_metric_and_dependent_procedure(tmp_path: Path) -> None:
    occurred_at = datetime(2026, 2, 10, tzinfo=UTC)
    with populated_repository(tmp_path / "metric.duckdb") as repository:
        report = ChangePropagator(repository).propagate(
            trigger="metric_renamed",
            service="checkout",
            occurred_at=occurred_at,
        )
        facts = {fact.fact_id: fact for fact in repository.list_facts()}

    affected_types = {facts[fact_id].fact_type for fact_id in report.affected_fact_ids}
    assert {FactType.METRIC_NAME, FactType.PROCEDURE} <= affected_types
    assert any("metric_at_open" in question for question in report.affected_question_ids)
    assert report.applied is False


def test_dry_run_and_apply_have_same_impact_and_apply_is_idempotent(tmp_path: Path) -> None:
    occurred_at = datetime(2026, 2, 1, tzinfo=UTC)
    with populated_repository(tmp_path / "owner.duckdb") as repository:
        propagator = ChangePropagator(repository)
        dry_run = propagator.propagate(
            trigger="ownership_changed",
            service="checkout",
            occurred_at=occurred_at,
        )
        applied = propagator.propagate(
            trigger="ownership_changed",
            service="checkout",
            occurred_at=occurred_at,
            apply=True,
        )
        repeated = propagator.propagate(
            trigger="ownership_changed",
            service="checkout",
            occurred_at=occurred_at,
            apply=True,
        )

        assert dry_run.affected_fact_ids == applied.affected_fact_ids
        assert applied.affected_fact_ids == repeated.affected_fact_ids
        assert repository.count("invalidations") == len(applied.records)


def test_incident_closure_expires_only_incident_scoped_mitigation(tmp_path: Path) -> None:
    incident = load_corpus().incidents[0]
    with populated_repository(tmp_path / "incident.duckdb") as repository:
        report = ChangePropagator(repository).propagate(
            trigger="incident_closed",
            service=incident.service,
            incident=incident.id,
            occurred_at=incident.closed_at,
            apply=True,
        )
        facts = {fact.fact_id: fact for fact in repository.list_facts()}

    assert report.affected_fact_ids
    assert all(facts[fact_id].incident_scope == incident.id for fact_id in report.affected_fact_ids)


def test_historical_query_remains_reproducible_after_propagation(tmp_path: Path) -> None:
    incident = load_corpus().incidents[0]
    before = incident.opened_at + timedelta(minutes=5)
    after = incident.closed_at + timedelta(minutes=1)
    with populated_repository(tmp_path / "history.duckdb") as repository:
        ChangePropagator(repository).propagate(
            trigger="incident_closed",
            service=incident.service,
            incident=incident.id,
            occurred_at=incident.closed_at,
            apply=True,
        )
        retriever = ContextRetriever(repository)
        base = {
            "service": incident.service,
            "incident": incident.id,
            "fact_type": FactType.TEMPORARY_MITIGATION,
            "access": AccessContext(principal="test-operator"),
        }
        historical = retriever.retrieve(ContextQuery(question_time=before, **base))
        current = retriever.retrieve(ContextQuery(question_time=after, **base))

    assert historical.evidence
    assert current.abstained is True


def test_dependency_expansion_handles_cycles_once() -> None:
    bundle = build_corpus_bundles(load_corpus())[0]
    first, second = bundle.facts[:2]
    relationships = (
        FactRelationship(
            source_fact_id=second.fact_id,
            target_fact_id=first.fact_id,
            relationship_type=RelationshipType.DERIVED_FROM,
            source=bundle.source,
            recorded_at=bundle.source.recorded_at,
        ),
        FactRelationship(
            source_fact_id=first.fact_id,
            target_fact_id=second.fact_id,
            relationship_type=RelationshipType.DEPENDS_ON,
            source=bundle.source,
            recorded_at=bundle.source.recorded_at,
        ),
    )

    affected, traversed = expand_dependents(frozenset({first.fact_id}), relationships)
    assert affected == {first.fact_id, second.fact_id}
    assert len(traversed) == 2
