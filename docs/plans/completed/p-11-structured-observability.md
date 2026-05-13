# P-11 — Structured pipeline observability for long unattended runs

**Status**: backlog.
**Source proposal**: [`docs/proposals/prop-11-structured-observability.md`](../../proposals/prop-11-structured-observability.md)
at commit `30cd82a`.
**Plan-base commit**: `30cd82a`. To gauge drift before executing,
run
`git diff 30cd82a..HEAD --
src/bffi_pipeline/stages/
src/bffi_pipeline/cli.py
src/bffi_pipeline/config.py
scripts/run-full-pipeline.sh
docker-compose.yml
docs/runbook.md`.
**Phase commits**:

- Phase A (structured event emission from every stage): `f3a22b3` (2026-05-13). Code: `observability.py` + Settings + CLI hook + wired into M2/M3/M6/M8/M9/skosify/load; watchdog absorption forwards to the new stream; 5 unit tests + 867 total green. M5 embeddings deferred to a follow-up sub-step (the build/emit-candidates surface needs more event-cadence design than fit in this commit). Real-pipeline-run sanity check folds into the next bench.
- Phase B (`bffi-pipeline status` CLI): `25b2c6e` (2026-05-13). Code: `src/bffi_pipeline/status.py` (parse + collate + render + tail) + new `bffi-pipeline status` subcommand in `cli.py`. `--sidecar` / `--tail` / `--since now|<iso>` / `--run-uuid` flags. 13 new unit tests against synthetic event streams. 880 total tests green.
- Phase C (dependency health probes): `2cd00e2` (2026-05-13). Code: `stages/probes.py` with probe_fuseki / probe_mlx_lm / probe_finto + emit_health_probes helper. Wired into M9 (entry + every 1000 entities re-probe), M6 (primary + fallback mlx-lm at entry), load (Fuseki at entry). M3 cascade probe deferred (small follow-up). 13 new unit tests covering up/degraded/down for each probe + emit_health_probes write shape. 894 total tests green.
- Phase D (Prometheus exporter + Grafana dashboard): `<unfilled>`

**Owner**: TBD.
**Estimated wall-time**: ~3-4.5 days end-to-end. Phase A ~1-2 days (the bulk: per-stage emission + watchdog absorption + tests). Phase B ~½ day. Phase C ~½ day. Phase D ~1-1.5 days (CLI + Docker Compose + Grafana dashboard JSON + runbook docs). Each phase is independently shippable; A is the prerequisite for B/C/D, which can land in any order after A.

## Goal

Make the pipeline's in-flight state observable from a single canonical event stream so an operator running an unattended overnight batch can answer "what's happening right now?" without composing five system tools. P-10's bench iterations established the pain (see snapshot § "What's the progress?" diagnostic sessions); P-11 makes the answer one command (Phase B) or one browser tab (Phase D).

Concrete targets:
- One sidecar (`<BFFI_DATA_DIR>/stage-events.jsonl`) carries every stage's start / progress / phase-boundary / end / health events, plus a copy of every watchdog event.
- `bffi-pipeline status` returns in <1 s with elapsed + counters + ETA + last dependency-probe verdict per stage.
- `make observability-up` brings up a local Prometheus + Grafana that auto-loads a pre-provisioned dashboard with per-stage progress, M9 tier-outcome distribution, dependency state timeline, and watchdog event rates.
- No outbound telemetry; the stack is purely local-in-container.

## Definition of done

- All four phases have filled-in phase commits, each on its own commit so partial revert is mechanical.
- After Phase A ships: tailing `stage-events.jsonl` during a fresh 5k run produces one `start` per stage entry, ≥10 `progress` events per stage at the configured cadence, and one `end` per stage exit. Watchdog events appear both in `watchdog-events.jsonl` (existing) and in `stage-events.jsonl` (new, with `event="watchdog"` shape).
- After Phase B ships: `bffi-pipeline status` and `bffi-pipeline status --tail` render the expected layout against the events written by Phase A's 5k run.
- After Phase C ships: a `health` event lands at every stage entry that uses Fuseki / mlx-lm / Finto, with `status="up"` when those services respond and `status="degraded"` when they don't.
- After Phase D ships: `make observability-up` is a one-step start; pointing a browser at `http://localhost:3001` shows the provisioned dashboard with live counters from the running pipeline.
- The plan moves through `backlog/` → `in-progress/` → `completed/` via `git mv` in the corresponding phase commits.
- `make lint && make test` stays green throughout; new code is covered by unit tests against synthetic event streams (no real Prometheus / Grafana / Fuseki in unit tests).

