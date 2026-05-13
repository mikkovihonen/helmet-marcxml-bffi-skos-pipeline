# P-12 — Observability cleanup: exporter tail bug + Phase 2 progress + dashboard overview row + freshness + `not_configured` health status

**Status**: done (→ [`docs/plans/completed/p-12-observability-cleanup.md`](../plans/completed/p-12-observability-cleanup.md), shipped 2026-05-13 across phases A `0eaea9b` / B `3990a9d` / C `08f6121` / D `5ef8e51` / E `af45d25`).
**Scope**: ~1 day (five small, independent fixes). The Phase 2
progress emitter is the largest single item (~half a day for the
thread-safe completion counter + test fixtures); the dashboard
overview row is JSON-only (no test code) so ~1 hour. The other
three are minute-scale fixes plus tests.
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

### Bug 4 — M9 Phase 2 emits no mid-stream progress events

During the 2026-05-13 P-10 Phase B + E bench, the sidecar pattern
for the active run was:

| Phase | Events | Cadence |
|---|---|---|
| Phase 1 (tier-0 + Finto/VIAF) | 63 progress + 1 phase_boundary | every 200 entities (`_M9_PROGRESS_CADENCE`) |
| Cache lookup | 1 progress | once at the end of the pass |
| Phase 2 (picker dispatch) | **0 progress + 1 phase_boundary** | only at start |
| Phase 3 (graph mutation + provenance) | — | (fast; not a dashboard concern) |

Phase 2 is the **longest** stretch on this corpus (Phase A2 measured
~27 min of the 60 min total) and it's invisible to the dashboard
between `phase_boundary=phase2` and the M9 `end` event. The operator
sees a flatline graph for the most operationally interesting period
— picker latency, cache hit-rate trend, watchdog event arrival — and
has to fall back to `tail -F mlx-lm-8001.log | grep -c POST` to
estimate progress, defeating the point of the structured pipeline.

The fix is straightforward: emit `progress` events from the picker
pool's `as_completed` loop in `_picker_phase_pool` (and from
`_picker_phase_seq`'s serial loop) at the same cadence Phase 1
uses, with counters showing how many picker calls have completed
against the deferred-pool total. The dashboard's existing m9 progress
panel will then render a smooth curve through Phase 2 instead of
flatlining.

### Bug 5 — Dashboard layout is overview-hostile

The current dashboard (9 panels, `config/grafana/dashboards/bffi-pipeline.json`)
puts three M9-Phase-1-specific stat tiles at the top and a
`Pipeline stages (last start / end timestamps)` panel at the
bottom. That bottom panel is the worst offender:

- It's a `stat` panel rendering one tile per `(stage, run_uuid)`
  pair from `bffi_stage_started_timestamp` *and*
  `bffi_stage_ended_timestamp`. With 10+ historical runs in the
  sidecar, the panel paints **20+ tiny tiles** in a 12-wide row,
  each cropped to "..." because labels don't fit.
- It mixes the *currently active* run with prior runs (the same
  staleness root cause as Bug 1). Operators reading the dashboard
  mid-bench can't tell which row reflects "right now".
- There's no top-of-screen "what's the pipeline doing right now"
  overview — the operator has to scroll past the M9-specific
  detail to discover M9 is even running.

The dashboard's primary use case is "I just kicked off
`run-full-pipeline.sh`, where is it?" — and the panel set today
doesn't answer that question without scanning the whole layout.

All five bugs produce the same operator confusion: the dashboard
shouts about things that are either historical, by-design, phantom
counts from idle re-reads, silent during the work that matters
most — or **drowning the answer to "what's running now?" in
historical-run dust**.

## Approach

Five small, independent changes:

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

This is the smallest of the five changes (one character + one
test) but the most impactful: it makes the watchdog and outcome
counters readable during a live bench. Should be the first commit
in the PR so the other four changes can be visually verified
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

### 4. M9 Phase 2 progress events

Emit `progress` events from both picker-dispatch paths in
`src/bffi_pipeline/stages/reconcile.py` at the same cadence Phase 1
uses (`_M9_PROGRESS_CADENCE`, default 200):

- `_picker_phase_pool` — emit after each batch of N futures resolves
  out of `concurrent.futures.as_completed(...)`. A thread-safe
  completion counter (atomic int or lock-guarded) drives the
  cadence; emission stays on the orchestrator thread so JSONL writes
  remain serialised.
- `_picker_phase_seq` — emit after each N-th result in the
  single-threaded loop. Trivial.

Event payload mirrors Phase 1's shape so the dashboard panel uses
one query for both phases:

```json
{"stage":"m9","event":"progress","phase":"phase2",
 "counters":{"processed":600,"total":1358},
 "extra":{"cache_hits":0,"watchdog_aborted":2}}
```

