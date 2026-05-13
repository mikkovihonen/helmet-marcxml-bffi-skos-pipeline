# Pipeline observability — metric vocabulary and dashboard

P-11 ships a local observability stack so an operator running an
unattended overnight batch can answer "what's happening right now?"
from a single source. This document is the reference for the metric
vocabulary the pipeline exposes and the panels the bundled Grafana
dashboard renders.

See also:

- [`docs/proposals/prop-11-structured-observability.md`](proposals/prop-11-structured-observability.md)
  — the design rationale.
- [`docs/plans/in-progress/p-11-structured-observability.md`](plans/in-progress/p-11-structured-observability.md)
  — the execution plan.
- [`docs/runbook.md`](runbook.md) § "Local observability stack" — the
  operator workflow.

## Architecture

```
pipeline stages ─emit→ stage-events.jsonl ─tail→ serve-metrics ─scrape→ Prometheus ─query→ Grafana
   (Phase A)          (sidecar)          (Phase D.1, port 9100) (port 9091)  (port 3001)
```

All local; no outbound telemetry. The exporter runs on the host
alongside the pipeline (it shares the `BFFI_DATA_DIR` mount without
volume mapping); Prometheus and Grafana run as Docker Compose
services under the `observability` profile.

### Counter inheritance across exporter restarts

Counters (`bffi_*_total`) are cumulative within a single exporter
process. Each `bffi-pipeline serve-metrics` invocation starts from
zero, rehydrates the full sidecar once, then tails for new events.
Restarting the exporter resets every counter to "sum of sidecar
events on disk at startup" — the displayed numbers can drop
visibly on the dashboard at the restart boundary.

This is by design: rehydration replays the JSONL ground truth, so
counters always reflect the actual on-disk event count regardless
of whether the exporter was bounced. Operators sweeping the data
dir between benches (e.g. `rm data/stage-events.jsonl` for a clean
slate) and restarting the exporter get a clean zero baseline;
operators wanting historical continuity keep the sidecar in place.

PromQL queries spanning an exporter restart should account for the
discontinuity (use `increase(...[5m])` or `rate(...[5m])` rather
than raw counter values across the boundary). The Grafana
dashboard does this for `Watchdog event rate (5m)` already; the
other counter-based panels show cumulative-by-design.

### Per-run metric isolation

Every Counter and Gauge carries an explicit `run_uuid` label (P-13
Phase A). Dashboards filter every panel by `run_uuid="$active_run"`
(P-13 Phase B), where `$active_run` is a Grafana templating variable
derived from `topk(1, bffi_stage_started_timestamp) by (run_uuid)`
— i.e. "the run whose start event is the most recent".

Effect: starting a new pipeline run instantly flips the dashboard
view to that run. Prior runs' data remains queryable in Prometheus
(filter by an explicit `run_uuid=` value) for forensic comparison
but does not visually compete with the live view.

Cardinality estimate at the bottom of the next section accounts for
this label.

## Metric vocabulary

The exporter publishes the canonical metric set below. Every metric
maps one-to-one to a Phase A `stage-events.jsonl` event field.

