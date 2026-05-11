# Plans

Documents in this folder are **plans of record** — concrete,
sequenced work that has graduated from `docs/proposals/` and is
committed to. A plan lays out:

- the **goal** and how we know we've reached it,
- the **current state** the plan starts from,
- the **sequence of steps** with the exact commands / files touched,
- the **verification checkpoints** between steps,
- a **risk register** with named mitigations,
- a **rollback procedure** if the change has to be reversed.

Each plan carries a `Status:` field — `draft` / `in-progress` /
`completed` / `aborted (reason)`. A completed plan stays in place for
the audit trail; the milestone shows up in `docs/BUILD_PLAN.md`
separately.

## Tying plans to version control

Plans are time-stamped artifacts — the "Current state" section is
true *as of* a particular commit, and parts of the plan can become
stale if `main` moves before execution. To keep the plan verifiable,
each plan carries three commit-hash fields near the top:

- **Source proposal**: the commit(s) where the originating proposal
  (and any subsequent discussion that fed into the plan) lives, so
  the intellectual history is one `git show` away.
- **Plan-base commit**: the HEAD the plan was drafted against. The
  "Current state" section is accurate against this commit; the
  executor verifies whether `main` has drifted via
  `git diff <plan-base>..HEAD -- <files the plan touches>` before
  starting work.
- **Phase commits**: filled in as each phase ships. An empty phase
  commit is a positive signal that the phase has not yet met its
  acceptance criteria. Once a phase merges, its commit hash goes in;
  this lets future readers reconstruct exactly which change
  implemented which phase even after the plan itself has been
  archived.

When the plan completes, the final phase commit is the canonical
reference; `docs/BUILD_PLAN.md` cross-links to the plan rather than
duplicating the detail.

A plan ships when the engineer executing it could pick up the
document cold and follow it through without re-deriving any choices.
If a step requires judgment, the plan should say what the judgment
hinges on and what the answer should be in the expected case.

## Current plans

- [`p-02-inference-stack-tuning.md`](p-02-inference-stack-tuning.md)
  — `draft`. Migrate M6 from Ollama to vllm-mlx and layer prefix
  caching + speculative decoding (graduated from prop-02). Also
  absorbs the `--concurrency` sweep BUILD_PLAN M6 followed up on.
- [`p-04-m5-calibration.md`](p-04-m5-calibration.md) — `draft`.
  Lock in M5's embedding model + `efSearch` via one-time benchmark
  runs on the M5 Max. Two independent phases.
- [`p-05-m3-cascade-follow-ups.md`](p-05-m3-cascade-follow-ups.md)
  — `draft`. Three sequenced M3 follow-ups (F1, F2, F3) that make
  cascade-extracted contributions cataloguer-visible on canonical
  Works and bind them to KANTO. F3 gated on P-06's gold-set growth.
- [`p-06-gold-set-growth.md`](p-06-gold-set-growth.md) — `draft`.
  Grow `gold/gold.jsonl` from 17 to 50-100 cataloguer-vetted cases
  with proper per-category holdout stratification. Hard prerequisite
  for P-01, P-04 statistical power, and P-05 F3.
