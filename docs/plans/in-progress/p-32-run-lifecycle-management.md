# P-32 — Run lifecycle management: canonical root + manifest + CLI + reset

**Status**: in-progress.
**Source proposal**: this file at commit `1fdabcd` (proposal-shape; recover via `git show 1fdabcd:docs/plans/proposed/p-32-run-lifecycle-management.md`).
**Plan-base commit**: `1fdabcd`. To gauge drift before executing, run
`git diff 1fdabcd..HEAD --
src/bffi_pipeline/cli.py
src/bffi_pipeline/stages/observability.py
src/bffi_pipeline/config.py
src/bffi_pipeline/metrics_exporter.py
src/bffi_pipeline/stages/load.py
src/bffi_pipeline/stages/load_finto.py
docker-compose.yml`.
**Phase commits**:

- Phase A (`bffi-run.json` manifest writer + emit-site wiring): `ff83135` (code + 13 unit tests + plan graduation, 2026-05-14).
- Phase B (`bffi-pipeline runs list` CLI): `<unfilled>`
- Phase C (`bffi-pipeline runs prune` CLI with `--reset-*` flags): `93d50da` (code + 9 unit tests + reset stubs for Phases G/H, 2026-05-14).
- Phase D (`bffi-pipeline runs tag` / `untag` / `info`): `<unfilled>`
- Phase E (canonical `<BFFI_RUNS_ROOT>/<run_uuid>/` invariant for new runs): `1b9f1f0` (code + 5 unit tests + `.env.example` migration note, 2026-05-14).
- ~~Phase F (one-time `bffi-pipeline runs migrate` for legacy run dirs)~~ — **dropped 2026-05-14**, see "What this plan does NOT do". Post-Phase-E new runs already land canonical; legacy data in `scratchpad/`, `data/` etc. stays where it is as historical artifact. A future `runs adopt` command can pull individual legacy dirs into the canonical root if/when a concrete need surfaces.
- Phase G (Prometheus + exporter reset on prune): `bcf803a` (code + 8 unit tests + docker-compose admin-API + --no-relaunch-exporter flag, 2026-05-14).
- Phase H (pre-run Fuseki clear + manual `runs clear-fuseki` CLI): `b7a2a87` (code + 8 unit tests + runs_reset.reset_fuseki wired through, 2026-05-14).

**Owner**: operator (Mikko) + claude pair on backend implementation. No paired-Grafana phase — all backend / CLI / SPARQL.
**Estimated wall-time**: 2-3 days end-to-end if executed in one push. Per-phase: A ~half-day, B ~half-day, C ~half-day, D ~quarter-day, E ~quarter-day, F ~half-day (mostly cautious migration runs), G ~half-day, H ~half-day.

## Goal

Pipeline runs accumulate ~25-50 GB of artifacts each at full-corpus scale (BIBFRAME + BFFI + corpus concat + caches), plus per-run cataloguer-review TSVs (P-31) that the cataloguer fills in and hands back. Today the operator manages this by hand-tracking which `BFFI_DATA_DIR` belongs to which bench. P-32 fixes six failure modes around run identity and lifecycle:

1. No registry of what runs exist (`runs list` — Phase B).
2. No "delete runs older than X" pattern (`runs prune` — Phase C).
3. No tagging / status concept (`runs tag` — Phase D).
4. No canonical location for run artifacts (Phase E: `<BFFI_RUNS_ROOT>/<run_uuid>/` invariant).
5. Dashboard / Prometheus state drifts from on-disk reality when runs are pruned (Phase G: `--reset-exporter` / `--reset-prometheus` flags on prune).
6. Fuseki accumulates triples across runs, breaking reproducibility and producing duplicate `prov:Activity` URIs (Phase H: pre-run clear of `<graph_base>*` named graphs, vocabularies preserved).

Phase A (manifest writer) is the foundation everything else builds on. Legacy data on disk (pre-Phase-E run dirs scattered under `scratchpad/`, `data/`, etc.) stays as historical artifact — see "What this plan does NOT do" for the Phase F migration rationale.

