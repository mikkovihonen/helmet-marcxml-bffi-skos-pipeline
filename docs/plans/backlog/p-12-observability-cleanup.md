# P-12 — Observability cleanup: exporter tail bug + Phase 2 progress + dashboard overview row + freshness + `not_configured` health status

**Status**: backlog.
**Source proposal**: [`docs/proposals/prop-12-dashboard-stale-and-not-configured.md`](../../proposals/prop-12-dashboard-stale-and-not-configured.md)
at commit `374eb60`.
**Plan-base commit**: `374eb60`. To gauge drift before executing,
run
`git diff 374eb60..HEAD --
src/bffi_pipeline/metrics_exporter.py
src/bffi_pipeline/stages/probes.py
src/bffi_pipeline/stages/reconcile.py
src/bffi_pipeline/stages/judge.py
config/grafana/dashboards/bffi-pipeline.json`.
**Phase commits**:

- Phase A (exporter tail-loop double-count fix): `<unfilled>`
- Phase B (`not_configured` health status): `<unfilled>`
- Phase C (dashboard freshness overlay): `<unfilled>`
- Phase D (M9 Phase 2 progress events): `<unfilled>`
- Phase E (top-of-dashboard pipeline overview row + remove the noisy bottom panel): `<unfilled>`

**Owner**: TBD.
**Estimated wall-time**: ~1 day end-to-end. Phase A is minute-scale
(one character + one test). Phase B + Phase C are ~1-2 hours each.
Phase D is the biggest single item at ~½ day (thread-safe completion
counter in the picker pool + fixtures for the byte-stable cadence
test). Phase E is dashboard-JSON-only at ~1 hour. Each phase is
independently shippable; A goes first so the post-fix counters are
accurate when the other four are visually verified during the next
bench.

## Goal

Make the P-11 Phase D Grafana dashboard correct, readable, and
useful as a real-time operator surface during a long bench. Today
the dashboard shows phantom watchdog counts, stale "down" badges
for stages that haven't run recently, a flatline panel during the
30-minute M9 picker phase, and an unreadable bottom panel that
dumps every historical run into one cropped stat tile. P-12 fixes
each of those without re-architecting the underlying P-11 emit /
scrape / render pipeline.

## Definition of done

- All five phases have filled-in phase commits, each on its own
  commit so a partial revert is mechanical.
- After Phase A ships: a 60-poll idle interval of the exporter
  against a sidecar with a known event count produces zero delta on
  every `bffi_*_total` counter. The current bench's inflated
  watchdog counts (~81 550 from 70 raw events) will stop growing on
  the next exporter restart; the values remain frozen at whatever
  inflated state they were last at — see plan § "Counter
  inheritance" below.
- After Phase B ships: M6's `mlx-lm-fallback` probe reports
  `status="not_configured"` on the M2 Max dev box (where the 72B
  fallback is never started) instead of `status="down"`. The
  exporter maps the new status to `NaN`; Grafana's default mapping
  greys out the cell.
- After Phase C ships: the dashboard tracks a per-dependency
  freshness gauge and greys out any cell whose probe is >60 s old.
  M6 panels stop showing "DOWN" 30 minutes after M6 last ran.
- After Phase D ships: M9 Phase 2 emits one `progress` event per
  `_M9_PROGRESS_CADENCE` (default 200) completed picker calls in
  both `_picker_phase_pool` and `_picker_phase_seq`. The
  dashboard's m9 progress panel renders a continuous curve through
  the picker phase instead of flatlining.
- After Phase E ships: the dashboard has a full-width top row with
  one tile per pipeline stage (m2 / m3 / m5 / m6 / m8 / m9 /
  skosify / load), colour-coded by current state, filtered to the
  active run. The bottom-right "Pipeline stages (last start / end
  timestamps)" panel is removed.
- `docs/plans/backlog/p-12-observability-cleanup.md` has been
  `git mv`'d through `in-progress/` → `completed/` per the
  lifecycle convention in [`docs/plans/README.md`](../README.md).
- `make lint && make test` stay green at each phase commit. No
  regression in the existing P-11 tests; M9 byte-stability tests
  still pass (the new emissions in Phase D only add JSONL lines,
  no graph mutation).

## Current state (as of plan-base `374eb60`)

