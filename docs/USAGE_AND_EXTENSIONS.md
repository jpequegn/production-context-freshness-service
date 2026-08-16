# Usage Patterns and Extensions

## Capabilities

The service can ingest immutable operational source versions, reconstruct what
was knowable at a historical time, distinguish current/stale/disputed/missing
context, enforce restricted service scopes, explain abstention, propagate change
impact, and compare retrieval strategies on a reproducible incident corpus.

## Practical usage patterns

### Agent context gateway

Call strict retrieval before an operational agent plans a query or command. Feed
the agent the compact evidence packet, not unrestricted source documents. Treat
`disputed`, `unknown`, and `insufficient_evidence` as mandatory handoff states.

### Incident reconstruction

Use `--at` with the incident decision timestamp. This prevents corrected
runbooks or later ownership changes from leaking into a review of what the
operator or agent could actually have known.

### Context maintenance queue

Run `pcfs status` on a schedule. Exit code `1` is an actionable signal for stale,
disputed, invalidated, or missing context. Route the listed service/fact groups
to their declared owner rather than asking a model to refresh them silently.

### Change-aware evaluation

Dry-run `pcfs propagate` when a metric, owner, deploy, incident, or permanent fix
changes. Use the affected eval case IDs to run a focused regression subset before
publishing refreshed context.

### Release observation planning

Pass release-specific context packets to a post-deploy observer. The observer can
derive telemetry checks from current metric and dependency facts while retaining
the source and freshness receipt behind each choice.

## Innovative extensions

### Signed decision snapshots

Sign a digest containing the query, selected source versions, policy versions,
invalidation frontier, and packet. This would make an agent decision replayable
even after the live store changes.

### Eval selection compiler

Map invalidated facts to code paths, monitors, and historical incident questions.
A change event could then produce a minimal, evidence-backed eval plan instead of
rerunning an undifferentiated benchmark suite.

### Freshness budget market

Assign review cost and risk to each fact type. Use status output to prioritize the
smallest set of human reviews that restores the most high-risk context packets,
without converting freshness into an employee score.

### Contradiction review workspace

Render diagnostic packets as a human review queue showing both authorities,
validity intervals, and source spans. The resolution action should append a new
source version and explicit supersession rather than edit history.

### Portable storage and API adapters

Implement the repository interface over PostgreSQL with bitemporal indexes, then
add a Go read API for low-latency packets. Keep DuckDB as the executable reference
and run identical contract/eval fixtures against both adapters.

### Graph-backed propagation

Use a provenance graph for larger dependency sets while preserving declared edge
semantics. Graph adjacency should expand review impact, never imply causation or
silently increase authority.

## Safety constraints

- Do not ingest unrestricted chat, production logs, or secrets by default.
- Do not infer a fresh fact when its source is stale.
- Do not resolve equal-authority conflicts by timestamp or confidence alone.
- Do not expose diagnostic evidence before access filtering.
- Do not let V1 mutate production, monitoring, ownership, or incident state.
