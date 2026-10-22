import json
from contextlib import suppress
from datetime import timedelta

import pytest
from typer.testing import CliRunner

from pcfs.cli import app
from pcfs.models import AccessContext
from pcfs.store import Repository
from pcfs.subscriptions.advice import OfflineAssessor
from pcfs.subscriptions.cli import replay_scenario
from pcfs.subscriptions.fixtures import (
    PINNED_TIME,
    demo_scenario,
    evaluation_scenarios,
    offline_cache,
)
from pcfs.subscriptions.models import ReviewDisposition
from pcfs.subscriptions.service import SubscriptionService

ACCESS = AccessContext(principal="synthetic-reviewer")


def test_demo_is_idempotent_and_dispatches_one_opaque_request(tmp_path):
    runner = CliRunner()
    db = tmp_path / "demo.duckdb"
    reports = tmp_path / "reports"
    command = ["subscriptions", "demo", "--db", str(db), "--report-dir", str(reports)]
    for _ in range(2):
        result = runner.invoke(app, command)
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["unique_receipts"] == 3
        assert json.loads(result.output)["review_episodes"] == 1
    report = json.loads((reports / "impact.json").read_text())
    assert len(report["deliveries"]) == 4
    assert report["deliveries"][1]["receipt_ids"] == report["deliveries"][3]["receipt_ids"]
    with Repository(db) as repository:
        service = SubscriptionService(repository)
        at = PINNED_TIME + timedelta(minutes=10)
        dispatched = service.dispatch(ACCESS, at)
        assert [(item["status"], item["receipt_count"]) for item in dispatched] == [
            ("delivered", 3)
        ]
        assert service.dispatch(ACCESS, at) == []
        assert len(service.store.receipts()) == 3
        assert len(repository.invalidated_fact_ids(at)) == 2


def test_current_access_and_reviewer_are_required_for_history_and_delivery():
    scenario = next(s for s in evaluation_scenarios() if s.sequence_id == "access-revocation/larch")
    with Repository() as repository:
        report = replay_scenario(
            repository, scenario, OfflineAssessor(offline_cache(scenario)), ACCESS
        )
        assert report["packets"] == []
        service = SubscriptionService(repository)
        at = PINNED_TIME + timedelta(minutes=10)
        assert service.dispatch(ACCESS, at) == []
        permitted = AccessContext(principal="synthetic-reviewer", scopes={"synthetic:restricted"})
        visible = service.inspect(permitted, at)
        assert len(visible["packets"]) == 2
        assert service.inspect(AccessContext(principal="different-reviewer"), at)["packets"] == []
        assert service.process(scenario.events[0], ACCESS, at) == ()
        assert service.store.receipts()[0].assessment is None


def test_review_is_idempotent_stale_review_rejected_and_policy_never_restored():
    scenario = demo_scenario()
    with Repository() as repository:
        replay_scenario(repository, scenario, OfflineAssessor(offline_cache(scenario)), ACCESS)
        service = SubscriptionService(repository)
        at = PINNED_TIME + timedelta(minutes=10)
        episode = service.inspect(ACCESS, at)["outbox"][0]
        values = dict(
            command_id="review-1",
            receipt_id=episode["receipt_ids"][0],
            reviewer=ACCESS.principal,
            decision="confirmed",
            rationale="Reviewed exact sources",
            at=at,
        )
        service.review(ReviewDisposition(**values), ACCESS, at)
        assert service.store.list_outbox()[0]["status"] == "pending"
        before = repository.invalidated_fact_ids(at)
        values["receipt_id"] = episode["receipt_ids"][-1]
        values["command_id"] = "review-latest"
        command = ReviewDisposition(**values)
        service.review(command, ACCESS, at)
        service.review(command, ACCESS, at)
        assert service.store.list_outbox()[0]["status"] == "resolved"
        assert [row["type"] for row in service.inspect(ACCESS, at)["history"]] == [
            "disposition",
            "disposition",
        ]
        assert service.inspect(AccessContext(principal="someone-else"), at)["history"] == []
        assert repository.invalidated_fact_ids(at) == before
        assert len(before) == 2


def test_missing_cache_gap_and_late_delivery_cannot_claim_unchanged():
    scenario = next(s for s in evaluation_scenarios() if s.sequence_id == "late-event/larch")
    with Repository() as repository:
        replay_scenario(repository, scenario, OfflineAssessor({}), ACCESS)
        receipts = SubscriptionService(repository).store.receipts()
        assert len(receipts) == 2
        assert all(item.action == "review" for item in receipts)
        assert any("order_gap" in item.guard.reasons for item in receipts)
        assert all(item.assessment.materiality == "unknown" for item in receipts)


