"""Versioned domain contracts for temporal operational context."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import IntEnum, StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pcfs.identifiers import stable_id


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceClass(StrEnum):
    SERVICE_CATALOG = "service_catalog"
    RUNBOOK = "runbook"
    DEPLOYMENT = "deployment"
    INCIDENT = "incident"
    FEATURE_FLAG = "feature_flag"
    TELEMETRY = "telemetry"


class FactType(StrEnum):
    OWNERSHIP = "ownership"
    DEPENDENCY = "dependency"
    SLO = "slo"
    METRIC_NAME = "metric_name"
    PROCEDURE = "procedure"
    FLAG = "flag"
    INCIDENT_OBSERVATION = "incident_observation"
    TEMPORARY_MITIGATION = "temporary_mitigation"
    RELEASE = "release"


class ContextState(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    DISPUTED = "disputed"
    UNKNOWN = "unknown"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class AuthorityLevel(IntEnum):
    UNVERIFIED = 0
    INFORMAL = 10
    MAINTAINER = 20
    AUTHORITATIVE = 30


class ReviewState(StrEnum):
    UNREVIEWED = "unreviewed"
    REVIEWED = "reviewed"
    REJECTED = "rejected"


class Sensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


class RelationshipType(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    SUPERSEDES = "supersedes"
    DERIVED_FROM = "derived_from"
    DEPENDS_ON = "depends_on"


class FreshnessMode(StrEnum):
    SOFT_REVIEW = "soft_review"
    HARD_EXPIRY = "hard_expiry"
    EVENT_DRIVEN = "event_driven"


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


class SourceSpan(StrictModel):
    line_start: Annotated[int, Field(ge=1)]
    line_end: Annotated[int, Field(ge=1)]
    heading: str | None = None

    @model_validator(mode="after")
    def validate_order(self) -> SourceSpan:
        if self.line_end < self.line_start:
            raise ValueError("line_end must be greater than or equal to line_start")
        return self


class SourceVersion(StrictModel):
    source_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    source_class: SourceClass
    uri: str = Field(min_length=1)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    recorded_at: datetime

    @model_validator(mode="after")
    def validate_time(self) -> SourceVersion:
        _require_aware(self.observed_at, "observed_at")
        _require_aware(self.recorded_at, "recorded_at")
        return self

    @property
    def key(self) -> str:
        return stable_id(
            "src",
            {"source_id": self.source_id, "version": self.version, "digest": self.content_digest},
        )


class FreshnessPolicy(StrictModel):
    policy_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    fact_type: FactType
    modes: frozenset[FreshnessMode]
    soft_review_after: timedelta | None = None
    hard_expire_after: timedelta | None = None
    invalidation_triggers: frozenset[str] = frozenset()
    minimum_authority: AuthorityLevel = AuthorityLevel.INFORMAL

    @model_validator(mode="after")
    def validate_modes(self) -> FreshnessPolicy:
        if FreshnessMode.SOFT_REVIEW in self.modes and self.soft_review_after is None:
            raise ValueError("soft review mode requires soft_review_after")
        if FreshnessMode.HARD_EXPIRY in self.modes and self.hard_expire_after is None:
            raise ValueError("hard expiry mode requires hard_expire_after")
        if FreshnessMode.EVENT_DRIVEN in self.modes and not self.invalidation_triggers:
            raise ValueError("event-driven mode requires at least one trigger")
        if self.soft_review_after is not None and self.soft_review_after <= timedelta(0):
            raise ValueError("soft_review_after must be positive")
        if self.hard_expire_after is not None and self.hard_expire_after <= timedelta(0):
            raise ValueError("hard_expire_after must be positive")
        return self


FactValue = str | int | float | bool | list[str] | dict[str, str]


class Fact(StrictModel):
    fact_id: str | None = None
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    value: FactValue
    fact_type: FactType
    source: SourceVersion
    source_span: SourceSpan | None = None
    observed_at: datetime
    valid_from: datetime
    valid_to: datetime | None = None
    recorded_at: datetime
    superseded_at: datetime | None = None
    authority: AuthorityLevel
    confidence: Annotated[float, Field(ge=0, le=1)] = 1.0
    owner: str = Field(min_length=1)
    review_state: ReviewState = ReviewState.UNREVIEWED
    environment: str = Field(min_length=1)
    service: str = Field(min_length=1)
    incident_scope: str | None = None
    release_scope: str | None = None
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    required_scopes: frozenset[str] = frozenset()
    freshness_policy_id: str = Field(min_length=1)
    next_review_at: datetime | None = None
    invalidation_triggers: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def validate_temporal_contract(self) -> Fact:
        for name in ("observed_at", "valid_from", "recorded_at"):
            _require_aware(getattr(self, name), name)
        for name in ("valid_to", "superseded_at", "next_review_at"):
            value = getattr(self, name)
            if value is not None:
                _require_aware(value, name)
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        if self.superseded_at is not None and self.superseded_at < self.recorded_at:
            raise ValueError("superseded_at cannot precede recorded_at")
        if self.sensitivity is Sensitivity.RESTRICTED and not self.required_scopes:
            raise ValueError("restricted facts require at least one access scope")
        if self.fact_id is None:
            identity = {
                "subject": self.subject,
                "predicate": self.predicate,
                "value": self.value,
                "fact_type": self.fact_type,
                "source_key": self.source.key,
                "valid_from": self.valid_from,
                "environment": self.environment,
                "service": self.service,
            }
            object.__setattr__(self, "fact_id", stable_id("fact", identity))
        return self

    def was_knowable_at(self, question_time: datetime) -> bool:
        _require_aware(question_time, "question_time")
        return self.recorded_at <= question_time and (
            self.superseded_at is None or self.superseded_at > question_time
        )

    def was_valid_at(self, question_time: datetime) -> bool:
        _require_aware(question_time, "question_time")
        return self.valid_from <= question_time and (
            self.valid_to is None or self.valid_to > question_time
        )


class FactRelationship(StrictModel):
    relationship_id: str | None = None
    source_fact_id: str = Field(min_length=1)
    target_fact_id: str = Field(min_length=1)
    relationship_type: RelationshipType
    source: SourceVersion
    recorded_at: datetime

    @model_validator(mode="after")
    def validate_relationship(self) -> FactRelationship:
        _require_aware(self.recorded_at, "recorded_at")
        if self.source_fact_id == self.target_fact_id:
            raise ValueError("relationships cannot point to the same fact")
        if self.relationship_id is None:
            object.__setattr__(
                self,
                "relationship_id",
                stable_id(
                    "rel",
                    {
                        "source": self.source_fact_id,
                        "target": self.target_fact_id,
                        "type": self.relationship_type,
                        "source_version": self.source.key,
                    },
                ),
            )
        return self


class AccessContext(StrictModel):
    principal: str = Field(min_length=1)
    scopes: frozenset[str] = frozenset()
    environment: str | None = None


class InvalidationRecord(StrictModel):
    invalidation_id: str
    trigger: str
    target_fact_id: str
    source_fact_id: str | None = None
    service: str
    occurred_at: datetime
    reason: str

    @model_validator(mode="after")
    def validate_time(self) -> InvalidationRecord:
        _require_aware(self.occurred_at, "occurred_at")
        return self


EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
