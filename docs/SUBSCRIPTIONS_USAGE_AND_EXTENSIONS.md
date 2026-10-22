# Evidence-change subscriptions

Subscriptions attach an advisory review workflow to a versioned conclusion. Each
subscription pins an assumption, dependency fact IDs, source-version keys, an
operational scope, a reviewer, and a deadline. An accepted source-change event
produces immutable receipts, mandatory invalidations, and a local review episode.
The implementation uses Python, DuckDB, and an optional pinned Rust graph adapter.

All shipped scenarios and advice are synthetic. There is no live Jev integration,
paid inference, external notification transport, recurring job, or UI server.
The commands do not edit a conclusion, runbook, or production system.

## Local command reference

Commands below use `pcfs` from an installed environment. The repository's
noneditable development install is `uv sync --frozen --group dev --no-editable`.
The examples use new paths and never require `--force`.

### Demonstration

```sh
pcfs subscriptions demo \
	--db /tmp/subscriptions-demo.duckdb \
	--report-dir /tmp/subscriptions-demo-report
```

`impact.json` contains exact old and new evidence, subscription bindings, receipts,
current guards, event deliveries, and the outbox. `impact.md` summarizes the review
requests. The demo has three unique receipts, one review episode, and four input
deliveries. One delivery repeats an earlier event. These are fixture assertions,
not measured notification counts or productivity results.

Repeating the same demo against the same database reuses accepted event receipts.
Changing an immutable event identity's content is an error. The service commits
fact ingestion and subscription effects through the repository's reentrant
transaction. A successful event cannot leave only part of its local effects.

### Scenario replay

```sh
pcfs subscriptions replay \
	--db /tmp/subscriptions-replay.duckdb \
	--scenario-file /absolute/scenario.json \
	--responses /absolute/synthetic-responses.json \
	--principal synthetic-reviewer \
	--report-dir /tmp/subscriptions-replay-report
```

`Scenario` contains `sequence_id`, `split`, `facts`, `subscriptions`, and ordered
event deliveries. Repeated event IDs are deliveries of the same immutable event.
The response file maps exact `AssessmentInput.key` values to `Assessment` objects.
Each assessment binds the full input, model version, rubric version, and synthetic
provenance. An absent response file uses an empty cache. A cache miss, wrong input
binding, or incompatible cached advice becomes unknown advice and conservative
review. Unknown advice never establishes that evidence is current.

`--scope` is repeatable. Scenario and response files have bounded input sizes.
Malformed command input is rejected rather than treated as evidence.

### Inspection and export

```sh
pcfs subscriptions inspect \
	--db /tmp/subscriptions-demo.duckdb \
	--at 2026-06-01T12:05:00Z \
	--principal synthetic-reviewer
```

`export` is an alias for `inspect`. Unauthorized principals receive no packets or
outbox entries. Inspection rechecks access and freshness rather than treating a
historical receipt as a current authorization grant. Fresh event processing also
reauthorizes before advice or receipt disclosure, including duplicate delivery.

`--at` selects historical freshness. It is not a permission time machine.
Backdating it must not bypass current restricted metadata or revoked access.
`AccessContext`, `--principal`, and `--scope` are caller-asserted local inputs, not
authentication. A deployment needs a trusted identity boundary, current grants,
and control over database access. The local CLI alone provides none of those.

### Local dispatch and review

```sh
pcfs subscriptions dispatch \
	--db /tmp/subscriptions-demo.duckdb \
	--at 2026-06-01T12:05:00Z \
	--principal synthetic-reviewer

pcfs subscriptions review \
	--db /tmp/subscriptions-demo.duckdb \
	--command-file /absolute/review.json \
	--at 2026-06-01T12:05:00Z \
	--principal synthetic-reviewer
```

Dispatch records an opaque local delivery result. It sends no message. A repeated
successful dispatch does not deliver the episode again. `--fail` simulates a failed
attempt. The local ledger records retry attempts and eventual dead-letter status.

One active episode collects causes for a subscription revision. A delivered episode
can collect later causes without another delivery until it is resolved. An observer
must inspect the active episode to see its latest causes. A delivery record is not
proof that someone read, reviewed, or acted on those causes.

A `ReviewDisposition` command has this shape. Replace `receipt_id` with an actual
receipt ID from the episode, and choose a time no earlier than that receipt.

