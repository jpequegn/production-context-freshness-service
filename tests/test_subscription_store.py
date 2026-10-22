from datetime import UTC, datetime, timedelta

import pytest

from pcfs.identifiers import content_digest
from pcfs.ingestion import SourceBundle
from pcfs.models import (
    AuthorityLevel,
    Fact,
    FactType,
    InvalidationRecord,
    SourceClass,
    SourceVersion,
)
from pcfs.store import Repository
from pcfs.subscriptions.models import (
    AssessmentInput,
    ChangeEvent,
    EvidenceSnapshot,
    GuardResult,
    Receipt,
    ReviewDisposition,
    Subscription,
)
from pcfs.subscriptions.store import SubscriptionStore

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def snapshot(version, *, source_id="source", service="svc", environment="prod"):
    document = f"Evidence version {version} for {source_id}"
    source = SourceVersion(
        source_id=source_id,
        version=str(version),
        source_class=SourceClass.RUNBOOK,
        uri="local://evidence",
        content_digest=content_digest({"text": document}),
        observed_at=NOW,
        recorded_at=NOW,
    )
    return EvidenceSnapshot(
        document=document,
        fact=Fact(
            subject="subject",
            predicate="procedure",
            value=document,
            fact_type=FactType.PROCEDURE,
            source=source,
            observed_at=NOW,
            recorded_at=NOW,
            valid_from=NOW,
            authority=AuthorityLevel.AUTHORITATIVE,
            owner="owner",
            environment=environment,
            service=service,
            freshness_policy_id="policy",
        ),
    )


def ingest(repo, *snapshots):
    for item in snapshots:
        repo.ingest(SourceBundle(source=item.fact.source, facts=(item.fact,)))


def subscription(item, **updates):
    data = dict(
        subscription_id="sub",
        conclusion_id="conclusion",
        revision=1,
        assumption="assumption",
        assumption_version="1",
        dependencies=(item.fact.fact_id,),
        source_versions={item.fact.fact_id: item.fact.source.key},
        service=item.fact.service,
        environment=item.fact.environment,
        reviewer="reviewer",
        deadline=NOW + timedelta(hours=1),
        debounce_seconds=60,
    )
    return Subscription(**(data | updates))


def change(old, new, sequence=1, *, at=NOW, event_id=None):
    return ChangeEvent(
        event_id=event_id or f"event-{sequence}",
        old=old,
        new=new,
        observed_at=at,
        available_at=at,
        sequence=sequence,
    )


def receipt(sub, event, *, mandatory=False, affected=(), action="review", **updates):
    return Receipt(
        **(
            dict(
                receipt_id=f"receipt-{sub.subscription_id}-{sub.revision}-{event.event_id}",
                subscription_id=sub.subscription_id,
                revision=sub.revision,
                event_id=event.event_id,
                input_key=AssessmentInput(subscription=sub, event=event).key,
                guard=GuardResult(
                    mandatory=mandatory,
                    reasons=("policy",) if mandatory else (),
                    accessible=True,
                    affected_fact_ids=affected,
                    graph_fingerprint="graph-v1",
                    traversal_truncated=False,
                ),
                action=action,
                created_at=event.available_at,
            )
            | updates
        )
    )


def invalidation(item, *, identity="invalidation", **updates):
    return InvalidationRecord(
        **(
            dict(
                invalidation_id=identity,
                trigger="policy",
                target_fact_id=item.fact.fact_id,
                service=item.fact.service,
                occurred_at=NOW,
                reason="mandatory invalidation",
            )
            | updates
        )
    )


def command(item, *, identity="command", decision="confirmed", **updates):
    return ReviewDisposition(
        **(
            dict(
                command_id=identity,
                receipt_id=item.receipt_id,
                reviewer="reviewer",
                decision=decision,
                rationale="Reviewed the evidence.",
                at=NOW + timedelta(minutes=5),
            )
            | updates
        )
    )


@pytest.fixture
def setup_store():
    with Repository() as repo:
        store = SubscriptionStore(repo)
        old, new = snapshot(0), snapshot(1)
        ingest(repo, old, new)
        sub = subscription(old)
        store.register(sub)
        yield repo, store, old, new, sub


