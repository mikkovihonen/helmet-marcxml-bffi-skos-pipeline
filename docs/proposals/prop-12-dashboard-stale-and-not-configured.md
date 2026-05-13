# P-12 — Dashboard freshness + `not_configured` health status

**Status**: proposed.
**Scope**: half-day to 1 day.
**Proposal-base commit**: `cbaa7b2`.

## Motivation

The P-11 Phase D Grafana dashboard mis-reports two unrelated probes
as "down" during the P-10 Phase B + E bench (2026-05-13 ~07:43 UTC):

1. `bffi_dependency_health{dep="mlx-lm-primary",stage="m6"} = 0.0`
2. `bffi_dependency_health{dep="mlx-lm-fallback",stage="m6"} = 0.0`

Cross-checking the sidecar (`data/stage-events.jsonl`):

- The M6 health event that set those gauges fired at `07:17:54Z`,
  before mlx-lm was running. Both probes correctly reported
  `status="down"` *at that time*. M6 has not run since.
- Prometheus `Gauge` retains its last-set value indefinitely, so the
  M6 row sits at "down" 26+ minutes after mlx-lm came back up. The
  M9 row, which is currently emitting fresh probes during the bench,
  correctly reports `mlx-lm: up`.
- The fallback (`:8002`) is **never** started on the M2 Max dev box
  — per `CLAUDE.md` memory, the 72B cascade fallback does not fit
  on 64 GB unified memory. The probe is checking a port nobody asked
  for and the dashboard frames its "down" as a fault.

Both issues are real but produce the same operator confusion: the
dashboard shouts "DOWN" for things that are either historical
("M6 last looked 30 min ago") or by-design ("the fallback was never
configured on this host").

## Approach

Two small, independent changes:

### 1. New health status `not_configured` (gauge value `NaN`)

Extend the `ProbeResult.status` literal in
`src/bffi_pipeline/stages/probes.py` from
`{up, degraded, down}` to
`{up, degraded, down, not_configured}`. Apply it in
`probe_mlx_lm` when:

- The fallback URL is empty / unset, OR
- The fallback URL equals the primary URL (degenerate cascade
  config — same process probed twice).

The metrics exporter maps `not_configured` to `NaN` rather than `0`
(the down sentinel). Grafana's default value-mapping then greys out
the cell instead of colouring it red, so an operator can see
"fallback is intentionally not in play" without reading the sidecar.
No-op for M9 (which only probes the primary today) and for fuseki /
finto (always configured).

### 2. Dashboard freshness overlay

Add a derived metric `bffi_dependency_health_age_seconds` =
`time() - bffi_dependency_last_probe_timestamp{...}` (the timestamp
gauge already exists implicitly via `bffi_stage_started_timestamp`;
a per-dep `bffi_dependency_last_probe_timestamp` is the missing
piece — one extra Prometheus `Gauge` in `metrics_exporter.py`).

Grafana value-mapping on the dashboard panel:

| Condition | Cell display |
|---|---|
| age < 60 s, status=up | green "up" |
| age < 60 s, status=degraded | yellow "degraded" |
| age < 60 s, status=down | red "DOWN" |
| age >= 60 s | grey "stale Ns ago" (no colour) |
| status=not_configured | grey "—" (no colour) |

The threshold is tunable; 60 s lines up with Prometheus's 5 s scrape
interval × the in-bench probe cadence (`_M9_HEALTH_PROBE_CADENCE`
currently fires per 1000 entities, roughly once per minute on the
M2 Max).

## Out of scope

- Changing the per-stage probe wiring so that *all* stages share one
  global "as of now" probe set. That's a bigger redesign — the
  per-stage probes are deliberate because stage-specific runtime
  flags (e.g. M9's `BFFI_M9_TIER0_EXPANSION`) gate which
  dependencies actually matter. The stale-detection overlay handles
  the immediate UX confusion without forcing that re-architecture.
- Re-running M6 in the bench just to re-set its gauges. The user
  shouldn't have to fake stage activity to get accurate dashboards.

## Acceptance

- `probe_mlx_lm` returns `status="not_configured"` when the URL is
  empty or equals the primary; unit-tested under fixtures.
- `metrics_exporter.apply_event` maps `not_configured` to `NaN` and
  emits a `bffi_dependency_last_probe_timestamp` gauge per
  `(stage, dep)`.
- Grafana dashboard JSON updated with the freshness value-mapping
  above; visual check during the next bench shows the M6 row greying
  out within 60 s of M6 ending while the M9 row stays live.
- No regression in the existing P-11 health-probe tests.

## Risk

Tiny. The status enum extension is additive; the dashboard JSON
change is operator-side display. The `NaN` mapping is the only
non-trivial bit — `prometheus_client.Gauge.set(float("nan"))` works
(verified in the prom client tests upstream), and Grafana handles
NaN as "no data" by default.

## Cross-references

- [`docs/plans/completed/p-11-structured-observability.md`](../plans/completed/p-11-structured-observability.md) — the plan this follows up.
- [`src/bffi_pipeline/stages/probes.py`](../../src/bffi_pipeline/stages/probes.py) — `ProbeResult` + `probe_mlx_lm`.
- [`src/bffi_pipeline/stages/judge.py:1474`](../../src/bffi_pipeline/stages/judge.py) — the M6 probe call site that triggered this.
- [`src/bffi_pipeline/metrics_exporter.py`](../../src/bffi_pipeline/metrics_exporter.py) — gauge mapping.
- [`config/grafana/dashboards/`](../../config/grafana/dashboards/) — dashboard JSON to update.
- 2026-05-13 conversation on dashboard "DOWN" panels during the P-10 Phase B + E bench — the trigger for this proposal.
