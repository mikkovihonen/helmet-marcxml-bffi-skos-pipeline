# P-14 — M9 Phase C tier-0 expansion: cataloguer audit + M5 Max re-bench

**Status**: abandoned 2026-05-14.

## Abandonment reason

P-10 Phase C shipped the dual-path resolver and the
`load-finto --fold-pref-labels` materialiser feature-flagged **off**
(commit `8e47a69`) pending the validation gate this plan was supposed
to clear: a 200-sample cataloguer audit + a clean M5 Max re-bench
to confirm tier-0 expansion buys back enough tier-2 LLM-picker calls
to pay for the doubled SPARQL traffic the 2026-05-13 attempt
surfaced.

Neither gate ever cleared. The 2026-05-13 M2 Max bench attempt
(see [`docs/performance/2026-05-13-5k-m2-max-phase-c-attempt.md`](../../performance/2026-05-13-5k-m2-max-phase-c-attempt.md))
crashed on mlx-lm GPU OOM mid-run with the *partial* observation
that tier-0 expansion appeared to **increase** SPARQL traffic
without an offsetting reduction in tier-2 picker calls on the
heterogeneous cataloguer-curated 5 k corpus. That's a Phase A2
baseline regression, not a win — and the cataloguer-audit half of
the gate would only have mattered if Phase B's wall-time numbers
had come back convincingly green.

Sitting on flag-off shipped code past the original P-10 ship date
became the rot pattern: a dead code path in production runs, with
no driver pushing the audit forward. Closing P-14 by yanking the
dual-path code is cleaner than keeping the surface around for an
audit nobody's queued.

**What gets yanked in the closing commit:**

- `Settings.m9_tier0_expansion` field + the `BFFI_M9_TIER0_EXPANSION` env alias (`src/bffi_pipeline/config.py`).
- `tier0_expansion_enabled` field + `_folded_match` method + `_build_folded_query` helper + `_BFFI_FOLDED_LABEL_URI` constant on `FusekiConceptResolver` (`src/bffi_pipeline/stages/local_concept_resolver.py`).
- `fold_pref_labels` parameter + the materialise branch + `BFFI_FOLDED_LABEL_URI` constant on `load_finto.run()` (`src/bffi_pipeline/stages/load_finto.py`).
- `--tier0-expansion/--no-tier0-expansion` flag on `bffi-pipeline reconcile` + `--fold-pref-labels/--no-fold-pref-labels` on `bffi-pipeline load-finto` (`src/bffi_pipeline/cli.py`).
- `tier0_expansion_enabled` key in the M9 start-event observability emit (`src/bffi_pipeline/stages/reconcile.py`).
- The five tier-0-folded test cases in `tests/unit/test_local_concept_resolver.py` + the `fold_pref_labels=False` kwargs in `tests/unit/test_load_finto.py`.

**What stays:**

- `fold_label` / `fold_diacritics` / `strip_label_decoration` in
  `src/bffi_pipeline/blocking.py` — used independently of tier-0
  expansion as the picker-cache key composition in `reconcile.py`
  (so diacritic-equivalent literals hit the same cached decision).
  `tests/unit/test_blocking_fold.py` stays.
- `LocalConceptHit.is_fuzzy_match` (always `False` after the yank).
  Kept as a forward-compat field so a future fuzzy-resolver can
  reintroduce the needs-review flag path in `reconcile.py:1466`
  without re-adding the field.

**What would resurrect this work:**

- A cataloguer-driven audit window opens for the 200-sample review
  (P-06 territory).
- The picker-call ratio on the heterogeneous corpus shifts (e.g.
  P-22-29 land and reduce tier-2 traffic enough that tier-0's
  SPARQL-traffic cost becomes the dominant term — at which point
  the trade-off is worth re-measuring).
- A different lexical-matching technique (BM25 over a Finto-side
  inverted index, embeddings on prefLabels, …) supersedes the
  fold-prefLabel approach. In that case P-14 is the wrong shape
  and a new proposal under `proposed/` is the right venue.

## Original plan (preserved for the historical record below)

**Source**: spun out of P-10 [`docs/plans/completed/p-10-m9-reconcile-throughput.md`](../completed/p-10-m9-reconcile-throughput.md) Phase C (commit `8e47a69`, feature-flagged **off**). P-10's Phase C shipped code but its validation gate — a 200-row cataloguer audit + a clean wall-time bench — remained open. P-10 graduated to `completed/` with Phase C left flag-off; this plan tracked the work needed to flip `BFFI_M9_TIER0_EXPANSION` to `True` by default.
**Plan-base commit**: `<unfilled>` (set on the first phase commit). To gauge drift before executing, run:
`git diff <plan-base>..HEAD --
src/bffi_pipeline/stages/local_concept_resolver.py
src/bffi_pipeline/stages/load_finto.py
src/bffi_pipeline/config.py`.
**Phase commits**:

- Phase A (200-sample cataloguer audit): `<unfilled>`
- Phase B (M5 Max 128 GB re-bench): `<unfilled>`

