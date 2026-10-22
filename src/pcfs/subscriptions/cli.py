"""Offline subscription demonstrations and explicit local review commands."""

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from pcfs.identifiers import canonical_json
from pcfs.models import AccessContext
from pcfs.store import Repository
from pcfs.subscriptions.advice import OfflineAssessor
from pcfs.subscriptions.fixtures import Scenario, demo_scenario, offline_cache
from pcfs.subscriptions.models import Assessment, ReviewDisposition
from pcfs.subscriptions.projection import RustGraphBackend
from pcfs.subscriptions.service import SubscriptionService

app = typer.Typer(no_args_is_help=True, help="Replay advisory evidence-change subscriptions.")


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("time must include a UTC offset")
    return parsed


def _read(path: Path) -> object:
    if path.stat().st_size > 2_000_000:
        raise ValueError("input exceeds 2 MB")
    return json.loads(path.read_text())


def replay_scenario(
    repository: Repository,
    scenario: Scenario,
    assessor: OfflineAssessor,
    access: AccessContext,
    graph_backend: RustGraphBackend | None = None,
) -> dict[str, object]:
    service = SubscriptionService(repository, assessor, graph_backend)
    for snapshot in scenario.facts:
        service.ingest_snapshot(snapshot)
    initial = max(snapshot.fact.recorded_at for snapshot in scenario.facts)
    for subscription in scenario.subscriptions:
        service.register(subscription, access, initial)
    deliveries: list[dict[str, object]] = []
    clock = initial
    for event in scenario.events:
        clock = max(clock, event.available_at)
        receipts = service.process(event, access, clock)
        deliveries.append(
            {"event_id": event.event_id, "receipt_ids": [item.receipt_id for item in receipts]}
        )
    view = service.inspect(access, clock)
    return {
        "sequence_id": scenario.sequence_id,
        "deliveries": deliveries,
        **view,
        "clock": clock.isoformat(),
        "provenance": "synthetic_cached_not_live_jev",
        "human_minutes": None,
        "measured_token_savings": None,
    }


def _write_report(report: dict[str, object], directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "impact.json").write_text(canonical_json(report) + "\n")
    packets = report["packets"]
    lines = [
        "# Subscription impact",
        "",
        "Synthetic offline evidence. No live Jev accuracy claim.",
        "",
        f"Unique receipts: {len(packets)}",
        f"Review episodes: {len(report['outbox'])}",
        "",
    ]
    for packet in packets:
        receipt = packet["receipt"]
        lines.append(
            f"- `{receipt['event_id']}`: {receipt['action']}; "
            f"mandatory={receipt['guard']['mandatory']}"
        )
    lines.extend(
        [
            "",
            "Exact old/new documents and current guards are in impact.json.",
            "Review disposition never changes source authority or expiry.",
            "",
        ]
    )
    (directory / "impact.md").write_text("\n".join(lines))


