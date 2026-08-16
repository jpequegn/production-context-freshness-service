from datetime import UTC, datetime
from pathlib import Path

from pcfs.corpus import load_corpus
from pcfs.diagnostics import inspect_status
from pcfs.evaluation import SystemName, render_evaluation_markdown, run_evaluation
from pcfs.ingestion import build_corpus_bundles
from pcfs.policy import PolicyRegistry
from pcfs.store import Repository


def populated_repository(path: Path) -> Repository:
    repository = Repository(path)
    for policy in PolicyRegistry.defaults().all():
        repository.put_policy(policy)
    for bundle in build_corpus_bundles(load_corpus()):
        repository.ingest(bundle)
    return repository


def test_temporal_evaluation_beats_both_baselines(tmp_path: Path) -> None:
    with populated_repository(tmp_path / "eval.duckdb") as repository:
        report = run_evaluation(repository)

    metrics = {item.system: item for item in report.systems}
    temporal = metrics[SystemName.TEMPORAL_PROVENANCE]
    assert report.question_count == 60
    assert len(report.outcomes) == 180
    assert report.passed is True
    assert temporal.state_accuracy == 1.0
    assert temporal.answer_accuracy == 1.0
    assert temporal.unsupported_answer_rate == 0
    assert temporal.state_accuracy > metrics[SystemName.NAIVE_TEXT].state_accuracy
    assert temporal.state_accuracy > metrics[SystemName.CURRENT_KEYWORD].state_accuracy


def test_evaluation_markdown_contains_reproducibility_and_metrics(tmp_path: Path) -> None:
    with populated_repository(tmp_path / "report.duckdb") as repository:
        report = run_evaluation(repository)
    markdown = render_evaluation_markdown(report)

    assert report.package_version in markdown
    assert report.corpus_version in markdown
    assert "temporal_provenance" in markdown
    assert "Gate: PASS" in markdown


def test_status_is_healthy_at_fresh_time_and_attention_when_stale(tmp_path: Path) -> None:
    with populated_repository(tmp_path / "status.duckdb") as repository:
        fresh = inspect_status(
            repository, as_of=datetime(2026, 1, 1, 12, tzinfo=UTC)
        )
        stale = inspect_status(repository, as_of=datetime(2026, 8, 16, tzinfo=UTC))

    assert fresh.needs_attention is False
    assert fresh.missing_context == ()
    assert stale.needs_attention is True
    assert stale.state_counts["stale"] > 0


def test_empty_database_status_requires_attention(tmp_path: Path) -> None:
    with Repository(tmp_path / "empty.duckdb") as repository:
        repository.initialize()
        report = inspect_status(repository, as_of=datetime(2026, 1, 1, tzinfo=UTC))

    assert report.needs_attention is True
    assert "database contains no facts" in report.reasons