The `cache_hits` value carries the *running* Phase B hit rate
(known at lookup-pass time, already in the orchestrator's local
state). `watchdog_aborted` increments whenever a future returns a
``was_watchdog_aborted=True`` outcome — surfaces picker
hangs to the dashboard live rather than only after the run ends.

Cadence is intentionally rate-limited (default every 200 calls,
not every call) so the JSONL doesn't bloat — picker calls are
typically O(seconds) but a bench with 1 358 deferred calls would
write a 7-event Phase 2 progress trail under the default cadence.
Operator override via `BFFI_M9_PROGRESS_CADENCE` env var if a
denser trail is wanted during a smaller bench.

Test: replay a fixture with N=600 deferred calls, assert the picker
phase emits exactly `floor(600/200) = 3` progress events plus the
existing start/end boundaries, in submission order.

### 5. Top-of-dashboard pipeline-overview row + remove the noisy bottom panel

Two changes to the Grafana dashboard JSON:

**Add** a full-width pipeline-overview row at `(x=0, y=0, w=24, h=4)`,
pushing every existing panel down by 4 rows. Eight `stat` tiles in
one row (3 columns wide each, `24 / 8 = 3`), one per pipeline
stage: **m2**, **m3**, **m5**, **m6**, **m8**, **m9**, **skosify**,
**load**. Each tile shows:

| Field | Source | Value mapping |
|---|---|---|
| Title | hard-coded stage name | — |
| Big number | most recent ``bffi_stage_entities_total{stage="<stage>"}`` over the active run only | — |
| Sub-label | progress fraction when available (M9 has `processed`/`total` from `progress` events; other stages just show "running" / "done" / "idle") | — |
| Background | ``time() - bffi_stage_started_timestamp`` < 60 s **and** ``bffi_stage_ended_timestamp`` < ``started`` → green "running"; ``ended >= started`` → blue "done"; otherwise → grey "idle" | colour drives the at-a-glance overview |

Selecting "active run only" requires filtering by ``run_uuid``. The
exporter already labels every metric with ``run_uuid``; the new
top row uses a Grafana variable ``$active_run`` populated from
``topk(1, bffi_stage_started_timestamp)`` so the dashboard
auto-tracks the most recent invocation without an operator
manually editing the query.

**Remove** Panel 9 (``Pipeline stages (last start / end
timestamps)``) entirely. Its information is fully covered by the
new top row (current state) and by Prometheus's history (for
post-run analysis via ``range`` queries / ad-hoc PromQL). The
operator-visible regression is small: anyone wanting historical
timestamps now PromQL's them via Grafana's Explore tab instead of a
broken stat tile. Acceptable trade-off — the panel as currently
configured is unreadable on any run with >2 prior history entries.

Implementation: edit ``config/grafana/dashboards/bffi-pipeline.json``
once; Grafana auto-provisions on next dashboard refresh because the
file is bind-mounted into the container by ``docker-compose.yml``.
No code change in ``metrics_exporter.py`` — the existing metrics
already carry every label needed (``run_uuid``, ``stage``,
``processed``, ``total``).

Visual smoke test on the next bench: at any point during the run,
the top row should show m9 green/running with its processed/total
sub-label, every other stage either done (blue, if M9 follows them
in the same shell driver) or idle (grey). Compare against the
current dashboard's bottom-right panel and verify the same
information is conveyed with eight tiles instead of 20+.

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
- M9 Phase 2 emits `progress` events at the configured cadence; the
  dashboard's m9 progress panel renders a continuous curve from
  Phase 1 start through Phase 2 end on the next bench, with no
  flatline gap between `phase_boundary=phase2` and `m9 end`.
- Dashboard JSON has a new top-of-screen pipeline overview row with
  eight stage tiles, filtered to the active run via the
  ``$active_run`` Grafana variable; the bottom-right "Pipeline
  stages (last start / end timestamps)" panel is removed. Visual
  check during the next bench: the top row shows m9 green/running
  with its processed/total sub-label, no broken-tile-cropping on
  the cleaned-up layout.
- No regression in the existing P-11 health-probe tests; M9
  byte-stability tests still pass (the new emissions only add JSONL
  lines, no graph mutation).

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
- [`src/bffi_pipeline/stages/reconcile.py`](../../src/bffi_pipeline/stages/reconcile.py) — `_picker_phase_pool` + `_picker_phase_seq` (the picker-dispatch paths missing mid-stream progress emission), plus the existing `_M9_PROGRESS_CADENCE` constant that Phase 1 already uses.
- [`config/grafana/dashboards/`](../../config/grafana/dashboards/) — dashboard JSON to update.
- 2026-05-13 conversation on dashboard "DOWN" panels during the P-10 Phase B + E bench — the trigger for this proposal.