- **`metrics_exporter.py:267-289` — `_tail_step`**: the
  truncation-detection branch uses `size <= state.last_pos`. When
  `size == last_pos` (nothing appended), this branch resets
  `last_pos=0` and the subsequent read re-applies the entire
  sidecar. Counters double-count proportionally to exporter uptime;
  gauges are unaffected. Verified mid-bench:
  `bffi_watchdog_events_total{event="timeout"} = 58 250` against a
  sidecar with 50 raw timeout events (1 165× multiplier matches the
  ~1 poll/sec × ~19 min uptime).
- **`stages/probes.py:probe_mlx_lm`**: `ProbeResult.status` is a
  three-value Literal (`up` / `degraded` / `down`). No way to
  express "not configured / not applicable on this host". The
  exporter maps every status to a gauge value:
  `up=2, degraded=1, down=0`. So a not-configured probe shows up
  as `0.0` indistinguishable from a genuine outage.
- **`stages/judge.py:1474`**: M6 unconditionally probes both the
  primary and fallback mlx-lm URLs at stage entry. On the M2 Max
  dev box the 72B fallback is never started (per `CLAUDE.md`
  memory); the probe reports `down`.
- **`stages/reconcile.py:_picker_phase_pool` + `_picker_phase_seq`**:
  no mid-stream `progress` event emission. Phase 1 already uses
  `_M9_PROGRESS_CADENCE` (currently every 200 entities, see
  the existing `_M9_PROGRESS_CADENCE` constant) — Phase 2 silently
  spins through 1 000+ picker calls without dashboard-visible
  signal.
- **`config/grafana/dashboards/bffi-pipeline.json`**: 9-panel
  layout. Top row has three M9-Phase-1-specific stat tiles
  (progress / ETA / throughput); the bottom-right panel
  (`id=9, "Pipeline stages (last start / end timestamps)"`)
  renders one tile per `(stage, run_uuid)` from sidecar history —
  20+ cropped tiles in the dev environment.

## Counter inheritance note

Phase A stops the bleed; it does **not** retroactively correct the
inflated counter values that prior exporter runs have written to
their in-memory state. Operators wanting clean post-fix counts
**restart the exporter** after deploying Phase A — rehydration
replays the sidecar once, then the corrected tail loop preserves
accuracy. Counter values from a long-uptime pre-fix exporter cannot
be reconciled in-place; the Prometheus history before the restart
will show inflated values for the affected counters, and queries
spanning the restart boundary should slice by the restart
timestamp. Documented in `docs/observability.md` as part of Phase A.

---

## Phase A — Exporter tail-loop double-count fix

Estimated wall-time: ~30 minutes (one-char fix + one unit test).
Smallest of the five but the prerequisite for visually verifying
B/C/D/E — Phase E's dashboard overview row in particular reads the
same counters the bug inflates.

### A.1. The fix

`src/bffi_pipeline/metrics_exporter.py:279`:

```python
# Before:
if size <= state.last_pos:
    state.last_pos = 0

# After:
if size < state.last_pos:
    # File was truncated / rotated. Reset and re-read from the start.
    state.last_pos = 0
if size == state.last_pos:
    return 0    # no new bytes; idle poll
```

The existing `if size == state.last_pos: return 0` further down the
function becomes dead code after this change (the truncation branch
no longer fires on idle polls), so the explicit return above is
the only path that returns 0 on idle. Remove the now-unreachable
check or leave it as a defensive belt-and-braces — judgement call;
either works.

### A.2. Tests

- **Idle polls don't double-count**: write a fixture sidecar with N
  known events (mix of `start` / `progress` / `end` / `health` /
  `watchdog`), construct a `PipelineMetrics`, call `rehydrate(...)`
  once, capture every counter value, then call `_tail_step(...)` 50
  times with no appended bytes between calls. Assert every counter
  value is byte-identical to the post-rehydrate snapshot. Pin the
  regression for posterity.
- **Truncation still triggers re-read**: write N events, rehydrate,
  truncate the sidecar to 0 bytes, append N' fresh events, call
  `_tail_step`. Assert the N' new events were applied (counter
  delta equals N' on the relevant counters).
- **Mixed pattern**: rehydrate, idle 5 polls (no delta), append 3
  events, idle 5 polls. Assert total counter delta is exactly the
  3 appended events.

### A.3. Acceptance

