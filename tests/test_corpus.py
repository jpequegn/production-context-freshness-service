from typer.testing import CliRunner

from pcfs.cli import app
from pcfs.corpus import load_corpus, validate_corpus
from pcfs.models import ContextState, Sensitivity


def test_corpus_meets_size_and_integrity_contract() -> None:
    corpus = load_corpus()

    assert corpus.fictional is True
    assert len(corpus.services) == 8
    assert len(corpus.incidents) == 15
    assert len(corpus.releases) == 8
    assert len(corpus.questions()) == 60
    assert validate_corpus(corpus) == []


def test_question_set_covers_every_context_state() -> None:
    states = {question.expected_state for question in load_corpus().questions()}
    assert states == set(ContextState)


def test_first_slice_and_required_changes_are_present() -> None:
    corpus = load_corpus()
    services = {service.id: service for service in corpus.services}
    change_types = {change.type for change in corpus.changes}

    assert {"checkout", "currency", "stream-router"} <= services.keys()
    assert {"ownership_change", "metric_rename", "authority_conflict"} <= change_types
    assert any(service.sensitivity is Sensitivity.RESTRICTED for service in corpus.services)


def test_every_reference_and_timestamp_is_valid() -> None:
    corpus = load_corpus()
    service_ids = {service.id for service in corpus.services}

    assert all(set(service.dependencies) <= service_ids for service in corpus.services)
    assert all(incident.service in service_ids for incident in corpus.incidents)
    assert all(incident.closed_at > incident.opened_at for incident in corpus.incidents)
    assert len({question.id for question in corpus.questions()}) == 60


def test_validate_corpus_cli() -> None:
    result = CliRunner().invoke(app, ["data", "validate-corpus"])

    assert result.exit_code == 0
    assert "8 services, 15 incidents, 60 questions" in result.stdout