## Phase dependencies + sequence

Critical path (post-Phase-F-drop):

```
A (manifest writer)  [done at ff83135]
  ├── B (list CLI)        [parallel]
  ├── C (prune CLI)       [parallel]
  ├── D (tag/info CLI)    [parallel]
  ├── E (canonical root)  [done at 1b9f1f0]
  ├── G (Prometheus + exporter reset)   [parallel]
  └── H (pre-run Fuseki clear)          [parallel]
```

Recommended ship order: **A → E → C → G → H → B → D**. Rationale:

1. A first: the manifest is the data model everything reads / writes.
2. E next: canonical root invariant for new runs.
3. C: prune CLI — operates only on canonical-root runs (legacy dirs invisible by design; `--include-legacy` flag remains opt-in but is never the recommended path post-F-drop).
4. G + H: reset machinery now that the run-state model is settled.
5. B + D: ergonomic CLI commands last; can land in any order.

Phases B / C / D / G / H can ship in parallel branches if the operator and claude split work — they don't conflict at the file level (each touches a different module).

## Definition of done

### Phase A — `bffi-run.json` manifest writer

- [ ] New module `src/bffi_pipeline/run_manifest.py` exposing:
  - `RunManifest` Pydantic v2 model with fields: `run_uuid: str`, `started_at: datetime`, `ended_at: datetime | None`, `bffi_data_dir: str`, `description: str` (default `""`, max 256 chars enforced), `pipeline_git_sha: str | None`, `pipeline_version: str | None`, `stages_observed: list[str]`, `stages_completed: list[str]`, `tags: list[str]`, `status: Literal["running", "completed", "aborted", "adopted-legacy", "unknown"]`, `pre_run_fuseki_clear: dict[str, Any] | None` (populated by Phase H).
  - `read_manifest(path) -> RunManifest` and `write_manifest(path, manifest)` with atomic `.tmp` + rename.
  - `update_manifest_field(path, **kwargs)` — read-modify-write helper that preserves unknown top-level fields (forward-compat).
