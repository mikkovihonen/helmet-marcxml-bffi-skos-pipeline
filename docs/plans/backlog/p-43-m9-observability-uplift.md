# P-43 — M9 observability uplift: per-vocab outcomes, cache hit rate, picker confidence, Phase-1 timing

**Status**: backlog. Drafted 2026-06-05 after Phase B / YSO-Paikat investigation showed several silent failure modes that the current M9 metrics couldn't catch:

- A SPARQL unbound-variable bug in
  `bffi_pipeline.stages.m9.candidate_context.FusekiCandidateContextFetcher`
  triggered cartesian explosions against ~290 k cross-graph
  prefLabels. Wall-time symptoms (Phase 1 running for hours, no
  picker progress) were visible only by inspecting individual
  process state — no metric flagged it.
- A 413-entry persistent picker cache from prior runs hid 193
  picker outcomes behind cache hits, making the run's audit log
  appear empty (1 row) even though the M9 stage counters reported
  204 picker invocations. No metric distinguished fresh-fire from
  cache-hit.
- Phase B's three prompt iterations (v3 → v4 → v3+anchor) emitted
  visibly different confidence distributions (avg 0.943 / 0.918 /
  0.890) but the existing counters only count *outcomes*, not
  *confidences*. The v4 over-hedging would have been catchable
  from a confidence histogram before the smoke completed.
- YSO-Paikat was loaded into Fuseki but the Finto REST client
  queried only `vocab=yso`, so place candidates missed tier-1. The
  fix lifted tier-1 lexical hits 10× (28 → 268). No metric showed
  "which vocab is contributing to tier-1 binds", so the gap was
  invisible until a cataloguer noticed missing place candidates.

**Plan-base commit**: `aaeabc6`. Before executing, run
`git diff aaeabc6..HEAD -- src/bffi_pipeline/observability/ src/bffi_pipeline/stages/m9/ config/grafana/dashboards/bffi-pipeline.json`
to confirm no in-flight work has reshaped the observability or M9
surface this plan touches.

**Phase commits**:

- Phase A (Grafana dashboard refresh policy + sort-newest-first): `<unfilled>`
- Phase B (per-vocab outcome + cache hit/miss counters): `<unfilled>`
- Phase C (picker confidence histogram + Phase-1 timing): `<unfilled>`
- Phase D (Fuseki SPARQL fetch counters): `<unfilled>`

**Owner**: TBD.

**Estimated wall-time**: 1-2 days total. Phase A is ~30 min (one
dashboard field tweak + one sort field). Phase B is ~half-day
(extend the outcome counter labels + thread cache-hit signal up
from `apply_reconciliation`). Phase C + D are ~half-day each
(Prometheus histogram + Fuseki request hooks).

**Sequencing prerequisites**:

- None hard. Each phase is independently shippable.
- Phase B's per-vocab labelling assumes M9's
  `_KIND_TO_FINTO_VOCABS` change has landed (committed during the
  YSO-Paikat investigation, currently uncommitted on disk). Land
  that with a separate commit before starting Phase B.

## Motivation

Today's M9 observability is **outcome-shaped, not diagnosis-
shaped**. The exporter publishes per-stage counters keyed by
``(run_uuid, stage, outcome)`` — enough for the dashboard's
"how many records resolved at tier-N?" panels, but not enough
for any of the failure modes the Phase B / YSO-Paikat
investigation surfaced:

| Failure mode | Time-to-detect today | Time-to-detect with this plan |
|---|---|---|
| Unbound-OPTIONAL SPARQL cartesian explosion | hours (manual `lsof` + `ps`) | minutes (Fuseki avg latency > 1 s triggers alert) |
| Stale picker cache masking 95 % of picker fires | only by reading `picker-decisions.jsonl` | live (cache hit-rate panel) |
| Over-hedging prompt regression (v4) | full 90-min smoke + post-hoc rationale read | 5 min after Phase 2 starts (confidence histogram skew) |
| Tier-1 misses for a whole vocab subset (YSO-Paikat) | weeks (cataloguer notices) | hours (per-vocab tier-1 rate diverges from peers) |
| Phase 1 wall-time blow-up | only by tailing `stage-events.jsonl` for missing progress events | live (Phase 1 seconds gauge) |

The metric model already supports labels — we just need to extend
counter sets with the right dimensions and add a histogram for
confidences.

## Out of scope (deliberately not done here)

- **Adding new event TYPES** beyond what `stage-events.jsonl`
  already carries. This plan layers metrics onto the existing
  events (start / end / phase_boundary / progress / health). New
  event types would need a sidecar-schema bump that's bigger than
  the observability lift justifies.
