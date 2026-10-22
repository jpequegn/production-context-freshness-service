import copy
import hashlib
import json
from pathlib import Path

import pytest

from pcfs.subscriptions import evaluation


@pytest.fixture(scope="module")
def measured():
    calls = []
    repositories = []
    repository_type = evaluation.Repository
    service_type = evaluation.SubscriptionService

    class RecordingService(service_type):
        def process(self, event, access, at):
            calls.append((event.event_id, at.isoformat()))
            return super().process(event, access, at)

    def repository():
        result = repository_type()
        repositories.append(result)
        return result

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(evaluation, "SubscriptionService", RecordingService)
        patch.setattr(evaluation, "Repository", repository)
        report = evaluation.evaluate_subscriptions()
    return report, calls, repositories


def test_literal_split_counts_and_real_service_delivery_dedup(measured):
    report, calls, repositories = measured
    assert report["observation"]["periodic_ticks"] == [
        "2026-06-01T12:01:00+00:00",
        "2026-06-01T12:02:00+00:00",
        "2026-06-01T12:03:00+00:00",
        "2026-06-01T12:04:00+00:00",
        "2026-06-01T12:05:00+00:00",
    ]
    assert len(repositories) == 2
    assert len(calls) == 116
    assert sum(event_id == "demo/procedure-reversal" for event_id, _ in calls) == 4
    dev, heldout = report["splits"]["dev"], report["splits"]["heldout"]
    assert (
        dev["scenario_count"],
        dev["event_deliveries"],
        dev["unique_events"],
        dev["duplicate_deliveries"],
    ) == (19, 40, 39, 1)
    assert (
        heldout["scenario_count"],
        heldout["event_deliveries"],
        heldout["unique_events"],
        heldout["duplicate_deliveries"],
    ) == (9, 18, 18, 0)
    assert heldout["families"] == ["late-event", "metric-rename", "provider-failure"]
    for split, pairs, material in ((dev, 45, 29), (heldout, 18, 9)):
        assert set(split["baselines"]) == set(evaluation.BASELINES)
        for metrics in split["baselines"].values():
            assert (metrics["evaluated_pairs"], metrics["material_changes"]) == (pairs, material)


@pytest.mark.parametrize(
    "split,baseline,expected",
    [
        ("dev", "periodic_full_refresh", (0, 16, 1300.0, 105, None, None)),
        ("dev", "lexical_compare", (12, 10, 2700.0, 39, None, None)),
        ("dev", "deterministic_dependencies", (0, 10, 0.0, 39, 39, 39)),
        ("dev", "semantic_subscription", (0, 10, 0.0, 39, 9, 39)),
        ("heldout", "periodic_full_refresh", (0, 9, 630.0, 45, None, None)),
        ("heldout", "lexical_compare", (0, 9, 330.0, 18, None, None)),
        ("heldout", "deterministic_dependencies", (0, 9, 330.0, 18, 18, 18)),
        ("heldout", "semantic_subscription", (0, 9, 330.0, 18, 6, 18)),
    ],
)
def test_literal_metrics(measured, split, baseline, expected):
    metrics = measured[0]["splits"][split]["baselines"][baseline]
    keys = (
        "missed_material_changes",
        "false_alerts",
        "stale_exposure_seconds",
        "recomputation_count",
        "unknown_advice_count",
        "missing_token_usage_count",
    )
    assert tuple(metrics[key] for key in keys) == expected
    assert metrics["cost_usd"] is None
    assert metrics["human_minutes"] is None
    assert metrics["wall_clock_seconds"] >= 0