- [ ] Pipeline init in `cli.py:_init_observability()` writes the initial manifest with `started_at`, `run_uuid`, `bffi_data_dir`, `description` (from `BFFI_RUN_DESCRIPTION` env var; empty default), `pipeline_git_sha` (from `git rev-parse HEAD` if reachable; `None` otherwise), `status="running"`.
- [ ] `stages/observability.py:emit()` extension: when the event is `start` or `end`, also call `update_manifest_field` to append the stage to `stages_observed` / `stages_completed`. Idempotent on retries (no duplicate appends).
- [ ] New helper `mark_run_complete(data_dir, status="completed")` called from a `cli.py` `at_exit` hook; writes `ended_at` + `status`. If the pipeline crashes before the hook runs, the manifest stays at `status="running"` (manually clearable via Phase A's `mark-complete` CLI below).
- [ ] CLI subcommand `bffi-pipeline runs mark-complete <uuid> [--status=aborted|completed]` — manual fallback for the crash case.
- [ ] Settings additions in `src/bffi_pipeline/config.py`:
  - `BFFI_RUN_DESCRIPTION: str = ""`
  - `BFFI_RUNS_ROOT: Path = <repo>/runs/` (used by Phases B/C/D/E/F).
- [ ] `.gitignore` entry: `/runs/`.
- [ ] Unit tests:
  - `test_run_manifest_schema_round_trips` — write + read produces the same `RunManifest`.
  - `test_run_manifest_atomic_write_no_partial_file` — kill the writer mid-`.tmp` write; final manifest is fully-old or fully-new, never half-written.
  - `test_run_manifest_stage_tracking_is_idempotent` — two `start` events for the same stage produce one entry in `stages_observed`.
  - `test_run_manifest_preserves_unknown_fields` — write manifest with an extra `experimental_field`; `update_manifest_field` bumps another field; assert `experimental_field` survives.
  - `test_mark_run_complete_writes_ended_at_and_status`.
  - `test_description_max_length_256_enforced`.

### Phase B — `bffi-pipeline runs list`

- [ ] CLI subcommand `bffi-pipeline runs list` walks `BFFI_RUNS_ROOT` (single-rooted in v1).
- [ ] Renders a table to stdout: `run_uuid` (12-char prefix), `started_at` (relative + absolute on `--verbose`), `status`, `size` (human-readable: KB / MB / GB), `tags` (comma-sep), `description` (truncated to terminal width).
- [ ] Flags:
  - `--sort {started, ended, size, run_uuid}` (default `started` descending).
  - `--status <comma-sep>` filter.
  - `--tag <tag>` filter; repeatable for AND semantics.
  - `--older-than <duration>` filter (`30d`, `2w`, `6mo`). Parser shared with `prune`.
  - `--limit <N>` (default 50).
  - `--json` / `--tsv` output for piping.
- [ ] Legacy run dirs (no `bffi-run.json`) skipped by default; `--include-legacy` opts in (renders `run_uuid` as `legacy-<dirname-hash>`, `status="unknown"`).
- [ ] Startup-log echoes the resolved `BFFI_RUNS_ROOT`.
- [ ] Unit tests:
  - `test_runs_list_renders_runs_in_started_at_descending_order`.
  - `test_runs_list_filters_by_tag_and_status` (AND semantics).
  - `test_runs_list_json_output_is_parseable`.
  - `test_runs_list_handles_legacy_dirs` (default skips; `--include-legacy` includes).

### Phase C — `bffi-pipeline runs prune`

- [ ] CLI subcommand `bffi-pipeline runs prune` selects runs by the same filter flags as `list` (`--older-than`, `--status`, `--tag`, plus `--keep-last <N>` and `--keep-tagged`).
- [ ] `--dry-run` is the default. Operator must pass `--apply` to delete.
- [ ] CLI refuses to proceed with `--apply` unless at least one filter is set that *excludes* some runs.
- [ ] Pre-flight prints: list of dirs to be deleted with sizes, list of runs preserved by `--keep-tagged` / `--keep-last`, total bytes to free.
- [ ] After `--apply`, single `rm -rf` per selected dir. Hard-delete in v1; `--dry-run` default is the safety net.
- [ ] Three reset flags on `--apply` (Phases G + H provide the implementations):
  - `--reset-exporter` (Phase G).
  - `--reset-prometheus` (Phase G).
  - `--reset-fuseki` — drops `<graph_base>*` named graphs (same DROP-graph logic as Phase H's pre-run clear). Useful when the pruned run is the most recently loaded in Fuseki and the operator wants the SPARQL graph to reflect on-disk reality.
  - `--reset-all` shorthand expands to all three.
- [ ] Unit tests:
  - `test_runs_prune_dry_run_does_not_delete`.
  - `test_runs_prune_apply_requires_filter` — `--apply` without a filter exits non-zero.
  - `test_runs_prune_keep_tagged_preserves_tagged_runs`.
  - `test_runs_prune_keep_last_n_preserves_most_recent`.
  - `test_runs_prune_calls_reset_helpers_when_flags_set` — `--reset-exporter` / `--reset-prometheus` / `--reset-fuseki` invoke the Phase G / H helpers (mocked).

### Phase D — `bffi-pipeline runs tag` / `untag` / `info`

- [ ] `bffi-pipeline runs tag <run_uuid> <tag> [<tag>...]` adds tags. `<run_uuid>` resolves as a prefix match against `BFFI_RUNS_ROOT/*`.
- [ ] `bffi-pipeline runs untag <run_uuid> <tag>` removes a tag (no-op if not present).
- [ ] `bffi-pipeline runs info <run_uuid>` pretty-prints: full manifest + dir size + artifact-file enumeration (`bibframe/`, `bffi/`, `bffi-corpus.ttl`, `canonical.ttl`, `canonical-map.jsonl`, `canonical-conflicts.jsonl`, cataloguer TSVs from P-31, etc., with row counts / file sizes as appropriate).
- [ ] Tag operations atomic against the manifest (`update_manifest_field` from Phase A).
- [ ] Unit tests:
  - `test_runs_tag_adds_and_persists_tag` round-trip.
  - `test_runs_untag_is_noop_on_missing_tag`.
  - `test_runs_info_renders_manifest_and_dir_size`.
  - `test_runs_uuid_prefix_resolution_unique` — unique prefix resolves; ambiguous prefix exits non-zero with a hint.

### Phase E — Canonical `<BFFI_RUNS_ROOT>/<run_uuid>/` invariant

- [ ] `Settings.data_dir` resolution rule changes:
  1. If `BFFI_DATA_DIR` is explicitly set, use it (escape hatch).
  2. Otherwise, `runs_root / run_uuid` (the canonical path).
- [ ] Pipeline init creates the canonical dir (`mkdir -p`) and writes the Phase A manifest there.
- [ ] Startup log echoes:
  - `[bffi-pipeline] BFFI_RUN_UUID=<uuid>`
  - `[bffi-pipeline] data_dir=<resolved-path> (canonical)` *or* `(override via BFFI_DATA_DIR — non-canonical; runs list / prune will not see this run unless you adopt it)`
- [ ] Operator runbook section in `docs/operator-runbook.md` updated to deprecate `BFFI_DATA_DIR` for new runs; recommend `BFFI_RUN_DESCRIPTION` instead.
- [ ] Unit tests:
  - `test_settings_data_dir_defaults_to_runs_root_slash_uuid`.
  - `test_settings_data_dir_respects_explicit_override`.
  - `test_startup_log_warns_on_override`.

### ~~Phase F — `bffi-pipeline runs migrate`~~ (DROPPED 2026-05-14)

Decision recorded in "What this plan does NOT do". Phase F was scoped to sweep existing legacy run dirs (`scratchpad/overnight-sample-2026-05-13/`, `scratchpad/data-cataloguer-audit-*/`, etc.) into the canonical `<BFFI_RUNS_ROOT>/<run_uuid>/` layout in one shot. With Phase E ensuring every NEW run lands canonical, the value of migrating PAST runs reduces to "make legacy dirs appear in `runs list`/`prune`" — which the operator can address case-by-case via a future `runs adopt` command if a real need surfaces. The Phase F write-up is preserved in the source proposal (`git show 1fdabcd:docs/plans/proposed/p-32-run-lifecycle-management.md`).

### Phase G — Prometheus / exporter reset on prune

- [ ] `bffi-pipeline serve-metrics` writes `<BFFI_RUNS_ROOT>/.exporter.pid` + `.exporter.argv` (full argv list) on startup; removes them on graceful exit (atexit hook).
- [ ] `prune --apply --reset-exporter` reads the PID file:
  - Absent → warn ("exporter not running; nothing to reset"), skip.
  - Present + process alive → `SIGTERM`, wait 10 s for clean exit, optionally relaunch with the recorded argv (default: relaunch; `--no-relaunch-exporter` opts out).
  - Present + PID dead → clean up stale file, warn, skip.
- [ ] `prune --apply --reset-prometheus`:
  - Reads `BFFI_PROMETHEUS_URL` setting (default `http://localhost:9090`).
  - For each pruned `run_uuid`, POSTs to `/api/v1/admin/tsdb/delete_series?match[]={run_uuid="<uuid>"}`.
  - POSTs to `/api/v1/admin/tsdb/clean_tombstones` after all deletions.
  - Falls back to warning + skip on 405 (admin API not enabled) or connection refused; does not abort the prune.
- [ ] `docker-compose.yml` updated to start Prometheus with `--web.enable-admin-api`. Localhost-bound so the admin API is safe in the local-only stack.
- [ ] Operator runbook documents R9 ("don't `--reset-exporter` mid-run").
- [ ] Unit tests:
  - `test_exporter_writes_pid_file_on_startup` (fixture launches in subprocess; PID file appears + cleans up).
  - `test_reset_exporter_sends_sigterm_and_relaunches` (mock `os.kill` + `subprocess.Popen`).
  - `test_reset_exporter_skips_when_pid_absent` (warning, exit 0).
  - `test_reset_prometheus_posts_delete_for_each_uuid` (mock HTTP; one POST per uuid + one tombstone-clean).
  - `test_reset_prometheus_continues_on_admin_api_405` (mock returns 405; reset returns success-with-warning).

### Phase H — Pre-run Fuseki clear + manual CLI

- [ ] New module `src/bffi_pipeline/stages/fuseki_clear.py` exposing `clear_run_output_graphs(fuseki_url, graph_base, *, max_triples_per_graph, dry_run) -> ClearResult`.
- [ ] At pipeline init (after `_init_observability`, before any stage starts), if `BFFI_FUSEKI_CLEAR_ON_RUN_START` is `true` (default), call `clear_run_output_graphs`:
  1. SPARQL SELECT enumerates named graphs: `SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }`.
  2. Filter to graphs whose URI starts with `settings.graph_base`.
  3. For each candidate, SPARQL SELECT counts triples: `SELECT (COUNT(*) AS ?n) WHERE { GRAPH <uri> { ?s ?p ?o } }`.
  4. If `n > BFFI_FUSEKI_CLEAR_MAX_TRIPLES` and `--force-clear` not passed: log warning, skip.
  5. `DROP GRAPH <uri>` for each remaining.
  6. Write to manifest: `pre_run_fuseki_clear = { dropped_graphs, skipped_oversized, total_triples_before, ts }`.
- [ ] CLI subcommand `bffi-pipeline runs clear-fuseki` (manual diagnostic + recovery):
  - `--dry-run` default lists what would be dropped + triple counts.
  - `--apply` executes the drop.
  - `--verbose` shows per-graph URI + triple count + namespace classification (run-output / preserved).
  - `--force-clear` bypasses `MAX_TRIPLES` safety threshold (with confirmation prompt).
- [ ] Settings additions:
  - `BFFI_FUSEKI_CLEAR_ON_RUN_START: bool = True`
  - `BFFI_FUSEKI_CLEAR_MAX_TRIPLES: int = 100_000_000`
- [ ] CLI flag `--no-clear-fuseki` on pipeline-driving subcommands (`bffi-pipeline merge`, `reconcile`, `marc-to-bf`, etc.) sets `BFFI_FUSEKI_CLEAR_ON_RUN_START=false` for that invocation only.
- [ ] Fuseki unreachable at init: log warning ("Fuseki at <url> unreachable; skipping pre-run clear"), continue. `BFFI_FUSEKI_CLEAR_ON_RUN_START=strict` makes this fatal.
- [ ] Operator runbook documents: the preserve-vs-wipe boundary (graphs under `graph_base` go; everything else stays), the `--no-clear-fuseki` opt-out, the `runs clear-fuseki` recovery command.
- [ ] Unit tests:
  - `test_clear_drops_only_graph_base_prefixed` (three-graph fixture: two under `graph_base`, one Finto-style; only the two are dropped).
  - `test_clear_safety_threshold_refuses_large_graph` (graph > threshold; refused without `--force-clear`).
  - `test_clear_fuseki_unreachable_warns_and_continues` (non-existent URL; pipeline init succeeds with warning).
  - `test_clear_records_outcome_in_manifest`.
  - `test_clear_fuseki_strict_mode_fails_on_unreachable`.

### Cross-phase

- [ ] `make lint && make test` green at each phase landing.
- [ ] Operator runbook section in `docs/operator-runbook.md` covers: the canonical `<BFFI_RUNS_ROOT>/<run_uuid>/` layout, the prune workflow (dry-run-first discipline), Fuseki preserve-vs-wipe semantics, the "don't `--reset-exporter` mid-run" caveat (R9), the Fuseki cross-reference caveat (R6 — addressed by Phase H for the next run; pruned runs' historical references remain until `--reset-fuseki` drops them), and the "legacy dirs in scratchpad/data are not managed by `runs list / prune`" note (the Phase F migration was dropped; legacy dirs stay as historical artifact).
- [ ] Snapshot at `docs/performance/<date>-p-32-run-lifecycle.md` summarising what landed + the disk hygiene the operator gets (1-2 paragraphs + sample `runs list` output).

## Risks

- **R1 — Manifest write races.** Single-process pipeline assumption applies; atomic `.tmp` + rename ensures partial files never observed.
- **R2 — `prune` deletes the wrong thing.** `--dry-run` default; `--apply` requires a filter that excludes at least some runs; per-run sizes + totals printed pre-delete. No soft-delete in v1.
- **R3 — Legacy runs without manifests.** Phase F (the bulk-migration command) was dropped 2026-05-14; legacy dirs in `scratchpad/`, `data/` etc. remain on disk as historical artifact, outside `runs list / prune` scope. Operator manages them with `rm -rf` as before. A future `runs adopt <dir>` command (deferred) can pull individual legacy dirs into the canonical root if a real need surfaces.
- **R4 — Search root coverage drift.** Single `BFFI_RUNS_ROOT` in v1 sidesteps the multi-root problem.
- **R5 — Tag-protection bypass.** No `--ignore-tags` flag; operator must `untag` first to delete a tagged run.
- **R6 — Fuseki accumulates triples across runs.** Addressed by Phase H (pre-run clear). Pruned-run historical references addressed by Phase G's `--reset-fuseki` flag.
- ~~**R7 — Migration interrupted mid-way.**~~ Not applicable post-Phase-F-drop.
- ~~**R8 — Cross-filesystem migration.**~~ Not applicable post-Phase-F-drop.
- **R9 — Exporter restart drops in-flight metrics.** "Don't `--reset-exporter` mid-run" runbook note; gap bounded by ~15 s Prometheus scrape interval.
- **R10 — Prometheus admin API disabled.** Phase G enables it by default in `docker-compose.yml`; falls back to warning + skip if unreachable.
- **R11 — `BFFI_DATA_DIR` ergonomics regression.** Stays as escape hatch; startup-log warns when used; runbook documents the new pattern.
- **R12 — Misconfigured `graph_base` wipes vocabularies.** `BFFI_FUSEKI_CLEAR_MAX_TRIPLES` safety threshold (default 100M) catches it. Startup log echoes every URI before dropping.
- **R13 — Phase H runs against unreachable Fuseki.** Best-effort default (warn + continue); `BFFI_FUSEKI_CLEAR_ON_RUN_START=strict` makes it fatal.
- **R14 — Concurrent pipeline runs collide via Phase H.** Documented as "one pipeline at a time per Fuseki" in the runbook.

## Rollback procedure

Each phase is independently revertable:

- **Phase A / B / C / D**: revert the relevant commits; canonical root still exists but isn't actively managed.
- **Phase E**: revert. `Settings.data_dir` resolution returns to `BFFI_DATA_DIR`-or-default; new runs land outside the canonical root again.
- ~~**Phase F**~~ — dropped 2026-05-14; nothing to roll back.
- **Phase G**: revert; prune loses the reset flags but still works for on-disk deletion. Operator manually restarts exporter / wipes Prometheus TSDB if needed.
- **Phase H**: revert; pipeline init no longer clears Fuseki. Operator manually runs `bffi-pipeline runs clear-fuseki --apply` between runs if desired (the CLI is still useful standalone). Or set `BFFI_FUSEKI_CLEAR_ON_RUN_START=false` to disable without reverting.

No data loss across rollback for any phase.

## What this plan does NOT do (deferred)

- **Soft-delete on `prune --apply`.** Hard-delete in v1 with `--dry-run` default as the safety net.
- **Phase F — one-time `bffi-pipeline runs migrate` for legacy run dirs.** Dropped 2026-05-14 (operator call). Rationale: post-Phase-E every NEW run lands at the canonical `<BFFI_RUNS_ROOT>/<run_uuid>/` location, so the canonical-root invariant is established going forward. Migrating PAST runs — sweeping `scratchpad/overnight-sample-2026-05-13/`, `scratchpad/data-cataloguer-audit-*/`, etc. — would just make legacy dirs appear in `runs list` / `prune`, which is a convenience, not a load-bearing fix. The operator continues to manage legacy dirs with `rm -rf` as before. The Phase F design (5-test acceptance criteria; `--from <path>`, `--dry-run`, `.migration.log`, atomic per-dir moves, cross-filesystem fallback) stays preserved in the proposal-shape source at commit `1fdabcd` should the decision reverse.
- **`runs adopt <dir>` for one-off legacy dirs.** Pulls a single legacy dir into the canonical root with a synthesised `bffi-run.json` (`status="adopted-legacy"`). Deferred until a concrete need surfaces; with Phase F dropped, this is the only path back into managed-state for any individual legacy run.
- **`runs list` reads P-31's TSV `reviewed_at` column** to show "un-reviewed rows per run". Tight coupling to P-31's schema; deferred.
- **Run manifest exposed via Fuseki PROV-O graph.** v1 keeps the manifest on-disk only.
- **`runs adopt` reconstructs `stages_completed` from artifact signatures.** v1 stubs `status="adopted-legacy"`.
- **Promote `judge-cache.sqlite` / `reconcile-cache.sqlite` to a shared cross-run location.** Architecturally substantial; caches stay per-run in v1.
- **Per-stage TSDB partition reset.** Phase G is per-run-uuid coarseness.
- **Per-run Fuseki graph namespaces.** Would allow concurrent runs against one Fuseki (R14). Deferred to a future plan if multi-pipeline becomes a real need.
- **Vocabulary graph refresh.** `bffi-pipeline load-finto` is the existing path; Phase H leaves vocabularies untouched on every pipeline run.
- **Multi-root `--root` flag on `runs list` / `prune`.** Single `BFFI_RUNS_ROOT` in v1.
- **Cloud / remote storage.** All operations on local disk.

## Composition with sibling plans + proposals

- **P-31 (dashboard artifacts panel + per-run cataloguer-review TSVs)** — P-31's per-run TSVs become part of the artifact set this plan manages. P-31 can ship before or after P-32; the cleanup story in P-31's R4 becomes resolvable once `bffi-pipeline runs prune --keep-tagged` exists. The manifest's `pre_run_fuseki_clear` field is independent of P-31; the TSV paths are exposed via P-31's `bffi_artifact_path` gauge (Phase A.1 of P-31), unaffected.
- **P-30 (observability + audit-trail critical audit)** — sequenced *after* P-32. P-30's truth-table catalogues every surface this plan introduces: `bffi-run.json` schema, the `runs` CLI subcommand tree, the `<BFFI_RUNS_ROOT>` canonical layout, the `.exporter.pid` + `.migration.log` files, the Fuseki clear semantics, the Prometheus admin API usage.
- **P-22..29 (FP veto stack + audit-meta proposals)** — write to a run's `data_dir` like any other run; the canonical-root change is transparent to them (they read `settings.data_dir`; the resolution rule is what changed in Phase E).
- **`docs/performance/<date>-*.md` snapshots** — when a snapshot references a specific run, the operator should tag the run with `audit-bench` so it survives subsequent `prune --older-than` sweeps. Existing snapshots that cite `scratchpad/overnight-sample-2026-05-13/` stay as historical record (Phase F's path-rewrite was dropped); the path remains valid on disk because legacy dirs aren't migrated. Going forward, new snapshots should cite by `run_uuid` rather than path for durability — covered in the operator runbook note.