- [ ] One-line code change in `metrics_exporter.py:279`.
- [ ] Three unit tests added; all pass.
- [ ] `make lint && make test` green.
- [ ] `docs/observability.md` adds a note about the
  "restart the exporter to clear inflated counters" pattern.

### A.4. Rollback

Single-commit revert. The change is purely defensive; no Settings
changes, no schema migration, no data on disk depends on the new
behaviour. `git revert <A_commit>` and the exporter returns to its
prior (buggy) shape.

---

## Phase B — `not_configured` health status

Estimated wall-time: ~1-2 hours.

### B.1. Probe-side extension

`src/bffi_pipeline/stages/probes.py`:

- Extend `ProbeStatus = Literal["up", "degraded", "down",
  "not_configured"]`.
- Update `probe_mlx_lm(url: str, *, dep: str) -> ProbeResult`:
  return `ProbeResult(dep=dep, status="not_configured",
  latency_ms=0, note="empty url; probe skipped")` when `url` is
  empty / `""`. Existing callers pass `settings.llm_base_url_*` —
  unset settings already collapse to `""` via Pydantic defaults.
- For symmetry, return `not_configured` when the fallback URL
  equals the primary URL (degenerate cascade — same process
  probed twice). Detected at the caller side
  (`judge.py:1474`-ish): if `primary_url == fallback_url`, pass
  `""` as the fallback URL into `probe_mlx_lm`.

### B.2. Exporter mapping

`src/bffi_pipeline/metrics_exporter.py`:

- Extend `_HEALTH_STATUS_VALUE` to include
  `"not_configured": float("nan")`. `prometheus_client.Gauge.set`
  accepts NaN and renders it as `NaN` in `/metrics`. Prometheus
  scrapes NaN as "stale / unknown"; Grafana's default value-mapping
  greys the cell.
- No change to `bffi_dependency_health` gauge name / labels — the
  existing label set `(stage, dep)` is sufficient.

### B.3. Tests

- Unit: `probe_mlx_lm(url="")` returns `not_configured`.
- Unit: `apply_event` on a `health` event whose probe carries
  `status="not_configured"` sets the gauge to NaN (assert via
  `math.isnan(metrics.dependency_health.labels(...)._value.get())`
  or by reading `/metrics` text and asserting `NaN` is on the
  line — pick whichever is cleaner against the
  `prometheus_client` API surface).
- Integration: call M6's probe-emit path with an empty fallback
  URL via a temporarily-overridden `Settings`; assert the sidecar
  event carries `status="not_configured"` and the exporter's
  gauge shows NaN.

### B.4. Acceptance

- [ ] `ProbeStatus` Literal extended.
- [ ] `probe_mlx_lm` returns `not_configured` on empty URL.
- [ ] M6 + M9 probe call sites updated to skip-via-empty-URL when
  the cascade fallback is not configured.
- [ ] Exporter maps `not_configured` to NaN.
- [ ] Three unit tests added; all pass.
- [ ] `make lint && make test` green.

### B.5. Rollback

Single-commit revert. The Literal extension is additive in old
callers (string comparison falls through); the exporter mapping
change is forward-compatible (old events still map cleanly to
up=2 / degraded=1 / down=0).

---

## Phase C — Dashboard freshness overlay

Estimated wall-time: ~1-2 hours.

### C.1. Per-probe timestamp gauge

`src/bffi_pipeline/metrics_exporter.py`:

- Add `dependency_last_probe_timestamp: Gauge` with labels
  `(stage, dep)`. Help string: "Unix timestamp of the most recent
  `health` event for this stage / dep."
- In `apply_event` for `event="health"`, set the gauge to
  `row.ts.timestamp()` for each probe.

### C.2. Dashboard freshness queries

`config/grafana/dashboards/bffi-pipeline.json`:

- Add a Grafana value-mapping function `age = time() -
  bffi_dependency_last_probe_timestamp{...}` to the existing
  "Dependency health" panel's queries.
- Add a `valueMappings` block to the panel:

| Match | Display |
|---|---|
| `health=2, age<60` | green "up" |
| `health=1, age<60` | yellow "degraded" |
| `health=0, age<60` | red "DOWN" |
| `age>=60` | grey "stale {age}s ago" |
| `health=NaN` | grey "—" |

Threshold is tunable; 60 s lines up with Prometheus's 5 s scrape
interval × the in-bench probe cadence
(`_M9_HEALTH_PROBE_CADENCE`).

