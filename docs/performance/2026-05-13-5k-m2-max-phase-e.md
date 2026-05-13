# 5 000-record M9 bench — Phase E A/B (submission vs prefix-cache), M2 Max 64 GB, 2026-05-13

A/B comparison of the P-10 Phase E hypothesis: ordering the
Phase 2 picker queue by prompt-prefix similarity (so consecutive
mlx-lm calls share more of the system prompt + few-shot
exemplars) should lift the prompt-cache hit rate and reduce
picker-phase wall by ≥ 5 % vs the pre-Phase-E `submission` order.

Two consecutive cold runs against the same `data/canonical.ttl`,
mlx-lm restarted between them so the prompt-cache state is
equal at the start of each shot. Operator-paced via
`/tmp/phase-e-shot.sh`.

## Run metadata

| Field | Value |
|---|---|
| Date | 2026-05-13 |
| Hardware | MacBook Pro, M2 Max, **64 GB unified memory** |
| Git HEAD at run start | `46e2f8e` (P-12 follow-up dashboard fixes; Phase E code path `c07d333` unchanged) |
| Sample | Same `data/canonical.ttl` used by Phase A / A2 / B / B.1 benches (M8 output dated `May 12 08:53`); 12 666 entities, 1 348 deferred to picker |
| Fuseki state | Same Finto graphs + dormant Phase C `bffi:foldedLabel` triples; tier-0 expansion **off** |
| `mlx_lm.server` 8B | `--decode-concurrency 4 --prompt-concurrency 4 --prompt-cache-size 100 --prompt-cache-bytes 1073741824 --chat-template-args '{"enable_thinking":false}'` — identical to Phase B.1 bench |
| M9 flags | `--concurrency 4 --phase1-concurrency 8 --field-timeout-seconds 180 --no-cache` (reconcile-cache **deleted between shots**, both runs cold) |
| Shot 1 ordering | `submission` (Phase A2 baseline behaviour) |
| Shot 2 ordering | `prefix-cache` (Phase E proposal) |
| Shot 1 run_uuid | `073131d21c0e47aab30320c54290cef4` |
| Shot 2 run_uuid | `300716775a3b4a77946a3979683b200a` |
| mlx-lm restart between shots | yes — confirms equal cache state at start of each shot |

## Wall-time headline

| Shot | `time -p real` | M9 Phase 1 | M9 Phase 2 (picker) | M9 Phase 3 | M9 total wall |
|---|---:|---:|---:|---:|---:|
| Submission | **4 085.79 s** (1 h 08 m 06 s) | 229 s (3 m 49 s) | **3 802 s (63 m 22 s)** | 8 s | 4 039 s (67 m 19 s) |
| Prefix-cache | **4 295.43 s** (1 h 11 m 35 s) | 240 s (4 m 00 s) | **3 993 s (66 m 33 s)** | 7 s | 4 240 s (70 m 40 s) |
| **Δ prefix-cache vs submission** | **+209.6 s (+5.1 %)** | +11 s (+4.8 %) | **+191 s (+5.0 %)** | -1 s | +201 s |

Per-call picker wall (Phase 2 wall ÷ 1 348 picker calls):

| Shot | Avg seconds / picker call |
|---|---:|
| Submission | **2.82 s** |
| Prefix-cache | **2.96 s** |
| Δ | **+0.14 s (+5.0 %)** |

## Outcome distribution — Phase 1 + Phase 2 combined

Phase 1 is ordering-independent (per-entity tier-0 + lexical
decisions), so its outcome counts are identical across shots.
Phase 2's `llm_pick` vs `fallback` split varies by ±2 between
runs, well within picker-stochasticity noise.

| Outcome | Submission | Prefix-cache | Δ |
|---|---:|---:|---:|
| local | 7 526 | 7 526 | 0 |
| lexical | 193 | 193 | 0 |
| no_candidate | 2 752 | 2 752 | 0 |
| fictional | 847 | 847 | 0 |
| llm_pick | 883 | 885 | +2 |
| fallback | 465 | 463 | -2 |
| watchdog_aborted | 0 | 0 | 0 |
| **Total** | **12 666** | **12 666** | 0 |

## Acceptance gate

P-10 Phase E plan ([§ Phase E acceptance criteria, line 409](../plans/in-progress/p-10-m9-reconcile-throughput.md)):