**Owner**: TBD (Phase A needs cataloguer time; Phase B gated on M5 Max being available).
**Estimated wall-time**: half a day for Phase A (the audit itself is ~3 h of cataloguer attention plus the JSONL-write tooling); half a day for Phase B (single 5 k re-run + snapshot).

## Goal

Resolve the open Phase C validation gate from P-10 so the tier-0 expansion (`BFFI_M9_TIER0_EXPANSION=True` + `load-finto --fold-pref-labels`) can ship as a default-on feature, or be reverted with a documented rationale.

P-10's 2026-05-13 bench attempt at [`docs/performance/2026-05-13-5k-m2-max-phase-c-attempt.md`](../../performance/2026-05-13-5k-m2-max-phase-c-attempt.md) crashed mid-run on a mlx-lm GPU OOM (M2 Max 64 GB, prompt-cache too large). The crash leaves two open questions:

1. **Bind quality**: does folded-label tier-0 introduce false positives that cataloguers would reject? The codepath is committed but the spot-check the plan committed to has not happened.
2. **Wall-time delta**: P-10's interrupted run suggested tier-0 expansion *increases* SPARQL traffic without reducing picker calls on this corpus, but the bench didn't complete. A clean re-bench on the M5 Max would settle whether the feature pays for its overhead.

## Definition of done

- `gold/reconcile-audit-200.jsonl` exists with 200 audited rows, each carrying the cataloguer's verdict (`bind_correct` / `bind_incorrect` / `unclear`) on the M9 binding produced under tier-0 expansion. Zero `bind_incorrect` rows is the merge-default gate per P-10 § C.5; any incorrect bind is investigated and either explained (cataloguer-side data fix) or rolls back the feature.
- A fresh [`docs/performance/<date>-5k-m5-max-phase-c.md`](../../performance/) snapshot taken on the M5 Max 128 GB shows tier-0 hit count up ≥ 30 % of P-10's Phase A2 baseline, tier-2 (LLM) count ≤ 0.7 × baseline, M9 wall ≤ 70 s with the warm cache. Failure to meet these is documented and the flag stays off.
- A commit either flips `Settings.m9_tier0_expansion` default to `True` (gate met), or adds a comment to the Field explaining why the gate failed and the flag stays off.
- `docs/plans/backlog/p-14-m9-phase-c-audit.md` is `git mv`'d through `in-progress/` → `completed/` per [`docs/plans/README.md`](../README.md).

## Current state (as of plan-base `<unfilled>`)

- P-10 Phase C code is **shipped at `8e47a69`** but **feature-flagged off**:
  - `Settings.m9_tier0_expansion` defaults `False` (env: `BFFI_M9_TIER0_EXPANSION`).
  - `load-finto --fold-pref-labels` defaults `False` (was flipped to off post-bench-attempt 2026-05-13).
- `src/bffi_pipeline/stages/local_concept_resolver.py` carries the dual-path query: when expansion is on, the SPARQL CONSTRUCT also walks `bffi:foldedLabel` triples that `load-finto` materialises at vocab-load time.
- The 2026-05-13 Phase C bench attempt is documented as **incomplete** in [`docs/performance/2026-05-13-5k-m2-max-phase-c-attempt.md`](../../performance/2026-05-13-5k-m2-max-phase-c-attempt.md). 1 500 picker calls had completed when mlx-lm OOM'd on the M2 Max; that's already past Phase A2's 1 348 baseline, implying tier-0 expansion may not reduce tier-2 work on the heterogeneous 5 k cataloguer-curated sample. The M5 Max re-bench will confirm or refute.
- P-06 (`docs/plans/backlog/p-06-gold-set-growth.md`) is the long-running plan that consumes audit deliverables. The Phase A audit JSONL from this plan feeds directly into P-06's `gold/` set.

## Phase A — 200-sample cataloguer audit

### A.1. Sample-generation tooling

Add `scripts/p14-sample-audit-candidates.py` that:

- Loads `data/canonical.ttl` from the operator's latest reconcile run with `BFFI_M9_TIER0_EXPANSION=True` (operator runs M9 once with the flag on, against a representative 5 k slice, before this audit fires).
- Filters to bindings produced by tier-0 (`bffi-prov:stage = "reconciliation-local"`), specifically the ones that wouldn't have bound without the folded-label path. The diagnostic is a second M9 run with the flag off — the audit set is the symmetric difference of bound URIs between the two runs.
- Random-samples 200 entries with `random.seed(42)` for reproducibility.
- Writes `gold/reconcile-audit-200-candidates.jsonl` with each row carrying `{work_uri, literal, field, bound_uri, candidate_pref_label, candidate_alt_labels}` so the cataloguer can verify each bind in context without re-running the pipeline.

### A.2. Audit workflow

