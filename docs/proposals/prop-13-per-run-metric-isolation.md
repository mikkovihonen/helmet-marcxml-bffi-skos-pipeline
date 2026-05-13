# P-13 — Per-run metric isolation

**Status**: proposed.
**Scope**: ~1 day.
**Proposal-base commit**: `8e05225`.

## Motivation

P-12 Phase E added a top-of-dashboard pipeline overview row filtered
by `run_uuid="$active_run"` so the eight stage tiles only reflect
the *currently active* invocation. P-12 Phase C added a freshness
filter to the Dependency health panel so stale probes drop out.
Everything *else* on the dashboard still aggregates across every
run that has ever fed the exporter:

| Metric | Series shape | Failure mode |
|---|---|---|
| `bffi_stage_outcomes_total{stage,outcome}` | counter, no `run_uuid` | "M9 outcome distribution" bar gauge stacks today's tier counts on top of every prior 5 k bench since the exporter started. |
| `bffi_stage_entities_total{stage,phase}` | gauge, no `run_uuid` | "M9 Phase 1 progress" denominator can show a prior bench's total until the new run emits its first `phase_boundary`. |
| `bffi_stage_entities_processed_total{stage,phase}` | counter, no `run_uuid` | "M9 Phase 1 progress" numerator climbs above the current-run count. |
| `bffi_stage_throughput_per_minute{stage,phase}` | gauge, no `run_uuid` | "M9 Phase 1 throughput" shows last run's value at every run boundary. |
| `bffi_stage_eta_seconds{stage,phase}` | gauge, no `run_uuid` | Same as above for the ETA tile. |
| `bffi_dependency_probe_latency_ms{stage,dep}` | gauge, no `run_uuid` | Latency tile shows whatever value the last probe set, even if the dep hasn't been probed in this run yet. |

Observed live during the P-10 Phase B + E bench (2026-05-13): after
restarting the exporter to pick up P-12 Phase A / C, the new top
row correctly reported the active warm run while the M9 outcome
distribution still summed the cold run's outcomes on top — both
contributing to the same uncoloured stack. Operators reading the
dashboard mid-warm-run couldn't tell where one run ended and the
next began.

## Approach

Add `run_uuid` as an explicit label on every counter + gauge in
`PipelineMetrics`, then update every dashboard query to filter by
`run_uuid="$active_run"` (the templating variable that P-12 Phase E
already provisioned).

### A. Exporter changes (`src/bffi_pipeline/metrics_exporter.py`)

Every `Gauge` and `Counter` in `PipelineMetrics.__post_init__`
gains `"run_uuid"` in its `labelnames` tuple. Every `.labels(...)`
call site in `apply_event` (and `_update_throughput`) passes
`run_uuid=row.run_uuid`.

Specifically:

```python
self.stage_entities_total = Gauge(
    "bffi_stage_entities_total",
    "Total entities the stage / phase is processing.",
-    labelnames=("stage", "phase"),
+    labelnames=("stage", "phase", "run_uuid"),
    registry=self.registry,
)
```

…repeated for the seven additional metrics in the table above.
`bffi_stage_started_timestamp` / `bffi_stage_ended_timestamp`
already carry `run_uuid`; this change brings the rest into line.

### B. Dashboard query updates (`config/grafana/dashboards/bffi-pipeline.json`)

Every panel that consumes one of the relabelled metrics gets a
`run_uuid="$active_run"` clause added to its PromQL. Concretely:

- Panels 1-3 (M9 Phase 1 progress / ETA / throughput) — three
  metrics each.
- Panel 4 (M9 outcome distribution) — bar gauge.
- Panel 6 (Per-stage throughput) — time series, all stages.
- Panel 8 (Dependency probe latency) — stat.
- Watchdog event rate (panel 7) — already uses `rate(...[5m])`,
  but adding `run_uuid="$active_run"` to the rate input scopes
  the window correctly.

The Phase C freshness filter on Dependency health (panel 5) stays;
adding `run_uuid` on top would over-filter (the panel intentionally
shows every stage's probe verdict, not just M9's, and probes are
emitted per stage entry across multiple runs in one invocation
chain — `run-full-pipeline.sh` runs M2 → M3 → ... → load all with
the same `run_uuid`).

The eight overview tiles (panels 9-16, added in Phase E) already
filter by `$active_run`; they need no change.

### C. Cardinality discussion

Adding `run_uuid` as a label multiplies the series count by the
number of distinct `run_uuid` values the exporter has seen since
its last restart. Per `docs/observability.md` § Counter
inheritance, an exporter restart re-zeroes the registry, so the
cardinality is bounded by **runs per exporter uptime**:

- **Dev box**: typically <20 runs between restarts → ~20 × current
  cardinality. Today's ~100-series total grows to ~2 000. Well
  within Prometheus's comfort zone (10 k–100 k is normal).
- **Production overnight cadence**: 1 run per night, weekly
  restarts → ~7 × cardinality. Negligible.
