# Plans

Documents in this folder are **plans of record** — concrete,
sequenced work that has graduated from [`proposed/`](proposed/) and
is committed to. A plan lays out:

- the **goal** and how we know we've reached it,
- the **current state** the plan starts from,
- the **sequence of steps** with the exact commands / files touched,
- the **verification checkpoints** between steps,
- a **risk register** with named mitigations,
- a **rollback procedure** if the change has to be reversed.

A plan ships when the engineer executing it could pick up the
document cold and follow it through without re-deriving any choices.
If a step requires judgment, the plan should say what the judgment
hinges on and what the answer should be in the expected case.

## Lifecycle: state folders

A plan's state is encoded by which sub-folder it lives in. There are
five, traversed in order from intake to terminal state:

| Folder | Meaning |
|---|---|
| [`proposed/`](proposed/) | Forward-looking idea — `prop-<NN>-<slug>.md`. Status `proposed` or `rejected (reason)`. Graduating to a plan **deletes** the proposal file (the plan becomes the canonical record). |
| [`backlog/`](backlog/) | Drafted but not yet started — no phase commit landed. Ready to be picked up cold. |
| [`in-progress/`](in-progress/) | At least one phase has shipped but the plan's definition of done has not been met. |
| [`completed/`](completed/) | Every phase has a filled-in phase commit; the audit trail of what shipped. |
| [`abandoned/`](abandoned/) | Dropped before completion. Each plan in here carries an `Abandonment reason` section. |

**Transitions are commit-level events.** Moving a plan between
folders happens in the same commit that lands the corresponding
phase commit (or, for abandonment, the commit that documents the
reason). `git mv` preserves blame.

**The folder is the canonical state.** Inside each plan body, the
`Status:` field is a self-documenting annotation that must agree
with the folder; if they disagree, the folder wins and the field is
the bug.

Cross-references to a plan from outside the `plans/` tree (CLAUDE.md,
source code) need to be updated when a plan transitions state,
because the path changes. That's a small recurring cost accepted
in exchange for state being immediately visible at a filesystem
glance.

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
  implemented which phase.

When the plan completes, the final phase commit is the canonical
reference for what shipped.

## Current plans

### Backlog

- [`backlog/p-04-m5-calibration.md`](backlog/p-04-m5-calibration.md)
  — Lock in M5's embedding model + `efSearch` via one-time benchmark
  runs on the M5 Max. Two independent phases.
- [`backlog/p-06-gold-set-growth.md`](backlog/p-06-gold-set-growth.md)
  — Grow `gold/gold.jsonl` from 17 to 50-100 cataloguer-vetted
  cases with proper per-category holdout stratification. Hard
  prerequisite for P-01, P-04 statistical power, and P-35 F3.
### In progress

- [`in-progress/p-34-m8-mint-anonymous-main-entry-works.md`](in-progress/p-34-m8-mint-anonymous-main-entry-works.md)
  — Editor-anchored canonical-Work mint for anonymous-main-entry
  records (no MARC 1XX, contributors in 700). Phase A shipped
  2026-05-14 at `9261dfd`; recovered 662 / 707 dropped records on
  the helmet-5k bench. Phases B + C still backlog.
- [`in-progress/p-35-m3-cascade-follow-ups.md`](in-progress/p-35-m3-cascade-follow-ups.md)
  — Three sequenced M3 follow-ups (F1, F2, F3) that make
  cascade-extracted contributions cataloguer-visible on canonical
  Works and bind them to KANTO. F1 shipped pre-renumber at
  `464247e` + `b56d9c1`; F2 + F3 still backlog. F3 gated on
  P-06's gold-set growth. **Renumbered from P-05 on 2026-05-14**
  to clear collision with the now-abandoned
  `proposed/p-05-anonymous-work-canonicalisation.md`.

### Completed

- [`completed/p-02-inference-stack-tuning.md`](completed/p-02-inference-stack-tuning.md)
  — Migrated M6 from Ollama to mlx-lm + prefix caching +
  speculative decoding + dev-loop consolidation + Ollama removal.
- [`completed/p-03-m6-stall-watchdog.md`](completed/p-03-m6-stall-watchdog.md)
  — Per-call LLM timeout + watchdog event logging + kill-and-
  retry-same-pair against transient inference wedges.
- [`completed/p-08-richer-rda-33x-synthesis.md`](completed/p-08-richer-rda-33x-synthesis.md)
  — Slot-wise RDA 33X synth cascade: 007 refinement + universal
  (leader/06, 008-form) default + material-code + itype adapters +
  300$a extent fallback, plus a `$5 FI-HELME/synth-v<N>` provenance
  marker on every synthesised datafield. Phase A coverage analysis
  measured 566 records missing 33X on the P-02 5k sample; Phase B
  + the cascade close it to 0 (the plan's `≤ 50` goal was beaten
  by Phase B alone).

### Abandoned

*(none)*
