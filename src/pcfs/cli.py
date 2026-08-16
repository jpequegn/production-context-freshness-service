"""Command-line entry point."""

import typer

from pcfs import __version__
from pcfs.corpus import load_corpus, validate_corpus

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


@query_app.command("help")
def query_help() -> None:
    """Describe the query workflow while it is being implemented."""
    typer.echo("Point-in-time query commands are introduced by repository issue #6.")


@eval_app.command("help")
def eval_help() -> None:
    """Describe the evaluation workflow while it is being implemented."""
    typer.echo("Evaluation commands are introduced by repository issue #8.")
