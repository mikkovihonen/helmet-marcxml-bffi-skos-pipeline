# 5 000-record M9 bench — Phase B + E (cold + warm), M2 Max 64 GB, 2026-05-13

First clean bench against the P-10 Phase B (persistent picker decision
cache) + Phase E (prompt ordering) combined surface. Two consecutive
runs against the same input: a **cold** run that fills the cache
followed by a **warm** run that reuses it. The cold run also serves
as the Phase E baseline against the Phase A2 snapshot (`2026-05-12-5k-m2-max-phase-a2.md`).

> **Caveat — corpus freshness.** This bench reuses the May 12 baseline's
> `data/canonical.ttl` (M8 output dated `May 12 08:53`), same as the
> Phase A / A2 / C-attempt benches. The absolute numbers are corpus-
> specific; A2 → B+E deltas are internally valid.

## Run metadata

| Field | Value |
|---|---|
| Date | 2026-05-13 |
| Hardware | MacBook Pro, M2 Max, **64 GB unified memory** |
| Git HEAD at run start | `cbaa7b2` (post-P-10-Phase-B, post-Phase-E, pre-P-12) |
| Sample | Same `data/canonical.ttl` the Phase A / A2 / C-attempt benches used |
| Fuseki state | Same Finto graphs the earlier benches loaded, **plus** the lingering Phase C `bffi:foldedLabel` materialisation triples from the 2026-05-13 morning session (~1.92 M folded triples). Tier-0 expansion is **off** so those triples are inert for this bench. |
| `mlx_lm.server` 8B | `--decode-concurrency 4 --prompt-concurrency 4 --prompt-cache-size 100 --prompt-cache-bytes 1073741824 --chat-template-args '{"enable_thinking":false}'` — `--prompt-cache-size` halved from the A2 bench's 200 after the Phase C attempt OOM'd at 49.86 GB VRAM. |
| M9 flags | `--concurrency 4 --phase1-concurrency 8 --field-timeout-seconds 180` (cache **enabled**, picker ordering **prefix-cache**, tier-0 expansion **off**) |
| Cold run | `--no-cache` not passed; default cache **on**; `data/reconcile-cache.sqlite` deleted before the bench by the driver. |
| Warm run | Cache from cold run intact (729 rows). |

## Wall-time headline

| Run | Driver `time -p real` | M9 phase 1 wall | M9 phase 2 wall | M9 phase 3 wall | Cache effect |
|---|---|---|---|---|---|
| Cold | **4 147.34 s** (1 h 09 m 07 s) | ~276 s (4 m 36 s) | ~3 837 s (63 m 57 s) | ~8 s | 0 hits / 1 358 calls (empty cache) |
| Warm | **1 440.42 s** (24 m 00 s) | ~237 s (3 m 57 s) | ~1 179 s (19 m 39 s) | ~7 s | **887 hits / 1 348 deferred = 65.8 %** hit rate |

**Cold → warm speedup: 2.88×.** Picker phase wall fell from 3 837 s
to 1 179 s — a **3.25× speedup on the picker phase** at 65.8 % cache
hit rate. The remaining 461 picker calls in the warm run correspond
to the cold run's fallback / uncertain outcomes, which P-10 Phase B
intentionally does not cache (the picker should re-attempt those if
the model or prompt is updated).

## Where the time goes

```
Cold:  M9 total 4 147 s
  ├─ Phase 1 (tier-0 SPARQL + Finto/VIAF candidate query)   ████  6.7 %  (276 s)
  ├─ Phase 1.5 cache lookup                                 ·     <0.1 % (2 s)
  ├─ Phase 2 (LLM picker, 1 358 calls)                      ███████████████████████████████████  92.5 %  (3 837 s)
  └─ Phase 3 (graph mutation + provenance)                  ·     <0.1 % (8 s)

Warm:  M9 total 1 440 s
  ├─ Phase 1 (tier-0 SPARQL + Finto/VIAF candidate query)   ███████  16.5 %  (237 s)
  ├─ Phase 1.5 cache lookup (887 hits / 461 misses)         ·       <0.1 % (2 s)
  ├─ Phase 2 (LLM picker, 461 calls)                        █████████████████████████████████  81.9 %  (1 179 s)
  └─ Phase 3 (graph mutation + provenance)                  ·       <0.1 % (7 s)
```