## Current state (as of plan-base `30cd82a`)

- **`STAGE_*` stderr-prefix convention** is used by `scripts/run-full-pipeline.sh:91` for log-tail filtering. Each stage prints a `=== STAGE_<NAME> ===` banner at the start and `summary.render()` text at the end. No mid-run progress emission.
- **`WATCHDOG_EVENT` stderr-prefix + JSONL sidecar** (`src/bffi_pipeline/stages/watchdog.py`) is the closest analog and the structural reference. P-11 generalises its `emit_*_event` shape into `emit_stage_event` covering all stages.
- **Per-stage sidecars** exist for stage-specific use (`helmet-map.jsonl`, `judge-decisions.jsonl`, `embed-candidates.jsonl`, `canonical-conflicts.jsonl`, `canonical-map.jsonl`, `watchdog-events.jsonl`). All authoritative for their stage. P-11 doesn't replace any; it adds a *summary* sidecar.
- **Provenance graph** (`data/provenance.ttl`) carries the canonical audit trail per spec § 8 — Activity records per record processed. Forensic-grade; not designed for real-time tail.
- **No `bffi-pipeline status` CLI** — operators inspect state with `ps`, `curl`, `grep`, `docker logs`, `stat`.
- **No Prometheus / Grafana** services in `docker-compose.yml`; current containers are `bffi-fuseki` (port 3030) and `bffi-skosmos` (port 9090).
- **`CLAUDE.md` § "Operating constraints"** explicitly clarifies (as of `30cd82a`) that "no telemetry" means outbound-only; local-in-container observability is in scope.

---

## Phase A — Structured event emission from every stage

Estimated wall-time: ~1-2 days. Independent of B/C/D; the prerequisite for all three.

### A.1. The `emit_stage_event` API (`src/bffi_pipeline/stages/observability.py`)

New module mirroring `stages/watchdog.py`:

```python
StageEvent = Literal["start", "progress", "phase_boundary", "end", "health", "watchdog"]

@dataclass(frozen=True)
class StageEventEmitter:
    sidecar_path: Path | None
    run_uuid: str  # one UUID per pipeline invocation, anchors all events

    def emit(
        self,
        *,
        stage: str,                       # "m2" / "m3" / "m5" / "m6" / "m8" / "m9" / "skosify" / "load"
        event: StageEvent,
        phase: str | None = None,         # e.g. "phase1" / "phase2" / "phase3" for M9
        counters: dict[str, int] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None: ...
```

- Emits to stderr with prefix `STAGE_EVENT ` (one space, mirroring `WATCHDOG_EVENT `).
- Appends to `sidecar_path` if provided (one JSON object per line). Production callers pass `<BFFI_DATA_DIR>/stage-events.jsonl`; unit tests pass `None`.
- `run_uuid` is generated once per pipeline invocation and threaded through. CLI entry points assign it; the `Settings.run_uuid` plumbing adds one new field.
- Event payload (canonical JSON shape):
  ```json
  {
    "ts": "2026-05-13T05:13:36Z",
    "run_uuid": "01HXXX...",
    "stage": "m9",
    "event": "progress",
    "phase": "phase1",
    "counters": {"processed": 9876, "total": 12666},
    "extra": {"tier0_local": 7421, "no_candidate": 1893}
  }
  ```

### A.2. Wire emission into every stage

Each stage's `run()` (or equivalent entry point) gains:

- One `start` event at entry, carrying input cardinality where known (e.g. M2: number of MARCXML files; M9: number of canonical Works).
- `progress` events every N items per the stage's configured cadence. Defaults: M2 every 100, M3 every 100, M5 every 500, M6 every 25, M8 every 200, M9 every 200 (one event per phase boundary too), skosify-run + load every N triples or single end-only.
- `phase_boundary` events for M9 specifically: at the transition from Phase 1 → Phase 2, and Phase 2 → Phase 3.
- One `end` event carrying the per-tier summary counters.

The emitter instance is constructed in the CLI command and passed into the stage's `run()`. Existing function signatures gain an optional `event_emitter: StageEventEmitter | None = None` parameter (default `None` for backwards-compat with tests).

