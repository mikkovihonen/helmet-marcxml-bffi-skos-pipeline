# P-11 — Structured pipeline observability for long unattended runs

**Status**: planning (graduated). See
[`docs/plans/backlog/p-11-structured-observability.md`](../plans/backlog/p-11-structured-observability.md)
for the executable plan with sub-step detail, acceptance gates, and rollback procedures per phase.
**Scope**: 3-4.5 days. Phase A (structured event emission from every stage) is 1-2 days. Phase B (`bffi-pipeline status` CLI that tails the event sidecar) is half a day. Phase C (dependency health probes wired into the same event stream) is half a day. Phase D (`bffi-pipeline serve-metrics` Prometheus exporter + provisioned Grafana dashboard) is 1-1.5 days. Each phase is independently shippable; A is the prerequisite for B / C / D, but B-C-D can land in any order after A.

> **Policy note** — `CLAUDE.md` § "Operating constraints" reads "No telemetry / error reporting." Confirmed scope: this refers to *outbound* monitoring to external services (Datadog, Sentry, Honeycomb, etc.). Running an observability stack **locally in a container** — Prometheus scraping `localhost:9100`, Grafana querying the local Prometheus — is in-scope: no data leaves the operator's machine. The original "lean defer until policy is settled" hedge in the open-questions section has been dropped; Phase D ships.
**Proposal-base commit**: `8e47a69`. To gauge drift before acting,
run
`git diff 8e47a69..HEAD --
src/bffi_pipeline/stages/
src/bffi_pipeline/cli.py
scripts/run-full-pipeline.sh
docs/runbook.md`.

## Motivation

P-10's bench iterations surfaced the gap. During the Phase C bench's 50+ minute Phase 1, answering one question — "is M9 making forward progress?" — required composing five unrelated tools:

| Tool | What it answered |
|---|---|
| `ps -p <reconcile-pid> -o etime,%cpu,rss` | Process alive? CPU-active? |
| `grep -c 'POST /v1/chat' /tmp/mlx-lm-8001.log` | How many picker calls completed? |
| `curl -sf -m 2 http://localhost:3030/$/ping` | Fuseki responsive? |
| `docker logs bffi-fuseki 2>&1 \| grep -c '200 OK'` | How many SPARQL queries served? |
| `stat data/provenance.ttl` | Are any decisions landing? |

For a one-shot dev-laptop bench, that's a 60-second triage. For a 12-hour overnight run on the production M5 Max against the 800k corpus, the operator either babysits the terminal or accepts that "stuck vs. slow" can't be distinguished without manual probing. Neither is appropriate for the unattended-batch posture the pipeline is supposed to support.

Today's observability surface is real but fragmented:

- **`STAGE_*` stderr prefix** — convention referenced in `scripts/run-full-pipeline.sh`'s log-tail filter. Emits stage banners, no progress events.
- **`WATCHDOG_EVENT` stderr prefix + sidecar** (P-03 + P-10 Phase A) — structured events, but scoped to LLM timeouts only.
- **Per-stage `Summary.render()` text blocks** — pasted at end-of-stage, after the stage is done. Not visible mid-run.
- **Sidecar JSONLs** (`helmet-map.jsonl`, `judge-decisions.jsonl`, `embed-candidates.jsonl`, `canonical-conflicts.jsonl`, `canonical-map.jsonl`, `watchdog-events.jsonl`) — each authoritative for its stage, but heterogeneous shape, no unified consumer.
- **Provenance graph** (`provenance.ttl`) — RDF-typed Activity records per spec § 8. Authoritative but designed for forensic audit, not real-time tail.

P-11 doesn't replace any of these — they're each load-bearing for their existing consumers. It adds **one canonical event stream** that the others feed into, so an operator (or a follow-up Grafana dashboard, if we ever want one) can answer "where is the pipeline now?" from a single source.

## Approach

