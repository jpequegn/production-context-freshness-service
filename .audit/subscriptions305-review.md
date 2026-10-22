# Review record

Source: <https://github.com/jpequegn/project-ideas/issues/305>.

Independent agents reviewed design alternatives, the integration and the evaluation.
They used the configured parent model. This is independent review with reduced model
diversity, not cross-provider consensus. No unavailable model family is claimed.

## Findings addressed

- Preserve dependency-level freshness rather than trusting packet-level CURRENT.
- Commit ingestion, mandatory invalidations, receipts and outbox in one transaction.
- Mark caught nested failures rollback-only and prove retry after rollback.
- Prevent implicit reviewer or conclusion takeover through a new revision.
- Check current access before fresh advice, duplicate receipt return and history export.
- Do not let historical freshness time bypass current restrictions or reveal future bytes.
- Treat unavailable graph seeds as uncertain impact, not a proven empty result.
- Require effective invalidations that cover every directly affected mandatory dependency.
- Preserve set semantics during canonical hashing across process hash seeds, while
  retaining existing model/mapping timestamp encodings.
- Include the Rust adapter source and packaged corpus in source-drift checks.

The identity regressions failed before the fix with a set serialization error and
different hashes across Python processes. They passed afterward. The integration
review independently reproduced the transaction, access and graph failures before
the fixes and found no remaining blockers in task #22. Scientific review found no
additional error in matched baselines, horizons, duplicate scoring or truth coverage.

## Acceptance evidence

`.audit/subscriptions305-validation.json` retains the actual offline local run.
The validated code commit is `9892cd4db0ce4dc2d46fcabdf9cc069a7bdb6057`.
The subsequent evidence commit changes audit artifacts only.

- 324 Python tests passed; Ruff, locked Cargo build, Cargo test and Clippy passed.
- Cargo's adapter test target contains zero Rust unit tests. Its real executable is
  exercised by Python projection tests and the installed CLI proof.
- 39 installed acceptance checks passed across 32 recorded commands.
- All 23 installed Python/corpus files exactly match tracked source.
- A production-only environment drives the original demo and all subscription paths.
- The matched Rust graph case retains flat coverage and adds the latency dependency.
- GitHub Actions remained disabled. Last hosted run was still 31970043180 from
  2026-08-16, before this implementation.

## Attention

Advice is authored synthetic cache data, not live Jev inference. The comparison
does not establish semantic savings or model generalization. Current local identity
and scopes are caller assertions, not production authentication. Review disposition
never restores expired or invalidated source evidence. Delivered episodes collect
new causes without another delivery until resolved. A trusted graph binary directory
is required; hash checking is not an OS sandbox or defense against a concurrent
binary replacement. A production deployment needs separate approval and controls.