def test_registration_checks_pins_scope_and_immutable_revision(setup_store):
    repo, store, old, _, sub = setup_store
    assert store.connection is repo.connection
    store.register(sub)
    assert store.subscriptions() == (sub,)
    for field, value in (("assumption", "changed"), ("reviewer", "other")):
        with pytest.raises(ValueError, match="collision"):
            store.register(sub.model_copy(update={field: value}))
    for updates in (
        {"dependencies": ("missing",), "source_versions": {"missing": "missing"}},
        {"source_versions": {old.fact.fact_id: "wrong"}},
        {"service": "wrong"},
        {"environment": "wrong"},
    ):
        with pytest.raises(ValueError):
            store.register(sub.model_copy(update={"revision": 2, **updates}))
    assert store.subscriptions() == (sub,)


def test_all_revisions_are_listed_but_only_latest_receives_events(setup_store):
    _, store, old, new, sub = setup_store
    newer = sub.model_copy(update={"revision": 2})
    store.register(newer)
    store.register(sub)
    assert store.subscriptions() == (sub, newer)
    event = change(old, new)
    with pytest.raises(ValueError):
        store.commit_event(event, (receipt(sub, event),))
    expected = receipt(newer, event)
    assert store.commit_event(event, (expected,)) == (expected,)


def test_source_ingestion_is_a_durable_prerequisite_and_failure_is_retriable(setup_store):
    repo, store, old, _, sub = setup_store
    new = snapshot(2)
    event = change(old, new)
    result = receipt(sub, event)
    with pytest.raises(ValueError, match="ingested"):
        store.commit_event(event, (result,))
    assert store.event(event.event_id) is None
    assert repo.count("facts") == 2
    ingest(repo, new)
    assert store.commit_event(event, (result,)) == (result,)


def test_snapshot_must_equal_stored_fact(setup_store):
    _, store, old, new, sub = setup_store
    changed = new.model_copy(update={"fact": new.fact.model_copy(update={"owner": "other"})})
    event = change(old, changed)
    with pytest.raises(ValueError, match="snapshot"):
        store.commit_event(event, (receipt(sub, event),))
    assert store.receipts() == ()


@pytest.mark.parametrize("malformation", ["missing", "duplicate", "unknown", "input", "event"])
def test_bad_receipts_leave_no_event_or_outbox(setup_store, malformation):
    _, store, old, new, sub = setup_store
    event = change(old, new)
    good = receipt(sub, event)
    bad = {
        "missing": (),
        "duplicate": (good, good),
        "unknown": (good.model_copy(update={"subscription_id": "unknown"}),),
        "input": (good.model_copy(update={"input_key": "wrong"}),),
        "event": (good.model_copy(update={"event_id": "wrong"}),),
    }[malformation]
    with pytest.raises(ValueError):
        store.commit_event(event, bad)
    assert store.event(event.event_id) is None
    assert store.receipts() == ()
    assert store.list_outbox() == []
    assert store.classify_order(event) == "ordered"
    assert store.commit_event(event, (good,)) == (good,)


def test_unrelated_subscriptions_are_optional_but_graph_receipts_require_pinned_impact(setup_store):
    repo, store, old, new, sub = setup_store
    other = snapshot(0, source_id="other")
    ingest(repo, other)
    unrelated = subscription(other, subscription_id="unrelated")
    store.register(unrelated)
    event = change(old, new)
    good = receipt(sub, event)
    for affected in ((), (old.fact.fact_id,), ("unknown",)):
        with pytest.raises(ValueError, match="graph impact"):
            store.commit_event(event, (good, receipt(unrelated, event, affected=affected)))
    graph_receipt = receipt(unrelated, event, affected=(other.fact.fact_id,))
    assert set(store.commit_event(event, (good, graph_receipt))) == {good, graph_receipt}


def test_unrelated_and_other_scope_subscriptions_do_not_require_receipts(setup_store):
    repo, store, old, new, sub = setup_store
    for item in (
        snapshot(0, source_id="other"),
        snapshot(0, source_id="outside", service="outside"),
    ):
        ingest(repo, item)
        store.register(subscription(item, subscription_id=item.fact.source.source_id))
    event = change(old, new)
    good = receipt(sub, event)
    assert store.commit_event(event, (good,)) == (good,)