- **Per-record latency tracing.** A per-record histogram of LLM
  call duration is tempting but would explode cardinality
  (`run_uuid × stage × kind × outcome` is already 6-8 series per
  metric; per-record would add thousands). The picker confidence
  histogram in Phase C is the bounded compromise: distribution
  signal without per-record cardinality.
- **Alerting rules in Prometheus.** This plan adds the
  metrics; alert thresholds and PromQL queries go in a follow-up
  once the operator has lived with the new metrics for a few
  runs and the baseline is known.
- **Cross-run comparison panels.** The dashboard today is
  single-run focused (`active_run` template variable). A
  comparison view across the last N runs is valuable but out of
  scope — sequence as a separate dashboard work item.
- **VIAF + non-Finto authority latency.** Phase D's Fuseki
  counter covers the local-Fuseki path; the external Finto REST
  API hits don't get the same treatment because the operator
  doesn't control their latency anyway.

## Definition of done

### Phase A — Dashboard refresh policy + newest-first sort

- [ ] `config/grafana/dashboards/bffi-pipeline.json` template variable
      `active_run`: change `"refresh": 1` (OnDashboardLoad) →
      `"refresh": 2` (OnTimeRangeChanged) so the dropdown re-queries
      Prometheus on every time-range change.
- [ ] Same variable: change `"sort": 2` (alphabetical ascending) →
      `"sort": 6` (case-insensitive descending) so the new
      timestamp-prefixed run-ids (`YYYYMMDD-HHMM-<6hex>` per
      `Settings._post_init` as of commit `<run-id-scheme-commit>`)
      land at the top of the dropdown.
