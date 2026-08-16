"""Command-line workflows for temporal operational context."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from pcfs import __version__
from pcfs.corpus import load_corpus, validate_corpus
from pcfs.diagnostics import inspect_status
from pcfs.evaluation import render_evaluation_markdown, run_evaluation
from pcfs.ingestion import build_corpus_bundles
from pcfs.models import AccessContext, FactType
from pcfs.policy import PolicyRegistry
from pcfs.propagation import ChangePropagator, render_impact_markdown
from pcfs.retrieval import ContextQuery, ContextRetriever, QueryMode
from pcfs.store import Repository

app = typer.Typer(no_args_is_help=True, help="Inspect temporal operational context.")
data_app = typer.Typer(help="Validate packaged operational context data.")
query_app = typer.Typer(help="Retrieve point-in-time evidence packets.")
eval_app = typer.Typer(help="Run deterministic context evaluations.")

app.add_typer(data_app, name="data")
app.add_typer(query_app, name="query")
app.add_typer(eval_app, name="eval")


@app.command()
def version() -> None:
    """Print the installed package version."""
    typer.echo(__version__)


@data_app.command("validate-corpus")
def validate_corpus_command() -> None:
    """Validate the packaged fictional operational corpus."""
    corpus = load_corpus()
    errors = validate_corpus(corpus)
    if errors:
        for error in errors:
            typer.echo(f"ERROR: {error}")
        raise typer.Exit(1)
    typer.echo(
        f"valid corpus {corpus.corpus_version}: "
        f"{len(corpus.services)} services, {len(corpus.incidents)} incidents, "
        f"{len(corpus.questions())} questions"
    )


@app.command("init")
def initialize_database(
    db: Annotated[Path, typer.Option("--db", help="DuckDB database path.")] = Path(
        "context.duckdb"
    ),
) -> None:
    """Initialize schema and versioned default freshness policies."""
    with Repository(db) as repository:
        repository.initialize()
        for policy in PolicyRegistry.defaults().all():
            repository.put_policy(policy)
        typer.echo(f"initialized {db}: {repository.count('freshness_policies')} policies")


@app.command("ingest")
def ingest_corpus(
    db: Annotated[Path, typer.Option("--db", help="DuckDB database path.")] = Path(
        "context.duckdb"
    ),
) -> None:
    """Ingest the packaged fictional corpus idempotently."""
    corpus = load_corpus()
    with Repository(db) as repository:
        repository.initialize()
        for policy in PolicyRegistry.defaults().all():
            repository.put_policy(policy)
        results = [repository.ingest(bundle) for bundle in build_corpus_bundles(corpus)]
        typer.echo(
            f"ingested {sum(result.inserted_sources for result in results)} sources, "
            f"{sum(result.inserted_facts for result in results)} facts; "
            f"{sum(result.idempotent for result in results)} sources unchanged"
        )


@query_app.command("context")
def query_context(
    service: Annotated[str, typer.Option("--service")],
    at: Annotated[str, typer.Option("--at", help="ISO-8601 question time.")],
    db: Annotated[Path, typer.Option("--db")] = Path("context.duckdb"),
    fact_type: Annotated[FactType | None, typer.Option("--fact-type")] = None,
    predicate: Annotated[str | None, typer.Option("--predicate")] = None,
    incident: Annotated[str | None, typer.Option("--incident")] = None,
    text: Annotated[str | None, typer.Option("--text")] = None,
    mode: Annotated[QueryMode, typer.Option("--mode")] = QueryMode.STRICT,
    scope: Annotated[list[str] | None, typer.Option("--scope")] = None,
) -> None:
    """Return a JSON evidence packet for a historical or current question."""
    _require_database(db)
    with Repository(db) as repository:
        packet = ContextRetriever(repository).retrieve(
            ContextQuery(
                service=service,
                question_time=_parse_time(at),
                fact_type=fact_type,
                predicate=predicate,
                incident=incident,
                text=text,
                mode=mode,
                access=AccessContext(principal="cli-user", scopes=frozenset(scope or ())),
            )
        )
    typer.echo(packet.model_dump_json(indent=2))


@app.command("propagate")
def propagate_change(
    trigger: Annotated[str, typer.Option("--trigger")],
    service: Annotated[str, typer.Option("--service")],
    at: Annotated[str, typer.Option("--at")],
    db: Annotated[Path, typer.Option("--db")] = Path("context.duckdb"),
    incident: Annotated[str | None, typer.Option("--incident")] = None,
    apply: Annotated[bool, typer.Option("--apply")] = False,
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Dry-run or apply an operational invalidation event."""
    _require_database(db)
    with Repository(db) as repository:
        report = ChangePropagator(repository).propagate(
            trigger=trigger,
            service=service,
            occurred_at=_parse_time(at),
            incident=incident,
            apply=apply,
        )
    if output_format == "json":
        typer.echo(report.model_dump_json(indent=2))
    elif output_format == "markdown":
        typer.echo(render_impact_markdown(report))
    else:
        raise typer.BadParameter("format must be json or markdown")