### C.3. Tests

- Unit: `apply_event` on a `health` event sets
  `bffi_dependency_last_probe_timestamp{stage,dep}` to the event's
  `ts.timestamp()` for every probe.
- Unit: rehydrating a sidecar with health events from multiple
  runs sets the timestamp gauge to the *latest* probe per
  `(stage, dep)`, not the first.
- Visual smoke: post-bench, restart the exporter, confirm the
  dashboard renders stale-grey for M6 probes and live-coloured for
  M9 probes (M9 is still emitting fresh probes during a bench).

### C.4. Acceptance

- [ ] `dependency_last_probe_timestamp` gauge added.
- [ ] Dashboard JSON updated with the freshness value-mapping.
- [ ] Two unit tests added; all pass.
- [ ] `make lint && make test` green.

### C.5. Rollback

Single-commit revert. The new gauge is opt-in (only the new
dashboard panel consumes it); reverting drops the gauge and the
panel falls back to the pre-Phase-C "always-fresh, always-coloured"
behaviour.

---

## Phase D — M9 Phase 2 progress events

Estimated wall-time: ~½ day. Most of the work is the thread-safe
completion counter for the concurrent picker pool and the cadence
fixture.

### D.1. Sequential path

`src/bffi_pipeline/stages/reconcile.py:_picker_phase_seq`:

- Inside the per-deferred-entry loop, after each `_picker_call_with_budget`
  returns, increment a local `completed: int`. When
  `completed % _M9_PROGRESS_CADENCE == 0`, emit:

```python
emit_if_active(
    stage="m9",
    event="progress",
    phase="phase2",
    counters={"processed": completed, "total": len(deferred)},
    extra={"cache_hits": cache_hits, "watchdog_aborted": watchdog_aborted},
)
```

- `cache_hits` and `watchdog_aborted` live in the orchestrator's
  Phase-1.5 lookup pass and Phase-2 dispatch results respectively;
  thread `_picker_phase_seq` accepts them as kwargs (it already
  receives `field_timeout_seconds`, so the call-site extension is
  trivial).

### D.2. Concurrent path

`_picker_phase_pool`: the completion counter is the tricky bit
because `concurrent.futures.as_completed` yields futures
out-of-submission-order under N workers. Two patterns work:

- **Atomic counter + lock**: a `threading.Lock`-guarded `int` that
  the orchestrator increments on each `fut.result()`. The
  orchestrator (not the worker) does the increment and the
  cadence check — keeps emission single-threaded so the JSONL
  sidecar stays serialised.
- **`itertools.count` / `next()`**: thread-safe by CPython
  contract but only at the GIL boundary. The orchestrator-side
  iteration over `as_completed` is single-threaded anyway, so the
  simpler `int + lock` pattern is fine.

Use the lock pattern. Code shape:

```python
completed = 0
lock = threading.Lock()  # only needed if the cadence check is
                          # ever moved to worker threads; today the
                          # orchestrator's as_completed loop is
                          # single-threaded so the lock is
                          # belt-and-braces.
for fut in concurrent.futures.as_completed(futures):
    idx, outcome = fut.result()
    results.append((idx, outcome))
    completed += 1
    if completed % cadence == 0:
        emit_if_active(
            stage="m9", event="progress", phase="phase2",
            counters={"processed": completed, "total": len(deferred)},
            extra={"watchdog_aborted": _aborted_count(results)},
        )
```

`_aborted_count(results)` is a trivial helper that counts
`outcome.was_watchdog_aborted` over the accumulated results.
`cache_hits` is fixed at Phase-1.5 exit so the orchestrator passes
it in once.

### D.3. Cadence override

Promote `_M9_PROGRESS_CADENCE` from a module-level constant to a
Settings entry (`m9_progress_cadence: int = Field(default=200,
alias="BFFI_M9_PROGRESS_CADENCE")`) so operators can crank the
cadence down for short benches without a code change. Phase 1's
existing emission reads the same Settings field so cadence
overrides apply uniformly to both phases.

### D.4. Tests

- Unit: fixture with N=600 deferred picker calls, cadence=200,
  `_picker_phase_pool` emits exactly `floor(600/200) = 3` progress
  events plus the existing start/end boundaries.
- Unit: same fixture, `_picker_phase_seq` (single-threaded path)
  emits the same 3 events in submission order.