- [ ] Verify by: launch a fresh smoke, open dashboard with a
      pre-existing browser tab (don't reload), confirm the new
      run appears at the top of the dropdown after the dashboard's
      auto-refresh ticks.

### Phase B — Per-vocab outcome + cache hit/miss counters

#### B.1 Per-vocab outcome label

- [ ] Extend `bffi_stage_outcomes_total` (currently labelled by
      `outcome`) with a `vocab` label for M9 records. Values:
      `yso | yso-paikat | yso-aika | finaf | kauno | muso | viaf |
      slm | kaunokki | lcsh | lcgft | childrensSubjects | -` (the
      last for outcomes where vocab isn't meaningful: `fictional`,
      `no_candidate`).
- [ ] Source: the outcome's `chosen_uri` (when bound) maps to a
      vocab via the prefix → vocab table in
      `bffi_pipeline.stages.m10.load_finto.FINTO_VOCABS`. When
      `chosen_uri is None`, emit `vocab="-"`.
- [ ] Backward compatibility: callers that aggregate by stage
      `sum by (stage)` continue to work; the new label is
      additive.

#### B.2 Picker cache hit/miss counter

- [ ] New counter `bffi_m9_picker_cache_total{run_uuid, kind,
      result="hit|miss"}`. Increment in
      `apply_reconciliation` at the cache lookup site
      (`src/bffi_pipeline/stages/m9/apply.py` near
      `picker_cache.get(...)`).
- [ ] Hit-rate panel on the Grafana dashboard:
      `rate(bffi_m9_picker_cache_total{result="hit"}[5m]) /
      rate(bffi_m9_picker_cache_total[5m])`. Useful both during
      live runs and post-hoc.
- [ ] Wired so a stale cache directory immediately shows as
      hit-rate > 90 %, prompting `rm
      runs/<uuid>/reconcile-cache.sqlite` before re-running.

#### B.3 Tests

- [ ] `test_metrics_exporter.py`: extend the per-stage outcome
      test to assert the new `vocab` label is present and carries
      the right value for a fixture outcome.
- [ ] `test_apply_reconciliation.py` (or wherever the cache-hit
      path is tested today): assert the cache hit/miss counter
      increments in the expected branches. Stub-driven; no real
      Prometheus required.

### Phase C — Picker confidence histogram + Phase-1 timing

#### C.1 Picker confidence histogram

- [ ] New Prometheus histogram
      `bffi_m9_picker_confidence{run_uuid, kind, llm_decision}`.
      Buckets: `[0.0, 0.5, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0]`.
- [ ] Observe in `_decide_with_pick` (or the picker call site)
      after the LLM returns. One observation per fresh picker
      call (skip cache hits — they're the cached confidence,
      already counted under their original run).
- [ ] Grafana heatmap panel showing confidence distribution over
      time within the active run. The Phase B v4 over-hedging
      regression would have shown as a left-shifted distribution
      ~15 minutes into M9.

#### C.2 Phase-1 / Phase-2 / Phase-3 timing gauges

- [ ] New gauges
      `bffi_m9_phase_elapsed_seconds{run_uuid, phase}`. Set at
      each phase's `end` event (Phase 1 / Phase 2 / Phase 3).
      Source: existing `phase_boundary` events have the
      timestamps; just compute and emit.
- [ ] Stacked-bar panel on the dashboard breaks M9's total wall
      time into phase contributions. Today's smoke had M9 at
      ~21 min with Phase 1 dominating because of the
      candidate-context SPARQL — this panel would have made that
      visible at the first run, not after a separate
      investigation.

#### C.3 Tests

- [ ] Histogram observation test: drive
      `_decide_with_pick` with a fixture pick at confidence
      0.83, assert the histogram's `le="0.85"` bucket
      incremented and `le="0.8"` did not.
- [ ] Phase-timing test: emit fake `phase_boundary` events with
      controlled timestamps, assert the gauge values match the
      expected deltas.

### Phase D — Fuseki SPARQL fetch counters

#### D.1 SPARQL request counter + latency histogram

- [ ] Wrap `FusekiCandidateContextFetcher._post_sparql` and
      `FusekiConceptResolver._post_sparql` with metrics:
      - `bffi_m9_fuseki_requests_total{run_uuid, fetcher, result="ok|error|empty"}`
      - `bffi_m9_fuseki_request_seconds{run_uuid, fetcher}` histogram.
- [ ] Buckets:
      `[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]` —
      seconds. The unbound-cartesian bug had a 4 s+ tail at
      large input sizes; the panel would have flagged a tail
      shift instantly.
- [ ] Grafana panel: P50 / P95 / P99 SPARQL latency per
      fetcher. Useful both as a live alert (Fuseki under heavy
      load from a misshapen query) and post-hoc (compare a
      regression against a known-good baseline).

#### D.2 Tests

- [ ] Counter increments on both the success path and the
      `httpx.HTTPError` / empty-result paths.
- [ ] Latency histogram observation runs without sampling the
      real Fuseki — inject a stubbed clock + stubbed
      `http_client`.

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Per-vocab label explodes cardinality at the 800 k corpus | Low | Medium | The label set is bounded (≤ 13 vocabs); cardinality is `run_uuid × stage × outcome × vocab ≤ ~150` series, well within Prom's healthy zone |
| Histogram observation in the picker hot path slows M9 | Very low | Low | `prometheus_client.Histogram.observe` is a microsecond-scale operation; LLM calls take 10 s each, so the relative cost is negligible |
| Cache-hit counter wired in the wrong branch | Low | Medium | Phase B.3 tests pin the exact branch + a smoke against a known cache-warm dir confirms hit rate matches expectations |
| Dashboard sort change confuses operators who have a saved view | Very low | Very low | Saved views aren't an active workflow; the change is a one-line note in the next-stand-up |
| The proposed Phase C histogram doesn't catch every prompt regression | Medium | Low | Confidence distribution is one signal of N. The plan doesn't claim to catch every regression — just the over-hedging / over-confident class that v4 produced |

## Rollback procedure

Per phase:

- **Phase A**: revert the dashboard JSON change. Single-file edit;
  `git revert <commit>` is clean.
- **Phase B / C / D**: each phase adds new metrics without
  modifying existing ones, so rollback is a `git revert` per
  phase. No metric removed needs Prometheus retention
  considerations (only old samples linger; they don't break
  queries).

## Cross-references

- Phase B (the Phase B candidate-context investigation, June 2026)
  uncovered most of the failure modes this plan addresses. The
  investigation transcript lives in the chat record + the
  shipped commits `2103de3` (M9 URI dedup), `aaeabc6` (Phase A
  Work-context picker prompt), and the uncommitted Phase B
  branch on disk as of 2026-06-05.
- The current dashboard JSON is at
  `config/grafana/dashboards/bffi-pipeline.json`. Panel 28
  ("Run artifacts") already links to the cataloguer-review
  bundle; the Phase A / B panels added here slot above it on
  the M9 row.
- The metrics exporter lives at
  `src/bffi_pipeline/observability/exporter.py` (entrypoint
  `bffi-pipeline serve-metrics`). The `--watch-glob` rescan
  cadence is 30 s by default; that's fine for run-discovery
  but doesn't affect intra-run metric freshness.
- `src/bffi_pipeline/stages/m10/load_finto.py` carries the
  vocab → graph-URI table that Phase B.1 reuses for the
  outcome→vocab mapping.
