# P-13 — Per-run metric isolation

**Status**: completed.
**Source proposal**: `prop-13-per-run-metric-isolation` (deleted on 2026-05-13 plans/proposed reorganisation; recover via `git show f2d8486 -- <orig-path>`)
at commit `40abb2d`.
**Plan-base commit**: `40abb2d`. To gauge drift before executing,
run
`git diff 40abb2d..HEAD --
src/bffi_pipeline/metrics_exporter.py
config/grafana/dashboards/bffi-pipeline.json
docs/observability.md`.
**Phase commits**:

- Phase A (exporter: add `run_uuid` label to every Counter + Gauge): `b95597e` (2026-05-13). Every metric in `PipelineMetrics` now carries `run_uuid`; `apply_event` + `_update_throughput` thread it to every `.labels(...)` site; rolling-window throughput history keyed by `(stage, phase, run_uuid)`. Three new regression tests cover cross-run isolation, legacy empty-string `run_uuid` fallback, and per-run throughput-history separation. Existing test assertions adjusted for the new alphabetical label ordering. 935 total green; intentionally a no-op on dashboard panels until Phase B adds the filter.
- Phase B (dashboard JSON: filter every panel by `$active_run` + docs): `a4dfb23` (2026-05-13). Every PromQL target on panels 1-8 carries `run_uuid="$active_run"`; the freshness clause on panel 5 wraps both selectors. New schema test pins the contract as a forward-incompat trap. `docs/observability.md` § Per-run metric isolation explains the contract; metric vocabulary table updated with the `run_uuid` label on every metric; cardinality estimate restated. Exporter live-restarted to publish run-scoped labels. 936 total green.

**Owner**: TBD.
**Estimated wall-time**: ~1 day. Phase A is the bulk (~half a day:
relabel every metric in `PipelineMetrics`, thread `row.run_uuid`
through `apply_event` + `_update_throughput`, write unit tests).
Phase B is dashboard JSON edits + the per-panel test extension +
the doc note (~2-3 hours).

## Goal

Make every panel on the P-11 Phase D dashboard reflect the **active
run only**, completing what P-12 Phase E started for the top overview
row. After P-13: starting a new pipeline run leaves every prior
run's metric data on disk + queryable via PromQL `run_uuid=` label
but does not pollute the live dashboard view.

## Definition of done

- All `Counter` + `Gauge` declarations in
  `PipelineMetrics.__post_init__` carry `run_uuid` in their
  `labelnames` tuple (consistent with `stage_started_timestamp` /
  `stage_ended_timestamp`, which already have it).
- `apply_event` and `_update_throughput` thread `row.run_uuid` to
  every `.labels(...)` site. Old sidecars without `run_uuid` field
  default to empty string and stay queryable as a synthetic legacy
  series.
- Every dashboard panel that consumes a relabelled metric carries
  `run_uuid="$active_run"` in its PromQL target. The eight overview
  tiles (Phase E) already do; this plan extends the contract to
  the rest of the panel set.
- Visual check on the next bench: the M9 outcome distribution bar
  gauge shows only the current run's tier counts; the M9 throughput
  stat tile is empty until the current run emits its first progress
  event (no leak from prior runs).
- `docs/observability.md` gains a § "Per-run metric isolation"
  noting the new label convention + the updated cardinality
  estimate.
- All P-11 + P-12 tests stay green; new unit tests pin (1) the
  exporter's per-run labelling and (2) the dashboard JSON's
  `run_uuid="$active_run"` filter on every relevant panel.
- `docs/plans/backlog/p-13-per-run-metric-isolation.md` has been
  `git mv`'d through `in-progress/` → `completed/` per the
  lifecycle convention.

## Current state (as of plan-base `40abb2d`)

- Eight `Counter` + `Gauge` metrics in `PipelineMetrics` lack a
  `run_uuid` label — see `src/bffi_pipeline/metrics_exporter.py`
  lines 100-150. Two already have it (`stage_started_timestamp`,
  `stage_ended_timestamp`), which is the precedent the rest will
  follow.
- Dashboard panels 1-4, 6-8 in `config/grafana/dashboards/bffi-pipeline.json`
  consume the un-filtered metrics. Panels 5 (Dependency health) and
  the eight overview tiles (Phase E) are intentionally exempt from
  the rewrite — see plan § A.2 below.
- The `$active_run` Grafana templating variable already exists
  (P-12 Phase E) and is consumed by the overview row. P-13 reuses
  it; no new variables are needed.

## Phase ordering rationale

Phase A ships first because it's a no-op on the existing dashboard:
adding a label to a metric doesn't break existing queries that
don't filter on it (PromQL aggregates by default). Old panels
continue to show the cumulative-across-runs view until Phase B
updates their queries. Splitting the work this way means each phase
is independently shippable, independently revertible, and the
dashboard never enters a half-broken state.

