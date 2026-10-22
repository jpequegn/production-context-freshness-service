"""Transactional subscription ledger on the repository's existing connection."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Literal

from pcfs.identifiers import canonical_json, content_digest, stable_id
from pcfs.models import Fact, InvalidationRecord
from pcfs.store import Repository
from pcfs.subscriptions.models import (
    AssessmentInput,
    ChangeEvent,
    Receipt,
    ReviewDisposition,
    Subscription,
)

Order = Literal["ordered", "late", "gap"]


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("time must be timezone-aware")


class SubscriptionStore:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self.connection = repository.connection
        repository.initialize()
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS subscription_revisions (
                subscription_id VARCHAR NOT NULL,
                revision INTEGER NOT NULL,
                payload_hash VARCHAR NOT NULL,
                payload_json VARCHAR NOT NULL,
                PRIMARY KEY (subscription_id, revision)
            );
            CREATE TABLE IF NOT EXISTS subscription_events (
                event_id VARCHAR PRIMARY KEY,
                payload_hash VARCHAR NOT NULL,
                payload_json VARCHAR NOT NULL,
                source_id VARCHAR NOT NULL,
                sequence BIGINT NOT NULL,
                ordering VARCHAR NOT NULL
            );
            CREATE TABLE IF NOT EXISTS subscription_frontiers (
                source_id VARCHAR PRIMARY KEY,
                sequence BIGINT NOT NULL,
                source_key VARCHAR NOT NULL
            );
            CREATE TABLE IF NOT EXISTS subscription_receipts (
                receipt_id VARCHAR PRIMARY KEY,
                event_id VARCHAR NOT NULL,
                subscription_id VARCHAR NOT NULL,
                revision INTEGER NOT NULL,
                payload_json VARCHAR NOT NULL,
                UNIQUE(event_id, subscription_id, revision)
            );
            CREATE TABLE IF NOT EXISTS subscription_episodes (
                episode_id VARCHAR PRIMARY KEY,
                subscription_id VARCHAR NOT NULL,
                revision INTEGER NOT NULL,
                status VARCHAR NOT NULL,
                attempts INTEGER NOT NULL,
                due TIMESTAMPTZ NOT NULL,
                next_attempt TIMESTAMPTZ NOT NULL,
                resolved BOOLEAN NOT NULL
            );
            CREATE TABLE IF NOT EXISTS subscription_episode_receipts (
                episode_id VARCHAR NOT NULL,
                receipt_id VARCHAR NOT NULL UNIQUE,
                position INTEGER NOT NULL,
                PRIMARY KEY(episode_id, position)
            );
            CREATE TABLE IF NOT EXISTS subscription_commands (
                command_id VARCHAR PRIMARY KEY,
                payload_hash VARCHAR NOT NULL,
                result_json VARCHAR NOT NULL
            );
            CREATE TABLE IF NOT EXISTS subscription_history (
                position BIGINT PRIMARY KEY,
                payload_json VARCHAR NOT NULL
            );
            """
        )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self.connection.execute("BEGIN TRANSACTION")
        try:
            yield
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def register(self, sub: Subscription) -> None:
        sub = Subscription.model_validate(sub.model_dump())
        with self._transaction():
            existing = self.connection.execute(
                """SELECT payload_hash FROM subscription_revisions
                   WHERE subscription_id = ? AND revision = ?""",
                [sub.subscription_id, sub.revision],
            ).fetchone()
            if existing:
                if existing[0] != sub.key:
                    raise ValueError("immutable subscription revision collision")
                return
            for fact_id in sub.dependencies:
                fact = self._fact(fact_id)
                if fact.source.key != sub.source_versions[fact_id]:
                    raise ValueError("subscription source key does not match pinned fact")
                if (fact.service, fact.environment) != (sub.service, sub.environment):
                    raise ValueError("subscription scope does not match pinned fact")
                self._source_exists(fact)
            self.connection.execute(
                "INSERT INTO subscription_revisions VALUES (?, ?, ?, ?)",
                [sub.subscription_id, sub.revision, sub.key, sub.model_dump_json()],
            )
            self.connection.execute(
                """UPDATE subscription_episodes SET resolved = TRUE, status = 'superseded'
                   WHERE subscription_id = ? AND revision < ? AND NOT resolved""",
                [sub.subscription_id, sub.revision],
            )

    def subscriptions(self) -> tuple[Subscription, ...]:
        """Return every immutable registered revision."""
        rows = self.connection.execute(
            """SELECT payload_json FROM subscription_revisions
               ORDER BY subscription_id, revision"""
        ).fetchall()
        return tuple(Subscription.model_validate_json(row[0]) for row in rows)

    def _fact(self, fact_id: str) -> Fact:
        row = self.connection.execute(
            "SELECT payload_json FROM facts WHERE fact_id = ?", [fact_id]
        ).fetchone()
        if row is None:
            raise ValueError("fact must be durably ingested before subscription acceptance")
        return Fact.model_validate_json(row[0])

    def _source_exists(self, fact: Fact) -> None:
        row = self.connection.execute(
            "SELECT payload_json FROM source_versions WHERE source_key = ?", [fact.source.key]
        ).fetchone()
        if row is None or content_digest(json.loads(row[0])) != content_digest(fact.source):
            raise ValueError("source must be durably ingested with matching provenance")

    def classify_order(self, event: ChangeEvent) -> Order:
        event = ChangeEvent.model_validate(event.model_dump())
        existing = self.connection.execute(
            "SELECT payload_hash, ordering FROM subscription_events WHERE event_id = ?",
            [event.event_id],
        ).fetchone()
        if existing:
            if existing[0] != event.key:
                raise ValueError("immutable event ID collision")
            return existing[1]
        conflict = self.connection.execute(
            """SELECT 1 FROM subscription_events
               WHERE source_id = ? AND sequence = ? AND payload_hash <> ? LIMIT 1""",
            [event.new.fact.source.source_id, event.sequence, event.key],
        ).fetchone()
        if conflict:
            return "late"
        frontier = self.connection.execute(
            "SELECT sequence, source_key FROM subscription_frontiers WHERE source_id = ?",
            [event.new.fact.source.source_id],
        ).fetchone()
        if frontier is None:
            return "ordered" if event.sequence == 1 else "gap"
        if event.sequence <= frontier[0]:
            return "late"
        if event.sequence != frontier[0] + 1 or event.old.fact.source.key != frontier[1]:
            return "gap"
        return "ordered"

    def commit_event(
        self,
        event: ChangeEvent,
        receipts: tuple[Receipt, ...],
        invalidations: tuple[InvalidationRecord, ...] = (),
    ) -> tuple[Receipt, ...]:
        event = ChangeEvent.model_validate(event.model_dump())
        with self._transaction():
            existing = self.connection.execute(
                "SELECT payload_hash FROM subscription_events WHERE event_id = ?",
                [event.event_id],
            ).fetchone()
            if existing:
                if existing[0] != event.key:
                    raise ValueError("immutable event ID collision")
                return self._event_receipts(event.event_id)
            ordering = self.classify_order(event)
            for snapshot in (event.old, event.new):
                fact = self._fact(snapshot.fact.fact_id)
                if content_digest(fact) != content_digest(snapshot.fact):
                    raise ValueError("event snapshot does not match the ingested fact")
                self._source_exists(fact)
            latest = {sub.subscription_id: sub for sub in self.subscriptions()}
            subscriptions = {
                (sub.subscription_id, sub.revision): sub
                for sub in latest.values()
                if (sub.service, sub.environment)
                == (event.new.fact.service, event.new.fact.environment)
            }
            receipts = tuple(Receipt.model_validate(item.model_dump()) for item in receipts)
            keys = [(item.subscription_id, item.revision) for item in receipts]
            direct = {
                key
                for key, sub in subscriptions.items()
                if any(
                    self._fact(fact_id).source.source_id == event.old.fact.source.source_id
                    for fact_id in sub.dependencies
                )
            }
            if (
                len(keys) != len(set(keys))
                or not direct.issubset(keys)
                or not set(keys).issubset(subscriptions)
            ):
                raise ValueError(
                    "receipts must cover direct subscriptions without unknowns or duplicates"
                )
            for receipt in receipts:
                key = (receipt.subscription_id, receipt.revision)
                if key not in direct:
                    affected = set(receipt.guard.affected_fact_ids)
                    if not affected or not affected.issubset(subscriptions[key].dependencies):
                        raise ValueError("additional receipts require explicit pinned graph impact")
            self.connection.execute(
                "INSERT INTO subscription_events VALUES (?, ?, ?, ?, ?, ?)",
                [
                    event.event_id,
                    event.key,
                    event.model_dump_json(),
                    event.new.fact.source.source_id,
                    event.sequence,
                    ordering,
                ],
            )
            mandatory: set[str] = set()
            for receipt in receipts:
                sub = subscriptions[(receipt.subscription_id, receipt.revision)]
                expected_input = AssessmentInput(subscription=sub, event=event).key
                if receipt.event_id != event.event_id or receipt.input_key != expected_input:
                    raise ValueError("receipt must bind the exact event and subscription input")
                if ordering != "ordered" and receipt.action != "review":
                    raise ValueError("late or gap events require review")
                if receipt.created_at < event.available_at:
                    raise ValueError("receipt cannot precede event availability")
                if receipt.guard.mandatory:
                    mandatory.update(receipt.guard.affected_fact_ids)
                if self.connection.execute(
                    "SELECT 1 FROM subscription_receipts WHERE receipt_id = ?",
                    [receipt.receipt_id],
                ).fetchone():
                    raise ValueError("immutable receipt ID collision")
                self.connection.execute(
                    "INSERT INTO subscription_receipts VALUES (?, ?, ?, ?, ?)",
                    [
                        receipt.receipt_id,
                        event.event_id,
                        receipt.subscription_id,
                        receipt.revision,
                        receipt.model_dump_json(),
                    ],
                )
                if receipt.action == "review":
                    self._enqueue(sub, receipt)
            records = tuple(
                InvalidationRecord.model_validate(item.model_dump()) for item in invalidations
            )
            if not mandatory.issubset({item.target_fact_id for item in records}):
                raise ValueError("mandatory affected facts require atomic invalidations")
            for record in records:
                self._insert_invalidation(event, record)
            if ordering == "ordered":
                self.connection.execute(
                    """INSERT INTO subscription_frontiers VALUES (?, ?, ?)
                       ON CONFLICT(source_id) DO UPDATE
                       SET sequence = excluded.sequence, source_key = excluded.source_key""",
                    [event.new.fact.source.source_id, event.sequence, event.new.fact.source.key],
                )
            return self._event_receipts(event.event_id)

    def _insert_invalidation(self, event: ChangeEvent, record: InvalidationRecord) -> None:
        target = self._fact(record.target_fact_id)
        if (target.service, target.environment) != (
            event.new.fact.service,
            event.new.fact.environment,
        ) or record.service != target.service:
            raise ValueError("invalidation scope must match event and target")
        if record.source_fact_id is not None:
            source = self._fact(record.source_fact_id)
            if (source.service, source.environment) != (target.service, target.environment):
                raise ValueError("invalidation source scope does not match target")
        existing = self.connection.execute(
            "SELECT payload_json FROM invalidations WHERE invalidation_id = ?",
            [record.invalidation_id],
        ).fetchone()
        if existing:
            if InvalidationRecord.model_validate_json(existing[0]) != record:
                raise ValueError("immutable invalidation ID collision")
            return
        self.connection.execute(
            "INSERT INTO invalidations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                record.invalidation_id,
                record.trigger,
                record.target_fact_id,
                record.source_fact_id,
                record.service,
                record.occurred_at,
                record.reason,
                record.model_dump_json(),
            ],
        )

    def _enqueue(self, sub: Subscription, receipt: Receipt) -> None:
        row = self.connection.execute(
            """SELECT episode_id, due AT TIME ZONE 'UTC' FROM subscription_episodes
               WHERE subscription_id = ? AND revision = ? AND NOT resolved""",
            [sub.subscription_id, sub.revision],
        ).fetchone()
        if row is None:
            episode_id = stable_id("episode", {"receipt": receipt.receipt_id})
            due = receipt.created_at
            if not receipt.guard.mandatory:
                due = min(due + timedelta(seconds=sub.debounce_seconds), sub.deadline)
            self.connection.execute(
                "INSERT INTO subscription_episodes VALUES (?, ?, ?, 'pending', 0, ?, ?, FALSE)",
                [episode_id, sub.subscription_id, sub.revision, due, due],
            )
        else:
            episode_id, due = row
            due = due.replace(tzinfo=UTC)
            if receipt.guard.mandatory and receipt.created_at < due:
                self.connection.execute(
                    """UPDATE subscription_episodes SET due = ?,
                       next_attempt = CASE WHEN attempts = 0 THEN ? ELSE next_attempt END
                       WHERE episode_id = ?""",
                    [receipt.created_at, receipt.created_at, episode_id],
                )
        position = self.connection.execute(
            "SELECT count(*) FROM subscription_episode_receipts WHERE episode_id = ?", [episode_id]
        ).fetchone()[0]
        self.connection.execute(
            "INSERT INTO subscription_episode_receipts VALUES (?, ?, ?)",
            [episode_id, receipt.receipt_id, position],
        )

    def dispatch(
        self,
        now: datetime,
        fail: bool = False,
        allowed_episode_ids: set[str] | None = None,
    ) -> list[dict]:
        _aware(now)
        results = []
        with self._transaction():
            rows = self.connection.execute(
                """SELECT episode_id, attempts FROM subscription_episodes
                   WHERE status = 'pending' AND NOT resolved AND next_attempt <= ?
                   ORDER BY next_attempt, episode_id""",
                [now],
            ).fetchall()
            for episode_id, attempts in rows:
                if allowed_episode_ids is not None and episode_id not in allowed_episode_ids:
                    continue
                attempts += 1
                status = (
                    "dead_letter" if fail and attempts >= 3 else "pending" if fail else "delivered"
                )
                next_attempt = now + timedelta(seconds=60 * 2 ** (attempts - 1))
                self.connection.execute(
                    """UPDATE subscription_episodes SET status = ?, attempts = ?, next_attempt = ?
                       WHERE episode_id = ?""",
                    [status, attempts, next_attempt, episode_id],
                )
                count = self.connection.execute(
                    "SELECT count(*) FROM subscription_episode_receipts WHERE episode_id = ?",
                    [episode_id],
                ).fetchone()[0]
                result = {
                    "episode_id": episode_id,
                    "status": status,
                    "attempts": attempts,
                    "receipt_count": count,
                }
                self._append_history({"type": "delivery", "at": now.isoformat(), **result})
                results.append(result)
        return results

    def list_outbox(self) -> list[dict]:
        rows = self.connection.execute(
            """SELECT episode_id, subscription_id, revision, status, attempts,
                      due AT TIME ZONE 'UTC'
               FROM subscription_episodes ORDER BY due, episode_id"""
        ).fetchall()
        return [
            {
                "episode_id": row[0],
                "subscription_id": row[1],
                "revision": row[2],
                "status": row[3],
                "attempts": row[4],
                "due_at": row[5].replace(tzinfo=UTC).isoformat(),
                "receipt_ids": [
                    item[0]
                    for item in self.connection.execute(
                        """SELECT receipt_id FROM subscription_episode_receipts
                           WHERE episode_id = ? ORDER BY position""",
                        [row[0]],
                    ).fetchall()
                ],
            }
            for row in rows
        ]

    def _event_receipts(self, event_id: str) -> tuple[Receipt, ...]:
        rows = self.connection.execute(
            """SELECT payload_json FROM subscription_receipts WHERE event_id = ?
               ORDER BY subscription_id, revision""",
            [event_id],
        ).fetchall()
        return tuple(Receipt.model_validate_json(row[0]) for row in rows)

    def receipts(self) -> tuple[Receipt, ...]:
        rows = self.connection.execute(
            """SELECT payload_json FROM subscription_receipts
               ORDER BY event_id, subscription_id, revision"""
        ).fetchall()
        return tuple(Receipt.model_validate_json(row[0]) for row in rows)

    def event(self, event_id: str) -> ChangeEvent | None:
        row = self.connection.execute(
            "SELECT payload_json FROM subscription_events WHERE event_id = ?", [event_id]
        ).fetchone()
        return ChangeEvent.model_validate_json(row[0]) if row else None

    def disposition(self, cmd: ReviewDisposition) -> dict:
        cmd = ReviewDisposition.model_validate(cmd.model_dump())
        with self._transaction():
            existing = self.connection.execute(
                "SELECT payload_hash, result_json FROM subscription_commands WHERE command_id = ?",
                [cmd.command_id],
            ).fetchone()
            if existing:
                if existing[0] != cmd.key:
                    raise ValueError("immutable disposition command ID collision")
                return json.loads(existing[1])
            row = self.connection.execute(
                """SELECT r.payload_json, s.payload_json
                   FROM subscription_receipts r JOIN subscription_revisions s
                   USING(subscription_id, revision) WHERE r.receipt_id = ?""",
                [cmd.receipt_id],
            ).fetchone()
            if row is None:
                raise ValueError("unknown receipt")
            receipt = Receipt.model_validate_json(row[0])
            sub = Subscription.model_validate_json(row[1])
            if cmd.reviewer != sub.reviewer:
                raise ValueError("reviewer must exactly match the subscription reviewer")
            if cmd.at < receipt.created_at:
                raise ValueError("disposition cannot precede its receipt")
            episode = self.connection.execute(
                """SELECT e.episode_id, e.resolved, l.position
                   FROM subscription_episodes e JOIN subscription_episode_receipts l
                   USING(episode_id) WHERE l.receipt_id = ?""",
                [cmd.receipt_id],
            ).fetchone()
            resolved = False
            episode_id = episode[0] if episode else None
            if episode and not episode[1] and cmd.decision != "deferred":
                latest = self.connection.execute(
                    """SELECT max(position) FROM subscription_episode_receipts
                       WHERE episode_id = ?""",
                    [episode_id],
                ).fetchone()[0]
                if episode[2] == latest:
                    resolved = True
                    self.connection.execute(
                        """UPDATE subscription_episodes SET resolved = TRUE, status = 'resolved'
                           WHERE episode_id = ?""",
                        [episode_id],
                    )
            result = {
                "type": "disposition",
                **cmd.model_dump(mode="json"),
                "episode_id": episode_id,
                "resolved": resolved,
            }
            self.connection.execute(
                "INSERT INTO subscription_commands VALUES (?, ?, ?)",
                [cmd.command_id, cmd.key, canonical_json(result)],
            )
            self._append_history(result)
            return result

    def _append_history(self, record: dict) -> None:
        position = self.connection.execute(
            "SELECT coalesce(max(position), 0) + 1 FROM subscription_history"
        ).fetchone()[0]
        self.connection.execute(
            "INSERT INTO subscription_history VALUES (?, ?)", [position, canonical_json(record)]
        )

    def history(self) -> list[dict]:
        rows = self.connection.execute(
            "SELECT payload_json FROM subscription_history ORDER BY position"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]
