"""Versioned contracts for evidence-change subscriptions."""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from pcfs.identifiers import content_digest
from pcfs.models import Fact, Sensitivity, StrictModel

NonEmptyString = Annotated[str, Field(min_length=1)]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]
TokenCount = Annotated[int, Field(strict=True, ge=0)]


class EvidenceSnapshot(StrictModel):
    schema_version: Literal[1] = 1
    fact: Fact
    document: str

    @field_validator("document")
    @classmethod
    def validate_document_size(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 64 * 1024:
            raise ValueError("document must be at most 64 KiB of UTF-8")
        return value

    @model_validator(mode="after")
    def validate_document_binding(self) -> EvidenceSnapshot:
        # Subscription text envelopes do not reinterpret legacy source documents.
        if self.fact.source.content_digest != content_digest({"text": self.document}):
            raise ValueError("fact source digest must match the exact text envelope")
        return self

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.document.encode("utf-8")).hexdigest()

    @property
    def key(self) -> str:
        return content_digest(self)


class Subscription(StrictModel):
    schema_version: Literal[1] = 1
    subscription_id: NonEmptyString
    conclusion_id: NonEmptyString
    revision: PositiveInt
    assumption: NonEmptyString
    assumption_version: NonEmptyString
    dependencies: tuple[NonEmptyString, ...] = Field(min_length=1)
    source_versions: dict[NonEmptyString, NonEmptyString]
    service: NonEmptyString
    environment: NonEmptyString
    reviewer: NonEmptyString
    deadline: AwareDatetime
    debounce_seconds: Annotated[int, Field(strict=True, ge=0, le=3600)] = 60

    @model_validator(mode="after")
    def validate_dependencies(self) -> Subscription:
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("dependencies must not contain duplicates")
        if set(self.source_versions) != set(self.dependencies):
            raise ValueError("source_versions must have exactly the dependency keys")
        return self

    @property
    def key(self) -> str:
        return content_digest(self)


class ChangeEvent(StrictModel):
    schema_version: Literal[1] = 1
    event_id: NonEmptyString
    old: EvidenceSnapshot
    new: EvidenceSnapshot
    observed_at: AwareDatetime
    available_at: AwareDatetime
    sequence: PositiveInt
    trigger: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_versions(self) -> ChangeEvent:
        old, new = self.old.fact, self.new.fact
        if old.source.source_id != new.source.source_id:
            raise ValueError("event snapshots must have the same source_id")
        for name in ("subject", "predicate", "service", "environment"):
            if getattr(old, name) != getattr(new, name):
                raise ValueError(f"event snapshots must have the same {name}")
        if old.source.version == new.source.version and self.old.document != self.new.document:
            raise ValueError("the same source version cannot change document bytes")
        if self.available_at < self.observed_at:
            raise ValueError("available_at cannot precede observed_at")
        return self

    @property
    def sensitivity(self) -> Sensitivity:
        levels = (Sensitivity.PUBLIC, Sensitivity.INTERNAL, Sensitivity.RESTRICTED)
        return max((self.old.fact.sensitivity, self.new.fact.sensitivity), key=levels.index)

    @property
    def key(self) -> str:
        return content_digest(self)


class AssessmentInput(StrictModel):
    schema_version: Literal[1] = 1
    subscription: Subscription
    event: ChangeEvent

    @model_validator(mode="after")
    def validate_scope(self) -> AssessmentInput:
        for name in ("service", "environment"):
            if getattr(self.subscription, name) != getattr(self.event.new.fact, name):
                raise ValueError(f"subscription and event must have the same {name}")
        return self

    @property
    def key(self) -> str:
        return content_digest(self)


class Assessment(StrictModel):
    schema_version: Literal[1] = 1
    input_key: NonEmptyString
    model_version: NonEmptyString
    rubric_version: NonEmptyString
    relevance: Literal["relevant", "irrelevant", "unknown"]
    contradiction: Literal["contradicts", "supports", "unknown"]
    materiality: Literal["material", "unchanged", "unknown"]
    provenance: Literal["synthetic_cached"] = "synthetic_cached"
    uncertainty: NonEmptyString = "Not assessed."
    input_tokens: TokenCount | None = None
    output_tokens: TokenCount | None = None

    @property
    def key(self) -> str:
        return content_digest(self)


class GuardResult(StrictModel):
    schema_version: Literal[1] = 1
    mandatory: bool = Field(strict=True)
    reasons: tuple[NonEmptyString, ...]
    accessible: bool = Field(strict=True)
    affected_fact_ids: tuple[NonEmptyString, ...]
    graph_fingerprint: NonEmptyString
    traversal_truncated: bool = Field(strict=True)

    @model_validator(mode="after")
    def validate_reasons(self) -> GuardResult:
        if self.mandatory and not self.reasons:
            raise ValueError("mandatory guards require at least one reason")
        if len(set(self.affected_fact_ids)) != len(self.affected_fact_ids):
            raise ValueError("affected_fact_ids must not contain duplicates")
        return self

    @property
    def key(self) -> str:
        return content_digest(self)


class Receipt(StrictModel):
    schema_version: Literal[1] = 1
    receipt_id: NonEmptyString
    subscription_id: NonEmptyString
    revision: PositiveInt
    event_id: NonEmptyString
    input_key: NonEmptyString
    guard: GuardResult
    assessment: Assessment | None = None
    action: Literal["review", "none"]
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_decision(self) -> Receipt:
        if self.guard.mandatory and self.action != "review":
            raise ValueError("mandatory guards require review")
        if self.assessment is not None:
            if self.assessment.input_key != self.input_key:
                raise ValueError("assessment input_key must match receipt input_key")
            if not self.guard.accessible:
                raise ValueError("inaccessible evidence cannot have an assessment")
        return self

    @property
    def key(self) -> str:
        return content_digest(self)


class ReviewDisposition(StrictModel):
    schema_version: Literal[1] = 1
    command_id: NonEmptyString
    receipt_id: NonEmptyString
    reviewer: NonEmptyString
    decision: Literal["confirmed", "dismissed", "deferred"]
    rationale: NonEmptyString
    at: AwareDatetime

    @field_validator("rationale")
    @classmethod
    def validate_rationale(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("rationale must not be blank")
        return value

    @property
    def key(self) -> str:
        return content_digest(self)