---

## Phase A — Exporter labelling

Estimated wall-time: ~½ day.

### A.1. Metric declarations

`src/bffi_pipeline/metrics_exporter.py:PipelineMetrics.__post_init__`
gains `"run_uuid"` in the `labelnames` tuple of every metric that
doesn't already have it:

- `stage_entities_total` (currently `("stage", "phase")`)
- `stage_entities_processed_total` (currently `("stage", "phase")`)
- `stage_outcomes_total` (currently `("stage", "outcome")`)
- `stage_throughput_per_minute` (currently `("stage", "phase")`)
- `stage_eta_seconds` (currently `("stage", "phase")`)
- `dependency_health` (currently `("stage", "dep")`)
- `dependency_probe_latency_ms` (currently `("stage", "dep")`)
- `dependency_last_probe_timestamp` (currently `("stage", "dep")`)
- `watchdog_events_total` (currently `("stage", "event")`)

Append `"run_uuid"` as the last entry so existing
`.labels(stage=..., phase=...)` positional readability stays
intact.

### A.2. Why dependency_health stays in the rewrite

Dependency probes fire from multiple stages within a single
`run-full-pipeline.sh` invocation (M2 → M3 → ... → load), all
sharing the same `run_uuid`. Filtering by `run_uuid="$active_run"`
on panel 5 still shows the full cross-stage timeline for the active
invocation — the same desired behaviour. P-12 Phase C's freshness
filter complements this: stale probes from *prior* invocations
drop out via the >60s gate, and live probes are run-scoped via the
new label.

### A.3. `apply_event` thread-through

Every `.labels(...)` call site in `apply_event` and
`_update_throughput` adds `run_uuid=row.run_uuid`. The
`StageEventRow.run_uuid` field already exists (it's read from the
JSONL `run_uuid` key in `_tail_step`).

### A.4. Backward compatibility

Sidecars written before P-13 are missing the `run_uuid` field on
some old test fixture events, but every event written by P-11
Phase A onward carries it (verified in
`tests/unit/test_observability.py`). For absolute safety, the
default empty-string label value handles the legacy case
cleanly — Prometheus accepts empty strings, and old events surface
under `run_uuid=""` (visually distinguishable from real runs).

### A.5. Tests

- Unit: drive `apply_event` with two synthetic events sharing
  `(stage="m9", phase="phase1")` but different `run_uuid`;
  assert `bffi_stage_entities_total` produces two distinct series
  in the `generate_latest` output.
- Unit: drive `apply_event` with an event whose `run_uuid` is
  the empty string; assert the metric is still emitted (regression
  for the legacy-event case).
- Unit: every metric in the table above carries `run_uuid=` on
  its output line (PromQL-shape sanity check via `grep`).
- Cross-cutting: existing tests
  (`test_start_event_sets_started_timestamp`,
  `test_phase_boundary_sets_per_phase_total`,
  `test_progress_updates_processed_and_throughput`,
  `test_end_records_outcome_buckets`,
  `test_health_event_maps_status_to_gauge`, etc.) stay green —
  they construct rows via the `_row` factory which already supplies
  a `run_uuid` default.

### A.6. Acceptance

- [ ] Every Counter / Gauge in `PipelineMetrics.__post_init__`
  except `stage_started_timestamp` / `stage_ended_timestamp` gains
  `"run_uuid"` in its `labelnames` (those two already have it).
- [ ] Every `.labels(...)` call site in `apply_event` and
  `_update_throughput` passes `run_uuid=row.run_uuid`.
- [ ] Three new unit tests + every existing exporter test still
  passes.