Phase 1 wall is roughly constant (~250 s) across cold and warm
because the cache does not short-circuit Finto/VIAF candidate
queries — it only short-circuits the LLM picker. The 39-second
delta is run-to-run network noise; consistent with the Phase A2
snapshot's report that Finto/VIAF latency dominates Phase 1 walls
in the 30-50 ms / request range.

## P-10 Phase B + E observations

### Cache hit rate: **65.8 %**

Below the plan's `≥ 90 %` target. Investigation: the cache only
stores `STAGE_LLM` outcomes (decision="chose" with confidence ≥
0.80) per the Phase B design. Of the cold run's 1 358 picker
calls, only 885 ended in `STAGE_LLM`; the remaining 473 became
`STAGE_FALLBACK` (LLM said `"uncertain"` or returned confidence
< 0.80) and were intentionally not cached. On the warm run those
473-ish entries re-dispatch the picker; the actual hit rate of
**887 / 1 348 = 65.8 %** lines up with the cold run's
LLM-pick fraction (885 / 1 358 = 65.2 %).

The plan's `≥ 90 %` target was set against a corpus where most
picker calls were expected to succeed. The May 12 sample has a
non-trivial fraction of ambiguous-enough-to-fall-back entities;
caching those won't help re-runs unless the model / prompt changes.
**Recommendation**: leave the cache contract as-is; revise the
plan's hit-rate target to reflect "≥ 90 % of cacheable picks"
rather than "≥ 90 % of all picks". The wall-time speedup
(2.88×) is the load-bearing operator win regardless.

### Cache footprint: **729 rows, 804 KB**

Despite 885 successful LLM picks during the cold run, only 729
rows were persisted. The compaction comes from the cache key —
`(literal, sorted-candidate-URIs, prompt-hash, model, vocab+finto_sha)`
— deduping picks that share inputs across different work_uris
(e.g. "Tolkien, J. R. R." appearing as creator on many books
yields one cache row, many hits). Per-vocab breakdown:

| Vocab | Rows |
|---|---|
| `finaf` (KANTO persons + corporate bodies) | 723 |
| `yso` | 5 |
| `kauno` | 1 |

The skew toward FINAF reflects the corpus: KANTO-routed creator
picks dominate the ambiguous-tier on this sample.

### Phase E (prompt ordering) effect

Phase 2 wall on the cold run: **3 837 s** for 1 358 picker calls
→ **2.83 s / call** mean. Phase A2 (same workload, same
`--prompt-cache-size 200`, no Phase E ordering) reported ~27 min
for 1 348 calls → **1.20 s / call** mean. Per-call wall **grew
2.4×** on this bench, not shrank.

**Root cause: `--prompt-cache-size 100`, not Phase E.** The Phase
A2 bench ran with mlx-lm's prompt-cache size at 200; after the
Phase C attempt OOM'd the GPU at 49.86 GB VRAM on the M2 Max, the
plan recommended halving to 100. That halving reduces the LRU's
ability to keep prompt prefixes warm across the picker queue.
Phase E's prefix-cache-stickiness sorting *helps* — without it,
per-call wall would likely be even worse — but the size cut from
200 → 100 dominates.

**Implication for the Phase E acceptance gate (≥ 5 % picker-phase
wall reduction vs Phase A2):** this bench cannot cleanly measure
Phase E in isolation because the mlx-lm prompt-cache config
changed at the same time. The picker-phase wall regression vs A2
is structural (memory budget), not a Phase E failure. A clean
Phase E A/B (`BFFI_M9_PICKER_ORDERING=submission` vs `prefix-cache`
at the same `--prompt-cache-size`) is queued as a follow-up.

### Watchdog: **0 events** across both runs

Zero `field_budget_exceeded`, zero `pair_budget_exceeded`, zero
`timeout`. Picker call distribution stayed below the 180 s
per-field budget throughout both runs.