- **Long-uptime hypothetical**: 1 run/hour × 24 × 30 = 720
  run_uuid values. ~72 k series. Still fine, but the runbook
  recommends restarting the exporter monthly anyway (the same
  reason it recommends `make clean-stage-events` periodically).

If the cardinality bound turns out to be tight on real production
load, a future follow-up can clean up series past N runs (Prometheus
admin API supports `delete_series` matching old `run_uuid`s; or
the exporter itself can drop labels from registries whose
timestamps are >M hours old). Out of scope here.

### D. Tests

- Unit: `apply_event` emits metrics labelled with the event's
  `run_uuid`. Two runs with the same `(stage, phase)` produce two
  distinct series.
- Unit: `PromQL filter sample` against `generate_latest` snapshot
  confirms `run_uuid="..."` appears on the four affected
  counter/gauge families (`bffi_stage_outcomes_total`,
  `bffi_stage_entities_total`, `bffi_stage_entities_processed_total`,
  `bffi_stage_throughput_per_minute`).
- Dashboard JSON schema: extend the existing
  `test_grafana_dashboard_*` tests to assert every PromQL target
  in panels {1, 2, 3, 4, 6, 7, 8} contains `run_uuid="$active_run"`.
- Backward-compat: rehydrating a sidecar written before this
  change still works (events without `run_uuid` field default to
  empty-string per the `StageEventRow` constructor; the empty
  string is a valid Prometheus label value, so old data just
  surfaces under `run_uuid=""`). Pinned by a regression test.

### E. Documentation

`docs/observability.md` gains:

- A short § "Per-run metric isolation" explaining that every
  metric is keyed by `run_uuid` and that dashboards filter via
  `$active_run`.
- An updated cardinality estimate (the ~100-series figure becomes
  ~100 × N runs).

## Out of scope

- Auto-pruning old `run_uuid` series. Today the operator restarts
  the exporter to reset; this proposal doesn't change that. If
  bounded cardinality is needed in production, file a separate
  proposal for a TTL-based pruner.
- Filtering Counter increments by run-finish time (e.g.
  "outcome counter only counts a run's outcomes once the run
  ends"). The proposed `run_uuid` label is sufficient — partial-
  run counters are useful mid-run for live progress dashboards.
- Auto-rotating the sidecar between runs. Operator-side cleanup
  remains a `make clean-stage-events`-style follow-up.

## Acceptance

- All `Counter` + `Gauge` declarations in
  `metrics_exporter.PipelineMetrics` include `run_uuid` in their
  `labelnames`.
- `apply_event` threads `row.run_uuid` to every `.labels(...)` site.
- Dashboard JSON: every PromQL target on panels {1, 2, 3, 4, 6, 7,
  8} carries `run_uuid="$active_run"`.
- Unit tests pin the new label + the dashboard schema.
- Visual check on the next bench: the M9 outcome distribution bar
  gauge shows *only* the current run's tier counts; the M9
  throughput stat tile is empty until the current run emits its
  first progress event (no leak from prior runs).
- No regression in the existing P-11 / P-12 tests.
- `docs/observability.md` per-run isolation section added.

## Risk

| Risk | Likelihood | Mitigation |
|---|---|---|
| Cardinality blowup on long-uptime exporter | Low | Bounded by runs-per-restart; current dev / production cadences keep this well within Prometheus's comfort zone. Documented in observability.md. |
| Dashboard breaks for operators on old `bffi-pipeline.json` (pre-P-13) | Low | Dashboard is bind-mounted, auto-provisioned. Restart Grafana / `podman compose restart grafana` after upgrade. Documented. |
| Backward-compat regression on rehydrate of pre-P-13 sidecars | Low | Empty-string default for `run_uuid` keeps old events queryable as a synthetic "legacy" run. Regression test pins it. |
| Per-stage-throughput panel becomes empty when *no* run is active | Low | The panel was already sparse between runs; the explicit filter makes the empty state intentional rather than accidental. Time range default `now-30m` already covers this. |

## Cross-references

- [`docs/plans/completed/p-12-observability-cleanup.md`](../plans/completed/p-12-observability-cleanup.md) — the predecessor that introduced the `$active_run` variable but only applied it to the overview row.
- [`docs/plans/completed/p-11-structured-observability.md`](../plans/completed/p-11-structured-observability.md) — the parent plan defining the metric vocabulary.
- [`src/bffi_pipeline/metrics_exporter.py`](../../src/bffi_pipeline/metrics_exporter.py) — `PipelineMetrics` declaration site (Sec. A).
- [`config/grafana/dashboards/bffi-pipeline.json`](../../config/grafana/dashboards/bffi-pipeline.json) — dashboard JSON to update (Sec. B).
- [`docs/observability.md`](../../observability.md) — documentation to extend (Sec. E).
- 2026-05-13 live-bench session that triggered this proposal — the operator pain that motivated this cleanup.
