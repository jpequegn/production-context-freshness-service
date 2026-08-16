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

## Development

```bash
uv sync
uv run pcfs version
uv run ruff check .
uv run pytest
```

## Status

The implementation is being delivered through repository issues. See the
[issue tracker](https://github.com/jpequegn/production-context-freshness-service/issues)
for the ordered backlog.
Temporal, provenance-aware operational context with freshness, invalidation, and abstention
