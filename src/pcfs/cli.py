"""Command-line entry point."""

import typer

from pcfs import __version__

app = typer.Typer(no_args_is_help=True, help="Inspect temporal operational context.")
data_app = typer.Typer(help="Initialize and ingest operational context data.")
query_app = typer.Typer(help="Retrieve point-in-time evidence packets.")
eval_app = typer.Typer(help="Run deterministic context evaluations.")

app.add_typer(data_app, name="data")
app.add_typer(query_app, name="query")
app.add_typer(eval_app, name="eval")


@app.command()
def version() -> None:
    """Print the installed package version."""
    typer.echo(__version__)


@data_app.command("help")
def data_help() -> None:
    """Describe the data workflow while it is being implemented."""
    typer.echo("Data ingestion commands are introduced by repository issue #4.")


@query_app.command("help")
def query_help() -> None:
    """Describe the query workflow while it is being implemented."""
    typer.echo("Point-in-time query commands are introduced by repository issue #6.")


@eval_app.command("help")
def eval_help() -> None:
    """Describe the evaluation workflow while it is being implemented."""
    typer.echo("Evaluation commands are introduced by repository issue #8.")
