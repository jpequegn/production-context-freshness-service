"""Append-only DuckDB repository for source versions and temporal facts."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import duckdb
from pydantic import BaseModel, ConfigDict

from pcfs.identifiers import canonical_json, stable_id
from pcfs.ingestion import SourceBundle
from pcfs.models import (
    Fact,
    FactRelationship,
    FreshnessPolicy,
    InvalidationRecord,
    SourceVersion,
)


class IngestionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_key: str
    inserted_sources: int
    inserted_facts: int
    inserted_relationships: int
    events: tuple[str, ...]
    idempotent: bool = False


class Repository:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = duckdb.connect(self.path)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Repository:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def initialize(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS source_versions (
                source_key VARCHAR PRIMARY KEY,
                source_id VARCHAR NOT NULL,
                version VARCHAR NOT NULL,
                content_digest VARCHAR NOT NULL,
                recorded_at TIMESTAMPTZ NOT NULL,
                payload_json VARCHAR NOT NULL,
                UNIQUE(source_id, version)
            );
            CREATE TABLE IF NOT EXISTS facts (
                fact_id VARCHAR PRIMARY KEY,
                source_key VARCHAR NOT NULL,
                subject VARCHAR NOT NULL,
                predicate VARCHAR NOT NULL,
                value_json VARCHAR NOT NULL,
                fact_type VARCHAR NOT NULL,
                service VARCHAR NOT NULL,
                environment VARCHAR NOT NULL,
                recorded_at TIMESTAMPTZ NOT NULL,
                valid_from TIMESTAMPTZ NOT NULL,
                valid_to TIMESTAMPTZ,
                payload_json VARCHAR NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relationships (
                relationship_id VARCHAR PRIMARY KEY,
                source_key VARCHAR NOT NULL,
                source_fact_id VARCHAR NOT NULL,
                target_fact_id VARCHAR NOT NULL,
                relationship_type VARCHAR NOT NULL,
                recorded_at TIMESTAMPTZ NOT NULL,
                payload_json VARCHAR NOT NULL
            );
            CREATE TABLE IF NOT EXISTS freshness_policies (
                policy_key VARCHAR PRIMARY KEY,
                policy_id VARCHAR NOT NULL,
                version VARCHAR NOT NULL,
                fact_type VARCHAR NOT NULL,
                payload_json VARCHAR NOT NULL,
                UNIQUE(policy_id, version)
            );
            CREATE TABLE IF NOT EXISTS ingestion_events (
                event_id VARCHAR PRIMARY KEY,
                source_key VARCHAR NOT NULL,
                event_type VARCHAR NOT NULL,
                affected_id VARCHAR NOT NULL,
                recorded_at TIMESTAMPTZ NOT NULL,
                details_json VARCHAR NOT NULL
            );
            CREATE TABLE IF NOT EXISTS invalidations (
                invalidation_id VARCHAR PRIMARY KEY,
                trigger VARCHAR NOT NULL,
                target_fact_id VARCHAR NOT NULL,
                source_fact_id VARCHAR,
                service VARCHAR NOT NULL,
                occurred_at TIMESTAMPTZ NOT NULL,
                reason VARCHAR NOT NULL,
                payload_json VARCHAR NOT NULL
            );
            """
        )

    def ingest(self, bundle: SourceBundle) -> IngestionResult:
        self.initialize()
        existing = self.connection.execute(
            "SELECT content_digest FROM source_versions WHERE source_id = ? AND version = ?",
            [bundle.source.source_id, bundle.source.version],
        ).fetchone()
        if existing:
            if existing[0] != bundle.source.content_digest:
                raise ValueError(
                    "immutable source version collision: source_id/version has different content"
                )
            return IngestionResult(
                source_key=bundle.source.key,
                inserted_sources=0,
                inserted_facts=0,
                inserted_relationships=0,
                events=(),
                idempotent=True,
            )
        self._validate_bundle(bundle)
        event_types: list[str] = []
        self.connection.execute("BEGIN TRANSACTION")
        try:
            self.connection.execute(
                "INSERT INTO source_versions VALUES (?, ?, ?, ?, ?, ?)",
                [
                    bundle.source.key,
                    bundle.source.source_id,
                    bundle.source.version,
                    bundle.source.content_digest,
                    bundle.source.recorded_at,
                    bundle.source.model_dump_json(),
                ],
            )
            for fact in bundle.facts:
                prior = self.connection.execute(
                    """
                    SELECT fact_id, value_json FROM facts
                    WHERE subject = ? AND predicate = ? AND service = ? AND environment = ?
                    ORDER BY recorded_at DESC LIMIT 1
                    """,
                    [fact.subject, fact.predicate, fact.service, fact.environment],
                ).fetchone()
                self.connection.execute(
                    "INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        fact.fact_id,
                        bundle.source.key,
                        fact.subject,
                        fact.predicate,
                        canonical_json(fact.value),
                        fact.fact_type.value,
                        fact.service,
                        fact.environment,
                        fact.recorded_at,
                        fact.valid_from,
                        fact.valid_to,
                        fact.model_dump_json(),
                    ],
                )
                event_type = (
                    "correction"
                    if prior and prior[1] != canonical_json(fact.value)
                    else "addition"
                )
                self._record_event(
                    bundle.source,
                    event_type,
                    fact.fact_id,
                    {"prior": prior[0] if prior else None},
                )
                event_types.append(event_type)
            for relationship in bundle.relationships:
                self.connection.execute(
                    "INSERT INTO relationships VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        relationship.relationship_id,
                        bundle.source.key,
                        relationship.source_fact_id,
                        relationship.target_fact_id,
                        relationship.relationship_type.value,
                        relationship.recorded_at,
                        relationship.model_dump_json(),
                    ],
                )
                self._record_event(
                    bundle.source,
                    relationship.relationship_type.value,
                    relationship.relationship_id,
                    {
                        "source_fact_id": relationship.source_fact_id,
                        "target_fact_id": relationship.target_fact_id,
                    },
                )
                event_types.append(relationship.relationship_type.value)
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        return IngestionResult(
            source_key=bundle.source.key,
            inserted_sources=1,
            inserted_facts=len(bundle.facts),
            inserted_relationships=len(bundle.relationships),
            events=tuple(event_types),
        )

    def put_policy(self, policy: FreshnessPolicy) -> None:
        self.initialize()
        key = stable_id("policy", {"id": policy.policy_id, "version": policy.version})
        self.connection.execute(
            "INSERT OR IGNORE INTO freshness_policies VALUES (?, ?, ?, ?, ?)",
            [
                key,
                policy.policy_id,
                policy.version,
                policy.fact_type.value,
                policy.model_dump_json(),
            ],
        )

    def list_facts(self) -> tuple[Fact, ...]:
        self.initialize()
        rows = self.connection.execute(
            "SELECT payload_json FROM facts ORDER BY recorded_at, fact_id"
        ).fetchall()
        return tuple(Fact.model_validate_json(row[0]) for row in rows)

    def list_relationships(self) -> tuple[FactRelationship, ...]:
        self.initialize()
        rows = self.connection.execute(
            "SELECT payload_json FROM relationships ORDER BY recorded_at, relationship_id"
        ).fetchall()
        return tuple(FactRelationship.model_validate_json(row[0]) for row in rows)

    def list_policies(self) -> tuple[FreshnessPolicy, ...]:
        self.initialize()
        rows = self.connection.execute(
            "SELECT payload_json FROM freshness_policies ORDER BY policy_id, version"
        ).fetchall()
        return tuple(FreshnessPolicy.model_validate_json(row[0]) for row in rows)

    def record_invalidations(self, records: tuple[InvalidationRecord, ...]) -> int:
        self.initialize()
        inserted = 0
        self.connection.execute("BEGIN TRANSACTION")
        try:
            for record in records:
                before = self.connection.execute(
                    "SELECT count(*) FROM invalidations WHERE invalidation_id = ?",
                    [record.invalidation_id],
                ).fetchone()[0]
                self.connection.execute(
                    "INSERT OR IGNORE INTO invalidations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
                inserted += int(before == 0)
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        return inserted

    def invalidated_fact_ids(self, at_time: datetime) -> frozenset[str]:
        self.initialize()
        rows = self.connection.execute(
            "SELECT target_fact_id FROM invalidations WHERE occurred_at <= ?",
            [at_time],
        ).fetchall()
        return frozenset(row[0] for row in rows)

    def count(self, table: str) -> int:
        allowed = {
            "source_versions",
            "facts",
            "relationships",
            "freshness_policies",
            "ingestion_events",
            "invalidations",
        }
        if table not in allowed:
            raise ValueError(f"unsupported table: {table}")
        self.initialize()
        return int(self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])

    def _validate_bundle(self, bundle: SourceBundle) -> None:
        for fact in bundle.facts:
            if fact.source.key != bundle.source.key:
                raise ValueError("fact provenance does not match bundle source")
        fact_ids = {fact.fact_id for fact in bundle.facts}
        stored_ids = {fact.fact_id for fact in self.list_facts()}
        known_ids = fact_ids | stored_ids
        for relationship in bundle.relationships:
            if relationship.source.key != bundle.source.key:
                raise ValueError("relationship provenance does not match bundle source")
            if (
                relationship.source_fact_id not in known_ids
                or relationship.target_fact_id not in known_ids
            ):
                raise ValueError("relationship references an unknown fact")

    def _record_event(
        self,
        source: SourceVersion,
        event_type: str,
        affected_id: str,
        details: dict[str, str | None],
    ) -> None:
        timestamp = datetime.now(UTC)
        event_id = stable_id(
            "event",
            {
                "source_key": source.key,
                "event_type": event_type,
                "affected_id": affected_id,
            },
        )
        self.connection.execute(
            "INSERT INTO ingestion_events VALUES (?, ?, ?, ?, ?, ?)",
            [event_id, source.key, event_type, affected_id, timestamp, canonical_json(details)],
        )