def test_explicit_false_negatives_and_late_processing_clock(measured):
    scenarios = {item["sequence_id"]: item for item in measured[0]["scenarios"]}
    lexical = scenarios["access-revocation/larch"]["baselines"]["lexical_compare"]
    assert lexical["missed_material_changes"] == 2
    assert [row["false_negative"] for row in lexical["decisions"]] == [True, True]
    assert [row["stale_exposure_seconds"] for row in lexical["decisions"]] == [230.0, 220.0]
    late = scenarios["late-event/larch"]["baselines"]["semantic_subscription"]["decisions"]
    assert [row["sequence"] for row in late] == [2, 1]
    assert [row["processed_at"] for row in late] == [
        "2026-06-01T12:01:20+00:00",
        "2026-06-01T12:03:00+00:00",
    ]
    assert "order_gap" in late[0]["guard_reasons"]
    assert "source_version_changed" in late[1]["guard_reasons"]
    assert late[1]["stale_exposure_seconds"] == 110.0
    for scenario in scenarios.values():
        for baseline in evaluation.BASELINES[2:]:
            metrics = scenario["baselines"][baseline]
            assert metrics["mandatory_work_count"] == metrics["receipt_count"]


def test_periodic_has_fixed_ticks_and_misses_expose_until_horizon():
    report = evaluation.evaluate_subscriptions(periodic_interval_seconds=600)
    assert report["observation"]["periodic_ticks"] == []
    periodic = report["splits"]["dev"]["baselines"]["periodic_full_refresh"]
    assert (
        periodic["missed_material_changes"],
        periodic["recomputation_count"],
        periodic["stale_exposure_seconds"],
    ) == (29, 0, 6640.0)


def test_processing_clock_does_not_rewind_after_late_delivery():
    scenario = next(
        item for item in evaluation.evaluation_scenarios() if item.sequence_id == "late-event/larch"
    )
    reordered = scenario.model_copy(update={"events": tuple(reversed(scenario.events))})
    assert [delivery.at.isoformat() for delivery in evaluation._timeline(reordered)] == [
        "2026-06-01T12:03:00+00:00",
        "2026-06-01T12:03:00+00:00",
    ]


@pytest.mark.parametrize("family", ["access-revocation", "hard-expiry"])
def test_lexical_counts_same_byte_comparisons_as_work(family):
    scenario = next(
        item for item in evaluation.evaluation_scenarios() if item.sequence_id == f"{family}/larch"
    )
    rows, work = evaluation._decisions(
        scenario, evaluation._timeline(scenario), "lexical_compare", (), None
    )
    assert work == 2
    assert [row["alert"] for row in rows] == [False, False]


def test_revoked_access_hides_packets_but_keeps_internal_evaluation_receipts():
    scenario = next(
        item
        for item in evaluation.evaluation_scenarios()
        if item.sequence_id == "access-revocation/larch"
    )
    subscription = scenario.subscriptions[0]
    access = evaluation.AccessContext(
        principal=subscription.reviewer, environment=subscription.environment
    )
    timeline = evaluation._timeline(scenario)
    with evaluation.Repository() as repository:
        service = evaluation.SubscriptionService(
            repository, evaluation.OfflineAssessor(evaluation.offline_cache(scenario))
        )
        rows, work = evaluation._decisions(scenario, timeline, "semantic_subscription", (), service)
        assert service.inspect(access, timeline[-1].at)["packets"] == []
        assert service.inspect(access, timeline[-1].at)["outbox"] == []
        assert service.process(timeline[-1].event, access, timeline[-1].at) == ()
        receipts = service.store.receipts()
        assert len(receipts) == 2
        assert [receipt.action for receipt in receipts] == ["review", "review"]
        assert [receipt.assessment for receipt in receipts] == [None, None]
        assert work == 2
        assert [row["receipt_present"] for row in rows] == [True, True]
        assert [row["mandatory"] for row in rows] == [True, True]
        assert [row["unknown_advice"] for row in rows] == [True, True]


def test_repeatable_except_measured_wall_clock(measured):
    def without_elapsed(report):
        result = copy.deepcopy(report)
        for split in result["splits"].values():
            for metrics in split["baselines"].values():
                del metrics["wall_clock_seconds"]
        for scenario in result["scenarios"]:
            for metrics in scenario["baselines"].values():
                del metrics["wall_clock_seconds"]
        return result

    repeated = evaluation.evaluate_subscriptions()
    assert without_elapsed(repeated) == without_elapsed(measured[0])
    assert json.loads(json.dumps(repeated, allow_nan=False)) == repeated


