# Subscription graph adapter

Read-only selection aid, not a freshness decision or causal model. Flat mode does
not invoke this binary. Graph mode requires its absolute path and SHA-256 explicitly.
There is no Python graph fallback.

## Provenance and build

The real `org_core` library comes from
https://github.com/jpequegn/operational-reasoning-graph-lab at
`9194281ab2a387e820f8047de512a3b8763cc7e0`, pinned in Cargo.toml and Cargo.lock.
That revision declares `license = "MIT"` in its Cargo.toml but contains no LICENSE
file. No upstream source is vendored or represented as adapter-authored code.
Cargo uses the original library without modifications.

```sh
CARGO_NET_GIT_FETCH_WITH_CLI=true cargo build --locked --manifest-path graph-adapter/Cargo.toml
cargo build --locked --offline --manifest-path graph-adapter/Cargo.toml
shasum -a 256 graph-adapter/target/debug/pcfs-graph-adapter
```

The first build needs authenticated read access to upstream and registry access.
Later offline builds need the populated Cargo cache. The upstream CLI dependency
is transitive even though this adapter uses only `org_core::graph`.

## Contract

One JSON request on stdin, one JSON response on stdout, errors on stderr with exit
status 2. The input cap is 4 MiB. All timestamps are unsigned Unix microseconds.
`schema_version` is 1. Unknown fields and unsupported typed classes are rejected.

- `nodes` contains `id`, upstream node `class`, `sensitivity`, `source_key`, and
  a canonical complete `fact_sha256`.
- `edges` contains `prerequisite`, `dependent`, `class = "depends_on"`,
  `observed_at`, `valid_from`, nullable `valid_until`, and `provenance`.
- Edge provenance retains original relationship ID, original source/target fact
  IDs, original type, source-version key, and complete `relationship_sha256`.
- `time_window` contains inclusive relationship observation `start` and `end`.
  Selection is a point-in-time snapshot at `end`, not a union over history.
  Validity intervals are half-open. Future and expired edges are not traversed.
- `bounds` contains `max_hops` from 0 to 64 and `max_nodes` from 1 to 10000.
  Inputs allow at most 10000 nodes and 50000 edges. Seeds must exist and fit the
  node bound. Python removes unauthorized or unavailable seeds before invocation.
- `seeds` is the list of explicit changed fact IDs. Flat mode returns these only.

Rust sorts nodes, edges, and seeds before constructing `OpGraph`. All traversable
PCFS relationships reverse their original direction into prerequisite-to-dependent
`DependsOn`, including `supports`, matching PCFS's existing propagation convention.
This avoids upstream's special incoming epistemic traversal. It does not assert
that all relationships with these names in other systems have the same direction.

The adapter uses original `Node`, `NodeClass`, `Sensitivity`, `EdgeData`,
`EdgeClass`, `TrustState`, `Envelope`, `OpGraph::add_node`, `add_edge`,
`bounded_khop`, and `snapshot_fingerprint`. Seeds run in sorted order; upstream
deterministic BFS order chooses nodes within each seed's bound. The result shares
one global node cap, retains all seeds, and sorts selected IDs for output.

Output contains `schema_version`, `upstream_revision`, sorted `nodes`, `truncated`,
and `upstream_fingerprint` as 16 hexadecimal digits. Truncation means a node or hop
bound omitted eligible impact. It is uncertainty, never proof of no impact.

Python `build_projection` filters service, environment, optional incident/release,
principal scopes, fact availability/validity, source availability, and relationship
availability before constructing IDs or edges. It does not replace PolicyEngine's
authority, conflict, or freshness checks. Its manifest pins operational scope,
access context, exact fact/relationship digests, traversal bounds, time, direction,
and upstream revision with canonical SHA-256. The upstream FNV-style fingerprint
is retained separately; upstream omits some metadata and it is not a security hash.

`RustGraphBackend` hashes the explicit binary, rejects mismatches, invokes without
a shell and with an empty environment, bounds request/output/stderr bytes and wall
time, kills its process group on exit, then validates response revision, shape,
node allowlist, reachability, seeds, ordering, and bounds. The binary and directory
must be trusted against concurrent replacement after verification. No runtime build
or download occurs. Failures are explicit and never silently fall back to flat.
