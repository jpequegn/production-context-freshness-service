from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from pcfs.corpus import load_corpus
from pcfs.identifiers import content_digest
from pcfs.ingestion import (
    SourceBundle,
    build_corpus_bundles,
    parse_markdown_source,
    parse_yaml_source,
)
from pcfs.models import SourceClass, SourceVersion
from pcfs.store import Repository


def test_corpus_bundles_cover_all_source_classes_and_ingest_idempotently(tmp_path: Path) -> None:
    bundles = build_corpus_bundles(load_corpus())
    with Repository(tmp_path / "context.duckdb") as repository:
        first_results = [repository.ingest(bundle) for bundle in bundles]
        second_results = [repository.ingest(bundle) for bundle in bundles]

        assert sum(result.inserted_facts for result in first_results) >= 80
        assert all(result.idempotent for result in second_results)
        assert repository.count("source_versions") == len(bundles)
        assert repository.count("facts") == sum(len(bundle.facts) for bundle in bundles)
        assert repository.count("relationships") == len(load_corpus().changes)
        assert repository.count("ingestion_events") >= repository.count("facts")


def test_corrections_preserve_prior_fact_versions(tmp_path: Path) -> None:
    bundles = build_corpus_bundles(load_corpus())
    with Repository(tmp_path / "history.duckdb") as repository:
        for bundle in bundles:
            repository.ingest(bundle)

        checkout_owners = [
            fact
            for fact in repository.list_facts()
            if fact.service == "checkout" and fact.predicate == "owned_by"
        ]
        assert {fact.value for fact in checkout_owners} == {"team-orchid", "team-lumen"}
        assert repository.count("relationships") == 5


def test_source_version_collision_is_rejected(tmp_path: Path) -> None:
    bundle = build_corpus_bundles(load_corpus())[0]
    with Repository(tmp_path / "collision.duckdb") as repository:
        repository.ingest(bundle)
        altered_source = bundle.source.model_copy(update={"content_digest": "f" * 64})
        altered = bundle.model_copy(
            update={"source": altered_source, "facts": (), "relationships": ()}
        )

        with pytest.raises(ValueError, match="immutable source version collision"):
            repository.ingest(altered)


def test_transaction_rolls_back_on_duplicate_fact(tmp_path: Path) -> None:
    original = build_corpus_bundles(load_corpus())[0]
    duplicate = original.model_copy(update={"facts": (original.facts[0], original.facts[0])})
    with Repository(tmp_path / "rollback.duckdb") as repository:
        with pytest.raises(duckdb.ConstraintException):
            repository.ingest(duplicate)
        assert repository.count("source_versions") == 0
        assert repository.count("facts") == 0


def test_yaml_and_markdown_parsers_use_structured_envelopes(tmp_path: Path) -> None:
    source_fields = """source_id: runbooks/checkout
  version: v1
  source_class: runbook
  observed_at: 2026-01-01T00:00:00Z
  recorded_at: 2026-01-01T00:00:00Z"""
    fact_fields = """subject: checkout
    predicate: runbook_procedure
    value: Inspect checkout latency
    fact_type: procedure
    observed_at: 2026-01-01T00:00:00Z
    valid_from: 2026-01-01T00:00:00Z
    recorded_at: 2026-01-01T00:00:00Z
    authority: 20
    owner: team-orchid
    environment: production
    service: checkout
    freshness_policy_id: procedure-v1"""
    yaml_path = tmp_path / "runbook.yaml"
    yaml_path.write_text(f"source:\n  {source_fields}\nfacts:\n  - {fact_fields}\n")
    markdown_path = tmp_path / "runbook.md"
    markdown_path.write_text(
        f"---\nsource:\n  {source_fields}\nfacts:\n  - {fact_fields}\n---\n# Checkout\n"
    )

    yaml_bundle = parse_yaml_source(yaml_path)
    markdown_bundle = parse_markdown_source(markdown_path)

    assert len(yaml_bundle.facts) == len(markdown_bundle.facts) == 1
    assert yaml_bundle.source.source_class is SourceClass.RUNBOOK
    assert markdown_bundle.source.uri == markdown_path.as_posix()


def test_bundle_rejects_mismatched_provenance(tmp_path: Path) -> None:
    bundle = build_corpus_bundles(load_corpus())[0]
    other_source = SourceVersion(
        source_id="other",
        version="1",
        source_class=SourceClass.RUNBOOK,
        uri="fixture://other",
        content_digest=content_digest("other"),
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    bad_fact = bundle.facts[0].model_copy(update={"source": other_source})
    bad_bundle = SourceBundle(source=bundle.source, facts=(bad_fact,))

    with (
        Repository(tmp_path / "provenance.duckdb") as repository,
        pytest.raises(ValueError, match="provenance"),
    ):
        repository.ingest(bad_bundle)
