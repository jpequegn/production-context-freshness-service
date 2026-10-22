"""Version-bound synthetic advice and conservative review planning."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import TypeAdapter

from pcfs.identifiers import stable_id
from pcfs.subscriptions.models import Assessment, AssessmentInput, GuardResult, Receipt

MODEL_VERSION = "synthetic-authored-text-pairs-v1"
RUBRIC_VERSION = "synthetic-review-rubric-v1"
MAX_CACHE_BYTES = 1024 * 1024


def unknown_assessment(
    input: AssessmentInput,
    reason: str,
    *,
    model_version: str = MODEL_VERSION,
    rubric_version: str = RUBRIC_VERSION,
) -> Assessment:
    return Assessment(
        input_key=input.key,
        model_version=model_version,
        rubric_version=rubric_version,
        relevance="unknown",
        contradiction="unknown",
        materiality="unknown",
        uncertainty=reason,
    )


class OfflineAssessor:
    def __init__(
        self,
        cache: dict[str, Assessment],
        model_version: str = MODEL_VERSION,
        rubric_version: str = RUBRIC_VERSION,
    ) -> None:
        self.cache = cache
        self.model_version = model_version
        self.rubric_version = rubric_version

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        model_version: str = MODEL_VERSION,
        rubric_version: str = RUBRIC_VERSION,
    ) -> OfflineAssessor:
        with Path(path).open("rb") as stream:
            data = stream.read(MAX_CACHE_BYTES + 1)
        if len(data) > MAX_CACHE_BYTES:
            raise ValueError("assessment cache exceeds 1 MiB")
        cache = TypeAdapter(dict[str, Assessment]).validate_json(data)
        return cls(cache, model_version, rubric_version)

    def assess(self, input: AssessmentInput) -> Assessment:
        try:
            cached = self.cache.get(input.key)
        except Exception:
            reason = "Offline cache lookup failed; no advice is available."
        else:
            if cached is None:
                reason = "No cached assessment exists for this exact input."
            elif not isinstance(cached, Assessment):
                reason = "Cached assessment has an invalid schema."
            elif (
                cached.input_key != input.key
                or cached.model_version != self.model_version
                or cached.rubric_version != self.rubric_version
                or cached.provenance != "synthetic_cached"
            ):
                reason = "Cached assessment input, model, rubric or provenance does not match."
            else:
                return cached
        return unknown_assessment(
            input,
            reason,
            model_version=self.model_version,
            rubric_version=self.rubric_version,
        )


def plan_receipt(
    input: AssessmentInput,
    guard: GuardResult,
    assessment: Assessment | None,
    at: datetime,
) -> Receipt:
    if not guard.accessible:
        assessment = None
    elif assessment is not None and assessment.input_key != input.key:
        assessment = unknown_assessment(input, "Supplied assessment belongs to another input.")
    no_action = (
        not guard.mandatory
        and guard.accessible
        and assessment is not None
        and assessment.relevance != "unknown"
        and assessment.contradiction == "supports"
        and assessment.materiality == "unchanged"
    )
    return Receipt(
        receipt_id=stable_id(
            "receipt",
            {
                "subscription_key": input.subscription.key,
                "event_key": input.event.key,
                "input_key": input.key,
            },
            length=64,
        ),
        subscription_id=input.subscription.subscription_id,
        revision=input.subscription.revision,
        event_id=input.event.event_id,
        input_key=input.key,
        guard=guard,
        assessment=assessment,
        action="none" if no_action else "review",
        created_at=at,
    )
