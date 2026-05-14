# In progress

Plans where at least one phase has shipped but the plan's
"Definition of done" hasn't been met yet. The plan's `Phase
commits` field should carry concrete commit hashes for the shipped
phases and `<unfilled>` for the ones still ahead.

When the final phase commits and the plan's definition of done is
green, `git mv` the plan into [`../completed/`](../completed/) in
the same commit.

If the plan is dropped before completion, `git mv` it to
[`../abandoned/`](../abandoned/) and add a short
`Abandonment reason` section near the top.

## Current in-progress plans

- [`p-17-exporter-multi-sidecar-discovery.md`](p-17-exporter-multi-sidecar-discovery.md)
  — Phase A (multi-sidecar + watch-glob + per-sidecar error specs +
  startup-log echo) shipped at `9a0601d` with 5 unit tests. Phase B
  (bench smoke test confirming a fresh bench-dir's events +
  co-located error JSONLs surface without an exporter restart)
  pending.
- [`p-18-m8-emit-start-before-corpus-load.md`](p-18-m8-emit-start-before-corpus-load.md)
  — Phase A (lifecycle event reorder + new `phase_boundary` event
  with `phase="emit"`) shipped at `5148746` with a unit test
  pinning the event sequence. Phase B (dashboard smoke test on
  the next bench) pending.
- [`p-19-m8-corpus-load-throughput.md`](p-19-m8-corpus-load-throughput.md)
  — Phase A (Option A: `bffi-corpus.ttl` concat written at M3
  finalisation with `@prefix` dedup + atomic rename; M8 fast-path
  read with mtime-based fallback) shipped at `5148746` with 4 unit
  tests. Phase B (20 k bench wall-time validation: M8 load drops
  from ~8 min to <90 s) pending. Phase C (Option C bespoke
  streaming parser) deferred to "only if Phase B's win isn't
  enough at full corpus scale".
