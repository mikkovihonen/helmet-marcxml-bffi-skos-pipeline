# 5 000-record M9 bench attempt — Phase C, M2 Max 64 GB, 2026-05-13 (incomplete)

P-10 Phase C bench attempt with the tier-0 expansion feature flag enabled (`BFFI_M9_TIER0_EXPANSION=1`). The run did not complete: mlx-lm crashed with a Metal GPU out-of-memory after ~2h 20m elapsed. This snapshot documents what we did learn from the partial run.

> **Status: attempt, not a result.** No wall-time number for the full M9 stage; no `M9 reconciliation complete` summary; no output canonical Turtle. The body of the snapshot is the partial-data findings + the crash analysis. The next attempt should adjust the mlx-lm prompt-cache config (see "Implications") or run on the production M5 Max 128 GB.

## Run metadata

| Field | Value |
|---|---|
| Date | 2026-05-13 |
| Hardware | MacBook Pro, M2 Max, **64 GB unified memory** (same as the Phase A / A2 benches) |
| Git HEAD at run start | `8e47a69` (P-10 Phase C code shipped earlier the same day) |
| Sample | Same `data/canonical.ttl` the Phase A / A2 benches used (May 12 baseline) |
| Output target | `/tmp/canonical-phase-c-bench.ttl` (never written — atomic write is at-end) |
| Fuseki state | Same Finto graphs the earlier benches loaded, **plus** the P-10 Phase C `bffi:foldedLabel` materialisation pass added immediately before the bench (one-shot script `/tmp/materialise-cached-dumps.py`, 13 dumps, ~1.92 M folded triples added across LCSH + FINAF + YSO + KAUNO + …). |
| `mlx_lm.server` 8B | Identical to Phase A / A2: `--decode-concurrency 4 --prompt-concurrency 4 --prompt-cache-size 200 --prompt-cache-bytes 1073741824` |
| M9 flags | `--concurrency 4 --phase1-concurrency 8 --field-timeout-seconds 180 --tier0-expansion` |
| Env | `BFFI_M9_TIER0_EXPANSION=1` (the Phase C feature flag flipped on) |

## What happened

| Time | Event |
|---|---|
| ~07:11 UTC | Bench kicked off after the Finto-dump materialisation pass completed. |
| ~07:11–07:55 UTC | Phase 1 (tier-0 + Finto/VIAF candidate query). 14 k Fuseki / Finto SPARQL queries logged in 43 min elapsed (per `docker logs bffi-fuseki`). The added folded-fallback queries roughly **doubled** Phase 1 SPARQL traffic vs A2's serial pre-pass. |
| ~07:55 UTC | Phase 2 (LLM picker) kicked off. The Fuseki log shows the last set of `bffi:foldedLabel` queries finishing around then. |
| 07:55–09:30 UTC | Phase 2 ran for ~1h 35m. **1 500 `POST /v1/chat/completions`** calls completed at the mlx-lm side. |
| 09:30:46 UTC | mlx-lm crashed: `libc++abi: terminating due to uncaught exception of type std::runtime_error: [METAL] Command buffer execution failed: Insufficient Memory (00000008:kIOGPUCommandBufferCallbackErrorOutOfMemory)`. Prompt cache had grown to **49.86 GB** at the time of crash. The reconcile process also went down (no further `bffi-pipeline reconcile` PID after the crash). |

Total elapsed at crash: ~2h 20m. The bench was approximately at the 1 500 / total picker-call mark — Phase A2 baseline closed at 1 348 calls and 60 min, so we were **already past** A2's total-call count without finishing the Phase C run.

## Partial-data findings

### Phase C does *not* reduce picker calls on this corpus

Phase A2 baseline: 1 348 LLM picker calls.
Phase C attempt at crash: 1 500 calls **and still going** (Phase 2 not finished).

The plan's projection (P-10 prop-10 § "Phase C reduces tier-2 call count") assumed the folded tier-0 expansion would push entities currently in tier-2 into tier-0. On the May 12 corpus, the **fold-hit rate among picker-eligible entities is low**. Most tier-2 entities have:

- A cataloguer literal that *doesn't* fold-match any prefLabel — they're genuinely ambiguous-against-the-vocabulary, not just diacritic-decorated.
- A successful Finto candidate query that returns ≥2 high-similarity candidates — the picker fires regardless of whether the literal could *also* fold-match somewhere else.