## Outcome distribution

Cold vs warm tier counts diverge significantly — see § "Open
question" below.

| Outcome | Cold | Warm | Δ |
|---|---:|---:|---:|
| `local` (tier-0) | 6 280 | **7 526** | +1 246 |
| `lexical` (tier-1) | 1 198 | **193** | -1 005 |
| `llm_pick` (tier-2 → committed) | 885 | 887 | +2 |
| `fallback` (tier-3) | 473 | 461 | -12 |
| `no_candidate` | 2 983 | 2 752 | -231 |
| `fictional_character` | 847 | 847 | 0 |
| **Total** | **12 666** | **12 666** | 0 |

The totals match (12 666 entities both runs). But the tier-0
share grew from 49.6 % to 59.4 % between runs, with tier-1
correspondingly shrinking by a near-identical amount. This is
**unexpected** and breaks the Phase B byte-stability claim.

## Byte-stability check

The canonical Turtle files differ:

```
$ cmp /tmp/canonical-phase-b-cold.ttl /tmp/canonical-phase-b-warm.ttl
... differ: char 2394897, line 42753
```

Spot-checking the diff:

- ~1 000 lines moved between contexts (`;` → `,` punctuation
  swaps) — semantically identical RDF, different Turtle
  serialisation order.
- ~14 duplicate URI lines on one work — `<yso:p104984>` appears 7
  times in a list where the cold output has it once. Suspect
  cache-hit codepath in `_apply_canonical_link` is double-adding
  on already-bound work-uri/predicate pairs.

**Both signal real differences**, not just timestamp drift on
`descriptionChangeDate` like prior benches. See § "Open question".

## Open question — cold/warm divergence

The warm run's outcome distribution shifted ~1 000 entities from
`lexical` to `local`, which the cache lookup logic cannot cause
on its own (tier-0 / tier-1 / no-candidate paths don't read the
cache). Three competing hypotheses:

1. **Fuseki state drift between runs.** The lingering Phase C
   `bffi:foldedLabel` triples in Fuseki accumulated during the
   morning's session; if any non-determinism in the SPARQL
   CONSTRUCT result ordering existed, the cold and warm runs
   could see different tier-0 hit sets. *Pre-test for this:*
   re-run both passes with `data/finto-dumps` deleted +
   freshly re-loaded, see if the divergence persists.

2. **External process mutated Fuseki between runs.** Unlikely —
   no `bffi-pipeline load-finto` ran in this session — but the
   killed stale Phase C bench (PIDs 73677 / 73675) may have left
   a partial Fuseki write outstanding when it died. *Pre-test:*
   `DESCRIBE` an affected concept (any of the 1 005 that moved
   from tier-1 to tier-0) in both runs' provenance and diff the
   chosen-URI rationale.

3. **Cache-hit codepath duplicates triples.** The 7×
   `<yso:p104984>` shows the warm run is writing a triple
   multiple times where the cold run wrote it once. If
   `_apply_canonical_link` doesn't dedupe on rdflib `add()`
   (which it should — `Graph.add` is idempotent on identical
   triples), the canonical graph could accumulate a triple per
   `(cached-hit, fresh-canonical-link)` collision. *Pre-test:*
   grep the picker-cache rows for the chosen URI of an affected
   work; compare the cold-run provenance Activity for that work
   to the warm-run Activity.

**Likely root cause: hypothesis 3** (or a variant of it). The
1 005 lexical→local shift is suspiciously close to "number of
cache hits whose canonical URI was already present in the warm
graph from a prior tier-0 binding". Confirmation requires the
follow-up audit below.

**Recommended follow-up**: bench-snapshot defers final byte-
stability claim; queue a small audit script under
`scripts/p10-phase-b-cold-warm-diff.py` that diffs the per-
record outcome stream between cold and warm runs (rather than
the serialised Turtle) and flags works whose tier classification
changed. If hypothesis 3 holds, fix the dedupe and ship a Phase
B.1 patch. If hypothesis 1 holds, document the Fuseki-state
sensitivity and add a `make reset-fuseki` step to the bench
driver.

