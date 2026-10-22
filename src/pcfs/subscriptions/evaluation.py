"""Reproducible synthetic workflow comparisons, not model-accuracy estimates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from time import perf_counter

from pcfs.identifiers import content_digest
from pcfs.ingestion import SourceBundle
from pcfs.models import (
    AccessContext,
    FactRelationship,
    RelationshipType,
    SourceClass,
    SourceVersion,
)
from pcfs.store import Repository
from pcfs.subscriptions.advice import OfflineAssessor
from pcfs.subscriptions.fixtures import (
    PINNED_TIME,
    Scenario,
    demo_scenario,
    evaluation_scenarios,
    evaluation_truth,
    offline_cache,
)
from pcfs.subscriptions.models import ChangeEvent
from pcfs.subscriptions.projection import UPSTREAM_REVISION, RustGraphBackend
from pcfs.subscriptions.service import SubscriptionService

BASELINES = (
    "periodic_full_refresh",
    "lexical_compare",
    "deterministic_dependencies",
    "semantic_subscription",
)


@dataclass(frozen=True)
class _Delivery:
    event: ChangeEvent
    at: datetime
    duplicate: bool


def _timeline(scenario: Scenario) -> tuple[_Delivery, ...]:
    seen: dict[str, ChangeEvent] = {}
    clock = PINNED_TIME
    deliveries = []
    for event in scenario.events:
        clock = max(clock, event.available_at, event.observed_at)
        if event.event_id in seen and seen[event.event_id] != event:
            raise ValueError("evaluation event identity collision")
        deliveries.append(_Delivery(event, clock, event.event_id in seen))
        seen[event.event_id] = event
    return tuple(deliveries)


def _decisions(
    scenario: Scenario,
    timeline: tuple[_Delivery, ...],
    baseline: str,
    ticks: tuple[datetime, ...],
    service: SubscriptionService | None,
) -> tuple[list[dict], int]:
    rows = []
    work = len(ticks) * len(scenario.subscriptions) if baseline == BASELINES[0] else 0
    sources = {item.fact.fact_id: item.fact.source.source_id for item in scenario.facts}
    if service is not None:
        for snapshot in scenario.facts:
            service.ingest_snapshot(snapshot)
        for subscription in scenario.subscriptions:
            service.register(
                subscription,
                AccessContext(
                    principal=subscription.reviewer, environment=subscription.environment
                ),
                PINNED_TIME,
            )
    for delivery in timeline:
        event = delivery.event
        receipts = {}
        if service is not None:
            # Replay duplicate deliveries through the actual durable idempotency path.
            access = AccessContext(
                principal=scenario.subscriptions[0].reviewer,
                environment=event.new.fact.environment,
            )
            service.process(event, access, delivery.at)
        if delivery.duplicate:
            continue
        if service is not None:
            # Revoked access hides public packets, not the evaluator's durable decisions.
            receipts = {
                receipt.subscription_id: receipt
                for receipt in service.store.receipts()
                if receipt.event_id == event.event_id
            }
        for subscription in scenario.subscriptions:
            receipt = receipts.get(subscription.subscription_id)
            mandatory = None
            unknown = None
            missing_usage = None
            reasons: list[str] = []
            if baseline == "periodic_full_refresh":
                decision_at = next((tick for tick in ticks if tick >= delivery.at), None)
                alert = decision_at is not None
            elif baseline == "lexical_compare":
                affected = event.new.fact.source.source_id in {
                    sources[dependency] for dependency in subscription.dependencies
                }
                work += int(affected)
                alert = affected and event.old.document != event.new.document
                decision_at = delivery.at if alert else None
            else:
                mandatory = bool(receipt and receipt.guard.mandatory)
                alert = bool(
                    receipt
                    and (
                        receipt.guard.mandatory
                        if baseline == "deterministic_dependencies"
                        else receipt.action == "review"
                    )
                )
                decision_at = receipt.created_at if alert else None
                work += int(receipt is not None)
                if receipt is not None:
                    reasons = list(receipt.guard.reasons)
                    advice = receipt.assessment
                    unknown = advice is None or "unknown" in (
                        advice.relevance,
                        advice.contradiction,
                        advice.materiality,
                    )
                    missing_usage = (
                        advice is None
                        or advice.input_tokens is None
                        or advice.output_tokens is None
                    )
            rows.append(
                {
                    "event_id": event.event_id,
                    "subscription_id": subscription.subscription_id,
                    "sequence": event.sequence,
                    "observed_at": event.observed_at.isoformat(),
                    "processed_at": delivery.at.isoformat(),
                    "alert": alert,
                    "decision_at": decision_at.isoformat() if decision_at else None,
                    "receipt_present": receipt is not None if service is not None else None,
                    "mandatory": mandatory,
                    "guard_reasons": reasons,
                    "unknown_advice": unknown,
                    "missing_token_usage": missing_usage,
                }
            )
    return rows, work


def _score(rows: list[dict], truth: dict, horizon: datetime, work: int, elapsed: float) -> dict:
    missed = false_alerts = material = 0
    exposure = 0.0
    for row in rows:
        expected = truth[(row["event_id"], row["subscription_id"])]
        row["material_change"] = expected
        row["false_negative"] = expected and not row["alert"]
        row["false_alert"] = not expected and row["alert"]
        material += int(expected)
        missed += int(row["false_negative"])
        false_alerts += int(row["false_alert"])
        end = datetime.fromisoformat(row["decision_at"]) if row["decision_at"] else horizon
        seconds = (min(end, horizon) - datetime.fromisoformat(row["observed_at"])).total_seconds()
        row["stale_exposure_seconds"] = max(0.0, seconds) if expected else 0.0
        exposure += row["stale_exposure_seconds"]
    service_rows = [row for row in rows if row["receipt_present"]]
    service_baseline = any(row["receipt_present"] is not None for row in rows)
    return {
        "evaluated_pairs": len(rows),
        "material_changes": material,
        "alerts": sum(row["alert"] for row in rows),
        "missed_material_changes": missed,
        "false_alerts": false_alerts,
        "stale_exposure_seconds": exposure,
        "recomputation_count": work,
        "receipt_count": len(service_rows) if service_baseline else None,
        "mandatory_work_count": (
            sum(row["mandatory"] for row in service_rows) if service_baseline else None
        ),
        "unknown_advice_count": (
            sum(row["unknown_advice"] for row in service_rows) if service_baseline else None
        ),
        "missing_token_usage_count": (
            sum(row["missing_token_usage"] for row in service_rows) if service_baseline else None
        ),
        "wall_clock_seconds": elapsed,
        "cost_usd": None,
        "human_minutes": None,
        "decisions": rows,
    }


def _compare_graph(graph_backend: RustGraphBackend) -> dict:
    scenario = demo_scenario()
    event = scenario.events[0]
    subscriptions = {item.subscription_id: item for item in scenario.subscriptions}
    prerequisite = subscriptions["demo/restart"].dependencies[0]
    dependent = subscriptions["demo/latency"].dependencies[0]
    document = {"dependent": dependent, "prerequisite": prerequisite, "type": "depends_on"}
    source = SourceVersion(
        source_id="synthetic/demo/evaluation-relationship",
        version="v1",
        source_class=SourceClass.SERVICE_CATALOG,
        uri="synthetic://subscriptions/demo/evaluation-relationship",
        content_digest=content_digest(document),
        observed_at=PINNED_TIME,
        recorded_at=PINNED_TIME,
    )
    relationship = FactRelationship(
        source_fact_id=dependent,
        target_fact_id=prerequisite,
        relationship_type=RelationshipType.DEPENDS_ON,
        source=source,
        recorded_at=PINNED_TIME,
    )
    bundle = SourceBundle(source=source, relationships=(relationship,), raw_document=document)
    access = AccessContext(
        principal=scenario.subscriptions[0].reviewer, environment=event.new.fact.environment
    )
    modes = {}
    for mode, backend in (("flat", None), ("graph", graph_backend)):
        with Repository() as repository:
            service = SubscriptionService(
                repository, OfflineAssessor(offline_cache(scenario)), graph_backend=backend
            )
            for snapshot in scenario.facts:
                service.ingest_snapshot(snapshot)
            for subscription in scenario.subscriptions:
                service.register(subscription, access, PINNED_TIME)
            repository.ingest(bundle)
            service.process(event, access, max(PINNED_TIME, event.available_at, event.observed_at))
            receipts = service.store.receipts()
            reviews = [receipt for receipt in receipts if receipt.action == "review"]
            modes[mode] = {
                "receipt_ids": sorted(receipt.receipt_id for receipt in receipts),
                "subscription_ids": sorted(receipt.subscription_id for receipt in receipts),
                "review_subscription_ids": sorted(receipt.subscription_id for receipt in reviews),
                "receipt_count": len(receipts),
                "review_count": len(reviews),
                "mandatory_work_count": sum(receipt.guard.mandatory for receipt in receipts),
                "affected_fact_ids": sorted(
                    {fact_id for receipt in receipts for fact_id in receipt.guard.affected_fact_ids}
                ),
            }
    flat_ids = set(modes["flat"]["review_subscription_ids"])
    graph_ids = set(modes["graph"]["review_subscription_ids"])
    return {
        "status": "compared",
        "scenario_id": scenario.sequence_id,
        "event_id": event.event_id,
        "relationship_id": relationship.relationship_id,
        "prerequisite_fact_id": prerequisite,
        "dependent_fact_id": dependent,
        "binary_sha256": graph_backend.expected_sha256,
        "upstream_revision": UPSTREAM_REVISION,
        **modes,
        "parity": {
            "flat_reviews_retained": flat_ids <= graph_ids,
            "common_subscription_ids": sorted(flat_ids & graph_ids),
            "graph_only_subscription_ids": sorted(graph_ids - flat_ids),
            "flat_only_subscription_ids": sorted(flat_ids - graph_ids),
        },
        "reason": "Matched first-event coverage only; no performance or accuracy benefit claim.",
    }


def evaluate_subscriptions(
    *,
    periodic_interval_seconds: int = 60,
    observation_horizon_seconds: int = 300,
    graph_backend: RustGraphBackend | None = None,
) -> dict:
    """Run all fixtures with fixed virtual clocks and diagnostic elapsed timings."""
    for value in (periodic_interval_seconds, observation_horizon_seconds):
        if type(value) is not int or value <= 0:
            raise ValueError("evaluation intervals must be positive integer seconds")
    horizon = PINNED_TIME + timedelta(seconds=observation_horizon_seconds)
    ticks = tuple(
        PINNED_TIME + timedelta(seconds=offset)
        for offset in range(
            periodic_interval_seconds, observation_horizon_seconds + 1, periodic_interval_seconds
        )
    )
    scenarios = evaluation_scenarios()
    timelines = [_timeline(scenario) for scenario in scenarios]
    if any(delivery.at > horizon for timeline in timelines for delivery in timeline):
        raise ValueError("observation horizon must include every event delivery")
    runs = []
    for baseline in BASELINES:
        # Two databases for the corpus, not one database per event or trial.
        repository = Repository() if baseline in BASELINES[2:] else None
        try:
            assessor = OfflineAssessor({}) if repository is not None else None
            service = SubscriptionService(repository, assessor) if repository is not None else None
            for scenario, timeline in zip(scenarios, timelines, strict=True):
                if baseline == "semantic_subscription":
                    assessor.cache.update(offline_cache(scenario))
                started = perf_counter()
                rows, work = _decisions(scenario, timeline, baseline, ticks, service)
                runs.append((scenario, baseline, rows, work, perf_counter() - started))
        finally:
            if repository is not None:
                repository.close()

    # Labels are evaluator-only and never feed service registration, advice or decisions.
    truth = evaluation_truth()
    pairs = {(row["event_id"], row["subscription_id"]) for _, _, rows, _, _ in runs for row in rows}
    if set(truth) != pairs or any(type(value) is not bool for value in truth.values()):
        raise ValueError("evaluation truth must label exactly every unique event/subscription pair")
    scored = {
        (scenario.sequence_id, baseline): _score(rows, truth, horizon, work, elapsed)
        for scenario, baseline, rows, work, elapsed in runs
    }
    splits = {}
    for split in ("dev", "heldout"):
        selected = [scenario for scenario in scenarios if scenario.split == split]
        selected_timelines = [
            timeline
            for scenario, timeline in zip(scenarios, timelines, strict=True)
            if scenario.split == split
        ]
        summaries = {}
        for baseline in BASELINES:
            trials = [scored[(scenario.sequence_id, baseline)] for scenario in selected]
            summaries[baseline] = {
                key: None if trials[0][key] is None else sum(trial[key] for trial in trials)
                for key in trials[0]
                if key != "decisions"
            }
        splits[split] = {
            "scenario_count": len(selected),
            "families": sorted({scenario.sequence_id.split("/")[0] for scenario in selected}),
            "event_deliveries": sum(len(timeline) for timeline in selected_timelines),
            "unique_events": sum(
                not delivery.duplicate for timeline in selected_timelines for delivery in timeline
            ),
            "duplicate_deliveries": sum(
                delivery.duplicate for timeline in selected_timelines for delivery in timeline
            ),
            "baselines": summaries,
        }
    return {
        "schema_version": 1,
        "corpus": "authored_synthetic_subscription_sequences",
        "observation": {
            "start": PINNED_TIME.isoformat(),
            "horizon": horizon.isoformat(),
            "horizon_seconds": observation_horizon_seconds,
            "periodic_interval_seconds": periodic_interval_seconds,
            "periodic_ticks": [tick.isoformat() for tick in ticks],
        },
        "definitions": {
            "unit": "Unique event/subscription pair; duplicate input deliveries are scored once.",
            "processing_clock": (
                "Fixture delivery order; max(previous clock, available_at, observed_at)."
            ),
            "alert": (
                "Decision to recheck a conclusion, not a sent notification or completed review."
            ),
            "periodic_full_refresh": (
                "Refresh every subscription at fixed ticks after start through the horizon. "
                "Each delivered event/subscription pair is alerted at its first eligible tick. "
                "Quiet ticks count as work, not extra event alerts. No terminal off-schedule tick."
            ),
            "lexical_compare": (
                "Compare exact document bytes for every affected dependency source, even unchanged "
                "bytes; alert only on inequality."
            ),
            "deterministic_dependencies": (
                "Actual service receipts with an empty advice cache; alert iff guard.mandatory."
            ),
            "semantic_subscription": (
                "Actual service receipt.action with authored offline cache advice."
            ),
            "service_receipts": (
                "Internal store.receipts() decisions, not caller-visible inspection packets. "
                "Revoked access can hide packets while retaining mandatory review receipts."
            ),
            "stale_exposure_seconds": (
                "Sum over material pairs of seconds from observed_at to decision_at, or to horizon "
                "if missed. Overlapping pairs add; this is change-seconds, not union downtime. "
                "A review request ends this proxy exposure; freshness restoration is unmeasured."
            ),
            "recomputation_count": (
                "Work units: periodic subscription/ticks, all affected-source lexical comparisons, "
                "or service receipts. Not synthesis or measured compute savings."
            ),
            "unknown_advice_count": "Receipts with absent advice or any unknown advice field.",
            "missing_token_usage_count": (
                "Receipts lacking either token count, including absent advice."
            ),
            "wall_clock_seconds": (
                "Measured scenario execution including ingestion/registration, excluding database "
                "creation, cache construction and scoring; diagnostic, not benefit evidence."
            ),
            "unmeasured": (
                "Costs and human minutes are null; no pricing or productivity assumptions."
            ),
        },
        "limitations": [
            "Cache advice and evaluation_truth were authored from the same fixture texts.",
            "This is not real Jev accuracy or genuine independent model generalization.",
            "Heldout-family coverage is a harness contract check only.",
            "Mandatory source-version invalidation cannot be waived by semantic advice.",
            "No delivery transport, human review completion, or real model inference is measured.",
        ],
        "conclusion": {"proven_benefit": False, "summary": "No proven semantic benefit."},
        "flat_vs_graph": _compare_graph(graph_backend)
        if graph_backend is not None
        else {
            "status": "unavailable",
            "flat": "Actual service source guards with pinned dependency lists.",
            "graph": None,
            "parity": None,
            "reason": "No pinned graph backend was supplied; no comparison is fabricated.",
        },
        "splits": splits,
        "scenarios": [
            {
                "sequence_id": scenario.sequence_id,
                "split": scenario.split,
                "baselines": {
                    baseline: scored[(scenario.sequence_id, baseline)] for baseline in BASELINES
                },
            }
            for scenario in scenarios
        ],
    }


def render_evaluation_markdown(report: dict) -> str:
    """Render measurements without turning unavailable quantities into zeros."""
    lines = [
        "# Subscription evaluation",
        "",
        report["conclusion"]["summary"],
        "",
        f"Observation start: {report['observation']['start']}",
        f"Inclusive horizon: {report['observation']['horizon']}",
        f"Periodic interval: {report['observation']['periodic_interval_seconds']} seconds.",
        "",
        "| Split | Baseline | Missed material | False alerts | Stale change-seconds | Work units | "
        "Unknown advice | Missing token usage | Wall seconds (diagnostic) | "
        "Cost USD | Human minutes |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    columns = (
        "missed_material_changes",
        "false_alerts",
        "stale_exposure_seconds",
        "recomputation_count",
        "unknown_advice_count",
        "missing_token_usage_count",
        "wall_clock_seconds",
        "cost_usd",
        "human_minutes",
    )
    for split, results in report["splits"].items():
        for baseline, metrics in results["baselines"].items():
            values = ["null" if metrics[key] is None else str(metrics[key]) for key in columns]
            lines.append(f"| {split} | {baseline} | " + " | ".join(values) + " |")
    lines.extend(["", "## Metric definitions", ""])
    lines.extend(f"- {key}: {value}" for key, value in report["definitions"].items())
    lines.extend(["", "## Limits", ""])
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.extend(
        [
            "",
            f"Flat versus graph: {report['flat_vs_graph']['status']}.",
            report["flat_vs_graph"]["reason"],
            "",
        ]
    )
    comparison = report["flat_vs_graph"]
    if comparison["status"] == "compared":
        lines.extend(
            [
                f"Binary SHA-256: {comparison['binary_sha256']}",
                f"Upstream revision: {comparison['upstream_revision']}",
                f"Matched event: {comparison['event_id']}",
                "",
                "| Mode | Receipts | Reviews | Review subscription IDs | Receipt IDs |",
                "| --- | ---: | ---: | --- | --- |",
            ]
        )
        for mode in ("flat", "graph"):
            result = comparison[mode]
            lines.append(
                f"| {mode} | {result['receipt_count']} | {result['review_count']} | "
                f"{', '.join(result['review_subscription_ids'])} | "
                f"{', '.join(result['receipt_ids'])} |"
            )
        lines.append("")
    return "\n".join(lines)
