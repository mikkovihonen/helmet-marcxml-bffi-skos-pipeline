# P-31 — Dashboard artifacts panel + per-run cataloguer review CSVs

**Status**: proposed.
**Scope**: 1-2 days end-to-end. Phase A (path gauges + dashboard panel): half a day. Phase B (source-review CSV): half a day. Phase C (target-review CSV): half a day. Phases ship independently.
**Proposal-base commit**: `b16ac16`. To gauge drift before acting, run
`git diff b16ac16..HEAD --
src/bffi_pipeline/stages/
src/bffi_pipeline/metrics_exporter.py
config/observability/grafana/`.

## Motivation

The dashboard today tells the operator about progress (counters, throughput, ETA) and failures-by-type (the M2+M3 failure-mode bargauge) but **doesn't tell them where the pipeline's outputs are or which records need cataloguer attention**. Two practical consequences:

1. **"Where did my files land?"** — every run the operator either remembers the `BFFI_DATA_DIR` they passed or `find`s the bench dir. The dashboard already knows the answer (it's reading `stage-events.jsonl` from there); surfacing the path as a panel saves a manual lookup.
2. **Cataloguer round-trip is JSONL-shaped, not spreadsheet-shaped.** Today the operator's "show me the records that failed" answer is *"open `bibframe/_errors.jsonl`, `bffi/_validation.jsonl`, `canonical-conflicts.jsonl` in your editor"*. Cataloguers live in spreadsheets — they want a CSV they can sort, filter, mark "reviewed" on, and hand back as the audit trail.

The split between the two failure surfaces is operationally important:

- **Source-system fixes** — bib_ids whose MARCXML needs correction in Sierra/Helmet. Failure modes: M2 boundary errors (missing 100/110, malformed 008), M3 SHACL failures (boundary 3), M9 reconcile failures (no-candidate / fictional). Fixing means re-cataloguing in Sierra and re-exporting.
- **Target-system review** — canonical Work / Expression URIs in the BFFI graph that need cataloguer attention in the target store (Skosmos / Fuseki). Failure modes: M8 conflicts (contradictory same/different decisions in one cluster), M9 low-confidence picks (fallback band), and — once the P-22..26 audit stack lands — flagged false-positive merge classes. Fixing means SPARQL updates or Skosmos editor changes, not re-cataloguing.

A single combined list collapses two different review workflows. Two CSVs keeps each cataloguer in the right tooling lane.

## Approach

Three phases. All three compose; A is a prerequisite for B and C's path-surfacing but the CSVs themselves write irrespective of A.

### Phase A — Path gauges + dashboard "Artifacts" panel

New low-cardinality Prometheus gauge:

```
bffi_artifact_path{kind, run_uuid, path, state} 1
```

- `kind` — small finite set: `bibframe_dir`, `bffi_dir`, `bffi_corpus`, `canonical_ttl`, `canonical_map`, `canonical_conflicts`, `cataloguer_source_csv`, `cataloguer_target_csv`, `provenance_ttl`, `judge_decisions`, `manifest`, plus future additions per stage.
- `path` — the resolved absolute path, carried as a label so the dashboard can render it.
- `state` — `expected` at run start (path may not exist yet), `present` once the file lands. Lets the dashboard distinguish "M3 will write `bffi-corpus.ttl` at /X" from "M3 has written `bffi-corpus.ttl` at /X".

Emit sites: each stage's `start` event triggers the `expected` rows for the artifacts the stage will produce; each stage's `end` event flips them to `present`. The first call to `emit_artifact_path(kind, path)` is also legitimate at pipeline init (for inputs like `bibframe_dir`).

Cardinality budget: ~12 kinds × active runs × 2 states = bounded by run count. At ten concurrent runs in the registry, that's ~240 time-series — well below the operator-friendly ceiling.

New Grafana panel: "Run artifacts" table, query `bffi_artifact_path{run_uuid="$active_run"}`. Columns: `kind`, `state`, `path`. The `path` column rendered as a Markdown link (`[path](file://path)`) so an operator-side click opens the file in their OS-default app.

### Phase B — Source-review CSV (per-stage append)

New helper `src/bffi_pipeline/cataloguer_review.py` exposing:

```python
def append_source_row(
    *,
    bib_id: str,
    stage: str,          # m2, m3, m9
    category: str,       # error_type / boundary-3 / no-candidate / fictional
    severity: str,       # blocking | warning | info
    details: str,        # short human-readable
    marcxml_path: str,
) -> None: ...
```

Each stage that already writes a per-record failure JSONL row also calls `append_source_row(...)` with the same data. Append target:

```
<BFFI_DATA_DIR>/cataloguer-source-review-<run_uuid>.csv
```

Header (written on first append; UTF-8 with BOM for Excel-friendliness):

```
run_uuid,bib_id,stage,category,severity,details,marcxml_path,flagged_at,reviewed_by,reviewed_at,notes
```

The trailing three columns (`reviewed_by` / `reviewed_at` / `notes`) are empty on write. The cataloguer fills them in during their pass and hands the CSV back as the audit trail.

Aggregation timing: per-stage append (each stage adds rows as it finds problems). Rationale per the design discussion: gives the cataloguer something to triage mid-run, avoids a slow "aggregate all errors" finalisation step at 800 k scale, and the dashboard's count-gauge ticks up live alongside the CSV growing on disk.

**Where rows come from** (initial wiring):

| Stage | Existing source data | Category values |
|---|---|---|
| M2 | `bibframe/_errors.jsonl` | `error_type` (e.g., `missing-100`, `parse-error`, `unmapped-language`) |
| M3 | `bffi/_validation.jsonl` | `boundary-3` (all rows by construction) |
| M9 | reconcile-stage outcomes | `no-candidate`, `fictional`, `watchdog-aborted` |

JSONL files stay on disk as the machine-readable surface; CSV is the cataloguer-friendly view.

### Phase C — Target-review CSV (per-stage append)

Same shape as Phase B, mirror helper:

```python
def append_target_row(
    *,
    canonical_work_uri: str,
    expression_uris: list[str],
    reason: str,         # m8-conflict | m9-fallback | m9-no-candidate | fp-<class>
    confidence: float | None,
    member_bib_ids: list[str],
    skosmos_url: str | None,
) -> None: ...
```

Append target:

```
<BFFI_DATA_DIR>/cataloguer-target-review-<run_uuid>.csv
```

Header:

```
run_uuid,canonical_work_uri,expression_uris,reason,confidence,member_bib_ids,skosmos_url,flagged_at,reviewed_by,reviewed_at,notes
```

`expression_uris` and `member_bib_ids` are pipe-separated lists (`|`) — keeps the column count flat and Excel-friendly.

`skosmos_url` is best-effort — once Skosmos exposes canonical Works at a stable URL pattern, populate it; until then, leave empty.

**Where rows come from** (initial wiring):

| Stage | Existing source data | Reason values |
|---|---|---|
| M8 | `canonical-conflicts.jsonl` | `m8-conflict` |
| M9 | low-confidence picks (`fallback` outcomes, especially with `confidence` below the M9 review threshold) | `m9-fallback`, `m9-no-candidate` |
| (future) M5/M6 audit | the P-22..26 veto stack's escalated FP classes | `fp-different_works_same_author`, `fp-series_volumes_collapsed`, etc. |

The future M5/M6 rows are out of scope here — they wire in when those plans graduate. P-31 lays the helper + CSV pattern; later plans add their categories.

## Prerequisites

- **Gating prerequisite — observability trustworthiness.** P-17, P-18, and P-19 must be implemented (done 2026-05-14; see `../completed/`), AND P-30 (critical audit of observability + audit-trail practices) must be either complete OR explicitly sequenced AFTER this proposal so the new `bffi_artifact_path` gauges and the two CSV surfaces get audited together as part of P-30's truth-table catalogue. Recommended sequencing: ship P-31 first, then P-30; this way P-30 audits the dashboard in its final shape rather than auditing a surface that's about to grow.
- Existing JSONL surfaces (`bibframe/_errors.jsonl`, `bffi/_validation.jsonl`, `canonical-conflicts.jsonl`) stay as the machine-readable source-of-truth — the CSVs are derived views.
- Grafana table panel + Markdown-link column rendering already supported by the version in `docker-compose.yml`; no plugin install.

## Risks

- **R1 — CSV escaping edge cases.** Bibliographic data carries commas, quotes, and newlines in titles + author names. Use Python's `csv.writer` with `QUOTE_MINIMAL` quoting and lineterminator `\n`; never hand-roll the format. Test fixture should include a title like `She said, "yes"` to pin the contract.
- **R2 — Concurrent appends.** Stages run sequentially today (M2 → M3 → … → M9 → M8); each stage's append is single-process. If a future P-X parallelises within a stage (e.g., multi-process M3), the append helper needs file-locking. Mitigation: document the single-process assumption now; the helper writes via `open(append_mode)` which is atomic per-line on POSIX up to PIPE_BUF (~4 KB); cataloguer rows are well under that.
- **R3 — File size at 800 k scale.** Source-review CSV: if 10 % of records have an M2/M3/M9 failure, that's ~80 k rows × ~250 B = ~20 MB. Excel handles 1 M rows; this is well within. No mitigation needed.
- **R4 — Per-run CSV proliferation.** Each run produces its own CSV under `BFFI_DATA_DIR`. After 100 runs, that's 200 CSVs (source + target). Operator can `git ignore` them under `scratchpad/` per existing practice; or sweep via `find <BFFI_DATA_DIR> -name 'cataloguer-*.csv' -mtime +30 -delete`. Not a problem at the bench dir but worth flagging as a hygiene item for the production cycle.
- **R5 — Skosmos URL is empty in Phase C v1.** The CSV's `skosmos_url` column ships empty until Skosmos publishes canonical Works at a stable pattern. Cataloguer can copy the `canonical_work_uri` into a Fuseki query or Skosmos search box manually. Address when Skosmos is ready; not blocking.
- **R6 — Path gauge cardinality drift.** New stages adding new artifact kinds inflate the `kind` label set. The cardinality budget (12 kinds × runs × 2 states) tolerates ~30 kinds without issue; adding 30+ kinds means rethinking. Document the budget in the gauge's docstring so future additions surface the question.

## Open questions

- **Path-state extension scope.** Phase A's `state` label distinguishes `expected` from `present`. Is `expected → present` enough, or do we want a third `skipped` value (when an artifact's producer ran idempotent-skip and the existing artifact is already fresh)? Recommend `expected | present` only in v1; the operator's question "is the file there" is binary at the dashboard layer. The `skipped` distinction is interesting but adds a label dimension for marginal value.
- **One CSV per run vs cumulative.** Per-run keeps history clean (operator can see what each run flagged); cumulative would be a single growing file. Strongly prefer per-run — matches the `run_uuid` discipline elsewhere in the pipeline and avoids cross-run row conflicts.
- **Should `bffi_artifact_path` use a `path` label or carry the path as a JSON-encoded annotation?** Label is simpler but means changing the path requires a new label series. For per-run paths (the common case), each run's paths are fixed at run start and don't change, so label-as-path works. If a path ever needed mid-run editing, we'd switch to annotations. Defer.
- **Severity values in the source CSV.** `blocking | warning | info` is a reasonable initial set; do we want a fourth `fyi` for "M9 fallback picks that landed above the review threshold but the operator might still want to glance at"? Recommend punting until the cataloguer feedback loop tells us; add severity values when there's a concrete review-workflow need.
- **CSV row de-duplication.** Should the helper de-duplicate `(bib_id, stage, category)` triples within a run? If M9 retries a record, two rows could appear. Recommend YES — de-dupe on `(bib_id, stage, category)` for source CSV, `(canonical_work_uri, reason)` for target CSV. Last-write-wins on `details` / `confidence`.