```json
{
	"schema_version": 1,
	"command_id": "review-command-001",
	"receipt_id": "REPLACE_WITH_LATEST_EPISODE_RECEIPT_ID",
	"reviewer": "synthetic-reviewer",
	"decision": "dismissed",
	"rationale": "Synthetic workflow check, not a source correction.",
	"at": "2026-06-01T12:05:00Z"
}
```

Decisions are `confirmed`, `dismissed`, or `deferred`. A disposition for an earlier
receipt is recorded with `resolved=false`. It cannot close later causes. A
non-deferred disposition for the latest receipt can resolve the episode. Repeating
an identical command is idempotent. Reusing its command ID with changed content
collides and is rejected. The service checks the current context and exact reviewer.
New subscription revisions cannot take over another reviewer or conclusion.

Review changes workflow state only. It does not restore invalidated evidence,
extend expiry, raise source authority, remove a dispute, or make a conclusion
current. Corrected evidence needs a new admissible source version and the normal
policy checks. Inspection packets remain `usable_for_synthesis=false`.

### Optional graph selection

```sh
pcfs subscriptions demo \
	--db /tmp/subscriptions-graph.duckdb \
	--report-dir /tmp/subscriptions-graph-report \
	--graph-adapter /absolute/pcfs-graph-adapter \
	--adapter-sha256 ACTUAL_BINARY_SHA256
```

Graph mode requires both an explicit absolute binary path and its SHA-256. Flat
mode invokes no graph binary. A graph failure is explicit, not a silent flat
fallback. The local gate builds the binary from the locked dependency and supplies
its actual digest. No inference API is involved in either mode.

Python filters operational scope, access, fact availability, and relationship
availability before projection. Relationships follow the project's explicit
prerequisite-to-dependent convention. Graph reachability is impact selection,
not causality, authority, or freshness. Missing or expired changed seeds and
truncated traversal mean uncertain impact and require conservative review.

The manifest SHA-256 binds the filtered projection and its context. The upstream
fingerprint is a compatibility identifier, not a security hash of all metadata.
Binary verification assumes that the binary and its directory remain trusted
against concurrent replacement. A hash does not authenticate the caller, source
publisher, or graph author. See the [adapter contract](../graph-adapter/README.md)
for filtering, bounds, subprocess checks, and upstream provenance.

### Synthetic evaluation

```sh
pcfs subscriptions evaluate --report-dir /tmp/subscriptions-evaluation

pcfs subscriptions evaluate \
	--report-dir /tmp/subscriptions-graph-evaluation \
	--graph-adapter /absolute/pcfs-graph-adapter \
	--adapter-sha256 ACTUAL_BINARY_SHA256
```

After integration, this command writes `evaluation.json` and `evaluation.md`.
The evaluation compares periodic full refresh, lexical comparison, deterministic
dependency guards, and synthetic semantic advice. It scores unique
event/subscription pairs against evaluator-only truth. Duplicate deliveries are
not independent trials. Development and held-out fixture families remain separate.
Without a graph backend, `flat_vs_graph` is unavailable. With the explicit binary
and digest, the report runs a matched case where a latency dependency points to the
changed procedure. Flat mode retains direct procedure impact. Graph mode also
selects the latency subscription. That coverage comparison is not evidence of lower
latency, better accuracy, or reduced inference cost.

Advice and truth are authored from the same synthetic texts. Held-out families
check the evaluation contract, not independent model generalization or live Jev
accuracy. Mandatory source-version guards can produce expected false alerts for
editorial changes whose meaning is unchanged. Semantic advice cannot waive them.

Recomputation counts describe evaluation work units, not measured inference savings.
Stale exposure ends at a review request in the evaluation, not at restored evidence.
Diagnostic execution timings do not establish human benefit. Token savings, costs,
and human minutes are unmeasured. No semantic advantage is claimed without evidence.

## Evidence and policy boundaries

`EvidenceSnapshot.document` binds exact UTF-8 text to the canonical
`{"text": document}` source envelope digest. Its raw text SHA-256 is a separate
identifier. The binding is not a signature or proof that a publisher told the truth.