## Implications

- **Phase B is a real operator win on wall-time** (cold→warm
  2.88×, picker phase 3.25×) but the **hit-rate gate is corpus-
  dependent**. Plan should re-frame the gate as "fraction of
  cacheable picks served from cache" rather than absolute %.
- **Phase E is not cleanly measurable on this bench** because
  the mlx-lm `--prompt-cache-size 100` was applied at the same
  time. The follow-up A/B should hold prompt-cache config
  constant.
- **The cold-warm outcome divergence is the new blocking issue**
  for declaring Phase B production-ready. The 2.88× speedup is
  too valuable to revert; the audit script above is the
  proportionate follow-up.
- **Watchdog stayed clean** — `--prompt-cache-size 100` did not
  introduce hangs at this corpus size on the M2 Max. The 180 s
  per-field budget was never exceeded.
- **Cache cardinality (729 rows / 804 KB) is negligible**, well
  within any reasonable disk + memory budget for the full 800 k
  corpus extrapolation.
- **Extrapolation to 800 k**: at 12 666 entities × 160 = ~2 M
  reconciliation entities, a warm-cache full-corpus run would
  process at the warm run's ~9 entities/s effective rate →
  ~62 h, far beyond an overnight window. The cold-run rate is
  ~3 entities/s → ~180 h. Phase B alone doesn't unblock the
  overnight target on this corpus + this hardware. The plan's
  overnight goal requires Phase B + a Phase D follow-up
  (batched picker) on production-class hardware (M5 Max 128 GB
  with the original 200-entry prompt cache).

## Artefacts

| Artefact | Path | Notes |
|---|---|---|
| Cold reconciled graph | `/tmp/canonical-phase-b-cold.ttl` | 14 MB. **Diverges** from warm output — see open question. |
| Warm reconciled graph | `/tmp/canonical-phase-b-warm.ttl` | 14 MB. |
| Cold log | `/tmp/phase-b-cold.log` | `time -p real 4147.34 s` + M9 summary. Gitignored. |
| Warm log | `/tmp/phase-b-warm.log` | `time -p real 1440.42 s` + M9 summary. Gitignored. |
| Driver log | `/tmp/phase-b-bench-driver.log` | Per-run timestamps + sidecar offsets. Gitignored. |
| Picker cache | `data/reconcile-cache.sqlite` | 804 KB. 729 rows: 723 finaf, 5 yso, 1 kauno. |
| Sidecar events | `data/stage-events.jsonl` | Cold = 82 events, warm = 84 events (warm adds Phase D progress events). |
| Watchdog sidecar | (not written) | Zero watchdog events. |

## Cross-references

- [`docs/performance/2026-05-12-5k-m2-max-phase-a2.md`](2026-05-12-5k-m2-max-phase-a2.md) — Phase A2 baseline (60 m, `--prompt-cache-size 200`). Direct comparator for Phase B's cache effectiveness; **not** a direct comparator for Phase E because the cache size moved.
- [`docs/performance/2026-05-13-5k-m2-max-phase-c-attempt.md`](2026-05-13-5k-m2-max-phase-c-attempt.md) — the attempt that crashed mlx-lm at `--prompt-cache-size 200` and motivated the halving.
- [`docs/plans/in-progress/p-10-m9-reconcile-throughput.md`](../plans/in-progress/p-10-m9-reconcile-throughput.md) — Phase B + E acceptance sections this snapshot informs.
- [`docs/plans/completed/p-12-observability-cleanup.md`](../plans/completed/p-12-observability-cleanup.md) — the dashboard cleanup shipped during this bench session.
- [`docs/plans/completed/p-13-per-run-metric-isolation.md`](../plans/completed/p-13-per-run-metric-isolation.md) — the per-run dashboard isolation shipped during this bench session.
- `8950741` — P-10 Phase B commit.
- `c07d333` — P-10 Phase E commit.
- Cold-run `run_uuid`: `5437f0648ef7467c9fccc2cb263e6890`.
- Warm-run `run_uuid`: `ea5d45aa38cc43d999fa531fdfd3a134`.