## Acceptance criteria (drafted; refine on graduation)

**Phase A**
- [ ] `bffi_artifact_path{kind, run_uuid, path, state} 1` gauge wired up in `metrics_exporter.py`. Cardinality bounded by ~12 kinds × active runs × 2 states.
- [ ] Each stage emits `expected` rows at its `start` event and `present` rows at its `end` event for the artifacts it produces. Pipeline init emits `expected | present` rows for inputs (`bibframe_dir`).
- [ ] Grafana table panel "Run artifacts" added to the dashboard, query `bffi_artifact_path{run_uuid="$active_run"}`, columns `kind` / `state` / `path` with the path column rendered as a Markdown link.
- [ ] Unit tests: per-stage path emission; gauge cardinality stays bounded; panel JSON parses.

**Phase B**
- [ ] `src/bffi_pipeline/cataloguer_review.py` exposes `append_source_row(...)`. Helper writes UTF-8 BOM + header on first append; appends per row; handles CSV escaping via `csv.writer`.
- [ ] M2 / M3 / M9 wired to call `append_source_row(...)` whenever they write a row to their existing JSONL surfaces.
- [ ] De-duplication on `(bib_id, stage, category)` within a run.
- [ ] Source CSV path exposed as `bffi_artifact_path{kind="cataloguer_source_csv", ...}` (depends on Phase A).
- [ ] Unit test: fixture with a title containing `"She said, \"yes\""` round-trips through the CSV without corruption.

