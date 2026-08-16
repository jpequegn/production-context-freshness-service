# Production Context Freshness Service

A deterministic reference service for retrieving operational facts with provenance,
validity intervals, authority, freshness, contradictions, and explicit abstention.

The project implements [project-ideas #236](https://github.com/jpequegn/project-ideas/issues/236).
V1 uses only fictional, synthetic operational data. It cannot connect to production
systems or mutate runbooks, ownership, monitoring, or incident state.

## Architecture decision

V1 is a Python 3.12 reference implementation using Pydantic, Typer, and DuckDB.
This keeps temporal semantics, invalidation rules, retrieval, and evaluation in one
testable runtime. PostgreSQL and a Go read service are deployment extensions, not
prerequisites for proving correctness.

## Quick start

```bash
uv sync --no-editable
uv run --no-sync pcfs version
uv run --no-sync pcfs demo --db demo.duckdb --report-dir reports/demo --force
```

The demo builds a fresh database, shows historical ownership before a change,
demonstrates strict abstention during an authority conflict, previews metric-rename
impact, and compares three retrieval systems over 60 point-in-time questions.

## Core workflows

```bash
# Build an idempotent reference database.
uv run --no-sync pcfs init --db context.duckdb
uv run --no-sync pcfs ingest --db context.duckdb

# Retrieve only evidence that was knowable on January 10.
uv run --no-sync pcfs query context \
  --db context.duckdb \
  --service checkout \
  --fact-type ownership \
  --at 2026-01-10T00:00:00Z

# Expose stale/disputed candidates and exclusion reasons.
uv run --no-sync pcfs query context \
  --db context.duckdb \
  --service checkout \
  --fact-type ownership \
  --at 2026-02-02T10:00:00Z \
  --mode diagnostic

# Preview an invalidation impact, then apply it deliberately.
uv run --no-sync pcfs propagate \
  --db context.duckdb \
  --trigger metric_renamed \
  --service checkout \
  --at 2026-02-10T00:00:00Z \
  --format markdown

# Run comparative evaluation and write durable reports.
uv run --no-sync pcfs eval run \
  --db context.duckdb \
  --json-report reports/evaluation.json \
  --markdown-report reports/evaluation.md
```

`pcfs status` returns `0` when required context groups are healthy, `1` when
stale, disputed, invalidated, or missing evidence needs attention, and `2` when
the database does not exist.

## What it proves

- Observed, valid, recorded, and superseded times are independent.
- Immutable source versions and citations accompany every selected fact.
- Authority and freshness take precedence over model confidence.
- Equal-authority conflict remains disputed; strict retrieval abstains.
- Permissions and historical recorded time are filtered before text ranking.
- Change events preserve prior history and identify impacted queries and evals.
- Temporal retrieval is measured against naive and current-only baselines.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Usage patterns and extensions](docs/USAGE_AND_EXTENSIONS.md)

## Development

```bash
uv sync --no-editable
uv run --no-sync ruff check .
uv run --no-sync pytest
```
Temporal, provenance-aware operational context with freshness, invalidation, and abstention