### A.3. Absorb watchdog events into the new stream

`stages/watchdog.py`'s `emit_watchdog_event` keeps its existing surface (writes to `watchdog-events.jsonl` + stderr). It additionally writes a `watchdog`-typed event to `stage-events.jsonl` when an `StageEventEmitter` is reachable. Forensic audit still uses the dedicated sidecar; the status CLI / dashboard read the summary sidecar.

Implementation: add a module-level `_active_emitter` slot in `observability.py` that the CLI sets at startup; `emit_watchdog_event` checks it. If `None`, behaves as today.

### A.4. CLI plumbing + settings

- `Settings.observability_sidecar: Path | None` (default `<BFFI_DATA_DIR>/stage-events.jsonl`, alias `BFFI_OBSERVABILITY_SIDECAR`).
- `Settings.run_uuid: str | None` (default `None`, alias `BFFI_RUN_UUID`). When `None`, CLI generates one at command entry.
- Every CLI subcommand that runs a pipeline stage constructs the `StageEventEmitter` and passes it through. The dispatch is mechanical (Argument plumbing); no per-stage logic changes.

### A.5. Tests

- Unit: `emit` produces the expected stderr prefix + JSONL line for each event type.
- Unit: idempotent re-runs of a stage write multiple `start`/`end` events with monotonic timestamps; no clobbering of the sidecar.
- Unit: when `sidecar_path` is None, no file is written; stderr emission still happens.
- Unit (watchdog absorption): `emit_watchdog_event` writes both to `watchdog-events.jsonl` AND `stage-events.jsonl` when the active emitter is set; only `watchdog-events.jsonl` when not.
- Integration: end-to-end mini-pipeline (synthetic 50-record fixture) produces the expected event-log shape (one `start` per stage, ≥1 `progress`, one `end`).

### A.6. Acceptance

- [ ] `stages/observability.py` exists with the `StageEventEmitter` + `emit_stage_event` API.
- [ ] Every stage (M2, M3, M5, M6, M8, M9, skosify-run, load) emits `start`, `progress` (at cadence), and `end` events.
- [ ] M9 emits `phase_boundary` events between Phase 1/2/3.
- [ ] Watchdog events also appear in `stage-events.jsonl` with `event="watchdog"`.
- [ ] All unit tests + the integration mini-pipeline test pass.
- [ ] `make lint && make test` green.
- [ ] No regression in existing per-stage sidecars (existing tests still pass byte-for-byte on their outputs).

### A.7. Rollback

- Set `BFFI_OBSERVABILITY_SIDECAR=""` (empty) and the CLI commands' emitter construction skips initialising. Stages still call `emit_stage_event`, but with `sidecar_path=None` → stderr-only.
- Full revert: `git revert` the Phase A commit. The new sidecar file is gitignored; no on-disk artefact to clean up beyond removing `stage-events.jsonl`.

---

## Phase B — `bffi-pipeline status` CLI

Estimated wall-time: ~½ day. Depends on Phase A. Cheap once the event stream is in place.

### B.1. The subcommand

`bffi-pipeline status [--tail] [--since <iso-timestamp>] [--sidecar <path>]`

- Default mode: parses the full sidecar, renders the latest state per stage as paste-ready text, exits.
- `--tail` mode: follows the sidecar (`tail -F` semantics), re-renders the summary on each new event. Plain text, no curses; clean exit on `Ctrl-C`.
- `--since <iso-timestamp>` filters to events after that wall-clock — useful when the sidecar accumulates across runs and the operator wants "the current run only". `--since now` is shorthand for "use the latest `start` event as the anchor".
- `--sidecar <path>` override; defaults to `settings.observability_sidecar`.

### B.2. Output shape

```
M9 reconcile (run 01HXXX..., started 20:09:00, elapsed 50m23s)
  phase1   ████████████░░░  9876 / 12666  (78%, ~14m to phase boundary)
  phase2   waiting
  health   fuseki ok 23ms, mlx-lm ok 12ms, finto (not probed)
  watchdog 0 field_budget_exceeded events
```

- Throughput: items/sec computed from the last 5 `progress` events.
- ETA: linear extrapolation of remaining items / current throughput. Surface median + p95 throughput when variance is high (>2× ratio).

### B.3. Tests