**Phase C**
- [ ] `append_target_row(...)` mirrors the source helper. M8 + M9 wired.
- [ ] `expression_uris` and `member_bib_ids` columns are pipe-separated lists.
- [ ] `skosmos_url` is best-effort; empty when Skosmos has no stable URL pattern yet.
- [ ] Target CSV path exposed via Phase A gauge.
- [ ] Unit test: M8-conflict + M9-fallback rows for the same canonical Work de-duplicate correctly (one row per `(canonical_work_uri, reason)`).

## What this proposal does NOT do

- Doesn't replace the existing JSONL surfaces. `bibframe/_errors.jsonl`, `bffi/_validation.jsonl`, `canonical-conflicts.jsonl` stay as the machine-readable source-of-truth; the CSVs are the cataloguer-facing view.
- Doesn't change the existing stage-event vocabulary. Path gauges layer alongside the existing metrics; cataloguer CSVs are a new on-disk surface.
- Doesn't install a Grafana plugin (no JSON datasource, no file datasource). The Markdown-link rendering uses Grafana's stock table panel features.
- Doesn't define Skosmos's URL pattern for canonical Works — that's an upstream question. The CSV column reserves space; population happens when Skosmos is ready.
- Doesn't add audit logic for the CSVs themselves (e.g., "verify reviewed_by is set on every row"). That's a cataloguer-workflow tooling question, not a pipeline question.

## Composition with sibling proposals

- **P-30 (observability + audit-trail critical audit)** — recommended to sequence P-31 *before* P-30 so P-30 audits the new gauges + CSV surfaces as part of the truth-table catalogue. Adds ~3 surfaces (path gauges, source CSV, target CSV) to P-30's scope; small.
- **P-22..26 (the FP veto stack)** — once those land, their audit-flagged FP classes feed the target CSV via Phase C's `reason="fp-<class>"` values. P-31 lays the helper; the FP vetoes use it.
- **P-27 (M6 verdict audit)** — independent. P-27's findings could surface in either CSV but don't require P-31; P-27 ships standalone.
- **P-28 (audit script as CI regression fixture)** — orthogonal. P-28's fixture is a frozen bench artifact; the CSV format is a runtime artifact.
- **P-29 (M5 missed-merge recall audit)** — independent. P-29's gold set is a separate input; it doesn't write to either CSV.
