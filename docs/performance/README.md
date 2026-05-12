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
