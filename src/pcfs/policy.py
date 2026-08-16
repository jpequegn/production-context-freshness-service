"""Freshness, authority, conflict, and abstention decisions."""

from __future__ import annotations

from datetime import datetime, timedelta

from pcfs.models import (
    AuthorityLevel,
    ContextState,
    Fact,
    FactRelationship,
    FactType,
    FreshnessMode,
    FreshnessPolicy,
    RelationshipType,
    ReviewState,
    StrictModel,
)


class DecisionReason(StrictModel):
    code: str
    message: str
    fact_ids: tuple[str, ...] = ()


class PolicyDecision(StrictModel):
    state: ContextState
    evaluated_at: datetime
    policy_id: str | None = None
    policy_version: str | None = None
    selected_fact_ids: tuple[str, ...] = ()
    excluded_fact_ids: tuple[str, ...] = ()
    reasons: tuple[DecisionReason, ...]


class PolicyRegistry:
    def __init__(self, policies: tuple[FreshnessPolicy, ...]) -> None:
        self._policies = {policy.policy_id: policy for policy in policies}

    def get(self, policy_id: str) -> FreshnessPolicy | None:
        return self._policies.get(policy_id)

    def all(self) -> tuple[FreshnessPolicy, ...]:
        return tuple(sorted(self._policies.values(), key=lambda policy: policy.policy_id))

    @classmethod
    def defaults(cls) -> PolicyRegistry:
        specs = {
            FactType.OWNERSHIP: (30, 180, {"ownership_changed"}),
            FactType.DEPENDENCY: (90, 365, {"dependency_changed"}),
            FactType.SLO: (30, 180, {"slo_changed"}),
            FactType.METRIC_NAME: (30, 180, {"metric_renamed"}),
            FactType.PROCEDURE: (14, 90, {"metric_renamed", "deploy_changed"}),
            FactType.FLAG: (1, 7, {"flag_changed"}),
            FactType.INCIDENT_OBSERVATION: (1, 7, {"incident_closed"}),
            FactType.TEMPORARY_MITIGATION: (1, 2, {"incident_closed", "permanent_fix"}),
            FactType.RELEASE: (30, 180, {"deploy_changed"}),
        }
        policies = []
        for fact_type, (soft_days, hard_days, triggers) in specs.items():
            policies.append(
                FreshnessPolicy(
                    policy_id=f"{fact_type.value}-v1",
                    version="1",
                    fact_type=fact_type,
                    modes={
                        FreshnessMode.SOFT_REVIEW,
                        FreshnessMode.HARD_EXPIRY,
                        FreshnessMode.EVENT_DRIVEN,
                    },
                    soft_review_after=timedelta(days=soft_days),
                    hard_expire_after=timedelta(days=hard_days),
                    invalidation_triggers=triggers,
                    minimum_authority=AuthorityLevel.INFORMAL,
                )
            )
        return cls(tuple(policies))