def test_missing_mandatory_invalidations_rolls_back_every_effect(setup_store):
    repo, store, old, new, sub = setup_store
    event = change(old, new)
    result = receipt(sub, event, mandatory=True, affected=(old.fact.fact_id,))
    with pytest.raises(ValueError, match="atomic invalidations"):
        store.commit_event(event, (result,))
    assert store.event(event.event_id) is None
    assert store.receipts() == ()
    assert store.list_outbox() == []
    assert store.history() == []
    assert repo.count("invalidations") == 0
    assert repo.count("facts") == 2
    store.commit_event(event, (result,), (invalidation(old),))
    assert repo.invalidated_fact_ids(NOW) == frozenset({old.fact.fact_id})


def test_later_malformed_invalidation_rolls_back_prior_sql_insert(setup_store):
    repo, store, old, new, sub = setup_store
    event = change(old, new)
    result = receipt(sub, event, mandatory=True, affected=(old.fact.fact_id,))
    with pytest.raises(ValueError, match="scope"):
        store.commit_event(
            event,
            (result,),
            (invalidation(old), invalidation(new, identity="bad", service="other")),
        )
    assert repo.count("invalidations") == 0
    assert store.event(event.event_id) is None
    assert store.list_outbox() == []
    assert store.receipts() == ()
    assert store.classify_order(event) == "ordered"
    store.commit_event(event, (result,), (invalidation(old),))


def test_invalidation_identity_collision_is_not_silently_ignored(setup_store):
    repo, store, old, new, sub = setup_store
    original = invalidation(old)
    repo.record_invalidations((original,))
    event = change(old, new)
    result = receipt(sub, event, mandatory=True, affected=(old.fact.fact_id,))
    with pytest.raises(ValueError, match="invalidation ID collision"):
        store.commit_event(event, (result,), (original.model_copy(update={"reason": "changed"}),))
    assert store.event(event.event_id) is None
    store.commit_event(event, (result,), (original,))
    assert repo.count("invalidations") == 1


def test_event_duplicate_and_collision_survive_restart(tmp_path):
    path = tmp_path / "ledger.duckdb"
    old, new = snapshot(0), snapshot(1)
    sub = subscription(old)
    event = change(old, new)
    result = receipt(sub, event, mandatory=True, affected=(old.fact.fact_id,))
    with Repository(path) as repo:
        store = SubscriptionStore(repo)
        ingest(repo, old, new)
        store.register(sub)
        assert store.commit_event(event, (result,), (invalidation(old),)) == (result,)
    with Repository(path) as repo:
        store = SubscriptionStore(repo)
        assert store.commit_event(event, ()) == (result,)
        assert store.event(event.event_id) == event
        assert store.event("missing") is None
        assert store.receipts() == (result,)
        assert len(store.list_outbox()) == 1
        assert repo.count("invalidations") == 1
        for changed in (
            event.model_copy(update={"sequence": 2}),
            event.model_copy(update={"trigger": "changed"}),
            event.model_copy(update={"available_at": NOW + timedelta(seconds=1)}),
        ):
            with pytest.raises(ValueError, match="event ID collision"):
                store.commit_event(changed, ())
        assert len(store.dispatch(NOW)) == 1
        assert store.dispatch(NOW + timedelta(days=1)) == []
        assert len(store.history()) == 1