- Synthetic event-stream fixture (a list of `StageEvent` dicts) → renderer produces the expected ASCII output.
- ETA test: monotonically increasing progress events produce monotonically decreasing ETA.
- `--since` filter test: events before the anchor are dropped.

### B.4. Acceptance

- [ ] `bffi-pipeline status` returns in <1 s on a 50 k-event sidecar.
- [ ] `--tail` mode re-renders within 100 ms of a new event appearing.
- [ ] Test fixtures cover the panel set for M9, M6, M8.

### B.5. Rollback

`git revert` the Phase B commit. Phase A's emission and sidecar stay intact; only the consumer goes away.

---

## Phase C — Dependency health probes

Estimated wall-time: ~½ day. Depends on Phase A. Independent of B and D.

### C.1. Probe helpers

In `stages/observability.py` (or a sibling `observability/probes.py`):

```python
def probe_fuseki(http_client: httpx.Client, fuseki_url: str) -> ProbeResult: ...
def probe_mlx_lm(http_client: httpx.Client, base_url: str) -> ProbeResult: ...
def probe_finto(http_client: httpx.Client) -> ProbeResult: ...
```

`ProbeResult` carries `status: "up" | "degraded" | "down"`, `latency_ms: int`, `note: str`. Each probe is a one-shot HTTP call with `timeout=5.0`. Never raises; failures return `status="degraded"` (HTTP error / timeout) or `status="down"` (connection refused).

### C.2. Call sites

Each stage that depends on an external service calls the relevant probe at stage entry, emits a `health` event via the active emitter, and (in long-running stages) re-probes every N progress events.

- M9 (`reconcile.py` `apply_reconciliation`): probe Fuseki + mlx-lm + Finto at entry; re-probe every 1000 entities.
- M6 (`judge.py` `judge_batch`): probe mlx-lm primary + fallback ports at entry; re-probe every 200 pairs.
- M3 (cascade-enabled `bf_to_bffi`): probe mlx-lm at entry only.
- M10/M11 (`load.py`): probe Fuseki at entry.

### C.3. Tests

- Unit: each probe handles success (200 OK with realistic body), timeout, and connection-refused without raising.
- Unit: `health` events are emitted at the documented call sites; payload shape matches the metric vocabulary Phase D consumes.

### C.4. Acceptance

- [ ] `health` events appear in `stage-events.jsonl` at stage entries for M2/M3/M5/M6/M8/M9/load (per the service-dependency map above).
- [ ] Probe failures don't fail the stage — the bench can run end-to-end with mlx-lm intentionally stopped (M9 ends up at tier-3 fallback for picker-required fields, but no orchestrator-level abort).
- [ ] All unit tests pass.

### C.5. Rollback