def test_missing_truth_fails_instead_of_defaulting_to_zero(monkeypatch):
    monkeypatch.setattr(evaluation, "evaluation_truth", dict)
    with pytest.raises(ValueError, match="truth must label exactly"):
        evaluation.evaluate_subscriptions()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"periodic_interval_seconds": 0},
        {"periodic_interval_seconds": True},
        {"observation_horizon_seconds": -1},
        {"observation_horizon_seconds": 100},
    ],
)
def test_invalid_observation_configuration_fails(kwargs):
    with pytest.raises(ValueError):
        evaluation.evaluate_subscriptions(**kwargs)


def test_markdown_and_limitations_are_explicit(measured):
    report = measured[0]
    markdown = evaluation.render_evaluation_markdown(report)
    assert report["conclusion"]["proven_benefit"] is False
    assert report["flat_vs_graph"]["status"] == "unavailable"
    assert report["flat_vs_graph"]["graph"] is None
    assert report["flat_vs_graph"]["parity"] is None
    assert "No proven semantic benefit." in markdown
    assert "not real Jev accuracy" in markdown
    assert "same fixture texts" in markdown
    assert "harness contract check" in markdown
    assert "independent model generalization" in markdown
    assert "if missed" in markdown
    assert "null" in markdown
    assert "diagnostic, not benefit evidence" in markdown


@pytest.fixture(scope="module")
def evaluation_rust_backend():
    binary = Path(__file__).resolve().parents[1] / "graph-adapter/target/debug/pcfs-graph-adapter"
    if not binary.is_file():
        pytest.skip(
            "Build the real graph adapter first; installed proof also covers this comparison"
        )
    return evaluation.RustGraphBackend(
        binary_path=binary,
        expected_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
    )


def test_optional_graph_comparison_uses_real_adapter_without_changing_corpus(
    measured, evaluation_rust_backend
):
    report = evaluation.evaluate_subscriptions(graph_backend=evaluation_rust_backend)
    comparison = report["flat_vs_graph"]
    assert comparison["status"] == "compared"
    assert comparison["event_id"] == "demo/editorial"
    assert comparison["flat"]["review_subscription_ids"] == ["demo/restart"]
    assert comparison["graph"]["review_subscription_ids"] == ["demo/latency", "demo/restart"]
    assert comparison["flat"]["receipt_count"] == comparison["flat"]["review_count"] == 1
    assert comparison["graph"]["receipt_count"] == comparison["graph"]["review_count"] == 2
    assert comparison["flat"]["mandatory_work_count"] == 1
    assert comparison["graph"]["mandatory_work_count"] == 2
    assert comparison["dependent_fact_id"] in comparison["graph"]["affected_fact_ids"]
    assert set(comparison["flat"]["receipt_ids"]) <= set(comparison["graph"]["receipt_ids"])
    assert comparison["parity"] == {
        "flat_reviews_retained": True,
        "common_subscription_ids": ["demo/restart"],
        "graph_only_subscription_ids": ["demo/latency"],
        "flat_only_subscription_ids": [],
    }
    assert comparison["binary_sha256"] == evaluation_rust_backend.expected_sha256
    assert comparison["upstream_revision"] == "9194281ab2a387e820f8047de512a3b8763cc7e0"
    for split in ("dev", "heldout"):
        for baseline in evaluation.BASELINES:
            actual = report["splits"][split]["baselines"][baseline].copy()
            original = measured[0]["splits"][split]["baselines"][baseline].copy()
            del actual["wall_clock_seconds"], original["wall_clock_seconds"]
            assert actual == original
    assert json.loads(json.dumps(comparison, allow_nan=False)) == comparison
    markdown = evaluation.render_evaluation_markdown(report)
    assert evaluation_rust_backend.expected_sha256 in markdown
    assert "demo/latency" in markdown