def test_frontiers_advance_only_for_consecutive_matching_predecessors(setup_store):
    repo, store, old, new, sub = setup_store
    third, fourth = snapshot(2), snapshot(3)
    ingest(repo, third, fourth)
    first = change(old, new)
    assert store.classify_order(first) == "ordered"
    assert store.event(first.event_id) is None
    store.commit_event(first, (receipt(sub, first),))
    wrong_predecessor = change(old, third, 2, event_id="wrong-predecessor")
    assert store.classify_order(wrong_predecessor) == "gap"
    with pytest.raises(ValueError, match="require review"):
        store.commit_event(wrong_predecessor, (receipt(sub, wrong_predecessor, action="none"),))
    gap = change(third, fourth, 4)
    assert store.classify_order(gap) == "gap"
    store.commit_event(gap, (receipt(sub, gap),))
    late = change(old, third, 1, event_id="late")
    assert store.classify_order(late) == "late"
    store.commit_event(late, (receipt(sub, late),))
    second = change(new, third, 2)
    assert store.classify_order(second) == "ordered"
    store.commit_event(second, (receipt(sub, second),))
    third_event = change(third, fourth, 3)
    assert store.classify_order(third_event) == "ordered"
    assert store.classify_order(gap) == "gap"


def test_unknown_first_sequence_does_not_seed_frontier(setup_store):
    _, store, old, new, sub = setup_store
    gap = change(old, new, 9)
    assert store.classify_order(gap) == "gap"
    store.commit_event(gap, (receipt(sub, gap),))
    first = change(old, new)
    assert store.classify_order(first) == "ordered"
    store.commit_event(first, (receipt(sub, first),))


def test_same_sequence_conflict_requires_review_without_frontier_rewind(setup_store):
    repo, store, old, new, sub = setup_store
    first = change(old, new)
    store.commit_event(first, (receipt(sub, first, action="none"),))
    third = snapshot(2)
    ingest(repo, third)
    conflict = change(old, third, event_id="conflict")
    assert store.classify_order(conflict) == "late"
    with pytest.raises(ValueError, match="require review"):
        store.commit_event(conflict, (receipt(sub, conflict, action="none"),))
    store.commit_event(conflict, (receipt(sub, conflict),))
    assert store.classify_order(change(new, third, 2)) == "ordered"


def test_coalescing_keeps_first_due_and_all_links(setup_store):
    repo, store, old, new, sub = setup_store
    third = snapshot(2)
    ingest(repo, third)
    first = change(old, new)
    second = change(new, third, 2, at=NOW + timedelta(seconds=30))
    r1, r2 = receipt(sub, first), receipt(sub, second)
    store.commit_event(first, (r1,))
    due = store.list_outbox()[0]["due_at"]
    store.commit_event(second, (r2,))
    outbox = store.list_outbox()
    assert len(outbox) == 1
    assert outbox[0]["due_at"] == due
    assert outbox[0]["receipt_ids"] == [r1.receipt_id, r2.receipt_id]
    assert store.dispatch(NOW + timedelta(seconds=59)) == []
    delivered = store.dispatch(NOW + timedelta(seconds=60))
    assert delivered == [
        dict(episode_id=outbox[0]["episode_id"], status="delivered", attempts=1, receipt_count=2)
    ]
    assert store.dispatch(NOW + timedelta(hours=1)) == []
    assert len(store.history()) == 1


def test_deadline_caps_debounce_and_mandatory_can_accelerate(setup_store):
    repo, store, old, new, sub = setup_store
    sub = sub.model_copy(update={"revision": 2, "deadline": NOW + timedelta(seconds=45)})
    store.register(sub)
    first = change(old, new)
    store.commit_event(first, (receipt(sub, first),))
    assert datetime.fromisoformat(store.list_outbox()[0]["due_at"]) == sub.deadline
    third = snapshot(2)
    ingest(repo, third)
    second = change(new, third, 2, at=NOW + timedelta(seconds=20))
    store.commit_event(
        second,
        (receipt(sub, second, mandatory=True, affected=(old.fact.fact_id,)),),
        (invalidation(old, occurred_at=second.available_at),),
    )
    assert datetime.fromisoformat(store.list_outbox()[0]["due_at"]) == second.available_at
    assert len(store.dispatch(second.available_at)) == 1