> 5 k re-run with `BFFI_M9_PICKER_ORDERING=prefix-cache` (against
> Phase A + A2, Phase C still flag-off) clocks picker-phase wall
> **≥ 5 % below** the Phase A2 baseline.

**Result: FAIL.** Prefix-cache ordering produced a **+5.0 %
regression** in picker-phase wall vs the `submission` baseline,
not a reduction. The acceptance gate's "regression vs Phase A2
fails" clause kicks in.

## Why the hypothesis didn't hold

The Phase E rationale assumed the mlx-lm prompt-cache would
exploit shared prefixes more effectively when consecutive
picker calls share more of the user-message body (the
candidate-list portion that varies per call). Empirically this
either:

1. Doesn't translate into prompt-cache hits in this mlx-lm
   build — the cache may be keyed at a granularity that
   doesn't match the sort key's prefix structure.
2. Costs more than it saves: the `prefix-cache` sort groups
   picker calls by `(entity_kind, source_vocabulary, literal_prefix)`,
   which destroys whatever natural locality the submission order
   already had (e.g. multiple subject-heading picks for the same
   MARC record arriving back-to-back).
3. The 5 k sample is dominated by heterogeneous calls — the
   plan flagged that "YSO-heavy corpora (long runs of same-kind
   picks)" would benefit more than this sample's mix. The
   on-corpus sample is what the production batches will look
   like, so any future re-run on a different corpus is
   speculative.

The cataloguer-curated 5 k sample has approximately the
heterogeneity profile we expect on full 800 k runs (mix of
subject / person / corporate / fictional). Extrapolating a
positive Phase E result from the 5 k sample to the full corpus
would have required at least a flat (non-regression) result
here.

## Recommendation

**Flip the `BFFI_M9_PICKER_ORDERING` default back to
`submission`.** Keep `prefix-cache` available behind the env
var for future re-benches on different corpora (e.g. once the
NLF YSO-heavy sample lands), but don't ship it as the default
when the only on-corpus measurement shows a regression.

Code change: ~1 line in `src/bffi_pipeline/config.py`
(`m9_picker_ordering` Field default). Tests for byte-stability
under both orderings (Phase E.3 in the plan) stay green —
they verify the orchestrator's post-pool sort-by-`idx`
preserves canonical output regardless of dispatch order, not
which order is faster.

## Where the time goes (visual)

```
Submission:  M9 total 4 039 s
  ├─ Phase 1 (parallel tier-0 SPARQL)                      ███  5.7 %  (229 s)
  ├─ Phase 1.5 cache lookup                                ·    <0.1 % (1 s)
  ├─ Phase 2 (LLM picker, 1 348 calls)                     ████████████████████████████████████  94.1 %  (3 802 s)
  └─ Phase 3 (graph mutation + provenance write)           ·    0.2 %  (8 s)

Prefix-cache: M9 total 4 240 s
  ├─ Phase 1                                               ███  5.7 %  (240 s)
  ├─ Phase 1.5                                             ·    <0.1 % (~1 s)
  ├─ Phase 2 (LLM picker, 1 348 calls)                     █████████████████████████████████████ 94.2 %  (3 993 s)
  └─ Phase 3                                               ·    0.2 %  (7 s)
```

## Sidecar observation — Option C populated mid-run for the first time

P-12 follow-up (`780d61a`) shipped between shots 1 and 2 had
M9 emit per-tier outcomes inside each `progress` event's
`extra` (cumulative `llm_pick` / `fallback` /
`watchdog_aborted`). Shot 2 was the first run where the
dashboard's M8+M9 outcome bargauge populated **live** during
Phase 2 instead of jumping from empty to fully populated at
the `end` event:

| Progress event | processed | llm_pick | fallback |
|---|---:|---:|---:|
| 200 / 1 348 | 200 | 139 | 61 |
| 400 / 1 348 | 400 | 282 | 118 |
| 600 / 1 348 | 600 | 405 | 195 |
| 800 / 1 348 | 800 | 544 | 256 |
| 1 000 / 1 348 | 1 000 | 679 | 321 |
| 1 200 / 1 348 | 1 200 | 819 | 381 |
| 1 348 / 1 348 (final flush) | 1 348 | 885 | 463 |

This is orthogonal to the A/B verdict but confirms that the
mid-run dashboard signal will work on future overnight runs.