class PolicyEngine:
    def __init__(self, registry: PolicyRegistry | None = None) -> None:
        self.registry = registry or PolicyRegistry.defaults()

    def evaluate(
        self,
        facts: tuple[Fact, ...],
        *,
        question_time: datetime,
        relationships: tuple[FactRelationship, ...] = (),
        invalidated_fact_ids: frozenset[str] = frozenset(),
    ) -> PolicyDecision:
        if not facts:
            return self._decision(
                ContextState.UNKNOWN,
                question_time,
                reasons=(DecisionReason(code="no_facts", message="No candidate facts exist."),),
            )
        policy_ids = {fact.freshness_policy_id for fact in facts}
        if len(policy_ids) != 1:
            return self._decision(
                ContextState.UNKNOWN,
                question_time,
                reasons=(
                    DecisionReason(
                        code="mixed_policies",
                        message="Candidates do not share one freshness policy.",
                        fact_ids=_ids(facts),
                    ),
                ),
                excluded=_ids(facts),
            )
        policy = self.registry.get(next(iter(policy_ids)))
        if policy is None:
            return self._decision(
                ContextState.UNKNOWN,
                question_time,
                reasons=(
                    DecisionReason(
                        code="missing_policy",
                        message="The declared freshness policy is unavailable.",
                        fact_ids=_ids(facts),
                    ),
                ),
                excluded=_ids(facts),
            )

        knowable = tuple(fact for fact in facts if fact.was_knowable_at(question_time))
        if not knowable:
            return self._decision(
                ContextState.INSUFFICIENT_EVIDENCE,
                question_time,
                policy,
                reasons=(
                    DecisionReason(
                        code="future_evidence",
                        message="All candidates were recorded after the question time.",
                        fact_ids=_ids(facts),
                    ),
                ),
                excluded=_ids(facts),
            )

        current: list[Fact] = []
        stale: list[Fact] = []
        rejected: list[Fact] = []
        for fact in knowable:
            if (
                fact.review_state is ReviewState.REJECTED
                or fact.authority < policy.minimum_authority
            ):
                rejected.append(fact)
                continue
            age = question_time - fact.observed_at
            hard_expired = (
                FreshnessMode.HARD_EXPIRY in policy.modes
                and policy.hard_expire_after is not None
                and age >= policy.hard_expire_after
            )
            soft_stale = (
                FreshnessMode.SOFT_REVIEW in policy.modes
                and policy.soft_review_after is not None
                and age >= policy.soft_review_after
            )
            event_stale = fact.fact_id in invalidated_fact_ids
            if not fact.was_valid_at(question_time) or hard_expired or soft_stale or event_stale:
                stale.append(fact)
            else:
                current.append(fact)

        if not current:
            if stale:
                return self._decision(
                    ContextState.STALE,
                    question_time,
                    policy,
                    reasons=(
                        DecisionReason(
                            code="stale_or_invalid",
                            message="All otherwise admissible evidence is stale or invalid.",
                            fact_ids=_ids(stale),
                        ),
                    ),
                    excluded=_ids((*stale, *rejected)),
                )
            return self._decision(
                ContextState.INSUFFICIENT_EVIDENCE,
                question_time,
                policy,
                reasons=(
                    DecisionReason(
                        code="below_authority",
                        message="No candidate meets review and authority requirements.",
                        fact_ids=_ids(rejected),
                    ),
                ),
                excluded=_ids(rejected),
            )

        highest_authority = max(fact.authority for fact in current)
        strongest = tuple(fact for fact in current if fact.authority == highest_authority)
        strongest_ids = set(_ids(strongest))
        active_contradictions = {
            frozenset((relationship.source_fact_id, relationship.target_fact_id))
            for relationship in relationships
            if relationship.relationship_type is RelationshipType.CONTRADICTS
            and relationship.recorded_at <= question_time
        }
        values = {str(fact.value) for fact in strongest}
        explicit_conflict = any(edge <= strongest_ids for edge in active_contradictions)
        if len(values) > 1 or explicit_conflict:
            return self._decision(
                ContextState.DISPUTED,
                question_time,
                policy,
                reasons=(
                    DecisionReason(
                        code="authoritative_conflict",
                        message="Equal-authority current evidence conflicts.",
                        fact_ids=_ids(strongest),
                    ),
                ),
                selected=_ids(strongest),
                excluded=_ids(
                    (*stale, *rejected, *(fact for fact in current if fact not in strongest))
                ),
            )
        selected = tuple(sorted(strongest, key=lambda fact: (fact.recorded_at, fact.fact_id)))
        return self._decision(
            ContextState.CURRENT,
            question_time,
            policy,
            reasons=(
                DecisionReason(
                    code="current_authoritative",
                    message="Current evidence meets freshness and authority requirements.",
                    fact_ids=_ids(selected),
                ),
            ),
            selected=_ids(selected),
            excluded=_ids(tuple(fact for fact in facts if fact not in selected)),
        )

    def _decision(
        self,
        state: ContextState,
        evaluated_at: datetime,
        policy: FreshnessPolicy | None = None,
        *,
        reasons: tuple[DecisionReason, ...],
        selected: tuple[str, ...] = (),
        excluded: tuple[str, ...] = (),
    ) -> PolicyDecision:
        return PolicyDecision(
            state=state,
            evaluated_at=evaluated_at,
            policy_id=policy.policy_id if policy else None,
            policy_version=policy.version if policy else None,
            selected_fact_ids=selected,
            excluded_fact_ids=excluded,
            reasons=reasons,
        )


def _ids(facts: tuple[Fact, ...] | list[Fact]) -> tuple[str, ...]:
    return tuple(sorted(fact.fact_id for fact in facts if fact.fact_id is not None))