def test_delivery_allowlist_and_bounded_failures_are_durable(setup_store):
    _, store, old, new, sub = setup_store
    event = change(old, new)
    store.commit_event(event, (receipt(sub, event),))
    episode_id = store.list_outbox()[0]["episode_id"]
    at = NOW + timedelta(minutes=1)
    assert store.dispatch(at, allowed_episode_ids=set()) == []
    assert store.list_outbox()[0]["attempts"] == 0
    assert store.dispatch(at, fail=True, allowed_episode_ids={episode_id})[0]["attempts"] == 1
    assert store.dispatch(at + timedelta(seconds=59), fail=True) == []
    assert store.dispatch(at + timedelta(seconds=60), fail=True)[0]["attempts"] == 2
    assert store.dispatch(at + timedelta(seconds=179), fail=True) == []
    assert store.dispatch(at + timedelta(seconds=180), fail=True)[0]["status"] == "dead_letter"
    assert store.dispatch(at + timedelta(days=1)) == []
    assert store.list_outbox()[0]["attempts"] == 3
    assert [row["attempts"] for row in store.history()] == [1, 2, 3]
    assert all("document" not in str(row) for row in store.history())


@pytest.mark.parametrize("decision", ["confirmed", "dismissed"])
def test_stale_review_cannot_resolve_later_causes(setup_store, decision):
    repo, store, old, new, sub = setup_store
    third = snapshot(2)
    ingest(repo, third)
    first, second = change(old, new), change(new, third, 2, at=NOW + timedelta(seconds=1))
    r1, r2 = receipt(sub, first), receipt(sub, second)
    store.commit_event(first, (r1,))
    store.commit_event(second, (r2,))
    stale = command(r1, decision=decision)
    assert store.disposition(stale)["resolved"] is False
    assert store.list_outbox()[0]["status"] == "pending"
    assert (
        store.disposition(command(r2, identity="defer", decision="deferred"))["resolved"] is False
    )
    latest = command(r2, identity="latest", decision=decision)
    expected = store.disposition(latest)
    assert expected["resolved"] is True
    assert store.disposition(latest) == expected
    assert len(store.history()) == 3
    assert store.dispatch(NOW + timedelta(days=1)) == []
    assert store.subscriptions() == (sub,)
    assert store.receipts() == (r1, r2)
    assert repo.count("facts") == 3


def test_reviewer_match_command_collision_and_new_episode(setup_store):
    repo, store, old, new, sub = setup_store
    event = change(old, new)
    result = receipt(sub, event)
    store.commit_event(event, (result,))
    with pytest.raises(ValueError, match="reviewer"):
        store.disposition(command(result, reviewer="Reviewer"))
    assert store.history() == []
    cmd = command(result)
    store.disposition(cmd)
    with pytest.raises(ValueError, match="command ID collision"):
        store.disposition(cmd.model_copy(update={"decision": "dismissed"}))
    third = snapshot(2)
    ingest(repo, third)
    later = change(new, third, 2, at=NOW + timedelta(minutes=6))
    store.commit_event(later, (receipt(sub, later),))
    assert len(store.list_outbox()) == 2
    assert [row["status"] for row in store.list_outbox()] == ["resolved", "pending"]
    store.disposition(command(result, identity="old-again", at=NOW + timedelta(minutes=7)))
    assert store.list_outbox()[1]["status"] == "pending"


def test_new_revision_supersedes_pending_old_delivery(setup_store):
    _, store, old, new, sub = setup_store
    event = change(old, new)
    result = receipt(sub, event)
    store.commit_event(event, (result,))
    store.register(sub.model_copy(update={"revision": 2}))
    assert store.list_outbox()[0]["status"] == "superseded"
    assert store.dispatch(NOW + timedelta(days=1)) == []
    assert store.disposition(command(result))["resolved"] is False


def test_timezone_validation_at_every_mutation_boundary(setup_store):
    _, store, old, new, sub = setup_store
    naive = NOW.replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone"):
        store.dispatch(naive)
    with pytest.raises(ValueError):
        store.register(sub.model_copy(update={"revision": 2, "deadline": naive}))
    event = change(old, new)
    with pytest.raises(ValueError):
        store.classify_order(event.model_copy(update={"observed_at": naive}))
    with pytest.raises(ValueError):
        store.commit_event(event.model_copy(update={"available_at": naive}), ())
    result = receipt(sub, event)
    with pytest.raises(ValueError):
        store.commit_event(event, (result.model_copy(update={"created_at": naive}),))
    with pytest.raises(ValueError):
        store.commit_event(event, (result,), (invalidation(old, occurred_at=naive),))
    assert store.event(event.event_id) is None
    store.commit_event(event, (result,))
    with pytest.raises(ValueError):
        store.disposition(command(result).model_copy(update={"at": naive}))


