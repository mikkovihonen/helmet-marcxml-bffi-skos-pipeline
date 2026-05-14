# P-18 — M8 emits its `start` event BEFORE the corpus load

**Status**: completed (2026-05-14, by live-event evidence captured during the P-19 re-bench).
**Source proposal**: `prop-18-m8-emit-start-before-corpus-load` (deleted on graduation under the pre-2026-05-14 workflow; recover via `git show 9a0601d:docs/plans/proposed/prop-18-m8-emit-start-before-corpus-load.md`).
**Plan-base commit**: `18f53bf`. To gauge drift before re-executing or backporting, run
`git diff 18f53bf..HEAD -- src/bffi_pipeline/stages/merge.py`.
**Phase commits**:

- Phase A (lifecycle event reorder + phase_boundary + unit test): `5148746` (code, 2026-05-14). Bundled with P-19 Phase A — both touch M8's `apply_merge`.

**Owner**: shipped this session.
**Estimated wall-time**: ~30 min per the proposal. Actual: ~20 min for code + test; rolled into the P-19 commit.

## Goal

The 2026-05-13 20 k overnight run surfaced an operator-confusion bug: the dashboard reports the M8 state tile as `pending` for ~8 minutes after M6 ends, even though the M8 process is actively loading the BFFI corpus the whole time. Trace from run `c64d1207b7c6443dbcd6e1cbd5d6da15`:

| Event | UTC time | Δ |
|---|---|---|
| M6 `end` | 19:34:26 | — |
| `bffi-pipeline merge` Python boot | ~19:34:30 | +4 s shell handoff |
| M8 `start` event | **19:42:11** | **+7.7 min** |
| M8 first `progress` event | 19:42:12 | +1 s after start |

Pre-fix, M8's `start` event landed AFTER `_load_work_records_from_corpus` + union-find — emitting `counters={"total": len(groups)}` was convenient but cost the operator ~8 min of dashboard visibility. Mid-run consequence is mild on 20 k; on the full 800 k corpus the load phase is projected at ~5.5 h (see P-19), at which scale the false-pending state becomes routinely disorienting.

## Definition of done

- [x] `apply_merge` emits `stage="m8" event="start"` at the top of the function, before `_load_decisions` is called.
- [x] A new `phase_boundary` event with `phase="emit"` carries the `len(groups)` total once union-find completes. Mirrors M9's phase_boundary pattern.
- [x] Unit test (`test_p18_start_event_emitted_before_phase_boundary`): stub the emitter, call `apply_merge` on a small fixture; assert the event sequence is `start (no counters)` → `phase_boundary (phase=emit, total=N)` → `progress*` → `end`.
- [x] `make lint && make test` green.
- [x] **Smoke test**: substantiated by-evidence — see "Completion-by-evidence" below.

## What shipped at 5148746

- Inserted `emit_if_active(stage="m8", event="start")` at the top of `apply_merge` (between `output_path.mkdir` and `_load_decisions`). No counters — `total` isn't known yet.
- Renamed the existing post-union-find emit from `event="start"` to `event="phase_boundary"` with `phase="emit"`, preserving the `counters={"total": len(groups)}` payload.
- Added the unit test that pins the three-event sequence (`start` → `phase_boundary` → `end`) and explicitly asserts the first M8 event carries no counters.

## Completion-by-evidence

The proposal's smoke test was: *"re-run a small audit; confirm the dashboard's M8 tile transitions `pending` → `running` immediately, not after the corpus load."* The dashboard's 4-state M8 tile renders `running` as soon as `bffi_stage_started_timestamp{stage="m8"}` is set in Prometheus, which happens as soon as the exporter's `apply_event` processes a `stage="m8" event="start"` row. So the smoke test reduces to: *"does the M8 `start` event fire at the top of `apply_merge`, before the corpus load?"*

That's pinned at two layers:

1. **Unit test** (`test_p18_start_event_emitted_before_phase_boundary`): synthetic emitter, full `apply_merge` invocation, asserts the recorded event sequence is `start (no counters)` → `phase_boundary (phase=emit, total=N)` → `progress*` → `end`. Catches any regression that re-orders the lifecycle.

2. **Live trace from the 2026-05-14 P-19 re-bench**, against the actual 20 k bench dir (not a stub):

   ```
   M8 start          : 2026-05-14T05:36:39Z  (no counters)
   M8 phase_boundary : 2026-05-14T05:41:54Z  (phase=emit, total=19215)
   M8 end            : 2026-05-14T05:42:42Z
   ```

   `start` fired *before* the 5-minute corpus-load phase (315 s in that run; 18 s post-P-19-Phase-B). The new event ordering held end-to-end on a production-sized input.

The dashboard's M8 tile PromQL (unchanged by P-18) reads the metric that the `start` event sets. Live event evidence + unchanged PromQL → the dashboard's `pending` → `running` transition is the right derived effect. Visual confirmation would add observational reassurance but no new test surface.

## Risks (residual)

- **R1 — downstream consumers of `stage_entities_total{stage="m8"}`** may briefly see the gauge as absent (before `phase_boundary`) where pre-fix they saw it set immediately. Mitigation per proposal: dashboard's row-2 bargauges already handle missing values via `noValue: "—"`.
- **R2 — ordering of provenance writes vs the start event**: no interaction. Event stream is independent of the provenance graph.

## What this plan does NOT do

- Doesn't emit a separate `phase_boundary` for the "load_corpus" phase. The operator's confusion is "is M8 running at all", not "which sub-phase". Per proposal's open question.
- Doesn't touch the M8 cascade logic or the canonical-Work minting code. Pure observability fix.
