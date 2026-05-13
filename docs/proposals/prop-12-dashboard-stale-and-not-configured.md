# P-12 — Observability cleanup: exporter tail bug + dashboard freshness + `not_configured` health status

**Status**: proposed.
**Scope**: half-day to 1 day (three small, independent fixes).
**Proposal-base commit**: `cbaa7b2`.

## Motivation

Three unrelated UX bugs surfaced while operating the P-11 Phase D
observability stack during the P-10 Phase B + E bench
(2026-05-13 ~07:43 UTC). Each is small in isolation; bundled into
one PR for shared scope.

### Bug 1 — Stale `bffi_dependency_health` gauges

`bffi_dependency_health{dep="mlx-lm-primary",stage="m6"} = 0.0` and
`bffi_dependency_health{dep="mlx-lm-fallback",stage="m6"} = 0.0` —
both reported `down` long after mlx-lm came back up.

Cross-checking the sidecar (`data/stage-events.jsonl`):

- The M6 health event that set those gauges fired at `07:17:54Z`,
  before mlx-lm was running. Both probes correctly reported
  `status="down"` *at that time*. M6 has not run since.
- Prometheus `Gauge` retains its last-set value indefinitely, so the
  M6 row sits at "down" 26+ minutes after mlx-lm came back up. The
  M9 row, which is currently emitting fresh probes during the bench,
  correctly reports `mlx-lm: up`.

### Bug 2 — Fallback probed even when not configured

The fallback (`:8002`) is **never** started on the M2 Max dev box —
per `CLAUDE.md` memory the 72B cascade fallback does not fit on
64 GB unified memory. `src/bffi_pipeline/stages/judge.py:1474`
unconditionally probes `settings.llm_base_url_fallback`, and the
dashboard frames the resulting `down` as a fault rather than "not
configured for this host".

### Bug 3 — Counter double-counting in the exporter tail loop

`bffi_watchdog_events_total` shows ~81 550 events for a sidecar
that actually holds only 70 raw watchdog events:

| Inner event | Sidecar raw | Counter shows | Multiplier |
|---|---|---|---|
| `timeout` | 50 | 58 250 | ~1 165× |
| `give_up` | 10 | 11 650 | ~1 165× |
| `pair_budget_exceeded` | 10 | 11 650 | ~1 165× |

All 70 raw events come from prior unit-test runs (`pair_id":"a+b"`,
old Ollama-format `model` strings) — none from the active bench.
The 1 165× multiplier matches a ~1 poll/sec × ~19 min exporter
uptime, pointing at the tail loop.

Trace in `src/bffi_pipeline/metrics_exporter.py:279`:

```python
if size <= state.last_pos:
    # File was truncated / rotated. Reset and re-read from the start
    state.last_pos = 0
```

When `size == state.last_pos` (i.e. nothing was appended since the
last poll), the `<=` branch incorrectly resets `last_pos` to 0 and
re-applies the whole sidecar on the next read. Every idle poll
re-applies *every* event in the file. `Counter.inc()` accumulates,
so counters inflate linearly with exporter uptime regardless of
actual event activity. Gauges (`Counter.labels(...).set(...)`) are
unaffected — the most-recent-write wins for them.

Net effect on the current bench: any `field_budget_exceeded` events
the picker fires during the cold/warm runs will also inflate. The
dashboard's watchdog counts cannot be read literally until this is
fixed.

All three bugs produce the same operator confusion: the dashboard
shouts about things that are either historical, by-design, or
phantom counts from idle re-reads.

## Approach

Three small, independent changes:

### 1. Fix the exporter tail-loop double-count (one-character bug)

`src/bffi_pipeline/metrics_exporter.py:279` — change `<=` to `<` so
the truncation/rotation branch only fires when the file actually
got smaller, and add an explicit no-op when nothing was appended:

```python
if size < state.last_pos:
    state.last_pos = 0          # truncation only
if size == state.last_pos:
    return 0                    # no new bytes; was previously
                                # falling through to re-read everything
```

Unit test: drive `_tail_step` with no appended bytes between calls
and assert no event is re-applied (e.g. counter delta == 0 across
N idle polls).

This is the smallest of the three changes (one character + one
test) but the most impactful: it makes the watchdog and outcome
counters readable during a live bench. Should be the first commit
in the PR so the other two changes can be visually verified
against accurate counts.

### 2. New health status `not_configured` (gauge value `NaN`)

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

### 3. Dashboard freshness overlay

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

- `_tail_step` no longer re-applies events on idle polls. Unit test:
  call `_tail_step` N times with no appended bytes between calls and
  assert every `bffi_*_total` counter delta is zero across the N
  polls. Manually verified post-fix by starting the exporter against
  a sidecar with a known event count, idling for >60 polls, and
  confirming `bffi_watchdog_events_total` equals the on-disk count
  (not N+1× of it).
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

Tiny. The tail-loop fix is a one-character change with a clear unit
test; the status enum extension is additive; the dashboard JSON
change is operator-side display. The `NaN` mapping is the only
non-trivial bit — `prometheus_client.Gauge.set(float("nan"))` works
(verified in the prom client tests upstream), and Grafana handles
NaN as "no data" by default.

Cumulative counters that have already inflated from prior exporter
runs are *not* retroactively corrected — the fix only stops further
inflation. Operators wanting clean post-fix counts restart the
exporter (rehydrate replays the sidecar once, then the corrected
tail loop preserves accuracy). Not worth shipping a "clear counters
on startup" toggle; restart-as-reset is the existing operator
convention.

## Cross-references

- [`docs/plans/completed/p-11-structured-observability.md`](../plans/completed/p-11-structured-observability.md) — the plan this follows up.
- [`src/bffi_pipeline/stages/probes.py`](../../src/bffi_pipeline/stages/probes.py) — `ProbeResult` + `probe_mlx_lm`.
- [`src/bffi_pipeline/stages/judge.py:1474`](../../src/bffi_pipeline/stages/judge.py) — the M6 probe call site that triggered this.
- [`src/bffi_pipeline/metrics_exporter.py:267`](../../src/bffi_pipeline/metrics_exporter.py) — `_tail_step` (the off-by-one truncation check) and `apply_event` (the gauge / counter mapping).
- [`config/grafana/dashboards/`](../../config/grafana/dashboards/) — dashboard JSON to update.
- 2026-05-13 conversation on dashboard "DOWN" panels during the P-10 Phase B + E bench — the trigger for this proposal.