| Metric | Type | Labels | Source event | What it means |
|---|---|---|---|---|
| `bffi_stage_started_timestamp` | Gauge | `stage`, `run_uuid` | `start` | Unix ts the stage began. |
| `bffi_stage_ended_timestamp` | Gauge | `stage`, `run_uuid` | `end` | Unix ts the stage finished. |
| `bffi_stage_entities_total` | Gauge | `stage`, `phase`, `run_uuid` | `start` / `phase_boundary` | Total entities the stage/phase will process. |
| `bffi_stage_entities_processed_total` | Counter | `stage`, `phase`, `run_uuid` | `progress` | Cumulative entities processed so far. |
| `bffi_stage_outcomes_total` | Counter | `stage`, `outcome`, `run_uuid` | `end` | Per-outcome bucket counts (M9 tier counts: `local`, `lexical`, `llm_pick`, `fallback`, `no_candidate`, `fictional`, `watchdog_aborted`). |
| `bffi_stage_throughput_per_minute` | Gauge | `stage`, `phase`, `run_uuid` | derived | Rolling-window throughput from the last 5 progress events. |
| `bffi_stage_eta_seconds` | Gauge | `stage`, `phase`, `run_uuid` | derived | Linear-extrapolation ETA to phase boundary or stage end. |
| `bffi_dependency_health` | Gauge | `stage`, `dep`, `run_uuid` | `health` | `2`=up (green), `1`=degraded (amber), `0`=down (red), `NaN`=not_configured (grey). |
| `bffi_dependency_probe_latency_ms` | Gauge | `stage`, `dep`, `run_uuid` | `health` | Most recent probe round-trip latency in ms. |
| `bffi_dependency_last_probe_timestamp` | Gauge | `stage`, `dep`, `run_uuid` | `health` | Unix ts of the most recent probe (drives the dashboard's freshness overlay; P-12 Phase C). |
| `bffi_watchdog_events_total` | Counter | `stage`, `event`, `run_uuid` | `watchdog` | Cumulative watchdog events (`timeout`, `retry`, `give_up`, `field_budget_exceeded`, `pair_budget_exceeded`). |

### Label cardinality

- `stage` ∈ {`m2`, `m3`, `m5`, `m6`, `m8`, `m9`, `skosify`, `load`, `watchdog`}.
- `phase` ∈ {`_`, `phase1`, `phase2`, `phase3`}. `_` is the sentinel
  for stages without internal phases (M2/M3/M6/M8/skosify/load).
- `dep` ∈ {`fuseki`, `mlx-lm`, `mlx-lm-primary`, `mlx-lm-fallback`,
  `finto`}.
- `outcome` is per-stage but bounded; M9's seven values are the
  canonical set today.
- `run_uuid` is one value per pipeline invocation; old runs prune
  naturally as Prometheus retention rolls forward.

Cardinality cap (per `run_uuid`): ~6 stages × ~4 phases + ~5 deps
+ ~7 outcomes + ~5 watchdog event types ≈ 100 series. Multiplied by
runs accumulated in the current exporter session (an exporter
restart resets to zero — see § Counter inheritance above):

- **Dev box**: <20 runs / restart → ~2 000 series. Well within
  Prometheus's comfort zone.
- **Production**: 1 run / night, weekly restart → ~700 series.
- **Long-uptime hypothetical**: 1 run / hour for a month → ~72 000
  series. Still fine but the runbook recommends monthly exporter
  restarts.

## Bundled Grafana dashboard

Auto-loaded from `config/grafana/dashboards/bffi-pipeline.json` at
container start (via `config/grafana/provisioning/`). Read-only in
the UI; operators clone-and-edit if they want a custom view.

### Panel set (current)

| Panel | Type | What it shows |
|---|---|---|
| Pipeline overview (top row, 8 tiles) | Stat × 8 | One tile per stage (M2 / M3 / M5 / M6 / M8 / M9 / Skosify / Load). Coloured green if running, blue if done, grey if idle. Filtered to the active run via `$active_run`. Added in P-12 Phase E. |
| M9 reconcile — Phase 1 progress | Stat | Processed / total for M9 Phase 1 (the bench-relevant bottleneck). |
| M9 Phase 1 ETA | Stat | Linear-extrapolation ETA. |
| M9 Phase 1 throughput | Stat | Entities per minute over the last 5 progress events. |
| M9 outcome distribution | Bar gauge | Per-tier counts after M9 ends (`local`, `lexical`, `llm`, `fallback`, `no_candidate`, …). |
| Dependency health | State timeline | Fuseki / mlx-lm / Finto verdict over time. P-12 Phase C: series whose last probe is >60 s old drop to a grey gap rather than extending stale state. P-12 Phase B: `not_configured` deps render as NaN (grey) rather than red. |
| Per-stage throughput | Time series | All stages, all phases — overlay view of who's currently moving. |
| Watchdog event rate (5m) | Time series | Per-event-type rate; spikes here precede stuck runs. |
| Dependency probe latency | Stat | Most recent probe latency per dep. |

The dashboard schema is `v39` (Grafana 11.x). Image versions are
pinned in `docker-compose.yml`; on Grafana major-version bumps the
dashboard JSON occasionally needs a schema upgrade.

## Extending

Adding a new metric is two edits:

1. **Stage side**: extend the relevant event's `counters` /
   `extra` payload (Phase A `emit_if_active` call).
2. **Exporter side**: declare the metric in
   `src/bffi_pipeline/metrics_exporter.py`'s `PipelineMetrics`
   dataclass and route the event's payload to it inside
   `apply_event`.
3. **Dashboard** (optional): add a panel to the JSON.

The metric vocabulary table above is the spec the three sides must
agree on. Bump the table first; the code follows.