def test_delivery_attempts_and_dispositions_survive_restart(tmp_path):
    path = tmp_path / "restart.duckdb"
    old, new = snapshot(0), snapshot(1)
    sub, event = subscription(old), change(old, new)
    result = receipt(sub, event)
    at = NOW + timedelta(minutes=1)
    with Repository(path) as repo:
        store = SubscriptionStore(repo)
        ingest(repo, old, new)
        store.register(sub)
        store.commit_event(event, (result,))
        assert store.dispatch(at, fail=True)[0]["attempts"] == 1
    with Repository(path) as repo:
        store = SubscriptionStore(repo)
        assert store.dispatch(at, fail=True) == []
        assert store.dispatch(at + timedelta(minutes=1))[0]["attempts"] == 2
        outcome = store.disposition(command(result))
        assert outcome["resolved"] is True
    with Repository(path) as repo:
        store = SubscriptionStore(repo)
        assert store.disposition(command(result)) == outcome
        assert store.dispatch(NOW + timedelta(days=1)) == []
        assert [item["type"] for item in store.history()] == [
            "delivery",
            "delivery",
            "disposition",
        ]
        assert store.list_outbox()[0]["attempts"] == 2


def test_new_causes_after_delivery_link_without_duplicate_delivery(setup_store):
    repo, store, old, new, sub = setup_store
    first = change(old, new)
    r1 = receipt(sub, first)
    store.commit_event(first, (r1,))
    assert len(store.dispatch(NOW + timedelta(minutes=1))) == 1
    third = snapshot(2)
    ingest(repo, third)
    second = change(new, third, 2, at=NOW + timedelta(minutes=2))
    r2 = receipt(sub, second)
    store.commit_event(second, (r2,))
    assert store.list_outbox()[0]["receipt_ids"] == [r1.receipt_id, r2.receipt_id]
    assert store.list_outbox()[0]["attempts"] == 1
    assert store.dispatch(NOW + timedelta(days=1)) == []
    assert store.disposition(command(r1))["resolved"] is False
    assert store.disposition(command(r2, identity="latest"))["resolved"] is True


def test_receipt_id_collision_rolls_back_all_effects(setup_store):
    repo, store, old, new, sub = setup_store
    first = change(old, new)
    r1 = receipt(sub, first)
    store.commit_event(first, (r1,))
    third = snapshot(2)
    ingest(repo, third)
    second = change(new, third, 2)
    r2 = receipt(sub, second, receipt_id=r1.receipt_id)
    with pytest.raises(ValueError, match="receipt ID collision"):
        store.commit_event(second, (r2,))
    assert store.event(second.event_id) is None
    assert store.receipts() == (r1,)
    assert store.list_outbox()[0]["receipt_ids"] == [r1.receipt_id]
    assert store.classify_order(second) == "ordered"
    assert store.commit_event(second, (receipt(sub, second),)) == (receipt(sub, second),)


def test_no_effect_receipt_does_not_create_delivery(setup_store):
    _, store, old, new, sub = setup_store
    event = change(old, new)
    result = receipt(sub, event, action="none")
    assert store.commit_event(event, (result,)) == (result,)
    assert store.list_outbox() == []
    assert store.dispatch(NOW + timedelta(days=1)) == []


def test_later_malformed_receipt_rolls_back_prior_receipt_and_episode(setup_store):
    _, store, old, new, sub = setup_store
    second_sub = subscription(old, subscription_id="second")
    store.register(second_sub)
    event = change(old, new)
    first, second = receipt(sub, event), receipt(second_sub, event)
    with pytest.raises(ValueError, match="exact event"):
        store.commit_event(event, (first, second.model_copy(update={"input_key": "wrong"})))
    assert store.event(event.event_id) is None
    assert store.receipts() == ()
    assert store.list_outbox() == []
    assert store.classify_order(event) == "ordered"
    assert set(store.commit_event(event, (first, second))) == {first, second}
