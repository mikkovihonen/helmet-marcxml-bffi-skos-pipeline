# Pipeline observability — metric vocabulary and dashboard

P-11 ships a local observability stack so an operator running an
unattended overnight batch can answer "what's happening right now?"
from a single source. This document is the reference for the metric
vocabulary the pipeline exposes and the panels the bundled Grafana
dashboard renders.

See also:

- [`docs/plans/completed/p-11-structured-observability.md`](plans/completed/p-11-structured-observability.md)
  — the execution plan (carries the design rationale at the top, the
  source proposal was inlined when the plan graduated).
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

### Exporter lifecycle

Five phases, all in `src/bffi_pipeline/metrics_exporter.py` +
`src/bffi_pipeline/runs_reset.py`:

| Phase | Trigger | What happens |
|---|---|---|
| **Launch** | `bffi-pipeline serve-metrics` | Rehydrate every `--sidecar` JSONL into the in-memory registry; bind `:9100`; write `<BFFI_RUNS_ROOT>/.exporter.pid` + `.exporter.argv`; register the atexit cleanup hook; enter the tail loop. |
| **Steady state** | tail loop, `poll_seconds=1.0` | Per attached sidecar: `_tail_step()` reads new lines via inode + byte offset (no double-counting); each sidecar's co-located `_errors.jsonl` + `_validation.jsonl` get the same treatment. Every `glob_rescan_seconds=30s` the `--watch-glob` patterns re-list and auto-attach new matches. |
| **Clean shutdown** | SIGTERM / SIGINT / interpreter exit | atexit hook removes `.exporter.pid` + `.exporter.argv` (best-effort; OSError silently swallowed if the operator already cleaned them). |
| **Operator reset** | `bffi-pipeline runs prune --apply --reset-exporter` | Reads PID file, SIGTERMs the process, waits up to 10 s for clean exit, then (default) `subprocess.Popen(argv, start_new_session=True)` with the recorded argv. `--no-relaunch-exporter` skips the relaunch and logs a warning. |
| **Crash recovery** | Next `--reset-exporter` after SIGKILL / OOM / container halt | `_process_alive(pid)` returns False → unlink the stale PID file, log warning, skip. No manual `rm` ever needed. |

PID + argv file locations are **process-global**, not per-run:

```
<BFFI_RUNS_ROOT>/.exporter.pid       — one line: os.getpid()
<BFFI_RUNS_ROOT>/.exporter.argv      — one line per argv token
```

Both are written at launch and removed on graceful exit. They sit
at the runs-root because the exporter is multi-tenant over its
sidecars (see § Operating modes); per-run-uuid PID files don't
make sense.

The reset path exists because pruning a run from disk leaves the
live exporter's in-memory registry holding stale `{run_uuid="..."}`
counters that no on-disk sidecar can refute. Restarting the
exporter forces it to rehydrate from the (now-pruned) sidecars,
which omits the deleted runs cleanly. P-32 Phase G ships this as
plumbing; pruning a run without `--reset-exporter` leaves the
stale series visible until Prometheus's 15-day retention ages
them out.

### Operating modes: ambient observer vs per-run focused

The exporter is run-agnostic by design. The `serve()` signature
takes a *list* of sidecars (plus optional watch-globs), and the
in-memory registry emits `run_uuid`-labeled metrics from whatever
events each attached sidecar carries. Two natural launch shapes:

**Mode A — ambient observer (recommended; matches P-32 Phase G intent):**

```bash
uv run bffi-pipeline serve-metrics \
    --port 9100 \
    --watch-glob 'runs/*/stage-events.jsonl'
```

One long-lived exporter. Initial glob walk attaches every existing
sidecar; the 30 s rescan picks up new runs as they spawn. A fresh
`bffi-pipeline marc-to-bf` invocation that creates
`runs/<new-uuid>/stage-events.jsonl` shows up in the dashboard's
`active_run` dropdown within ~30 s — no exporter restart needed.

**Mode B — per-run focused bench:**

```bash
uv run bffi-pipeline serve-metrics \
    --port 9100 \
    --sidecar runs/<specific-uuid>/stage-events.jsonl
```

Single-tenant. Useful for an isolated A/B test where you want zero
noise from other runs. The dashboard dropdown only shows one
option. Operator kills the exporter when done; Prometheus retains
the data for its 15-day window.

**Run vs exporter lifecycle independence:**

| Question | Answer |
|---|---|
| Can the exporter start before any run exists? | Yes (mode A — `--watch-glob` attaches sidecars as they appear) |
| Can a run end while the exporter keeps running? | Yes — sidecar stops growing; last counter values stay served from the registry until restart |
| Can the exporter be "switched" to a new run? | No switch needed — it just attaches the new sidecar alongside the old ones |
| Can the exporter forget a pruned run without a restart? | No — its in-memory registry holds stale `{run_uuid="..."}` series the sidecar can't refute. Forces the `--reset-exporter` path |
| Does Prometheus see one run or many? | Many. Each scrape returns every attached sidecar's counters labeled by `run_uuid`. Grafana's dropdown filters the view |

Mode A is the durable shape. Mode B is a temporary specialization.
Either way, the exporter never owns the run — it's a passive tail
of the run's on-disk JSONL output.

### Inspecting exporter state

```bash
# Is anything live?
ps aux | grep "[s]erve-metrics"

# What's the canonical PID file say? (substitute your BFFI_RUNS_ROOT)
cat runs/.exporter.pid 2>/dev/null || echo "no exporter running"

# What was it launched with?
cat runs/.exporter.argv 2>/dev/null
```

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
populated by `label_values(bffi_stage_started_timestamp, run_uuid)`
— i.e. the set of every run_uuid the exporter currently knows
about, sorted alphabetically.

The dropdown holds every value the attached sidecars have ever
emitted (subject to Prometheus's 15-day retention). The operator
picks the run they want to watch; panels re-render against that
single run_uuid. Prior runs' data remains queryable in Prometheus
under their own run_uuid values for forensic comparison.

To narrow the dropdown to *current* runs, either run the exporter
in mode B (single `--sidecar`), prune old runs and SIGTERM-restart
the exporter (`runs prune --apply --reset-exporter`; see § Exporter
lifecycle), or both.

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
