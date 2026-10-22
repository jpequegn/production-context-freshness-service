from datetime import timedelta

from pcfs.ingestion import SourceBundle
from pcfs.models import AccessContext
from pcfs.store import Repository
from pcfs.subscriptions.fixtures import PINNED_TIME, demo_scenario, evaluation_scenarios
from pcfs.subscriptions.guards import affected_dependencies, evaluate_guards


def seed(repository, scenario, event):
    for snapshot in (*scenario.facts, event.old, event.new):
        repository.ingest(
            SourceBundle(
                source=snapshot.fact.source,
                facts=(snapshot.fact,),
                raw_document={"text": snapshot.document},
            )
        )


def test_editorial_advice_cannot_restore_obsolete_source():
    scenario = demo_scenario()
    event = scenario.events[0]
    with Repository() as repository:
        seed(repository, scenario, event)
        guard = evaluate_guards(
            repository,
            scenario.subscriptions[0],
            event,
            AccessContext(principal="synthetic-reviewer"),
            event.available_at,
        )
        assert guard.mandatory is True
        assert "source_version_changed" in guard.reasons
        assert guard.accessible is True
        assert guard.affected_fact_ids == (event.old.fact.fact_id,)
        assert affected_dependencies(repository, scenario.subscriptions[1], event) == ()


def test_revocation_and_expiry_remain_mandatory():
    scenarios = evaluation_scenarios()
    for family, expected_reason in (
        ("access-revocation", "access_denied"),
        ("hard-expiry", "dependency_not_current"),
    ):
        scenario = next(item for item in scenarios if item.sequence_id == f"{family}/larch")
        event = scenario.events[0]
        with Repository() as repository:
            seed(repository, scenario, event)
            guard = evaluate_guards(
                repository,
                scenario.subscriptions[0],
                event,
                AccessContext(principal="synthetic-reviewer"),
                event.available_at,
            )
            assert guard.mandatory is True
            assert expected_reason in guard.reasons
            if family == "access-revocation":
                assert guard.accessible is False


def test_unknown_projection_and_missing_bindings_cannot_be_no_impact():
    scenario = demo_scenario()
    event = scenario.events[0]
    with Repository() as repository:
        seed(repository, scenario, event)
        sub = scenario.subscriptions[0].model_copy(
            update={
                "source_versions": {
                    key: "wrong-source" for key in scenario.subscriptions[0].dependencies
                }
            }
        )
        guard = evaluate_guards(
            repository,
            sub,
            event,
            AccessContext(principal="synthetic-reviewer"),
            PINNED_TIME + timedelta(days=100),
            traversal_truncated=True,
        )
        assert guard.mandatory is True
        assert {"source_binding_mismatch", "impact_unknown_truncated", "review_deadline"} <= set(
            guard.reasons
        )
        assert len(guard.graph_fingerprint) == 64


def test_future_event_and_unverified_snapshot_are_visible():
    scenario = demo_scenario()
    event = scenario.events[0]
    with Repository() as repository:
        for snapshot in scenario.facts:
            repository.ingest(SourceBundle(source=snapshot.fact.source, facts=(snapshot.fact,)))
        guard = evaluate_guards(
            repository,
            scenario.subscriptions[0],
            event,
            AccessContext(principal="synthetic-reviewer"),
            PINNED_TIME,
        )
        assert guard.mandatory is True
        assert {"future_event", "unverified_snapshot"} <= set(guard.reasons)


def test_current_dependency_does_not_hide_denied_other_dependency():
    scenario = demo_scenario()
    event = scenario.events[0]
    with Repository() as repository:
        seed(repository, scenario, event)
        guard = evaluate_guards(
            repository,
            scenario.subscriptions[0],
            event,
            AccessContext(principal="synthetic-reviewer", environment="other"),
            event.available_at,
        )
        assert guard.mandatory is True
        assert guard.accessible is False
        assert "access_denied" in guard.reasons
