from datetime import UTC, datetime, timedelta
from pathlib import Path

from pcfs.corpus import load_corpus
from pcfs.ingestion import build_corpus_bundles
from pcfs.models import AccessContext, ContextState, FactType
from pcfs.retrieval import ContextQuery, ContextRetriever, QueryMode
from pcfs.store import Repository


def populated_repository(path: Path) -> Repository:
    repository = Repository(path)
    for bundle in build_corpus_bundles(load_corpus()):
        repository.ingest(bundle)
    return repository


def query(
    *,
    service: str = "checkout",
    question_time: datetime,
    fact_type: FactType = FactType.OWNERSHIP,
    mode: QueryMode = QueryMode.STRICT,
    scopes: frozenset[str] = frozenset(),
    text: str | None = None,
) -> ContextQuery:
    return ContextQuery(
        service=service,
        question_time=question_time,
        fact_type=fact_type,
        mode=mode,
        text=text,
        access=AccessContext(principal="test-operator", scopes=scopes),
    )


def test_historical_query_uses_old_owner_without_future_leakage(tmp_path: Path) -> None:
    with populated_repository(tmp_path / "historical.duckdb") as repository:
        packet = ContextRetriever(repository).retrieve(
            query(question_time=datetime(2026, 1, 10, tzinfo=UTC))
        )

    assert packet.state is ContextState.CURRENT
    assert [item.value for item in packet.evidence] == ["team-orchid"]
    assert all(item.citation.recorded_at <= packet.query.question_time for item in packet.evidence)


def test_superseding_change_selects_new_owner(tmp_path: Path) -> None:
    with populated_repository(tmp_path / "superseded.duckdb") as repository:
        packet = ContextRetriever(repository).retrieve(
            query(question_time=datetime(2026, 2, 1, 12, tzinfo=UTC))
        )

    assert packet.state is ContextState.CURRENT
    assert [item.value for item in packet.evidence] == ["team-lumen"]


def test_authority_conflict_requires_abstention_in_strict_mode(tmp_path: Path) -> None:
    with populated_repository(tmp_path / "conflict.duckdb") as repository:
        packet = ContextRetriever(repository).retrieve(
            query(question_time=datetime(2026, 2, 2, 10, tzinfo=UTC))
        )

    assert packet.state is ContextState.DISPUTED
    assert packet.abstained is True
    assert packet.evidence == ()


def test_diagnostic_mode_exposes_stale_evidence_and_reasons(tmp_path: Path) -> None:
    with populated_repository(tmp_path / "diagnostic.duckdb") as repository:
        packet = ContextRetriever(repository).retrieve(
            query(
                service="currency",
                question_time=datetime(2026, 2, 15, tzinfo=UTC),
                fact_type=FactType.METRIC_NAME,
                mode=QueryMode.DIAGNOSTIC,
            )
        )

    assert packet.state is ContextState.STALE
    assert packet.evidence == ()
    assert packet.diagnostic_evidence[0].state is ContextState.STALE
    assert "stale_or_invalid" in packet.diagnostic_evidence[0].reason_codes


def test_restricted_facts_are_filtered_before_ranking(tmp_path: Path) -> None:
    question_time = datetime(2026, 1, 2, tzinfo=UTC)
    with populated_repository(tmp_path / "permissions.duckdb") as repository:
        denied = ContextRetriever(repository).retrieve(
            query(
                service="identity",
                question_time=question_time,
                fact_type=FactType.PROCEDURE,
                text="issuer keys",
            )
        )
        allowed = ContextRetriever(repository).retrieve(
            query(
                service="identity",
                question_time=question_time,
                fact_type=FactType.PROCEDURE,
                text="issuer keys",
                scopes=frozenset({"service:identity:restricted"}),
            )
        )

    assert denied.state is ContextState.INSUFFICIENT_EVIDENCE
    assert denied.evidence == ()
    assert allowed.evidence[0].value.startswith("Check issuer keys")


def test_keyword_ranking_and_packet_limit_are_deterministic(tmp_path: Path) -> None:
    request = ContextQuery(
        service="checkout",
        question_time=datetime(2026, 1, 2, tzinfo=UTC),
        text="currency inventory procedure",
        mode=QueryMode.DIAGNOSTIC,
        limit=3,
        access=AccessContext(principal="test-operator"),
    )
    with populated_repository(tmp_path / "ranking.duckdb") as repository:
        retriever = ContextRetriever(repository)
        first = retriever.retrieve(request)
        second = retriever.retrieve(request)

    assert first.model_dump_json() == second.model_dump_json()
    assert len(first.diagnostic_evidence) == 3
    assert [item.score for item in first.diagnostic_evidence] == sorted(
        [item.score for item in first.diagnostic_evidence], reverse=True
    )


def test_incident_scope_and_event_invalidation(tmp_path: Path) -> None:
    corpus = load_corpus()
    incident = corpus.incidents[0]
    request = ContextQuery(
        service=incident.service,
        incident=incident.id,
        fact_type=FactType.TEMPORARY_MITIGATION,
        question_time=incident.opened_at + timedelta(minutes=5),
        access=AccessContext(principal="incident-commander"),
    )
    with populated_repository(tmp_path / "incident.duckdb") as repository:
        retriever = ContextRetriever(repository)
        current = retriever.retrieve(request)
        invalidated = retriever.retrieve(
            request, invalidated_fact_ids=frozenset({current.evidence[0].fact_id})
        )

    assert current.state is ContextState.CURRENT
    assert current.evidence[0].citation.source_id.endswith(incident.id)
    assert invalidated.state is ContextState.STALE
    assert invalidated.abstained is True