`git revert` the Phase C commit. Probes go away; Phase A's emission and Phase B's status CLI stay intact (status just won't have health data to show).

---

## Phase D — Prometheus exporter + provisioned Grafana dashboard

Estimated wall-time: ~1-1.5 days. Depends on Phase A. Independent of B and C.

### D.1. `bffi-pipeline serve-metrics --port 9100`

New module `src/bffi_pipeline/stages/metrics_exporter.py`. New CLI subcommand `serve-metrics`.

- Adds `prometheus_client` to the project's Python dependencies (one new OSS pin; outbound-free).
- Tails `<BFFI_DATA_DIR>/stage-events.jsonl` (`tail -F` semantics via watchdog or polling); for each event, updates the corresponding Prometheus metric.
- Reads the full sidecar at startup to rehydrate counters from a prior run.
- Exposes the standard `/metrics` endpoint on the configured port.
- Survives stage transitions / pipeline restarts; can be left running across multiple pipeline runs.

Metric vocabulary (one-to-one with Phase A event payloads):

| Metric | Type | Labels | Source event |
|---|---|---|---|
| `bffi_stage_started_timestamp` | gauge | `stage`, `run_uuid` | `start` |
| `bffi_stage_entities_total` | gauge | `stage`, `phase` | `start` / `phase_boundary` |
| `bffi_stage_entities_processed_total` | counter | `stage`, `phase` | `progress` |
| `bffi_stage_outcomes_total` | counter | `stage`, `outcome` | `progress` (M9 only) |
| `bffi_stage_throughput_per_minute` | gauge | `stage`, `phase` | derived (last 5 events) |
| `bffi_stage_eta_seconds` | gauge | `stage`, `phase` | derived |
| `bffi_dependency_health` | gauge | `dep`, `port` | `health` |
| `bffi_dependency_probe_latency_ms` | gauge | `dep`, `port` | `health` |
| `bffi_watchdog_events_total` | counter | `stage`, `event` | `watchdog` |
| `bffi_stage_ended_timestamp` | gauge | `stage`, `run_uuid` | `end` |

### D.2. Docker Compose extension

Extend `docker-compose.yml` with:

```yaml
prometheus:
  image: prom/prometheus:v2.55.0
  volumes:
    - ./config/prometheus.yml:/etc/prometheus/prometheus.yml:ro
  ports:
    - "9091:9090"  # host:9091 to avoid the existing Skosmos :9090

grafana:
  image: grafana/grafana:11.4.0
  volumes:
    - ./config/grafana/provisioning:/etc/grafana/provisioning:ro
    - ./config/grafana/dashboards:/etc/grafana/dashboards:ro
  ports:
    - "3001:3000"
  environment:
    GF_SECURITY_ADMIN_PASSWORD: admin
    GF_AUTH_ANONYMOUS_ENABLED: "true"
    GF_AUTH_ANONYMOUS_ORG_ROLE: Viewer
```

`config/prometheus.yml` carries one scrape job targeting `host.docker.internal:9100` (the operator's `serve-metrics` process), scrape interval 5 s.

`Makefile` targets:
- `observability-up`: `docker compose up -d prometheus grafana`
- `observability-down`: `docker compose stop prometheus grafana`

### D.3. Provisioned Grafana dashboard

- `config/grafana/provisioning/datasources.yml` auto-adds the Prometheus datasource pointing at the in-compose `prometheus:9090`.
- `config/grafana/provisioning/dashboards.yml` auto-loads the bundled dashboard JSON.
- `config/grafana/dashboards/bffi-pipeline.json` carries the dashboard (~12 panels):
  - **Row 1 — Per-stage progress**: one panel per active stage with entity count + throughput sparkline.
  - **Row 2 — M9 detail**: per-phase progress (phase1/phase2/phase3) + tier-outcome stacked bar (local / lexical / llm / fallback / no_candidate / watchdog-aborted).
  - **Row 3 — Health + watchdog**: dependency state timeline (Fuseki / mlx-lm primary / mlx-lm fallback / Finto) + watchdog event rate over time.
  - **Row 4 — Summary stats**: total wall-time elapsed + ETA + picker-cache hit rate (when P-10 Phase B's cache lands).
- Dashboard JSON is committed in human-readable format (preserved formatting) so changes are review-friendly.

### D.4. Runbook docs

New file: `docs/observability.md` documents the metric vocabulary + the dashboard panel set. Future panel additions extend this schema, not re-derive it.

Extend `docs/runbook.md` with a section on starting the local observability stack: `make observability-up`, point a browser at `http://localhost:3001`, drill into panels.

### D.5. Tests

- Unit: `serve-metrics` correctly updates each metric when fed a synthetic event stream. No real HTTP server in unit tests; assert against the `prometheus_client` registry's snapshot.
- Unit: the exporter handles a partial-line sidecar tail (the file is being written to as we read) without crashing on a truncated last line.
- Smoke: `make observability-up && curl localhost:9091/-/healthy && curl localhost:3001/api/health` confirms the stack starts cleanly. Not a unit test; documented as an operator-side smoke in the runbook.
- The Grafana dashboard JSON has a schema-validation test (load with `json.loads`, assert top-level shape).

### D.6. Acceptance

- [ ] `bffi-pipeline serve-metrics --port 9100` starts, tails the sidecar, exposes `/metrics`.
- [ ] `make observability-up` starts both containers; both health endpoints respond.
- [ ] Pointing a browser at `http://localhost:3001` shows the provisioned dashboard with at least one populated panel (the M9 progress stat).
- [ ] During a 5k re-run of the pipeline with `serve-metrics` running, the dashboard's per-stage progress panel updates within the 5 s scrape interval.
- [ ] `docs/observability.md` documents every metric the exporter emits.

### D.7. Rollback

- `make observability-down` stops the containers; the pipeline continues to run without them.
- `git revert` the Phase D commit removes the docker-compose entries + the Grafana provisioning + the `serve-metrics` CLI. Phase A's emission stays intact; Phase B's status CLI still works.

---

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Phase A — sidecar growth across runs balloons unbounded | Low | Default cadence is sparse (~64 M9 events / 5k run = ~10 KB). At 800k corpus that's ~1.6 MB / run. Bounded; no rotation needed. If the operator wants per-run isolation, name the sidecar `stage-events-<run-uuid>.jsonl` (one-line change). |
| Phase A — emitting to JSONL on hot paths slows the stage | Low | Cadence is per-N items, not per-item. M9's worst case: 64 emits over ~90 min = once per ~90 s. Negligible. |
| Phase B — ETA is wildly wrong when throughput is variable | Medium | Phase 1 of P-10 Phase C showed Fuseki SPARQL latency at 50ms–11s. Status CLI surfaces median + p95 throughput when variance is >2× ratio; operator learns to read it. |
| Phase C — health probes time out and look like outages on transient hiccups | Medium | 5 s timeout per probe; one transient timeout emits `status="degraded"`, not `down`. Consecutive failures across N probe intervals would be needed to surface a real outage in the dashboard panel. |
| Phase D — `prometheus_client` import slows non-exporter pipeline runs | Effectively zero | Exporter is a separate CLI subcommand; pipeline stages never import `prometheus_client`. Plus Phase A's emission is also unconditional — no import-cost differential. |
| Phase D — Grafana / Prometheus image version drift breaks the provisioned dashboard JSON | Medium | Pin images sharply (`prom/prometheus:v2.55.0`, `grafana/grafana:11.4.0`). The dashboard JSON is reviewed on image bump. |
| Phase D — Anonymous Grafana access exposes the dashboard if the operator port-forwards `3001` | Low (operator-controlled) | Documented in runbook: `localhost:3001` is read-only anonymous; if the operator forwards the port outside their machine, they make the decision to. |
| Phase D — Prometheus + Grafana memory pressure on the M2 Max dev box | Low | Default Prometheus retention is 15 days at moderate cardinality. Even at 1k samples / 5 s scrape, total RSS stays well under 500 MB; together with Grafana's ~200 MB, the two containers add <1 GB on top of the existing Fuseki + Skosmos stack. |

## Open issues to close before / during execution

- **Run-UUID generation**: `uuid.uuid4()` vs `ULID`. ULID is time-sortable which helps multi-run analysis in Grafana. Lean ULID; one-line dep (already vendored via rdflib's transitive deps? check at Phase A time).
- **Phase D dashboard JSON formatting**: Grafana exports tightly-minified JSON; we want a readable diff-friendly form. Pre-commit hook to `jq --indent 2` it? Or commit Grafana's native export and accept the noisy diffs. Lean the pretty-printed form.
- **Whether to add a `--observability/--no-observability` global CLI flag** that flips the emitter off for runs that don't want sidecar writes (e.g. one-off small tests). Lean *yes*; defaults on, operators turn off when noise matters.
- **Skosmos config**: existing `bffi-skosmos` Apache container also runs on `:9090`. The new Prometheus container claims host `:9091`; verify no port collisions across all four containers.

## Cross-references

- [`docs/proposals/prop-11-structured-observability.md`](../../proposals/prop-11-structured-observability.md) — source proposal; the policy note at the top settles the `CLAUDE.md` "no telemetry" scope question this plan acts on.
- [`src/bffi_pipeline/stages/watchdog.py`](../../../src/bffi_pipeline/stages/watchdog.py) — the closest analog. `emit_watchdog_event`'s signature is the shape Phase A's `emit_stage_event` generalises.
- [`scripts/run-full-pipeline.sh`](../../../scripts/run-full-pipeline.sh) — current `STAGE_*` filter pattern; the new `STAGE_EVENT` prefix slots in alongside.
- [`docker-compose.yml`](../../../docker-compose.yml) — current Fuseki + Skosmos stack that Phase D.2 extends.
- [`docs/runbook.md`](../../runbook.md) — operator workflow doc that gains Phase B + Phase D.4 sections.
- `docs/observability.md` (new in Phase D.4) — metric vocabulary + dashboard panel set reference.
- `CLAUDE.md` § "Operating constraints" — the post-`30cd82a` clarified telemetry-policy text.
- P-10 perf snapshots (Phase A, A2, in-flight Phase C) — the runs whose "what's the progress?" debugging surfaced the gap P-11 addresses.