Legacy ingestion adapters hash their canonical structured source documents. Those
digests are not raw text hashes and cannot be relabeled as exact subscription text.
A connector must retain the original source envelope or create an explicit new
text-envelope source version. It must preserve provenance and never rewrite an
existing immutable source version to force a match.

Hard guards cannot be waived by advice or review. They cover unavailable or
noncurrent dependencies, access restrictions, invalid source bindings, scope,
deadlines, source-version changes, structural triggers, ordering uncertainty,
and incomplete impact traversal. The policy engine still decides authority,
freshness, conflict, and invalidation. Invalidation time and effect are validated.
Neither a high-confidence assessment nor a resolved episode overrides those rules.
New synthetic source versions are recorded when first available, not backdated to
their observation time. A late source version must not leak into an earlier query.
If an inner repository transaction fails, catching that exception does not permit
the outer transaction to commit partial effects. The transaction remains rollback-only.

## Local verification reference

The local gate accepts a fresh absolute output path whose parent exists. It refuses
existing paths, including empty directories and dangling symlinks, and deletes no
files. Each run needs a different output path.

```sh
bash scripts/verify-subscriptions-local.sh /tmp/pcfs-subscriptions-check-001
```

The first build needs authenticated read access to the private GitHub upstream and
access to the Cargo registry. Python packages and build dependencies also need a
populated cache or package-index access. The upstream revision is pinned in
`graph-adapter/Cargo.toml` and `Cargo.lock`. Once all caches are populated, the
offline form disables network resolution for both uv and Cargo.

```sh
bash scripts/verify-subscriptions-local.sh /tmp/pcfs-subscriptions-check-002 --offline
```

The gate runs frozen noneditable development installation, Ruff, locked Cargo
build, tests, and Clippy with warnings denied, then the complete pytest suite with
`PYTHONPATH=src`. Cargo uses `graph-adapter/target` because the graph tests require
that binary location. Noneditable installation avoids the known editable-import failure
in paths containing spaces. It builds a wheel with `uv build`, installs locked
runtime dependencies and that wheel in a clean production environment, and runs
`prove-subscriptions.py` with isolated Python imports outside the source directory.

The proof checks every installed `pcfs` runtime `.py` file against the tracked
working-source hashes. It rejects missing, extra, or changed runtime files. New
runtime files must therefore be tracked before the final run. It also checks the
original demo, subscription counts, rerun state, denied inspection, one-time
dispatch, stale and latest review dispositions, command collisions, missing-cache
behavior, explicit graph execution, and flat direct-case parity. When the installed
CLI exposes `evaluate`, the proof runs it and checks exact report truth coverage
for every baseline. It passes the real binary pin and verifies the matched flat/graph
case, including graph-only latency impact. Otherwise `verification.json` records evaluation as unavailable,
not passed. The integrated final run must include that evaluation check.

`verification.json` records the commit, HEAD tree, dirty status, working-source and
input hashes, commands, exit codes, checks, installed package path, binary digest,
and wheel digest. A dirty checkout is recorded rather than presented as the HEAD
tree. Source changes during a run fail the gate. Full command output passes through
`tee` into `logs`. A failed run preserves its output and records failure. A successful
core run is not a claim of deployed safety, live model accuracy, or human benefit.

## Future extensions

These are proposals, not current capabilities.

- Research thesis changes. Pin each claim to paper versions, quoted passages, and
  assumptions. Separate a revised result from an editorial correction. Ask for
  review when a dependency is withdrawn or contradicted. Independent expert labels
  and a live-model evaluation are prerequisites for accuracy claims.
- Runbook impact. Map procedures, metric names, ownership, and release constraints
  to operational decisions. Preview affected conclusions before a maintainer
  appends corrected evidence. Add a trusted connector and identity boundary without
  letting advice edit runbooks or waive operational guards.
- Claim-radar subscriptions. Let a researcher subscribe to a claim's supporting and
  contradicting evidence across versioned sources. Surface changes in assumptions
  and missing evidence, not only matching keywords. Preserve access-filtered
  citations, uncertainty, and review history. Cross-source inference needs explicit
  dependency semantics and validation against independently labeled changes.
- Delivery integration. Add an authenticated transport, destination authorization,
  durable acknowledgments, and a policy for later causes in an already-delivered
  episode. The current local ledger is not evidence of exactly-once external delivery.