- Unit: `_M9_PROGRESS_CADENCE` env override (set to 100) doubles
  the emission count.
- Byte-stability regression: the existing
  `test_apply_reconciliation_byte_stable_at_c1_vs_c4` still passes
  (the new emissions add JSONL lines, no graph mutation).

### D.5. Acceptance

- [ ] `m9_progress_cadence` setting added.
- [ ] Both picker-dispatch paths emit per-cadence `progress`
  events.
- [ ] Four unit tests added; all pass.
- [ ] `make lint && make test` green.
- [ ] On the next bench, the dashboard's m9 progress panel renders
  a continuous curve through Phase 2 (no flatline gap between
  `phase_boundary=phase2` and `m9 end`).

### D.6. Rollback

Single-commit revert. Settings field stays (unused); the emission
calls are gone. The dashboard's Phase 2 progress panel falls back
to flatlining between phase_boundary and end — which is the
pre-Phase-D state.

---

## Phase E — Top-of-dashboard pipeline-overview row + remove the noisy bottom panel

Estimated wall-time: ~1 hour. Dashboard JSON only; no code touched
(the necessary metrics already exist).

### E.1. Active-run Grafana variable

`config/grafana/dashboards/bffi-pipeline.json`:

- Add a `templating.list` entry named `active_run`:

```json
{
  "name": "active_run",
  "type": "query",
  "datasource": "Prometheus",
  "query": "label_values(topk(1, bffi_stage_started_timestamp), run_uuid)",
  "refresh": 2,
  "current": { "selected": false },
  "hide": 2
}
```