- [ ] `make lint && make test` green.
- [ ] Dashboard remains functional (un-filtered queries still
  aggregate across run_uuids, identical to today's behaviour).

### A.7. Rollback

Single-commit revert. Removing the label re-collapses the per-run
series into one cross-run aggregate; the dashboard behaviour
matches its current state.

---

## Phase B — Dashboard JSON + docs

Estimated wall-time: ~2-3 hours.

### B.1. Per-panel filter updates

`config/grafana/dashboards/bffi-pipeline.json` — add
`run_uuid="$active_run"` to every PromQL target on the following
panels:

| Panel ID | Panel title | Target metric(s) |
|---|---|---|
| 1 | M9 reconcile — Phase 1 progress | `bffi_stage_entities_processed_total`, `bffi_stage_entities_total` |
| 2 | M9 Phase 1 ETA (seconds) | `bffi_stage_eta_seconds` |
| 3 | M9 Phase 1 throughput (per minute) | `bffi_stage_throughput_per_minute` |
| 4 | M9 outcome distribution (per tier) | `bffi_stage_outcomes_total` |
| 6 | Per-stage throughput | `bffi_stage_throughput_per_minute` |
| 7 | Watchdog event rate (5m) | `rate(bffi_watchdog_events_total{run_uuid="$active_run"}[5m])` |
| 8 | Dependency probe latency (ms) | `bffi_dependency_probe_latency_ms` |

Panel 5 (Dependency health) gets `run_uuid="$active_run"` added
inside the existing freshness clause so the verdict is run-scoped
without losing the >60s grey-out:

```promql
bffi_dependency_health{run_uuid="$active_run"}
  and on(stage, dep, run_uuid)
    (time() - bffi_dependency_last_probe_timestamp{run_uuid="$active_run"} < 60)
```

The eight overview tiles (panels 9-16) already filter by
`$active_run` — no change needed.

### B.2. Tests

Extend `test_grafana_dashboard_has_active_run_templating_variable`
+ add a new test that walks every panel's PromQL targets and
asserts `run_uuid="$active_run"` appears (except where the panel
intentionally lacks a `run_uuid` filter — none after P-13). The
test pins forward-incompatibility: adding a new panel without the
filter trips the assertion.

### B.3. Docs

`docs/observability.md` gains a § "Per-run metric isolation":

```markdown
### Per-run metric isolation

Every Counter and Gauge carries an explicit `run_uuid` label.
Dashboards filter every panel by `run_uuid="$active_run"`, where
`$active_run` is a Grafana templating variable derived from
`topk(1, bffi_stage_started_timestamp) by (run_uuid)` — i.e.
"the run whose start event is the most recent".

Effect: starting a new pipeline run replaces the dashboard
view immediately. Prior runs' data remains queryable via PromQL
(`run_uuid="<earlier-id>"`) for forensic comparison but does
not visually compete with the live view.

Cardinality: ~100 series per run. An exporter restart resets
the registry; runs accumulated in a single exporter session
multiply this baseline. For dev (~20 runs / restart) → ~2000
series, well within Prometheus's comfort zone. Production
overnight cadence (1 run / day, weekly restart) → ~700 series.
```

Update the cardinality estimate at the bottom of the existing
"Metric vocabulary" section to point at the new section.

### B.4. Acceptance

- [ ] Every PromQL target in panels 1-8 carries
  `run_uuid="$active_run"` (panel 5 inside the freshness clause).
- [ ] Dashboard JSON schema test extended; passes.
- [ ] Visual check on the next bench: starting a new run instantly
  flips the entire dashboard view to that run; prior-run data
  stays queryable via Grafana's Explore tab + manual
  `run_uuid=` filter.
- [ ] `docs/observability.md` updated.
- [ ] `make lint && make test` green.

### B.5. Rollback

`git revert` the Phase B commit. Dashboard reverts to its
post-Phase-A state (un-filtered, cross-run aggregate). The Phase
A label remains in place (it's purely additive), so re-applying
Phase B later is mechanical.

---

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Phase A cardinality blowup on long-uptime exporter | Low | Bounded by runs-per-restart; dev / production cadences keep this within Prometheus's comfort zone. Documented in observability.md. |
| Phase A breaks any test fixture that re-uses run_uuid="" | Low | Empty-string label is a valid Prometheus label value; regression test pins the legacy case. |
| Phase B per-panel test fragile to dashboard layout edits | Low | The test asserts content (PromQL string contains `run_uuid="$active_run"`), not position; layout edits don't trip it. |
| Operator on stale dashboard JSON (pre-P-13 cached) | Low | Dashboard auto-provisions; `podman compose restart grafana` after upgrade documented. |
| Per-panel emptiness when no run is active | Low | The "no-active-run" state is rare (start of an empty dashboard). Time-range default `now-30m` and Phase E's overview row showing "idle" tiles cover the operator UX. |

## Cross-references

- [`docs/plans/completed/p-11-structured-observability.md`](../completed/p-11-structured-observability.md) — defines the metric vocabulary.
- [`docs/plans/completed/p-12-observability-cleanup.md`](../completed/p-12-observability-cleanup.md) — added `$active_run` variable used here.
- [`src/bffi_pipeline/metrics_exporter.py`](../../../src/bffi_pipeline/metrics_exporter.py) — Phase A target.
- [`config/grafana/dashboards/bffi-pipeline.json`](../../../config/grafana/dashboards/bffi-pipeline.json) — Phase B target.
- [`docs/observability.md`](../../observability.md) — Phase B docs.
- 2026-05-13 live-bench session that surfaced the cross-run pollution — the trigger for the proposal.