@app.command("demo")
def demo(
    db: Annotated[Path, typer.Option()],
    report_dir: Annotated[Path, typer.Option()],
    graph_adapter: Annotated[Path | None, typer.Option()] = None,
    adapter_sha256: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Replay three conclusions, six facts and four event deliveries."""
    try:
        scenario = demo_scenario()
        backend = _backend(graph_adapter, adapter_sha256)
        with Repository(db) as repository:
            report = replay_scenario(
                repository,
                scenario,
                OfflineAssessor(offline_cache(scenario)),
                AccessContext(principal="synthetic-reviewer"),
                backend,
            )
        _write_report(report, report_dir)
        typer.echo(
            canonical_json(
                {
                    "report": str(report_dir / "impact.json"),
                    "unique_receipts": len(report["packets"]),
                    "review_episodes": len(report["outbox"]),
                }
            )
        )
    except (ValueError, OSError, PermissionError) as error:
        raise typer.BadParameter(str(error)) from error


@app.command("replay")
def replay(
    db: Annotated[Path, typer.Option()],
    scenario_file: Annotated[Path, typer.Option()],
    principal: Annotated[str, typer.Option()],
    report_dir: Annotated[Path, typer.Option()],
    responses: Annotated[Path | None, typer.Option()] = None,
    scope: Annotated[list[str] | None, typer.Option()] = None,
    graph_adapter: Annotated[Path | None, typer.Option()] = None,
    adapter_sha256: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Accept a bounded scenario and optional exact-input synthetic response cache."""
    try:
        scenario = Scenario.model_validate(_read(scenario_file))
        raw_cache = _read(responses) if responses else {}
        if not isinstance(raw_cache, dict):
            raise ValueError("responses must be an input-key mapping")
        cache = {key: Assessment.model_validate(value) for key, value in raw_cache.items()}
        with Repository(db) as repository:
            report = replay_scenario(
                repository,
                scenario,
                OfflineAssessor(cache),
                AccessContext(principal=principal, scopes=frozenset(scope or ())),
                _backend(graph_adapter, adapter_sha256),
            )
        _write_report(report, report_dir)
        typer.echo(str(report_dir / "impact.json"))
    except (ValueError, OSError, PermissionError) as error:
        raise typer.BadParameter(str(error)) from error


def _backend(path: Path | None, digest: str | None) -> RustGraphBackend | None:
    if (path is None) != (digest is None):
        raise ValueError("graph adapter path and SHA-256 must be supplied together")
    if path is None:
        return None
    return RustGraphBackend(binary_path=path, expected_sha256=digest)


@app.command("inspect")
@app.command("export")
def inspect(
    db: Annotated[Path, typer.Option()],
    at: Annotated[str, typer.Option()],
    principal: Annotated[str, typer.Option()],
    scope: Annotated[list[str] | None, typer.Option()] = None,
) -> None:
    """Export authorized review evidence with present-time freshness checks."""
    try:
        if not db.is_file():
            raise ValueError("database does not exist")
        with Repository(db) as repository:
            report = SubscriptionService(repository).inspect(
                AccessContext(principal=principal, scopes=frozenset(scope or ())), _time(at)
            )
        typer.echo(canonical_json(report))
    except (ValueError, OSError, PermissionError) as error:
        raise typer.BadParameter(str(error)) from error


@app.command("dispatch")
def dispatch(
    db: Annotated[Path, typer.Option()],
    at: Annotated[str, typer.Option()],
    principal: Annotated[str, typer.Option()],
    scope: Annotated[list[str] | None, typer.Option()] = None,
    fail: Annotated[bool, typer.Option()] = False,
) -> None:
    """Record a local opaque delivery receipt, or simulate a failed attempt."""
    try:
        if not db.is_file():
            raise ValueError("database does not exist")
        with Repository(db) as repository:
            result = SubscriptionService(repository).dispatch(
                AccessContext(principal=principal, scopes=frozenset(scope or ())),
                _time(at),
                fail=fail,
            )
        typer.echo(canonical_json(result))
    except (ValueError, OSError, PermissionError) as error:
        raise typer.BadParameter(str(error)) from error


@app.command("review")
def review(
    db: Annotated[Path, typer.Option()],
    command_file: Annotated[Path, typer.Option()],
    at: Annotated[str, typer.Option()],
    principal: Annotated[str, typer.Option()],
    scope: Annotated[list[str] | None, typer.Option()] = None,
) -> None:
    """Append a reviewer disposition without changing a conclusion or fact."""
    try:
        if not db.is_file():
            raise ValueError("database does not exist")
        command = ReviewDisposition.model_validate(_read(command_file))
        with Repository(db) as repository:
            SubscriptionService(repository).review(
                command,
                AccessContext(principal=principal, scopes=frozenset(scope or ())),
                _time(at),
            )
        typer.echo("review disposition recorded; source policy unchanged")
    except (ValueError, OSError, PermissionError) as error:
        raise typer.BadParameter(str(error)) from error


@app.command("evaluate")
def evaluate(
    report_dir: Annotated[Path, typer.Option()],
    graph_adapter: Annotated[Path | None, typer.Option()] = None,
    adapter_sha256: Annotated[str | None, typer.Option()] = None,
    interval_seconds: Annotated[int, typer.Option(min=1)] = 60,
    horizon_seconds: Annotated[int, typer.Option(min=1)] = 300,
) -> None:
    """Compare matched synthetic refresh strategies without benefit claims."""
    from pcfs.subscriptions.evaluation import (
        evaluate_subscriptions,
        render_evaluation_markdown,
    )

    try:
        report = evaluate_subscriptions(
            periodic_interval_seconds=interval_seconds,
            observation_horizon_seconds=horizon_seconds,
            graph_backend=_backend(graph_adapter, adapter_sha256),
        )
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "evaluation.json").write_text(canonical_json(report) + "\n")
        (report_dir / "evaluation.md").write_text(render_evaluation_markdown(report))
        typer.echo(str(report_dir / "evaluation.json"))
    except (ValueError, OSError, PermissionError) as error:
        raise typer.BadParameter(str(error)) from error
