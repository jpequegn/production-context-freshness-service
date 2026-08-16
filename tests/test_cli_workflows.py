import json
from pathlib import Path

from typer.testing import CliRunner

from pcfs.cli import app


def test_init_ingest_query_status_and_evaluation_workflow(tmp_path: Path) -> None:
    runner = CliRunner()
    database = tmp_path / "workflow.duckdb"
    json_report = tmp_path / "reports" / "evaluation.json"
    markdown_report = tmp_path / "reports" / "evaluation.md"

    initialized = runner.invoke(app, ["init", "--db", str(database)])
    ingested = runner.invoke(app, ["ingest", "--db", str(database)])
    repeated = runner.invoke(app, ["ingest", "--db", str(database)])
    queried = runner.invoke(
        app,
        [
            "query",
            "context",
            "--db",
            str(database),
            "--service",
            "checkout",
            "--fact-type",
            "ownership",
            "--at",
            "2026-01-10T00:00:00Z",
        ],
    )
    healthy = runner.invoke(
        app,
        ["status", "--db", str(database), "--at", "2026-01-01T12:00:00Z"],
    )
    evaluation = runner.invoke(
        app,
        [
            "eval",
            "run",
            "--db",
            str(database),
            "--json-report",
            str(json_report),
            "--markdown-report",
            str(markdown_report),
        ],
    )

    assert initialized.exit_code == 0, initialized.stdout
    assert ingested.exit_code == 0, ingested.stdout
    assert repeated.exit_code == 0, repeated.stdout
    assert "sources unchanged" in repeated.stdout
    assert queried.exit_code == 0, queried.stdout
    assert json.loads(queried.stdout)["evidence"][0]["value"] == "team-orchid"
    assert healthy.exit_code == 0, healthy.stdout
    assert healthy.stdout.startswith("HEALTHY")
    assert evaluation.exit_code == 0, evaluation.stdout
    assert json.loads(json_report.read_text())["passed"] is True
    assert "Gate: PASS" in markdown_report.read_text()


def test_status_and_eval_exit_codes_signal_attention_or_failure(tmp_path: Path) -> None:
    runner = CliRunner()
    database = tmp_path / "attention.duckdb"
    runner.invoke(app, ["ingest", "--db", str(database)])

    status = runner.invoke(
        app,
        ["status", "--db", str(database), "--at", "2026-08-16T00:00:00Z"],
    )
    failed_eval = runner.invoke(
        app,
        [
            "eval",
            "run",
            "--db",
            str(database),
            "--minimum-state-accuracy",
            "1.1",
        ],
    )

    assert status.exit_code == 1
    assert status.stdout.startswith("ATTENTION")
    assert failed_eval.exit_code != 0


def test_propagation_and_demo_write_reports(tmp_path: Path) -> None:
    runner = CliRunner()
    database = tmp_path / "propagation.duckdb"
    demo_database = tmp_path / "demo.duckdb"
    report_dir = tmp_path / "demo-reports"
    runner.invoke(app, ["ingest", "--db", str(database)])

    propagation = runner.invoke(
        app,
        [
            "propagate",
            "--db",
            str(database),
            "--trigger",
            "metric_renamed",
            "--service",
            "checkout",
            "--at",
            "2026-02-10T00:00:00Z",
            "--apply",
        ],
    )
    demo = runner.invoke(
        app,
        [
            "demo",
            "--db",
            str(demo_database),
            "--report-dir",
            str(report_dir),
        ],
    )

    assert propagation.exit_code == 0, propagation.stdout
    assert json.loads(propagation.stdout)["applied"] is True
    assert demo.exit_code == 0, demo.stdout
    assert (report_dir / "demo-summary.json").exists()
    assert (report_dir / "evaluation.json").exists()
    assert (report_dir / "evaluation.md").exists()
    assert (report_dir / "impact.md").exists()


def test_missing_database_is_distinct_error(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["status", "--db", str(tmp_path / "missing.duckdb")]
    )

    assert result.exit_code == 2
    assert "database does not exist" in result.stderr