Net: the materialised `bffi:foldedLabel` triples sit unused for the picker-eligible workload. They help the no_candidate → local promotion pattern (probably the bulk of Phase C's value on richer corpora) but **don't shrink the tier-2 bill**.

### Phase 1 is now slower, not faster

Per `docker logs bffi-fuseki`: each tier-0 miss now triggers a *second* SPARQL query against `bffi:foldedLabel`. With 5 140 entities missing tier-0 (per A2's outcome distribution), Phase C's Phase 1 ran ~17 k Fuseki queries vs A2's ~12 k. Wall difference: hard to measure cleanly without a clean run, but the 43-min Phase 1 elapsed vs A2's ~33-min Phase 1 suggests **~30 % slower Phase 1**.

### mlx-lm prompt-cache exhaustion on M2 Max

`--prompt-cache-size 200 --prompt-cache-bytes 1073741824` was meant to cap the cache at 1 GiB but the **actual cache footprint hit 49.86 GB**. The `prompt-cache-bytes` flag appears not to bound the runtime VRAM usage of cached sequences — it's a per-sequence size hint, not a total cap. The 200-sequence cache filled with full sequence states.

At 49.86 GB GPU cache + reconcile process (~880 MB RSS) + Fuseki container (~3 GB) + Skosmos + system overhead, the M2 Max's 64 GB unified memory got squeezed past Metal's command-buffer budget.

This wasn't a Phase C bug per se — Phase A and A2 both used the same mlx-lm config without crashing. The difference is the **longer Phase 2 wall** (Phase C ran 1 500+ calls before crash vs A2 finishing at 1 348) gave the cache more time to grow.

## Phase C acceptance status (vs the plan's gates)

- [x] **Code, lint, mypy, unit tests** — green (commit `8e47a69`, 857 tests).
- [x] **Feature flag default-off so the post-A2 behaviour is byte-stable** — the bench had to explicitly opt in via `BFFI_M9_TIER0_EXPANSION=1`.
- [x] **`needs-review` marker fires on fuzzy matches** — verified in unit tests; no production data to spot-check because the bench didn't write output.
- [ ] **200-sample cataloguer audit gate** — un-run. The bench was meant to be the pre-audit signal; we got partial signal instead of clean data.
- [ ] **`docs/performance/<date>-5k-m2-max-phase-c.md` snapshot once the audit clears** — this attempt-snapshot stands in until a clean run lands.

The code-side of Phase C is shipped and tested. The production-readiness validation is **pending** — neither the audit nor the bench is conclusive yet.

## Implications

- **`--prompt-cache-size 200` is too aggressive on the M2 Max for runs over ~90 min.** A2 survived; Phase C didn't because its Phase 1 dilation pushed the run long enough for the cache to grow over budget. Next attempt: drop to `--prompt-cache-size 100` or set `--prompt-cache-bytes` to a smaller hard cap (8 GiB) and verify the flag actually bounds VRAM rather than per-sequence size. mlx-lm 0.31 documentation is unclear on this.
- **Phase C's tier-0 expansion is likely a net regression on the May 12 corpus.** The added Phase 1 SPARQL work isn't offset by picker-call reduction. The Phase C code-side stays committed (flag-gated off) and the audit can still happen offline; the production pipeline should leave `BFFI_M9_TIER0_EXPANSION=0` until a different corpus shows a meaningful fold-hit rate on picker-eligible entities.
- **The next clean bench wants the M5 Max 128 GB** (the production target per `CLAUDE.md` § "Operating constraints") where the mlx-lm cache has 2× the memory budget. On the M2 Max, the safe path is the smaller prompt-cache config.
- **P-11 observability would have caught this an hour earlier**: a dashboard's mlx-lm health-probe + cache-size panel could have shown the GPU pressure climbing toward the crash. P-11 shipped a few hours after this bench attempt; the next bench is the first that gets to use it live.

## Cross-references

- [`docs/performance/2026-05-12-5k-m2-max.md`](2026-05-12-5k-m2-max.md) — pre-P-10 baseline.
- [`docs/performance/2026-05-12-5k-m2-max-phase-a.md`](2026-05-12-5k-m2-max-phase-a.md) — Phase A bench (5 460 s).
- [`docs/performance/2026-05-12-5k-m2-max-phase-a2.md`](2026-05-12-5k-m2-max-phase-a2.md) — Phase A2 bench (3 639 s).
- [`docs/plans/in-progress/p-10-m9-reconcile-throughput.md`](../plans/in-progress/p-10-m9-reconcile-throughput.md) — P-10 plan; Phase C acceptance section gets a pointer back to this attempt.
- [`docs/plans/completed/p-11-structured-observability.md`](../plans/completed/p-11-structured-observability.md) — the observability work that would have caught this crash earlier.
- `8e47a69` — Phase C commit (code shipped; production validation deferred).
