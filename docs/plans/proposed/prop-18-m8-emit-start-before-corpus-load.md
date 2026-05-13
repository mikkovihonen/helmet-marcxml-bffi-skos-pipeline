# P-18 — M8 emits its ``start`` event BEFORE the corpus load, so the dashboard shows it running during the 8-min loading phase

**Status**: proposed.
**Scope**: ~5 lines + one unit test. ~30 min including tests + commit.
**Proposal-base commit**: `99d8152`. To gauge drift before acting, run
`git diff 99d8152..HEAD -- src/bffi_pipeline/stages/merge.py`.

## Motivation

The 2026-05-13 20 k-record overnight run surfaced an operator-confusion bug: the dashboard shows the M8 state tile as ``pending`` for ~8 minutes after M6 ends, even though the M8 Python process is actively doing work the whole time. Concrete trace from run ``c64d1207b7c6443dbcd6e1cbd5d6da15``:

| Event | UTC time | Δ |
|---|---|---|
| M6 ``end`` | 19:34:26 | — |
| ``bffi-pipeline merge`` Python boot | ~19:34:30 | +4 s shell handoff |
| M8 ``start`` event | **19:42:11** | **+7.7 min** |
| M8 first ``progress`` event | 19:42:12 | +1 s after start |

Between Python boot and the ``start`` event the merge stage is running ``_load_work_records_from_corpus`` — reading and rdflib-parsing 19 570 BFFI Turtle files into an in-memory ``dict[str, WorkRecord]``. By the time it emits ``start`` it has already loaded the BFFI corpus (~8 GiB RSS for the 20 k-record sample), loaded ``judge-decisions.jsonl``, built the union-find, and detected conflicts. That's the heaviest part of M8's work — but the dashboard reports the stage as not-yet-running.

The mid-run consequence is mild (dashboard mis-states reality for ~8 min on a 20 k sample, would be ~hours on the full 800 k corpus), but the false-pending state has bitten the operator on this overnight run and would do so on every M8 invocation at scale.

## Approach

Today the M8 ``start`` event lands at ``merge.py:1099`` (post-P-15 line numbering), AFTER ``groups`` is computed, so the event can carry ``counters={"total": len(groups)}``. Move the ``start`` emission to the very top of ``run()`` (no counters), and add a ``phase_boundary`` event with the total once it's known.

Concrete change in ``src/bffi_pipeline/stages/merge.py``:

```python
def run(...) -> MergeResult:
    settings = get_settings()
    # ...path resolution...

    # P-18: emit ``start`` immediately so the dashboard shows M8 as
    # running during the BFFI-corpus load (8 min on 20k; hours at
    # full-corpus scale). The total is unknown at this point — we
    # don't know how many canonical groups there'll be until
    # union-find runs. Total lands in the phase_boundary event below.
    emit_if_active(stage="m8", event="start")

    decisions = _load_decisions(decisions_path)
    # ... existing load + union-find ...

    groups = uf.groups()
    emit_if_active(
        stage="m8",
        event="phase_boundary",
        phase="emit",
        counters={"total": len(groups)},
    )

    # ... existing minting loop ...
```

The dashboard's existing 4-state logic (skipped/pending/running/done) renders M8 as ``running`` from the moment ``stage="m8" event="start"`` lands, regardless of ``counters``. The ``entities_total`` gauge stays absent until the ``phase_boundary``, at which point the row-2 bargauge title (``${m8_total} works``) populates with the real number.

This mirrors the M9 pattern: M9 emits ``start`` early, then ``phase_boundary`` events as it transitions between Phase 1 (tier-0 + candidate query), Phase 1.5 (cache lookup), Phase 2 (picker), and Phase 3 (graph mutation).

## Prerequisites

- The dashboard's M8 tile already uses the 4-state PromQL — no panel change needed.
- The exporter already handles ``start`` events with empty ``counters`` (the dispatch table in ``apply_event`` only reads ``counters.get("total", 0)`` defensively).
- The eight-stage plan event the runner script emits at pipeline start already declares M8, so the ``pending`` → ``running`` transition is what we're fixing here.

## Risks

- **R1 — downstream consumers of ``stage_entities_total{stage="m8"}``** may briefly see the gauge as absent (before ``phase_boundary``) where today they see it set immediately. Mitigation: the only consumers we ship are the dashboard's row-2 bargauges, which already handle missing values via ``noValue: "—"``.
- **R2 — ordering of provenance writes vs the start event.** ``run()`` opens the provenance writer near the top; emitting ``start`` before that opens is fine because the event stream is independent of the provenance graph. No interaction.

## Open questions

- Should we ALSO emit a ``phase_boundary`` for the "load_corpus" phase, separate from the "emit" phase, so the dashboard can show "M8 loading" vs "M8 emitting"? Probably no — the operator's confusion is "is M8 running at all", not "which sub-phase". Keep the proposal small.

## Acceptance criteria

- [ ] ``merge.run()`` emits ``stage="m8" event="start"`` at the top of the function, before ``_load_decisions`` is called.
- [ ] A new ``phase_boundary`` event with ``phase="emit"`` carries the ``len(groups)`` total once union-find completes.
- [ ] Unit test: stub the emitter, call ``merge.run()`` on a small fixture; assert the event sequence is ``start (no counters)`` → ``phase_boundary (total=N)`` → ``progress*`` → ``end``.
- [ ] Smoke test: re-run a small audit (e.g. the cataloguer 19); confirm the dashboard's M8 tile transitions ``pending`` → ``running`` immediately, not after the corpus load.
- [ ] ``make lint && make test`` green.
