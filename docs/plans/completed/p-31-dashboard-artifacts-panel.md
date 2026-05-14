# P-31 — Dashboard artifacts panel + per-run cataloguer review TSVs

**Status**: completed 2026-05-14. Phase A.1 superseded; Phase A.2 shipped via alternate mechanism; Phases B + C shipped with the unified `cataloguer_review.py` helper module + per-stage wire-ins (M2 / M3 / M8 mint failures for source-review; M8 conflicts / M9 fallback / M9 no-candidate / M9 fictional-character for target-review). `member_bib_ids` is populated for every target-review row so cataloguers can estimate severity directly from the source MARC records.
**Source proposal**: this file at commit `338a7a3` (proposal-shape; recover via `git show 338a7a3:docs/plans/proposed/p-31-dashboard-artifacts-panel.md`).
**Plan-base commit**: `338a7a3`. To gauge drift before executing, run
`git diff 338a7a3..HEAD --
src/bffi_pipeline/stages/
src/bffi_pipeline/metrics_exporter.py
config/grafana/
config/Caddyfile
docker-compose.yml`.
**Phase commits**:

- Phase A.1 (path-gauge metric + per-stage emit-site wiring, **solo**): **superseded** by the Caddy reverse-proxy + Markdown-interpolation alternate (see Phase A.2 entry). No gauge added; the operator-facing outcome is achieved without one. If a future phase needs the gauge (e.g. P-30's truth-table audit wants a machine-readable artifact inventory), re-open as a new phase.
- Phase A.2 (Grafana "Run artifacts" panel, **paired**): **shipped via alternate mechanism on 2026-05-14**. Implementation in the Caddy reverse-proxy commit chain (`docker-compose.yml` + `config/Caddyfile`) + the dashboard panel id 28 ("Cataloguer review TSVs") update in `config/grafana/dashboards/bffi-pipeline.json`. Operator outcome — clickable links from the dashboard to per-run TSVs — fully delivered; the mechanism is Markdown links to `/files/${active_run}/...` served by Caddy's `file_server browse`, **not** the path-gauge + table-panel design originally specced.
- Phase B (source-review TSV + M2/M3/M8 mint-failure wiring, **solo**): **shipped 2026-05-14**. New module `src/bffi_pipeline/cataloguer_review.py` with `append_source_row`; wire-ins at `marc_to_bf.py::run` (three except branches), `bf_to_bffi.py::run` (SHACL-fail block), `merge.py::apply_merge` (post-mint-failure-emission loop). Per-stage TSVs (`_errors.tsv`, `_validation.tsv`, `canonical-mint-failures.tsv`) stay as-is; the unified TSV is the cataloguer-handoff superset.
- Phase C (target-review TSV + M8/M9 wiring, **solo**): **shipped 2026-05-14**. `append_target_row` in the same module; wire-ins at `merge.py::apply_merge` (post-conflict-emission loop with `reason="m8-conflict"`) and `reconcile.py::apply_reconciliation` (per-outcome cascade: `m9-fallback`, `fictional-character`, `m9-no-candidate`). FP-veto-class wiring lands as part of P-22..26 via the same helper.

**Owner**: operator (Mikko) + claude pair. Phase A.2's paired-session constraint was waived in practice because the Markdown-link alternative is structurally simpler than the original table-panel design; Phases B + C remain solo-then-commit work.
**Estimated wall-time** (remaining): ~3 h. Phase B: ~2 h (mostly consolidation of three existing per-stage TSVs into one unified file + adding the cataloguer-fill-in columns). Phase C: ~1 h (the M8 mint-failures TSV already covers most of the target-review surface; this phase adds the M9 fallback-band rows).

## Pair-programming constraint

Phase A.2 (Grafana dashboard JSON changes — `config/observability/grafana/...dashboard.json`) is reviewed live before each commit. Dashboard UI choices (column order, link rendering, panel placement, what to grey out when `state=expected`, etc.) benefit from operator-in-the-loop iteration that a unit test can't substitute for. Claude does not push dashboard JSON solo; operator drives the review pace, claude proposes edits and applies the agreed ones.

The other three phases are backend code with mechanically-verifiable behaviour (gauges populate the registry; TSV writer escapes correctly). They ship via the standard solo-then-commit flow with `make lint && make test` and per-phase unit tests.

## Goal

Surface pipeline-produced artifacts on the dashboard so the operator knows where files live each run AND so cataloguers get a spreadsheet they can actually work from (sort, filter, mark "reviewed", hand back as audit trail).

Two operationally distinct review workflows justify two separate files:

- **Source-system review** (Phase B) — every bib_id where the pipeline couldn't produce a complete, canonical BFFI record because the **MARCXML itself** was wrong or incomplete. Concrete surfaces in scope:
  - **M2 conversion errors** (`bibframe/_errors.tsv`): MARC parse failure, schema-invalid leader, required-tag misses — the bib_id has no BFFI output at all.
  - **M3 Boundary-3 SHACL violations** (`bffi/_validation.tsv`): per-record `.ttl` exists but is shape-noncompliant (missing 336/337/338 → can't type the content; malformed date literals; etc.) — the BFFI is partial.
  - **M8 mint failures** (`canonical-mint-failures.tsv`): per-record BFFI exists but couldn't mint into a canonical Work because the source lacked a `pref_label` or any minting anchor — the bib_id never reaches the canonical graph cataloguers browse.

  Cataloguer's path is the same for all three: fix the source MARC record in Sierra/Helmet → re-export → re-run the pipeline. *The pipeline did the right thing — the input wasn't right.*

- **Target-system review** (Phase C) — canonical Works the pipeline transformed in a way the cataloguer flags as wrong (mis-clustered, mis-reconciled to KANTO, wrong primary author, fictional-character that should have short-circuited, FP-veto candidate, etc.). Cataloguer checks against the source system to verify the source data was actually fine, then records the verdict + the expected behaviour. **The fix is in the pipeline** — gold-set additions, M5/M6/M9 prompt or heuristic tuning, new vetoes, new SHACL rules. The cataloguer's row becomes a **bug report against the pipeline**, not a Skosmos-edit ticket. A subsequent re-run produces the corrected output and replaces the wrong state in Fuseki / Skosmos atomically (consistent with the P-32 Phase H pre-run-clear stance: Fuseki state is always a pure function of the latest pipeline run, never hand-patched).

The two TSVs share the per-run-file pattern + cataloguer-fill-in columns but differ in the row schemas:
- **Source-review** focuses on what's wrong with the source record (`category`, `severity`, `marcxml_path`).
- **Target-review** focuses on (a) the pipeline's decision that triggered the flag (`reason`, `confidence`, `canonical_work_uri`, `member_bib_ids`), and (b) the cataloguer's bug-report disposition (`cataloguer_verdict`, `severity`, `expected_behavior`).

### File format decision: TSV, not CSV

The proposal called for CSV. **Switched to TSV** based on operator input during graduation:

1. **Finnish-locale Excel uses comma as the decimal separator.** A regular UTF-8 CSV opens as a single column in Finnish-locale Excel; the operator/cataloguer has to walk through the import wizard every time to pick a delimiter, or save-as-different-delimiter manually. TSV opens cleanly with each tab becoming a column.
2. **Tab characters are vastly rarer in bibliographic field content than commas.** Titles, author names, and rationale strings routinely contain commas; they almost never contain literal tab characters. Escaping edge cases shrink.
3. **Python's stdlib `csv.writer` accepts any delimiter** (`csv.writer(fh, delimiter='\t')`), so the implementation cost is identical.

UTF-8 without BOM (Excel handles UTF-8 TSV correctly; the BOM was a CSV-Excel quirk that doesn't carry over).

### One TSV per run, identified by `run_uuid` in the filename

The two TSV files are **strictly per-run** — the `run_uuid` is in the filename, never in a "growing file across runs" pattern. This is a load-bearing requirement of the cataloguer workflow, not a hygiene preference:

- The cataloguer fills in `reviewed_by` / `reviewed_at` / `notes` columns directly in the file. A cumulative TSV would interleave reviewed rows from earlier runs with un-reviewed rows from the current run, and the cataloguer would lose track of which findings have been acted on. Per-run isolation makes "this file = this run's review queue" unambiguous.
- The dashboard's "Run artifacts" panel filters by `$active_run`; the file the operator clicks through to is the one for the run on screen, no mental mapping required.
- When the cataloguer hands a reviewed TSV back, the receiving operator knows exactly which run it pertains to — no merge-with-cumulative-file step that could lose annotations.

The cost (more files on disk) is genuinely small and handled by the hygiene step in R4. The benefit (clean per-run review state) is what the workflow needs to work at all.

## Definition of done

### Phase A.1 — Path gauge + emit-site wiring (solo) — SUPERSEDED

**Not implemented.** The path-gauge design was preempted by the simpler Caddy file-server + Markdown-interpolation alternate that delivers Phase A.2's operator-facing outcome without needing a machine-readable artifact inventory in Prometheus. If a later phase (e.g. P-30 truth-table audit, or a future "what files exist in every active run" query) needs the gauge, re-open as a new phase rather than mining this checklist.

Original DOD preserved as documentation of the path **not** taken:

- [ ] ~~New gauge `bffi_artifact_path{kind, run_uuid, path, state} 1` in `src/bffi_pipeline/metrics_exporter.py`.~~
- [ ] ~~`kind` enum: `bibframe_dir`, `bffi_dir`, ..., documented as a `Literal[...]` type.~~
- [ ] ~~`state` enum: `expected` | `present`.~~
- [ ] ~~Emit sites in M2/M3/M8/M9 + cardinality budget.~~
- [ ] ~~Three unit tests.~~

### Phase A.2 — Grafana panel for cataloguer review TSVs (shipped via alternate mechanism)

**Shipped 2026-05-14**, but via a different mechanism than the original DOD specced. The trade-off (in retrospect):

- **Original design**: stage-emit `bffi_artifact_path` gauges → Prometheus → Grafana table panel with data-link rendering, distinguishing `expected` (pending) from `present` (file exists). Mechanism: PromQL query feeding a table panel.
- **What actually shipped**: Caddy reverse-proxy serving `./runs/` at `/files/*` + a Grafana **text** panel with Markdown links interpolating `${active_run}`. Mechanism: hardcoded link list + dashboard variable.

Where the alternate is **better**:
- No Prometheus cardinality cost (the gauge would have added ~240 series at ten concurrent runs; the text panel adds zero).
- Directory listing for free (Caddy's `file_server browse` lets the operator drill down without enumerating files in the dashboard).
- Reverse-proxy consolidation (same Caddy that serves files also routes Grafana + Prometheus behind one URL).

Where the alternate is **worse**:
- The panel can't distinguish "file present" from "stage hasn't started yet" — broken links 404. Acceptable because the four-state stage tiles (P-37's STAGE_PHASES work) already convey stage-pending state separately; the operator clicks the file link only after a stage has emitted its `end` event.
- The set of linked files is hardcoded in the panel Markdown (M2 errors, M3 SHACL fails, M8 mint failures, "browse all"). Adding a new TSV requires a panel edit, not just a stage emission. Acceptable for v1 since the file set is small + stable.

Acceptance — completed:

- [x] Caddy service in `docker-compose.yml` serving `./runs:/srv:ro` on `127.0.0.1:8080` with `file_server browse` enabled.
- [x] Panel id 28 ("Cataloguer review TSVs") in `config/grafana/dashboards/bffi-pipeline.json` rendering clickable Markdown links: M2 errors, M3 SHACL fails, M8 mint failures, plus a "Browse all run artifacts" entry that hits `/files/${active_run}/`.
- [x] Sized `h: 12, w: 6` to match the sibling progress bargauges on row y=10.
- [x] Smoke-tested end-to-end against an actual run (helmet-5k-clean-full bench, run `02924cb38191`).

### Phase B — Consolidate per-stage source-review TSVs into a unified file (solo)

**Re-scoped 2026-05-14**: the per-stage TSVs already exist on disk — `bibframe/_errors.tsv` (M2), `bffi/_validation.tsv` (M3), `canonical-mint-failures.tsv` (M8/P-34, source-side because mint-failures are bib_ids that didn't make it through). What's missing relative to the original Phase B is (a) consolidation into one cataloguer-handoff file with a unified row format, (b) the cataloguer-fill-in workflow columns (`reviewed_by`, `reviewed_at`, `notes`), (c) M9 source-side rows (`no-candidate` / `fictional` / `watchdog-aborted`). Two execution options:

**Option B1 — Consolidate in code (recommended)**: build the `append_source_row` helper as originally specced, retrofit M2/M3/M8/M9 call sites to also write to the unified TSV alongside their existing per-stage outputs. Per-stage TSVs stay (the dashboard panel links to them, P-30 audit references them); the unified file is the cataloguer-handoff superset.

**Option B2 — Consolidate at view time**: leave the three per-stage TSVs alone, build a small `bffi-pipeline cataloguer-source-review` typer command that reads them and emits the unified format on demand. Less code, but the cataloguer-fill-in columns can't be persisted in-place — the workflow would round-trip through a separate review file.

Recommend **B1** because the cataloguer-fill-in columns are the load-bearing UX feature (the original P-31 framing in "## Goal" calls this out as why TSV-per-run beats CSV). B2 loses that.

Updated DOD (under B1):

- [ ] New module `src/bffi_pipeline/cataloguer_review.py` exposing `append_source_row(...)`:

  ```python
  def append_source_row(
      *,
      bib_id: str,
      stage: str,          # m2 | m3 | m8
      category: str,       # M2 error_type | boundary-3 | mint-failure-<reason>
      severity: str,       # blocking | warning
      details: str,
      marcxml_path: str,
  ) -> None: ...
  ```

- [ ] Write target: `<BFFI_DATA_DIR>/cataloguer-source-review-<run_uuid>.tsv`. Resolved from `get_settings().data_dir` + the active emitter's `run_uuid`. No-op when no emitter is active (tests; mirrors `emit_if_active`).
- [ ] Header (written once on first append; UTF-8, no BOM):

  ```
  run_uuid\tbib_id\tstage\tcategory\tseverity\tdetails\tmarcxml_path\tflagged_at\treviewed_by\treviewed_at\tnotes
  ```

- [ ] Implementation uses Python's `csv.writer` with `delimiter='\t'`, `quoting=csv.QUOTE_MINIMAL`, `lineterminator='\n'`. Never hand-roll TSV formatting.
- [ ] De-duplication on `(bib_id, stage, category)` within a run — last write wins on `details` / `severity`. Implementation: small in-memory `set[tuple[str, str, str]]` keyed by the tuple; the helper checks before append. Per-run reset.
- [ ] `flagged_at`: UTC ISO 8601 with second precision. `reviewed_by` / `reviewed_at` / `notes`: empty strings on write (cataloguer fills in).
- [ ] **M2 wire-in** in `src/bffi_pipeline/stages/marc_to_bf.py` — wherever the existing `_errors.jsonl` row is appended, also call `append_source_row` with the same data. `category` = the `error_type` field; `severity` = `blocking` for hard errors, `warning` for partial-failure tolerated rows; `details` = the existing error message; `marcxml_path` = the per-record MARCXML path.
- [ ] **M3 wire-in** in `src/bffi_pipeline/stages/bf_to_bffi.py` — wherever the existing `_validation.jsonl` row is appended, also call `append_source_row`. `category` = `boundary-3`; `severity` = `warning` (Boundary 3 failures don't block, per spec); `details` = the SHACL report text; `marcxml_path` = the BIBFRAME-side `.rdf` parent of the BFFI record.
- [ ] **M8 mint-failure wire-in** in `src/bffi_pipeline/stages/merge.py` — every bib_id that landed in `canonical-mint-failures.tsv` (P-34) gets a parallel `append_source_row` with `category` = the mint-failure `reason` (`missing-pref-label` / `missing-mint-anchor` / etc.), `severity` = `blocking` (the bib_id is absent from the canonical graph), `details` = the human-readable reason, `marcxml_path` = the source `.xml`. The standalone `canonical-mint-failures.tsv` stays as the per-stage surface; the source-review TSV is the unified cataloguer-handoff superset.
- [ ] **No M9 wire-in for source-review.** M9's `no-candidate` / `fictional` / `watchdog-aborted` outcomes are target-review territory — the bib_id reached a canonical Work; the question is whether the pipeline's reconciliation decision was right, which the cataloguer answers against the source rather than fixing the source. See Phase C.
- [ ] ~~Source TSV path surfaced via Phase A.1 gauge.~~ Superseded — the dashboard's panel id 28 ("Cataloguer review TSVs") will gain a Markdown link `[Unified source review](/files/${active_run}/cataloguer-source-review-${active_run}.tsv)` once the file exists. Edit the panel JSON when Phase B ships.
- [ ] Unit tests:
  - `test_append_source_row_writes_header_once` — repeated calls produce exactly one header line.
  - `test_append_source_row_escapes_tab_quote_newline` — fixture with title `"She said,\t\"yes\""\n` survives round-trip (read back via `csv.reader(delimiter='\t')`).
  - `test_append_source_row_dedupes_within_run` — three calls with identical `(bib_id, stage, category)` produce one row.
  - `test_append_source_row_noop_without_active_emitter` — call without `set_active_emitter` doesn't raise + doesn't write.
- [ ] No regression on existing JSONL surfaces — `bibframe/_errors.jsonl`, `bffi/_validation.jsonl` continue to be written exactly as before.
- [ ] `make lint && make test` green.

### Phase C — Target-review TSV + M8/M9 wiring (solo)

**Re-scoped 2026-05-14** (twice): the M8 mint-failures TSV (`canonical-mint-failures.tsv` from P-34) is actually a **source-review** surface — those bib_ids never made it to a canonical Work, so they belong in Phase B's consolidation, not here. Phase C is for canonical Works the pipeline **did** mint but where the pipeline's transformation may be wrong (mis-clustered, low-confidence reconciliation, fictional-character escape, FP-veto candidate). Cataloguer's job: verify against source, record verdict + expected behaviour. **Pipeline fix follows.**

- [ ] `append_target_row(...)` added to the same module:

  ```python
  def append_target_row(
      *,
      canonical_work_uri: str,
      expression_uris: list[str],
      reason: str,         # m8-conflict | m9-fallback | m9-no-candidate
                           #   | fictional-character | fp-<class>
      confidence: float | None,
      member_bib_ids: list[str],
      skosmos_url: str | None,
  ) -> None: ...
  ```

  Cataloguer-fill-in columns (`cataloguer_verdict`, `severity`,
  `expected_behavior`, `reviewed_by`, `reviewed_at`, `notes`) are
  written as empty strings by the helper; the cataloguer fills them
  in directly in the TSV.

- [ ] Write target: `<BFFI_DATA_DIR>/cataloguer-target-review-<run_uuid>.tsv`. Header (14 columns):

  ```
  run_uuid\tcanonical_work_uri\texpression_uris\treason\tconfidence\tmember_bib_ids\tskosmos_url\tflagged_at\tcataloguer_verdict\tseverity\texpected_behavior\treviewed_by\treviewed_at\tnotes
  ```

  Cataloguer-fill-in column semantics:
  - `cataloguer_verdict`: `pipeline-correct` | `pipeline-incorrect` | `uncertain`. *pipeline-correct* means the cataloguer reviewed the pipeline's decision and confirmed it; the row is a confidence-flag false alarm that **the gold set should absorb** so M5/M6 stop flagging it. *pipeline-incorrect* means the pipeline genuinely got it wrong; the row is **a bug report**. *uncertain* means the cataloguer can't decide without more context (multilingual record, edge-case authority, etc.) — escalates to a follow-up review.
  - `severity`: `cosmetic` | `functional` | `blocking`. *cosmetic* = wrong but harmless display (e.g. misordered authors). *functional* = wrong enough to mislead a Skosmos user but doesn't block discovery (e.g. wrong KANTO bind, browsing still works). *blocking* = catastrophic (e.g. two distinct authors merged → search returns the wrong author entirely). Drives the developer's triage queue.
  - `expected_behavior`: free text — what the pipeline should have done. Reads like a one-line ticket title ("should have been two separate canonical Works", "primary author should bind to https://urn.fi/...", "FP veto on numeric-marker mismatch", etc.). Becomes the gold-set hint or the new heuristic spec.

- [ ] `expression_uris` and `member_bib_ids` are pipe-separated (`|`). Empty list serialises as empty string.
- [ ] `confidence` serialises as a string (`""` when None, `"0.84"` otherwise — period as decimal separator regardless of locale; Excel re-parses on import per the user's column-type override).
- [ ] `skosmos_url` is empty in v1 — Skosmos doesn't yet publish canonical Works at a stable URL. Column reserved for the cataloguer's verification click-through once the URL pattern stabilises.
- [ ] De-duplication on `(canonical_work_uri, reason)` within a run.
- [ ] **M8 wire-in** in `src/bffi_pipeline/stages/merge.py` — every canonical Work in `canonical-conflicts.jsonl` (the M6 judge disagreement subset where the pipeline made a default-no-merge call) calls `append_target_row` with `reason="m8-conflict"`, `confidence=None`. These are the highest-yield bug-report candidates because the judge cascade ALREADY surfaced uncertainty.
- [ ] **M9 wire-in** in the reconcile-stage's fallback-band branch — picks below the configured review threshold emit `reason="m9-fallback"` rows with the picker's confidence; `no-candidate` outcomes that escaped M9 entirely emit `reason="m9-no-candidate"` (no Finto candidates found at all); `fictional-character` short-circuits emit `reason="fictional-character"` (cataloguer verifies the qualifier was legitimate, not a false-positive on a real person whose name happens to match a fictional one).
- [ ] ~~Target TSV path surfaced via Phase A.1 gauge.~~ Superseded — add a Markdown link `[Unified target review](/files/${active_run}/cataloguer-target-review-${active_run}.tsv)` to panel id 28 when Phase C ships.
- [ ] Unit tests:
  - `test_append_target_row_writes_header_once` — covers the full 14-column header.
  - `test_append_target_row_serialises_list_columns_with_pipe` — `expression_uris` + `member_bib_ids`.
  - `test_append_target_row_dedupes_on_uri_plus_reason`.
  - `test_append_target_row_confidence_none_serialises_empty`.
  - `test_append_target_row_cataloguer_fillin_columns_emit_empty` — `cataloguer_verdict` / `severity` / `expected_behavior` are written as empty strings so the cataloguer fills them in by hand.
- [ ] `make lint && make test` green.

### Cross-phase

- [x] On graduation to `in-progress/` (first phase merged), `git mv` the plan from `backlog/` to `in-progress/`. — completed 2026-05-14 alongside the Phase A.2-via-alternate ship.
- [ ] On final phase merge with all DOD boxes checked, `git mv` to `completed/`.
- [ ] A short snapshot at `docs/performance/<date>-p-31-artifacts-panel.md` summarising the operator-facing change (screenshot or panel JSON excerpt + sample TSV row) — write when Phase B or C lands.

## Risks

- **R1 — TSV escaping edge cases.** Bibliographic data can carry tabs (rare), quotes (`"`), and newlines (multi-line subjects). Mitigated by `csv.writer(delimiter='\t', quoting=csv.QUOTE_MINIMAL)`; unit test pins the round-trip on a worst-case fixture. Never hand-roll.
- **R2 — Concurrent appends.** Stages run sequentially today (M2 → M3 → … → M9 → M8); within a stage there's no parallel writer. If a future plan parallelises within a stage (e.g., multi-process M3), `append_*_row` needs file-locking. Phase B + C document the single-process assumption in the helper's docstring; revisit if/when parallelism lands.
- **R3 — File size at 800 k scale.** Source TSV: at 10 % failure rate, ~80 k rows × ~250 B = ~20 MB. Excel handles 1 M rows. Target TSV: smaller (~5 % of records reach the review band). No mitigation needed.
- **R4 — Per-run TSV accumulation is expected; hygiene is the operator's job.** Per-run files are a load-bearing feature, not a risk (see "One TSV per run" in Goal). Each run produces two TSVs under `BFFI_DATA_DIR`; after 100 runs in a bench-heavy week that's 200 files. The real risk is the operator *forgetting to archive or clean up* reviewed TSVs after the cataloguer hands them back, which leaves the `BFFI_DATA_DIR` cluttered and obscures which runs still have outstanding review work. Mitigation: document the cleanup pattern in the operator runbook (e.g. `find <BFFI_DATA_DIR> -name 'cataloguer-*.tsv' -mtime +30 -delete` after the operator has confirmed the rows are archived in the cataloguer-handback location), and surface "TSVs awaiting cleanup" as a Phase A.1 follow-up gauge if it becomes a real problem in practice.
- **R5 — `skosmos_url` is empty in v1.** Cataloguer copies the `canonical_work_uri` into Skosmos search or a Fuseki SPARQL query manually. Acceptable v1; populate the column when Skosmos has a stable URL pattern.
- **R6 — Path-gauge cardinality drift.** Adding a `kind` value should remain a deliberate act (the `Literal[...]` type forces a code change with review). At 12 kinds × active runs × 2 states the budget is comfortable; at 30+ kinds revisit. Documented inline.
- **R7 — Phase ordering temptation.** A.1 plus a synthetic-test of the gauge can land before the paired A.2 session; this is fine. Resist the urge to write a "preview" version of the Grafana panel JSON solo — that violates the pair-programming constraint and creates two iterations where one would have served.

## What this plan does NOT do (deferred)

- **Replace the existing JSONL surfaces.** `bibframe/_errors.jsonl`, `bffi/_validation.jsonl`, `canonical-conflicts.jsonl` stay as machine-readable source-of-truth; the TSVs are derived cataloguer-facing views.
- **Install a Grafana plugin.** No JSON datasource, no file datasource. The "Run artifacts" panel uses Grafana's stock table panel + the chosen link-rendering mechanism (decided in the paired session).
- **Define Skosmos's URL pattern for canonical Works.** Upstream question; column reserved, populated when ready.
- **CSV / Excel-decimal-locale support.** TSV is the format; Excel handles UTF-8 TSV in any locale. If a future cataloguer workflow needs `.csv`, it's an export step from the TSV, not a parallel pipeline output.
- **Cataloguer-facing tooling for the round-trip.** The plan ships TSV-out; cataloguer fills in the workflow columns (source-review: `reviewed_by` / `reviewed_at` / `notes`; target-review: also `cataloguer_verdict` / `severity` / `expected_behavior`) and hands the file back. How the **target-review** rows are ingested into the development backlog — gold-set growth (P-06), new SHACL rules, new vetoes (P-22..26), prompt iteration on M6/M9 — is workflow-side, not pipeline-side. Out of scope here. The **source-review** round-trip (cataloguer fixes Sierra → re-export MARCXML → re-run) is workflow-side too.
- **Patching Fuseki / Skosmos directly.** Target-review rows are **bug reports against the pipeline**, not Skosmos-edit tickets. The fix path is always *fix the pipeline → re-run → Fuseki state updates atomically* (consistent with P-32 Phase H's pre-run clear). Manual `INSERT DATA` / `DELETE WHERE` against the bffi-works graph is not a supported workflow.
- **Audit the new surfaces.** That's P-30's territory. Sequencing puts P-31 before P-30 so P-30's truth-table catalogue covers the new gauges + TSVs.
- **De-dup history rotation.** The in-memory `(bib_id, stage, category)` set is per-run, reset at pipeline init. Cross-run dedup would require reading the previous TSV at startup; not needed in v1.

## Composition with sibling proposals + plans

- **P-30 (observability + audit-trail critical audit)** — sequenced *after* P-31. P-30's catalogue includes the file-server surface + the two TSV files + their derivation rules; the truth-table audits both in one pass.
- **P-22..26 (FP veto stack)** — once those land, their audit-flagged FP classes call `append_target_row(..., reason="fp-<class>")` via Phase C's helper. Target-review rows for these classes feed directly back into the veto plans (cataloguer-confirmed `pipeline-incorrect` verdicts → new veto rules or threshold adjustments). P-31 lays the helper signature; the veto plans wire to it.
- **P-27 (M6 verdict audit)** — independent. P-27 may surface findings that warrant target-TSV rows; if so, it adds a `reason` value when it ships. Cataloguer `pipeline-incorrect` verdicts on M6-flagged rows feed into M6 prompt iteration.
- **P-28 (audit script as CI regression fixture)** — orthogonal. P-28's fixture is a frozen bench artifact; the TSV is a runtime artifact.
- **P-29 (M5 missed-merge recall audit)** — independent. M5 recall failures surfaced by P-29 don't show up in target-review (they're invisible to M8/M9 by definition); P-29 is a separate audit surface.
- **P-06 (gold set growth)** — the natural sink for `cataloguer_verdict=pipeline-correct` rows. The pipeline flagged the case for review (uncertain confidence, edge-case heuristic), the cataloguer confirmed the pipeline got it right — that's exactly the high-information training signal the gold set wants. A future plan can automate "absorb pipeline-correct target-review rows into gold".

## Rollback procedure

If Phase A.2 ships and the panel turns out to be a UX miss, revert just the dashboard JSON commit; the backend gauges from A.1 stay populated and harmless. If Phase B or C exposes a TSV-format issue that breaks cataloguer workflow, the helper is a single module — disable the wire-in calls with a config flag (`BFFI_CATALOGUER_REVIEW_TSV=0`) without touching the JSONL surfaces. JSONL stays canonical; TSV is the derived view.
