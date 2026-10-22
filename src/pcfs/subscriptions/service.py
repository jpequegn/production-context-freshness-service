"""Local orchestration of guarded, version-linked review requests."""

from datetime import datetime

from pcfs.identifiers import content_digest, stable_id
from pcfs.ingestion import SourceBundle
from pcfs.models import AccessContext, InvalidationRecord
from pcfs.retrieval import ContextQuery, ContextRetriever
from pcfs.store import Repository
from pcfs.subscriptions.advice import OfflineAssessor, plan_receipt
from pcfs.subscriptions.guards import affected_dependencies, can_read, evaluate_guards
from pcfs.subscriptions.models import (
    AssessmentInput,
    ChangeEvent,
    EvidenceSnapshot,
    Receipt,
    ReviewDisposition,
    Subscription,
)
from pcfs.subscriptions.projection import RustGraphBackend, build_projection, select_impact
from pcfs.subscriptions.store import SubscriptionStore


class SubscriptionService:
    def __init__(
        self,
        repository: Repository,
        assessor: OfflineAssessor | None = None,
        graph_backend: RustGraphBackend | None = None,
    ) -> None:
        self.repository = repository
        self.store = SubscriptionStore(repository)
        self.assessor = assessor or OfflineAssessor({})
        self.graph_backend = graph_backend

    def ingest_snapshot(self, snapshot: EvidenceSnapshot) -> None:
        self.repository.ingest(
            SourceBundle(
                source=snapshot.fact.source,
                facts=(snapshot.fact,),
                raw_document={"text": snapshot.document},
            )
        )
        stored = {fact.fact_id: fact for fact in self.repository.list_facts()}
        if stored.get(snapshot.fact.fact_id) != snapshot.fact:
            raise ValueError("snapshot does not match immutable stored fact metadata")

    def register(self, subscription: Subscription, access: AccessContext, at: datetime) -> None:
        if access.principal != subscription.reviewer:
            raise PermissionError("only the declared reviewer may register this subscription")
        if any(
            (item.subscription_id, item.revision)
            == (subscription.subscription_id, subscription.revision)
            for item in self.store.subscriptions()
        ):
            self.store.register(subscription)
            return
        facts = {fact.fact_id: fact for fact in self.repository.list_facts()}
        for dependency in subscription.dependencies:
            fact = facts.get(dependency)
            if fact is None or not can_read(fact, access):
                raise PermissionError("dependency unavailable in current access context")
            packet = ContextRetriever(self.repository).retrieve(
                ContextQuery(
                    service=subscription.service,
                    environment=subscription.environment,
                    predicate=fact.predicate,
                    fact_type=fact.fact_type,
                    question_time=at,
                    access=access,
                )
            )
            if dependency not in {item.fact_id for item in packet.evidence}:
                raise ValueError("subscription must pin currently admissible evidence")
        self.store.register(subscription)

    def process(
        self, event: ChangeEvent, access: AccessContext, at: datetime
    ) -> tuple[Receipt, ...]:
        with self.repository.transaction():
            return self._process(event, access, at)

    def _process(
        self, event: ChangeEvent, access: AccessContext, at: datetime
    ) -> tuple[Receipt, ...]:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("processing time must be timezone-aware")
        if event.available_at > at or event.observed_at > at:
            raise ValueError("event is not yet available")
        prior = self.store.event(event.event_id)
        if prior is not None:
            if prior != event:
                raise ValueError("event identity collision")
            subscriptions = {(s.subscription_id, s.revision): s for s in self.store.subscriptions()}
            return tuple(
                r
                for r in self.store.receipts()
                if r.event_id == event.event_id
                and self._authorized(
                    subscriptions[(r.subscription_id, r.revision)], event, access, at
                )
            )
        self.ingest_snapshot(event.old)
        self.ingest_snapshot(event.new)
        ordering = self.store.classify_order(event)
        projected_ids: tuple[str, ...] = ()
        graph_fingerprint = "flat-v1"
        truncated = False
        if self.graph_backend is not None:
            facts = self.repository.list_facts()
            manifest = build_projection(
                facts,
                self.repository.list_relationships(),
                seeds=frozenset(
                    fact.fact_id
                    for fact in facts
                    if fact.source.source_id == event.old.fact.source.source_id
                ),
                service=event.new.fact.service,
                environment=event.new.fact.environment,
                question_time=at,
                access=access,
            )
            impact = select_impact(manifest, graph_enabled=True, backend=self.graph_backend)
            projected_ids = impact.nodes
            truncated = impact.truncated
            graph_fingerprint = content_digest(impact)
        latest: dict[str, Subscription] = {}
        for subscription in self.store.subscriptions():
            existing = latest.get(subscription.subscription_id)
            if existing is None or existing.revision < subscription.revision:
                latest[subscription.subscription_id] = subscription
        receipts: list[Receipt] = []
        invalidations: dict[str, InvalidationRecord] = {}
        for subscription in latest.values():
            if (subscription.service, subscription.environment) != (
                event.new.fact.service,
                event.new.fact.environment,
            ):
                continue
            affected = affected_dependencies(self.repository, subscription, event)
            affected = tuple(
                sorted(set(affected) | (set(subscription.dependencies) & set(projected_ids)))
            )
            if truncated:
                affected = subscription.dependencies
            if not affected:
                continue
            guard = evaluate_guards(
                self.repository,
                subscription,
                event,
                access,
                at,
                projected_ids=affected,
                graph_fingerprint=graph_fingerprint,
                traversal_truncated=truncated,
            )
            if not self._authorized(subscription, event, access, at):
                guard = guard.model_copy(
                    update={
                        "mandatory": True,
                        "accessible": False,
                        "reasons": tuple(dict.fromkeys((*guard.reasons, "access_denied"))),
                    }
                )
            if ordering != "ordered":
                guard = guard.model_copy(
                    update={"mandatory": True, "reasons": (*guard.reasons, f"order_{ordering}")}
                )
            bound = AssessmentInput(subscription=subscription, event=event)
            assessment = (
                self.assessor.assess(bound)
                if guard.accessible
                and not any(reason.startswith("future_") for reason in guard.reasons)
                else None
            )
            receipt = plan_receipt(bound, guard, assessment, at)
            receipts.append(receipt)
            if guard.mandatory:
                for fact_id in affected:
                    record = InvalidationRecord(
                        invalidation_id=stable_id(
                            "subscription-invalidation",
                            {
                                "event": event.key,
                                "fact": fact_id,
                            },
                        ),
                        trigger=event.trigger or "source_version_changed",
                        target_fact_id=fact_id,
                        source_fact_id=event.new.fact.fact_id,
                        service=subscription.service,
                        occurred_at=at,
                        reason="Evidence requires review; semantic advice cannot waive it.",
                    )
                    invalidations[record.invalidation_id] = record
        committed = self.store.commit_event(event, tuple(receipts), tuple(invalidations.values()))
        return tuple(
            receipt
            for receipt in committed
            if self._authorized(latest[receipt.subscription_id], event, access, at)
        )

    def _authorized(
        self, subscription: Subscription, event: ChangeEvent, access: AccessContext, at: datetime
    ) -> bool:
        if access.principal != subscription.reviewer:
            return False
        if (
            event.available_at > at
            or event.observed_at > at
            or any(
                value > at
                for snapshot in (event.old, event.new)
                for value in (
                    snapshot.fact.recorded_at,
                    snapshot.fact.observed_at,
                    snapshot.fact.source.recorded_at,
                    snapshot.fact.source.observed_at,
                )
            )
        ):
            return False
        relevant_sources = {event.old.fact.source.source_id, event.new.fact.source.source_id}
        facts = {fact.fact_id: fact for fact in self.repository.list_facts()}
        for fact_id in subscription.dependencies:
            fact = facts.get(fact_id)
            if fact is None:
                return False
            relevant_sources.add(fact.source.source_id)
        return all(
            can_read(fact, access)
            for fact in facts.values()
            if fact.source.source_id in relevant_sources
        )

    def inspect(self, access: AccessContext, at: datetime) -> dict[str, object]:
        subscriptions = {(s.subscription_id, s.revision): s for s in self.store.subscriptions()}
        packets: list[dict[str, object]] = []
        visible_ids: set[str] = set()
        for receipt in self.store.receipts():
            subscription = subscriptions[(receipt.subscription_id, receipt.revision)]
            event = self.store.event(receipt.event_id)
            if event is None or not self._authorized(subscription, event, access, at):
                continue
            current = evaluate_guards(self.repository, subscription, event, access, at)
            visible_ids.add(receipt.receipt_id)
            packets.append(
                {
                    "receipt": receipt.model_dump(mode="json"),
                    "current_guard": current.model_dump(mode="json"),
                    "subscription": subscription.model_dump(mode="json"),
                    "event": event.model_dump(mode="json"),
                    "usable_for_synthesis": False,
                }
            )
        outbox = [
            item for item in self.store.list_outbox() if set(item["receipt_ids"]) <= visible_ids
        ]
        return {
            "packets": packets,
            "outbox": outbox,
            "access_basis": "caller_asserted_local_context_not_authenticated_identity",
        }

    def dispatch(self, access: AccessContext, at: datetime, *, fail: bool = False) -> object:
        view = self.inspect(access, at)
        allowed = {item["episode_id"] for item in view["outbox"]}
        return self.store.dispatch(at, fail=fail, allowed_episode_ids=allowed)

    def review(self, command: ReviewDisposition, access: AccessContext, at: datetime) -> None:
        if command.reviewer != access.principal or command.at > at:
            raise PermissionError("review command identity or time is invalid")
        visible = self.inspect(access, at)
        if command.receipt_id not in {item["receipt"]["receipt_id"] for item in visible["packets"]}:
            raise PermissionError("receipt unavailable in current access context")
        self.store.disposition(command)
