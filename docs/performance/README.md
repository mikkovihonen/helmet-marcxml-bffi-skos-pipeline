# Performance snapshots

Per-run wall-time + throughput records, one file per snapshot.
Each snapshot is a *specific run* on *specific hardware* with the
*specific code state* indicated by the headers — they're not
forecasts. The whole point of carrying them in the repo is to
make extrapolations and regressions visible against real numbers
rather than against a moving "expected on M5 Max" target.

## File naming

`<YYYY-MM-DD>-<sample-size>-<hardware-class>.md`, e.g.
`2026-05-12-5k-m2-max.md`. Hardware-class is what's documented
in [`docs/local-inference.md`](../local-inference.md) §
"Throughput findings" — currently `m2-max` (development) and
`m5-max` (production, when the box arrives).

## Required sections per snapshot

- **Run metadata**: date, hardware + RAM, git HEAD, sample size,
  source, mlx-lm server config (chat-template-args, decode/
  prompt-concurrency, prompt-cache size), Fuseki state at start
  (Finto graphs loaded vs not).
- **Stage timings**: wall-time per pipeline stage, plus a
  visual breakdown of "where the time goes".
- **M6 / M9 observations**: candidate counts, decision counts,
  cascade-escalation count, cache hit rate, per-call median
  latency.
- **Outputs**: final canonical Work / Expression counts, conflict
  groups, Skosmos round-trip verification.
- **Extrapolation**: how the numbers translate to other corpus
  sizes; flag the stages that don't scale linearly.
- **Bonus findings**: any data-quality / pipeline-robustness
  issues the run surfaced, with the fix-commit hash so future
  re-runs can be compared honestly.

## Current snapshots

- [`2026-05-12-5k-m2-max.md`](2026-05-12-5k-m2-max.md) — first
  end-to-end production-style run after P-02 closed; 5 000
  randomly-sampled Helmet records on M2 Max 64 GB. ~115 min
  end-to-end, M9 reconcile is the dominant stage (82.9 % of
  total wall-time). Also documents the six converter / cache
  robustness fixes shipped during the run.
- [`2026-05-12-5k-m2-max-phase-a.md`](2026-05-12-5k-m2-max-phase-a.md)
  — P-10 Phase A bench (M9 at `c=4` + watchdog). Wall 5 460 s
  vs 5 722 s baseline (1.05× speedup), zero
  `field_budget_exceeded` events, byte-identical bindings.
  Surfaces that serial Phase 1 (tier-0 + Finto/VIAF candidate
  query) is the new bottleneck — Phase A's concurrency lever
  only covered ~30 % of the wall — informing Phase B + C
  scoping.
- [`2026-05-12-5k-m2-max-phase-a2.md`](2026-05-12-5k-m2-max-phase-a2.md)
  — P-10 Phase A2 bench (`phase1=8` + `c=4`). Wall 3 639 s,
  cumulative 1.57× speedup vs baseline (1.50× over Phase A).
  Phase 1 wall dropped ~1.9× (sublinear vs 8× nominal; server-
  side latency on Finto/VIAF dominates throughput). Still
  below the ≥3× target; A2 + B + C projected to close the
  gap. Outcome distribution byte-identical to Phase A
  (modulo run-time `descriptionChangeDate`).
- [`2026-05-13-5k-m2-max-phase-c-attempt.md`](2026-05-13-5k-m2-max-phase-c-attempt.md)
  — P-10 Phase C bench **attempt** (tier-0 expansion flag on).
  Did not complete: mlx-lm crashed with Metal GPU
  out-of-memory at ~2h 20m elapsed (prompt cache hit 49.86 GB
  on the M2 Max 64 GB). 1 500 picker calls had completed —
  already past Phase A2's 1 348 baseline without finishing,
  suggesting Phase C does *not* shrink tier-2 work on this
  corpus. Phase 1 ran ~30 % slower than A2 because each
  tier-0 miss now triggers a second SPARQL query against
  `bffi:foldedLabel`. The 200-sample audit gate (plan § C.5)
  remains un-run; next attempt either drops mlx-lm
  `--prompt-cache-size` to 100 or waits for the production
  M5 Max 128 GB.
- [`2026-05-13-5k-m2-max-phase-b-e.md`](2026-05-13-5k-m2-max-phase-b-e.md)
  — P-10 Phase B + E combined bench (cold + warm consecutive
  5k runs at `--prompt-cache-size 100`). Cold 4 147 s, warm
  **1 440 s** — cold→warm **2.88×** speedup; picker phase
  3.25× faster on warm at 65.8 % cache hit rate. Hit rate
  below the plan's ≥ 90 % target because the cache stores
  only successful LLM picks (cold's 473 fallback outcomes
  intentionally aren't cached → 461 warm misses re-dispatch
  the picker). Phase E unmeasurable cleanly because the
  mlx-lm prompt-cache size dropped from 200 to 100 at the
  same time; queued as a follow-up A/B. **Open issue**:
  cold and warm outcome distributions diverge (~1 000
  entities shifted lexical → local), likely a triple-
  deduplication bug in the cache-hit codepath; audit script
  + Phase B.1 fix queued before declaring Phase B
  production-ready.
- [`2026-05-13-5k-m2-max-phase-b1.md`](2026-05-13-5k-m2-max-phase-b1.md)
  — P-10 Phase B.1 re-bench (cache every picker decision, not
  just `STAGE_LLM`). Cold 3 992 s, warm **276 s (4 m 36 s)** —
  cold→warm **14.5×** speedup at **100 % cache hit rate**
  (1 348 / 1 348). Output **99.999 % byte-stable** (1 triple
  diff over 168 969, a single `needs-review ↔ auto-merged`
  flip attributable to upstream Finto candidate-set variance,
  not the cache). The pre-B.1 open issue is **closed**; the
  plan's ≥ 90 % hit-rate gate is comfortably exceeded. The
  ≤ 100 s warm wall target is unreachable on the M2 Max + this
  corpus because Phase 1's HTTP-bound floor is ~245 s — the
  picker phase itself is effectively free on warm. Phase E
  still unmeasurable cleanly (warm Phase 2 had nothing to
  prefix-cache). 800k extrapolation: **~10.8 h warm wall**,
  just at the overnight window edge.