def test_cli_denies_foreign_reviewer_and_bad_clock(tmp_path):
    runner = CliRunner()
    db = tmp_path / "demo.duckdb"
    demo = runner.invoke(
        app, ["subscriptions", "demo", "--db", str(db), "--report-dir", str(tmp_path / "reports")]
    )
    assert demo.exit_code == 0, demo.output
    query = [
        "subscriptions",
        "inspect",
        "--db",
        str(db),
        "--principal",
        "someone-else",
        "--at",
        "2026-06-01T13:00:00Z",
    ]
    result = runner.invoke(app, query)
    assert result.exit_code == 0
    assert json.loads(result.output)["packets"] == []
    query[-1] = "2026-06-01T13:00:00"
    bad = runner.invoke(app, query)
    assert bad.exit_code == 2
    assert "UTC offset" in bad.output


def test_revision_cannot_transfer_existing_subscription_to_another_reviewer():
    scenario = demo_scenario()
    with Repository() as repository:
        service = SubscriptionService(repository)
        for snapshot in scenario.facts:
            service.ingest_snapshot(snapshot)
        original = scenario.subscriptions[0]
        service.register(original, ACCESS, PINNED_TIME)
        takeover = original.model_copy(update={"revision": 2, "reviewer": "eve"})
        with pytest.raises(ValueError, match="ownership"):
            service.register(takeover, AccessContext(principal="eve"), PINNED_TIME)
        assert [(s.revision, s.reviewer) for s in service.store.subscriptions()] == [
            (1, "synthetic-reviewer")
        ]


def test_foreign_processor_never_receives_assumption_advice():
    scenario = demo_scenario()
    with Repository() as repository:
        service = SubscriptionService(repository, OfflineAssessor(offline_cache(scenario)))
        for snapshot in scenario.facts:
            service.ingest_snapshot(snapshot)
        service.register(scenario.subscriptions[0], ACCESS, PINNED_TIME)
        event = scenario.events[0]
        assert service.process(event, AccessContext(principal="eve"), event.available_at) == ()
        receipt = service.store.receipts()[0]
        assert receipt.assessment is None
        assert receipt.guard.accessible is False
        assert len(repository.invalidated_fact_ids(event.available_at)) == 1


def test_backdated_inspection_cannot_export_future_or_restricted_documents():
    scenario = next(s for s in evaluation_scenarios() if s.sequence_id == "access-revocation/larch")
    granted = AccessContext(principal="synthetic-reviewer", scopes={"synthetic:restricted"})
    with Repository() as repository:
        replay_scenario(repository, scenario, OfflineAssessor(offline_cache(scenario)), granted)
        service = SubscriptionService(repository)
        assert service.inspect(ACCESS, PINNED_TIME)["packets"] == []
        assert service.inspect(granted, PINNED_TIME)["packets"] == []
        assert len(service.inspect(granted, PINNED_TIME + timedelta(minutes=10))["packets"]) == 2


def test_failed_processing_rolls_back_ingestion_and_every_effect(monkeypatch):
    scenario = demo_scenario()
    with Repository() as repository:
        service = SubscriptionService(repository, OfflineAssessor(offline_cache(scenario)))
        for snapshot in scenario.facts:
            service.ingest_snapshot(snapshot)
        service.register(scenario.subscriptions[0], ACCESS, PINNED_TIME)
        before = repository.count("facts")
        commit = service.store.commit_event

        def broken(event, receipts, invalidations):
            altered = tuple(
                item.model_copy(update={"occurred_at": item.occurred_at + timedelta(days=365)})
                for item in invalidations
            )
            return commit(event, receipts, altered)

        monkeypatch.setattr(service.store, "commit_event", broken)
        event = scenario.events[0]
        with pytest.raises(ValueError, match="effective"):
            service.process(event, ACCESS, event.available_at)
        assert repository.count("facts") == before == 6
        assert service.store.event(event.event_id) is None
        assert service.store.receipts() == ()
        assert service.store.list_outbox() == []
        assert repository.invalidated_fact_ids(event.available_at) == frozenset()
        monkeypatch.setattr(service.store, "commit_event", commit)
        assert len(service.process(event, ACCESS, event.available_at)) == 1
        assert repository.count("facts") == 7


def test_catching_nested_failure_cannot_commit_partial_effects(monkeypatch):
    scenario = demo_scenario()
    with Repository() as repository:
        service = SubscriptionService(repository, OfflineAssessor(offline_cache(scenario)))
        for snapshot in scenario.facts:
            service.ingest_snapshot(snapshot)
        service.register(scenario.subscriptions[0], ACCESS, PINNED_TIME)
        commit = service.store.commit_event

        def broken(event, receipts, invalidations):
            return commit(event, receipts, ())

        monkeypatch.setattr(service.store, "commit_event", broken)
        event = scenario.events[0]
        with (
            pytest.raises(RuntimeError, match="nested operation failed"),
            repository.transaction(),
            suppress(ValueError),
        ):
            service.process(event, ACCESS, event.available_at)
        assert repository.count("facts") == 6
        assert service.store.receipts() == ()
        assert service.store.event(event.event_id) is None
        assert service.store.list_outbox() == []
        monkeypatch.setattr(service.store, "commit_event", commit)
        assert len(service.process(event, ACCESS, event.available_at)) == 1