- Cataloguer opens `gold/reconcile-audit-200-candidates.jsonl` and for each row writes a verdict to `gold/reconcile-audit-200.jsonl` as `{... candidate fields ..., verdict: "bind_correct" | "bind_incorrect" | "unclear", note?: "<freetext>"}`.
- `bind_incorrect` rows trigger a follow-up: investigate whether the cause is a Finto data issue (cataloguer fix), a fold-rule false positive (code fix on the resolver), or a genuine ambiguity that LLM tier-2 would have handled differently (revert the flag).
- `unclear` rows count as failures for the merge-default gate but don't block the snapshot.

### A.3. Acceptance criteria

- [ ] `scripts/p14-sample-audit-candidates.py` ships with a fixture-backed unit test (no network) that pins the sampling logic.
- [ ] `gold/reconcile-audit-200.jsonl` exists with 200 verdicts.
- [ ] Zero `bind_incorrect` rows (or each is explained with a follow-up commit/issue).
- [ ] Audit JSONL is referenced from `docs/plans/backlog/p-06-gold-set-growth.md` so P-06's gold-set ingestion picks it up.

### A.4. Rollback

If the audit surfaces ≥ 1 `bind_incorrect` row with no upstream-data fix, the Phase A commit either:
1. Adds a fold-rule exclusion + re-audits the affected rows, or
2. Marks the flag as permanently off and updates `Settings.m9_tier0_expansion`'s docstring with the audit-finding rationale.

## Phase B — M5 Max 128 GB re-bench

Depends on the M5 Max being available. Until then this phase stays unstarted.

### B.1. Bench driver

- Re-use `scripts/run-full-pipeline.sh` (or `republish.sh --from-stage m9` if M2+M3+M5+M6+M8 outputs already exist on the M5 Max).
- mlx-lm flags: `--decode-concurrency 4 --prompt-concurrency 4 --prompt-cache-size 50 --prompt-cache-bytes 1073741824 --chat-template-args '{"enable_thinking":false}'` — note `--prompt-cache-size 50` (half of the M2 Max bench attempt's 100) to give headroom against the OOM that crashed the prior run. The M5 Max has 2× the unified memory but the safe-margin lower bound is the right place to start.
- Two-shot bench: cold with `BFFI_M9_TIER0_EXPANSION=False` (baseline), then cold with `BFFI_M9_TIER0_EXPANSION=True` (treatment). Restart mlx-lm between shots so the prompt cache is equalised at entry.

### B.2. Acceptance criteria

- [ ] Both shots complete without mlx-lm OOM (sanity-check that the smaller prompt-cache works).
- [ ] Treatment shot's tier-0 hit count is ≥ 30 % above baseline tier-0.
- [ ] Treatment shot's tier-2 (LLM) call count is ≤ 0.7 × baseline tier-2.
- [ ] Treatment shot's M9 wall is ≤ 70 s with the warm cache (cold can be longer; the gate is warm-run wall, matching the operator pattern P-10 Phase B optimised for).
- [ ] [`docs/performance/<date>-5k-m5-max-phase-c.md`](../../performance/) snapshot committed with the same structure as P-10's Phase B.1 + Phase E snapshots: run metadata, wall-time headline, outcome distribution, where-time-goes ASCII chart, recommendation.

### B.3. Rollback

If Phase B fails the wall-time gate, the snapshot's "Recommendation" section says so and the flag stays off. The code stays committed (it's behind a flag) — no `git revert` needed.

## Risks

- **R1 — cataloguer availability.** The audit needs ~3 h of focused cataloguer attention. Without it, Phase A can't progress. Mitigation: the audit can be batched with other Helmet-cataloguer ask-list items (see [`docs/external-dependencies.md`](../../external-dependencies.md)) so the operator only schedules one cataloguer engagement.
- **R2 — M5 Max delay.** Phase B is hardware-gated. The plan can stay in `backlog/` indefinitely without blocking other work; the production pipeline runs fine with the flag off (P-10 Phase B.1's warm-cache speedup already meets the overnight-window target).
- **R3 — Audit reveals systematic false-positives.** The tier-0 fold rules might be too aggressive on Finnish-language data the original spec didn't anticipate. Mitigation: Phase A's rollback path lets us either tighten the rules or permanently leave the flag off.

## Open issues to close before / during execution

- None identified. The dependencies (P-10 Phase C code, P-06 gold-set ingestion convention) are settled.

## Cross-references

- [`docs/plans/completed/p-10-m9-reconcile-throughput.md`](../completed/p-10-m9-reconcile-throughput.md) — parent plan.
- [`docs/performance/2026-05-13-5k-m2-max-phase-c-attempt.md`](../../performance/2026-05-13-5k-m2-max-phase-c-attempt.md) — the failed bench attempt this plan re-runs.
- [`docs/plans/backlog/p-06-gold-set-growth.md`](p-06-gold-set-growth.md) — consumer of Phase A's audit JSONL.
- `src/bffi_pipeline/config.py` — `m9_tier0_expansion` Field.
- `src/bffi_pipeline/stages/local_concept_resolver.py` — resolver-side expansion logic.
- `src/bffi_pipeline/stages/load_finto.py` — `--fold-pref-labels` materialisation.