Refresh on time-range change (`refresh: 2`) so the variable
auto-tracks the most recent invocation. `hide: 2` keeps the
variable out of the dashboard toolbar (it's purely query-side).

### E.2. New pipeline overview row

Insert eight `stat` panels at `(x = 3*i, y = 0, w = 3, h = 4)` for
`i` in 0..7, one per stage in order: `m2`, `m3`, `m5`, `m6`, `m8`,
`m9`, `skosify`, `load`. Each panel's query:

```promql
bffi_stage_entities_total{
  stage="<stage>",
  run_uuid="$active_run"
}
```

Value-mapping:

| Source / condition | Display |
|---|---|
| `bffi_stage_ended_timestamp >= bffi_stage_started_timestamp` (for the same `stage` + `run_uuid`) | blue "done" + final count |
| `time() - bffi_stage_started_timestamp < 600` and no end yet | green "running" + current count |
| no `start` event for this run_uuid | grey "idle" / "—" |

Override the panel reduce-option to `lastNotNull` so the displayed
value tracks the latest progress event during a stage.

### E.3. Push existing panels down

Every existing panel's `gridPos.y` increments by 4 to make room for
the new overview row. Verified mechanically; no other layout edits
needed.

### E.4. Remove the noisy bottom panel

Delete `id=9` (`Pipeline stages (last start / end timestamps)`)
from the `panels[]` array. Historical timestamps remain queryable
via Grafana's Explore tab and PromQL `range` queries.

### E.5. Tests

- Unit: the dashboard JSON schema validator (already in the P-11
  Phase D test suite) accepts the new shape with the
  `templating.list` block and the eight new stat panels.
- Visual smoke on the next bench: the top row shows m9 green
  /running with its processed/total sub-label; other stages either
  blue/done or grey/idle. The bottom-right panel that the operator
  flagged as unreadable is gone.

### E.6. Acceptance

- [ ] Dashboard JSON has the `active_run` Grafana variable.
- [ ] Eight stat panels rendered in the new top row.
- [ ] Panel 9 removed.
- [ ] Existing JSON schema test still passes.
- [ ] Visual check during the next bench (or against a replayed
  fixture if no bench is queued) confirms the overview row reads
  cleanly.

### E.7. Rollback

`git revert <E_commit>`. Dashboard JSON returns to the pre-Phase-E
shape; the noisy panel comes back. No data on disk depends on the
layout.

---

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Phase A — counter values that look "lost" after the exporter restart confuse operators reading Prometheus history | Medium | Documented in `docs/observability.md` (Counter inheritance section). Operators can slice queries by the restart timestamp; no data is actually lost, just no longer accumulating. |
| Phase B — `NaN` gauges interact poorly with PromQL aggregations on rare Grafana setups | Low | `prometheus_client` upstream tests cover NaN serialisation; Grafana's default value-mapping treats NaN as "no data" (grey). The dashboard uses no aggregations on `bffi_dependency_health`; it's a stat panel reading directly. |
| Phase C — freshness threshold (60 s) too aggressive on slow benches where probes fire less often than the threshold | Low | Tunable via dashboard panel options; bench operators can extend the threshold per-run if needed. The default lines up with M9's `_M9_HEALTH_PROBE_CADENCE` so it's well-calibrated for the most common case. |
| Phase D — `cache_hits` / `watchdog_aborted` counters leaking from worker threads on the concurrent path | Low | The cadence check + emit run on the orchestrator thread (inside the `as_completed` loop), not on workers. Workers only return outcomes. The byte-stability test pins this. |
| Phase D — dashboard cardinality explodes if cadence is set very low (every-call) | Low | Default cadence 200; operators who override have to opt-in. JSONL sidecar size scales linearly with emission count; ~7 events per Phase 2 at default cadence is negligible. |
| Phase E — `topk(1, bffi_stage_started_timestamp)` evaluates to the wrong run_uuid during a stage transition where the next stage hasn't emitted `start` yet | Low | `topk(1, …)` always returns a non-empty result if any stage has ever started. Worst case the overview shows the previously-completed run for a few seconds; the next stage's `start` event refreshes the variable on the dashboard's next refresh tick (`refresh: 2`). |
| All — phases land sequentially during another bench window | Low | Each phase is independently shippable behind its own commit. None of the changes affect a running pipeline process (Python imports are loaded once at process start). Operators can rebench after deploying. |

## Open issues to close before / during execution

- **Grafana JSON schema test coverage of the `templating.list`
  block**: P-11 Phase D's existing schema test asserts top-level
  shape but doesn't deeply validate `templating`. Phase E's commit
  should extend the test to cover the new variable shape so future
  edits don't regress it.
- **Per-stage progress for stages other than M9** (Phase D's
  pattern generalised): M2 / M3 / M6 / M8 / skosify / load all
  emit `progress` events from P-11 Phase A, but only M9 carries
  `processed` / `total` counters. The new overview row's
  sub-labels are accordingly empty for those stages. Promoting
  every stage to emit `processed` / `total` is a P-12 follow-up
  (each stage knows its own work surface; the templating is
  uniform).
- **Renaming the M9-Phase-1-specific stat tiles** (panels 1-3):
  after Phase E ships, the top-row overview communicates "where in
  the pipeline" and the row below should arguably be relabelled to
  "M9 reconcile detail" so the operator knows it's stage-specific.
  Out of scope for P-12 (cosmetic-only); flag for a P-12-follow-up
  if the layout still confuses operators on the next bench.

## Cross-references

- [`docs/plans/completed/p-11-structured-observability.md`](../completed/p-11-structured-observability.md) — the parent plan this follows up.
- [`docs/proposals/prop-12-dashboard-stale-and-not-configured.md`](../../proposals/prop-12-dashboard-stale-and-not-configured.md) — source proposal.
- [`src/bffi_pipeline/metrics_exporter.py`](../../../src/bffi_pipeline/metrics_exporter.py) — `_tail_step` (Phase A) + `apply_event` (Phase B + C) + `PipelineMetrics` (Phase B + C gauges).
- [`src/bffi_pipeline/stages/probes.py`](../../../src/bffi_pipeline/stages/probes.py) — `ProbeResult` + `probe_mlx_lm` (Phase B).
- [`src/bffi_pipeline/stages/judge.py`](../../../src/bffi_pipeline/stages/judge.py) — M6 fallback-probe call site (Phase B).
- [`src/bffi_pipeline/stages/reconcile.py`](../../../src/bffi_pipeline/stages/reconcile.py) — `_picker_phase_pool` + `_picker_phase_seq` (Phase D).
- [`src/bffi_pipeline/config.py`](../../../src/bffi_pipeline/config.py) — Settings extension (Phase D).
- [`config/grafana/dashboards/bffi-pipeline.json`](../../../config/grafana/dashboards/bffi-pipeline.json) — dashboard JSON (Phase C + E).
- [`docs/observability.md`](../../observability.md) — metric vocabulary doc (Phase A note).
- 2026-05-13 live-bench session that triggered prop-12 — the operator pain that motivated this cleanup.