@app.command("status")
def database_status(
    db: Annotated[Path, typer.Option("--db")] = Path("context.duckdb"),
    at: Annotated[str | None, typer.Option("--at")] = None,
    output_format: Annotated[str, typer.Option("--format")] = "text",
) -> None:
    """Report whether stale, disputed, invalidated, or missing context needs attention."""
    _require_database(db)
    with Repository(db) as repository:
        report = inspect_status(
            repository, as_of=_parse_time(at) if at else datetime.now(UTC)
        )
    if output_format == "json":
        typer.echo(report.model_dump_json(indent=2))
    elif output_format == "text":
        label = "ATTENTION" if report.needs_attention else "HEALTHY"
        typer.echo(
            f"{label}: {report.fact_count} facts across {report.service_count} services; "
            f"states={report.state_counts}"
        )
        for reason in report.reasons:
            typer.echo(f"- {reason}")
    else:
        raise typer.BadParameter("format must be text or json")
    if report.needs_attention:
        raise typer.Exit(1)


@eval_app.command("run")
def evaluate_context(
    db: Annotated[Path, typer.Option("--db")] = Path("context.duckdb"),
    json_report: Annotated[Path | None, typer.Option("--json-report")] = None,
    markdown_report: Annotated[Path | None, typer.Option("--markdown-report")] = None,
    minimum_state_accuracy: Annotated[
        float, typer.Option("--minimum-state-accuracy", min=0, max=1)
    ] = 0.8,
) -> None:
    """Compare naive, current-only, and temporal/provenance retrieval."""
    _require_database(db)
    with Repository(db) as repository:
        report = run_evaluation(repository, minimum_state_accuracy=minimum_state_accuracy)
    _write_optional(json_report, report.model_dump_json(indent=2))
    _write_optional(markdown_report, render_evaluation_markdown(report))
    temporal = next(
        metrics for metrics in report.systems if metrics.system.value == "temporal_provenance"
    )
    typer.echo(
        f"{'PASS' if report.passed else 'FAIL'}: {report.question_count} questions; "
        f"temporal state accuracy={temporal.state_accuracy:.3f}"
    )
    if not report.passed:
        raise typer.Exit(1)


@app.command("demo")
def run_demo(
    db: Annotated[Path, typer.Option("--db")] = Path("demo-context.duckdb"),
    report_dir: Annotated[Path, typer.Option("--report-dir")] = Path("reports/demo"),
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    """Build a fresh database and run the end-to-end demonstration."""
    if db.exists():
        if not force:
            raise typer.BadParameter(f"{db} exists; pass --force to replace the demo database")
        db.unlink()
    report_dir.mkdir(parents=True, exist_ok=True)
    corpus = load_corpus()
    with Repository(db) as repository:
        repository.initialize()
        for policy in PolicyRegistry.defaults().all():
            repository.put_policy(policy)
        for bundle in build_corpus_bundles(corpus):
            repository.ingest(bundle)
        retriever = ContextRetriever(repository)
        before = retriever.retrieve(
            ContextQuery(
                service="checkout",
                fact_type=FactType.OWNERSHIP,
                question_time=datetime(2026, 1, 10, tzinfo=UTC),
                access=AccessContext(principal="demo"),
            )
        )
        disputed = retriever.retrieve(
            ContextQuery(
                service="checkout",
                fact_type=FactType.OWNERSHIP,
                question_time=datetime(2026, 2, 2, 10, tzinfo=UTC),
                access=AccessContext(principal="demo"),
            )
        )
        impact = ChangePropagator(repository).propagate(
            trigger="metric_renamed",
            service="checkout",
            occurred_at=datetime(2026, 2, 10, tzinfo=UTC),
        )
        evaluation = run_evaluation(repository)
    summary = {
        "historical_owner": before.model_dump(mode="json"),
        "disputed_owner": disputed.model_dump(mode="json"),
        "impact": impact.model_dump(mode="json"),
        "evaluation_passed": evaluation.passed,
    }
    _write_optional(report_dir / "demo-summary.json", json.dumps(summary, indent=2))
    _write_optional(report_dir / "evaluation.json", evaluation.model_dump_json(indent=2))
    _write_optional(report_dir / "evaluation.md", render_evaluation_markdown(evaluation))
    _write_optional(report_dir / "impact.md", render_impact_markdown(impact))
    typer.echo(f"demo complete: {db}; reports={report_dir}")


def _require_database(path: Path) -> None:
    if not path.exists():
        typer.echo(f"database does not exist: {path}", err=True)
        raise typer.Exit(2)


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise typer.BadParameter(f"invalid ISO-8601 timestamp: {value}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise typer.BadParameter("timestamp must include Z or an explicit UTC offset")
    return parsed


def _write_optional(path: Path | None, content: str) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
