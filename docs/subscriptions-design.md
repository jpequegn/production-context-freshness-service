# Meaning-aware subscriptions

Source: <https://github.com/jpequegn/project-ideas/issues/305>.

## Scope

Extend the Python/DuckDB reference service with local, advisory review requests.
The existing policy engine remains authoritative. No live inference, private uploads,
external notifications, automatic conclusion edits, or recurring jobs are included.
The eight captured implementation issues are #17 through #24.

## Existing flow

`Repository` stores immutable source versions, facts, relationships and invalidations.
`ContextRetriever` filters operational scope and access before calling `PolicyEngine`.
The engine checks dates, authority, conflict and invalidation. `ChangePropagator`
expands reverse dependencies. Subscription advice must not turn excluded evidence
back into admissible evidence.

## Design alternatives

The sidecar candidate keeps subscription revisions and immutable event receipts in
the same DuckDB database, with an explicit transactional outbox. It reuses the existing
policy and retrieval contracts. Its risk is coordinating event receipts and outbox rows.
One transaction owner addresses that risk.

The journal/reducer candidate makes subscription tables rebuildable projections of
all workflow events. It offers full workflow reconstruction but requires broader
transaction and schema changes. The existing fact service does not need this rewrite.

Choose the sidecar, with immutable event identity, atomic receipt/outbox persistence,
and append-only disposition/delivery history from the journal candidate. Model
diversity is reduced: independent agents use the configured parent model. Do not
claim cross-provider consensus.

## Data and ownership

- `Subscription` pins a conclusion revision, assumption version, dependency IDs,
  source keys, operational scope, reviewer and deadline.
- `EvidenceSnapshot` binds exact UTF-8 document bytes to a fact's canonical
  `{"text": document}` source envelope. This new adapter does not reinterpret the
  legacy corpus's canonical structured-document digests as raw-byte digests.
- `ChangeEvent` pins old/new snapshots, observation and availability times,
  source sequence, optional structural trigger and immutable event ID.
- `Assessment` records separate relevance, contradiction and materiality advice,
  with exact input, model and rubric identities. Missing advice is unknown.
- `Receipt` records mandatory policy reasons separately from optional advice.
- The sidecar store owns event acceptance, ordering, receipt persistence, one
  pending review episode per subscription revision, delivery attempts and history.

## Public workflow

Register a subscription, process a version-linked event, inspect its receipt, then
dispatch a local request and record a reviewer disposition. Repeated delivery returns
the original receipt. Reusing an identity with changed bytes is rejected. Late and
unresolved source order remains visible and cannot silently mark a conclusion current.

Graph projection is a bounded, versioned selection aid, never a new policy engine or
proof of causality. The flat dependency list remains a baseline. If a projection is
truncated, uncertain impact must not be treated as no impact.

## Verification and sequence

1. #17 contracts, this rationale, local gate and baseline.
2. #18 synthetic sequences with separate runtime inputs and hidden truth.
3. #19 guarded impact and typed graph projection.
4. #20 version-bound offline advice.
5. #21 transactional event/outbox/review history.
6. #22 replay, inspection, local delivery and review CLI.
7. #23 matched baseline evaluation with held-out sequences.
8. #24 installed-artifact proof, documentation and closeout.

Each unit runs focused behavior tests and the complete local suite before its PR
merges. Final verification builds and installs a wheel in a clean environment and
drives the CLI without a source-path import. Fixture results are not real Jev
accuracy, measured human productivity, or token savings.

The baseline is commit `6542e0b0c7acdc4d3a1a9c7d1ac34039bd40b2d6`, with 49 passing tests. Editable installation
failed to import `pcfs` under this space-containing path; frozen noneditable install
passed. The final gate must test both working source and installed artifact.

## Principles applied

Model the Domain separates evidence, advice, mandatory invalidation and review state.
Make Operations Idempotent requires durable event identity and atomic outbox writes.
Laziness Protocol selects a sidecar instead of replacing the repository architecture.
Build the Lever requires a rerunnable local verification script. Sequence Work into
Verifiable Units requires one checked PR per task. Prove It Works requires an installed
CLI demonstration, not only library tests.

## Independent design review

An independent same-model judge preferred the sidecar on scope and privacy, while
rating the journal stronger on reconstruction. Adopted findings require per-dependency
policy decisions, filtered graph inputs, and atomic event/receipt/invalidation/outbox
writes. Historical access grants must not authorize present disclosure.

Source ingestion is a prerequisite, not an implicit part of accepting a subscription
event. A failed event can be replayed against immutable ingested facts. Acceptance
must commit its mandatory invalidations and receipt/outbox together. No accepted event
may exist without those effects. Duplicate receipts are historical artifacts, not a
fresh recomputation or authorization decision.

The pinned graph library is `operational-reasoning-graph-lab` at
`9194281ab2a387e820f8047de512a3b8763cc7e0`. Its fingerprint is a compatibility ID,
not a cryptographic commitment to all metadata. Receipts must also pin a SHA-256
digest of the complete filtered projection. Export prerequisite-to-dependent edges
explicitly; the upstream traversal does not follow incoming `DependsOn` edges.

The packaged `deslop` and `babysit` commands are not available on this Codex runtime.
Perform their checks directly: inspect each diff for redundant layers/comments,
run local tests, inspect PR reviews/conflicts and exact head, then merge. No skill file
was modified and no claim is made that these unavailable commands ran.
