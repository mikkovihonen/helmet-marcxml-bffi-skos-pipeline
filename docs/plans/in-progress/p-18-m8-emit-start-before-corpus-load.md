# P-18 — M8 emits its `start` event BEFORE the corpus load

**Status**: in-progress (started 2026-05-14).
**Source proposal**: `prop-18-m8-emit-start-before-corpus-load` (deleted on graduation; recover via `git show 9a0601d:docs/plans/proposed/prop-18-m8-emit-start-before-corpus-load.md`).
**Plan-base commit**: `18f53bf`. To gauge drift before re-executing or backporting, run
`git diff 18f53bf..HEAD -- src/bffi_pipeline/stages/merge.py`.
**Phase commits**:

- Phase A (lifecycle event reorder + phase_boundary + unit test): `5148746` (code, 2026-05-14). Bundled with P-19 Phase A — both touch M8's `apply_merge`.
- Phase B (operator-side dashboard smoke test): `<unfilled>` — pending next bench launch.

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
- [ ] **Phase B — Smoke test**: re-run a small audit (e.g. the cataloguer 19); confirm the dashboard's M8 tile transitions `pending` → `running` immediately, not after the corpus load. Operator action.

## What shipped at 5148746

- Inserted `emit_if_active(stage="m8", event="start")` at the top of `apply_merge` (between `output_path.mkdir` and `_load_decisions`). No counters — `total` isn't known yet.
- Renamed the existing post-union-find emit from `event="start"` to `event="phase_boundary"` with `phase="emit"`, preserving the `counters={"total": len(groups)}` payload.
- Added the unit test that pins the three-event sequence (`start` → `phase_boundary` → `end`) and explicitly asserts the first M8 event carries no counters.

## Risks (residual)

- **R1 — downstream consumers of `stage_entities_total{stage="m8"}`** may briefly see the gauge as absent (before `phase_boundary`) where pre-fix they saw it set immediately. Mitigation per proposal: dashboard's row-2 bargauges already handle missing values via `noValue: "—"`.
- **R2 — ordering of provenance writes vs the start event**: no interaction. Event stream is independent of the provenance graph.

## What this plan does NOT do

- Doesn't emit a separate `phase_boundary` for the "load_corpus" phase. The operator's confusion is "is M8 running at all", not "which sub-phase". Per proposal's open question.
- Doesn't touch the M8 cascade logic or the canonical-Work minting code. Pure observability fix.