Four phases. Phase A is the prerequisite for B / C / D; B-C-D ship independently after A and can be done in any order. The dashboard consumer (Phase D) is what justifies the per-stage emission overhead — without it, the event stream is plumbing without a long-running consumer.

### Phase A — Structured stage events on stderr + sidecar

- Extend the existing `STAGE_*` stderr-prefix convention with a sibling `STAGE_EVENT ` prefix carrying one JSON object per line. Same regex broadening pattern the watchdog uses (`^(STAGE_|PIPELINE_|WATCHDOG_EVENT|STAGE_EVENT)`).
- Sidecar at `<BFFI_DATA_DIR>/stage-events.jsonl`, one JSON object per line. Each emission writes to **both** stderr and the sidecar so log-tail tooling and after-the-fact analysis share one source.
- Event shape (deliberately small to minimise per-record overhead):

  ```json
  {
    "ts": "2026-05-13T05:13:36Z",
    "stage": "m9",
    "event": "progress",
    "phase": "phase1",
    "counters": {"processed": 9876, "total": 12666},
    "extra": {"tier0_local": 7421, "no_candidate": 1893}
  }
  ```

- Event vocabulary (Literal, mirrors the watchdog enum's shape):
  - `start` — emitted at stage entry; carries the stage's input cardinality where known.
  - `progress` — emitted every N items (per-stage default — M9 every 200 entities, M6 every 25 pairs, M2 every 100 records).
  - `phase_boundary` — emitted at internal phase transitions (e.g. M9 Phase 1 → Phase 2).
  - `end` — emitted at stage exit; carries the per-tier summary counters.
  - `health` — emitted by Phase C (see below); not a progress event but rides the same stream.
- Implementation: one tiny `emit_stage_event(...)` function in a new `stages/observability.py` (mirroring the watchdog module's pattern); each stage calls it. No new dependencies.
- All existing sidecars (`watchdog-events.jsonl`, `helmet-map.jsonl`, etc.) stay where they are. P-11 doesn't migrate them — it adds an *additional* sidecar that summarises across them.

### Phase B — `bffi-pipeline status` CLI

A tiny CLI subcommand that reads the event sidecar and renders a live or one-shot summary.

- `bffi-pipeline status` — prints once, exits. Shows the latest `start` per stage, the latest `progress` counters, elapsed time, ETA (computed from items/sec), and recent watchdog events if any.
- `bffi-pipeline status --tail` — `tail -F`-style follow. Re-renders the summary on each new event. Cheap text (no curses); the existing `summary.render()` style of paste-ready output applies.
- `bffi-pipeline status --since <iso-timestamp>` — filter for after a run started; useful when the sidecar accumulates across runs.

Example output:

```
M9 reconcile (started 2026-05-12T20:09:00, elapsed 50m23s)
  phase1   ████████████░░░  9876 / 12666  (78%, ~14m to phase boundary at observed rate)
  phase2   waiting
  health   Fuseki ok (last probe 23ms), mlx-lm ok (last probe 12ms)
  watchdog 0 field_budget_exceeded events
```

ETA is best-effort: observed-rate extrapolation, no fancy modeling.

### Phase C — Dependency health probes

Every stage that depends on an external service (Fuseki, mlx-lm, Finto) emits a `health` event at stage entry **and** every N progress events thereafter. The probe is a one-shot `httpx.Client` call with a tight (5s) timeout; failures don't fail the stage, they just surface as `health` events with `status="degraded"`.

Health-event subjects:
- **M9**: `fuseki`, `mlx-lm`, `finto`.
- **M3** (if cascade enabled): `mlx-lm`.
- **M6**: `mlx-lm` (primary + fallback ports).
- **M10/M11 (load)**: `fuseki`.

The probes piggy-back on the same `httpx.Client` the stage already uses, so the overhead is one tiny request per probe interval — negligible against the production-scale wall-times.

### Phase D — Prometheus exporter + provisioned Grafana dashboard

The dashboard consumer that earns the per-stage emission overhead. For a 12-hour overnight run on the production M5 Max, an operator who's stepped away from the terminal needs to glance at a browser tab and immediately see: is each stage making forward progress, are throughputs steady, are any dependencies degraded, are watchdog events accumulating?

Architecture (all local, no outbound telemetry):

```
pipeline stages  ─emit─▶  stage-events.jsonl  ─tail─▶  serve-metrics  ─scrape─▶  Prometheus  ─query─▶  Grafana
                            (Phase A)                  (Phase D.1)             (Phase D.2)         (Phase D.3)
```

The pipeline itself stays unchanged from Phase A's POV: it emits events to the sidecar. The metrics exporter is a separate optional process that an operator launches alongside the pipeline; Prometheus + Grafana run as Docker containers next to the existing `bffi-fuseki` + `bffi-skosmos` services.

**D.1 — `bffi-pipeline serve-metrics --port 9100`**
- New CLI subcommand. `tail -F`-style follow on `<BFFI_DATA_DIR>/stage-events.jsonl`; updates a set of `prometheus_client` counters / gauges in process.
- Exposes the standard `/metrics` endpoint on the configured port (default `9100`).
- Designed to run for an arbitrarily long time independent of the pipeline — survives stage transitions, pipeline restarts.
- Reads the *full* sidecar at startup to rehydrate counters from a prior pipeline invocation, then tails forward.
- Metric vocabulary (Prometheus-named; one-to-one with the Phase A event payload):
  - `bffi_stage_started_timestamp{stage="m9"}` — gauge, unix seconds.
  - `bffi_stage_entities_total{stage="m9", phase="phase1"}` — gauge.
  - `bffi_stage_entities_processed_total{stage="m9", phase="phase1"}` — counter.
  - `bffi_stage_outcomes_total{stage="m9", outcome="local|lexical|llm|fallback|no_candidate|fictional|watchdog_aborted"}` — counter.
  - `bffi_stage_throughput_per_minute{stage="m9", phase="phase1"}` — gauge derived from last-N progress events.
  - `bffi_stage_eta_seconds{stage="m9", phase="phase1"}` — gauge.
  - `bffi_dependency_health{dep="fuseki|mlx-lm|finto", port="8001|8002|3030"}` — gauge: `2` healthy, `1` degraded, `0` down.
  - `bffi_dependency_probe_latency_ms{dep, port}` — gauge.
  - `bffi_watchdog_events_total{stage, event="timeout|retry|field_budget_exceeded|pair_budget_exceeded|give_up"}` — counter.

**D.2 — Docker Compose extension**
- Add `prometheus` + `grafana` services to the existing `docker-compose.yml` (which already runs `bffi-fuseki` + `bffi-skosmos`).
- `prometheus`: image `prom/prometheus:v2.x` pinned. Volume mount for `config/prometheus.yml` (scrape config: one job pointing at `host.docker.internal:9100`, scrape every 5s).
- `grafana`: image `grafana/grafana:11.x` pinned. Anonymous read access enabled (no auth — local-only deployment). Volume mounts for provisioning + dashboard JSON.
- Ports: `prometheus:9091` (host) → 9090 (container); `grafana:3001` (host) → 3000 (container). Avoid the existing Skosmos `:9090`.
- `make observability-up` Makefile target: `docker compose up -d prometheus grafana`. `make observability-down` for symmetry.

**D.3 — Provisioned Grafana dashboard**
- One JSON dashboard at `config/grafana/dashboards/bffi-pipeline.json`, auto-loaded via `config/grafana/provisioning/dashboards.yml`.
- Datasource auto-configured via `config/grafana/provisioning/datasources.yml` pointing at the in-compose Prometheus service.
- Panel set (single dashboard, ~12 panels in two rows):
  - **Row 1 — Pipeline progress** (one panel per stage in flight):
    - M2: entity gauge + throughput sparkline.
    - M3: same shape.
    - M5: same shape.
    - M6: pair count + throughput + watchdog-event tally.
    - M8: entity gauge + canonical-Work tally.
    - M9: per-phase progress (phase1 / phase2 / phase3) + tier-outcome stacked bar (local / lexical / llm / fallback / no_candidate).
  - **Row 2 — Health + auxiliary**:
    - Dependency state timeline (Fuseki / mlx-lm primary / mlx-lm fallback / Finto).
    - Watchdog event rate over time (5-minute rate, grouped by event type).
    - Picker cache hit rate (when P-10 Phase B's `reconcile-cache.sqlite` ships — gauge updated from the cache hit/miss event payload).
    - Total wall-time elapsed (single stat) + ETA to next stage boundary.
- Dashboard ships read-only in Grafana's provisioned mode so operators get a consistent default; they can clone-and-edit if they want a custom view.

**D.4 — Operator runbook section**
- Document the `make observability-up` workflow in `docs/runbook.md`: when to run it (alongside the pipeline; can stay up across runs), where to point the browser (`http://localhost:3001`), how to drill into a panel.
- Document the metric vocabulary in `docs/observability.md` (new file) so the schema is reviewable and future panels can extend it without re-deriving.

## Prerequisites

- The P-10 plan is in flight (Phase A, A2, C shipped; Phase B picker cache outstanding). Phase A's `WATCHDOG_EVENT` infrastructure is the closest analog and is the reference shape for P-11.
- No active plan touches `stages/*.py`'s public surface beyond what P-10 has already changed (checked at `docs/plans/in-progress/`).
- Operator runbook (`docs/runbook.md`) will get a new section on `bffi-pipeline status` (Phase B) and one on `make observability-up` (Phase D.4).
- Phase D adds `prometheus_client` to the Python dependency set (one new OSS pin, no telemetry — strictly local `/metrics` exporter). Prometheus + Grafana are pulled as pinned Docker images, no host installs.

## Risks

- **Per-record emission overhead at scale**: every progress event writes one stderr line plus one JSONL line. At an emission cadence of 1-per-200 entities and 12 666 entities, that's 64 events for M9 — negligible. The cadence is per-stage tunable for hot paths; if M5 needs it, lower it to 1-per-1000.
- **Sidecar growth across runs**: stage-events.jsonl accumulates. Mitigation: rotate per run (file named `stage-events-<run-uuid>.jsonl`), or truncate-on-stage-start when the operator passes `--reset-observability`. Latter is simpler; runs that share a JSONL are debugging-friendly.
- **`bffi-pipeline status` lying about ETA**: if a stage's per-item throughput is highly variable (Phase C's bench showed individual Fuseki SPARQLs at 50ms–11s), the linear-extrapolation ETA can be wildly wrong. Mitigation: surface throughput variance (median + p95) in the status output, not just a point ETA. Operators learn to read it.
- **Health-probe failures during transient hiccups**: a single 5s probe timeout shouldn't trigger an alarm. Mitigation: emit `health` with `status="degraded"` and recovery-on-next-probe; status CLI renders consecutive degradeds before flagging.
- **Two-source-of-truth concern with the watchdog sidecar**: `WATCHDOG_EVENT` events also matter to observability. Mitigation: P-11 leaves `watchdog-events.jsonl` in place but the new `stage-events.jsonl` *includes* a copy of each watchdog event (`event="watchdog"`, payload nested) so the status CLI doesn't need to read two files. Forensic audit still uses the dedicated `watchdog-events.jsonl`.
- **Phase D — Prometheus / Grafana image drift**: pinning Docker images to specific minor versions (e.g. `prom/prometheus:v2.55.x`, `grafana/grafana:11.4.x`) bounds the surface; provisioned dashboard JSON occasionally needs schema-version bumps when Grafana majors land. Mitigation: pin sharply, re-test on bump.
- **Phase D — `prometheus_client` import overhead**: the exporter is opt-in via a separate CLI subcommand, so non-exporter pipeline runs don't pay for the import. No effect on the hot-path stages.
- **Phase D — Anonymous Grafana access**: provisioned with `GF_AUTH_ANONYMOUS_ENABLED=true` because the deployment is local-only. If the operator forwards port 3001 outside their machine, that's a security decision the runbook flags explicitly.

## Open questions

- **Event-cadence tuning per stage**: the proposal picks defaults (M9 every 200, M6 every 25, M2 every 100). These should be revisited against actual run timings — too sparse and the status CLI looks frozen; too dense and the sidecar bloats. Phase A picks a starting set; the first bench-with-status confirms.
- **`run-uuid` per pipeline invocation**: should each `bffi-pipeline` invocation carry a `run_uuid` that anchors all events? Helps multi-run analysis on the Grafana side (filter to current run vs all-time). Adds one CLI option (or auto-generated). **Lean yes**; cheap and unlocks per-run filtering on the dashboard.
- **Dashboard panel set after the first overnight bench**: the 12-panel set sketched in D.3 is a first cut. The first real overnight run on the 800k corpus will surface which panels operators actually use; the dashboard JSON evolves from there. The panel set is data-driven, not committed in stone.
- **Alternative — keep grep-the-logs as the working interface**: rejected. The five-tool composition demonstrated above doesn't fit the unattended-batch use case the pipeline is built for. Even one operator-side `bffi-pipeline status` is strictly better; the dashboard is the next-level lift.
- **Alternative — static-HTML dashboard served by `serve-metrics` directly** (no Prometheus, no Grafana): considered. Cuts two Docker services from the stack, no metric-vocabulary learning curve. **Rejected** because: no historical time-series view (which matters for "why did throughput drop at hour 4 of the overnight run?"), no panel/alert ergonomics, and operators familiar with Prometheus + Grafana from any other ops context get instant onboarding. The two-container Docker overhead is bounded.

## Cross-references

- [`src/bffi_pipeline/stages/watchdog.py`](../../src/bffi_pipeline/stages/watchdog.py) — the closest analog (structured event emission to stderr + sidecar JSONL), the shape P-11 generalises.
- [`scripts/run-full-pipeline.sh`](../../scripts/run-full-pipeline.sh) — current log-tail / `STAGE_*` filter pattern that P-11's stderr prefix slots into.
- [`docs/runbook.md`](../../docs/runbook.md) — operator-facing documentation where `bffi-pipeline status` (Phase B) and `make observability-up` (Phase D.4) land.
- `docs/observability.md` — new file (Phase D.4) documenting the metric vocabulary and dashboard panel set so future extensions extend the schema rather than re-derive it.
- `docker-compose.yml` — current Fuseki + Skosmos stack that Phase D.2 extends with Prometheus + Grafana.
- `config/grafana/dashboards/bffi-pipeline.json` + `config/grafana/provisioning/` — Phase D.3 artefacts that ship the default dashboard read-only.
- `config/prometheus.yml` — Phase D.2 scrape configuration.
- [`docs/performance/2026-05-12-5k-m2-max-phase-a.md`](../performance/2026-05-12-5k-m2-max-phase-a.md), [`-phase-a2.md`](../performance/2026-05-12-5k-m2-max-phase-a2.md), and the in-progress Phase C bench — the runs whose "what's the progress?" debugging surfaced the gap.
- `CLAUDE.md` § "Operating constraints" — "No telemetry / error reporting" policy. Scope confirmed as outbound-only (see the policy note at the top of this proposal); local containerised Prometheus + Grafana sit alongside the existing local Fuseki + Skosmos services without violating the constraint.
- Existing per-stage sidecars (`helmet-map.jsonl`, `judge-decisions.jsonl`, `embed-candidates.jsonl`, `canonical-conflicts.jsonl`, `canonical-map.jsonl`, `watchdog-events.jsonl`) — left in place; P-11 adds a meta-sidecar, doesn't replace any.
