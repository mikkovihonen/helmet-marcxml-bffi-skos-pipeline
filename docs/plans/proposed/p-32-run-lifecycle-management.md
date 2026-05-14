# P-32 — Run lifecycle management: canonical root + manifest + CLI + migration

**Status**: proposed.
**Scope**: ~2 weeks across seven phases. A (manifest writer) is a prerequisite for B/C/D and is also the prerequisite for E (canonical root invariant) which is in turn a prerequisite for F (legacy-run migration). G (Prometheus reset) is independent and ships any time after A. B/C/D can ship in any order.
**Proposal-base commit**: `38f8e75`. To gauge drift before acting, run
`git diff 38f8e75..HEAD --
src/bffi_pipeline/cli.py
src/bffi_pipeline/stages/observability.py
src/bffi_pipeline/config.py`.

## Motivation

Each pipeline run writes a substantial bundle of artifacts to its `BFFI_DATA_DIR`:

| Artifact | 20 k bench | 800 k projection |
|---|---:|---:|
| `bibframe/*.rdf` | 437 MB | ~17 GB |
| `bffi/*.ttl` | 94 MB | ~4 GB |
| `bffi-corpus.ttl` | 53 MB | ~2 GB |
| `embeddings.faiss` + caches | varies | ~1-3 GB |
| Other (canonical.ttl, judge-decisions, stage-events, etc.) | tens of MB | hundreds of MB |
| **Total per production run** |  | **~25-50 GB** |

Plus the per-run cataloguer-review TSVs that P-31 will add — small individually, but workflow-coupled to the cataloguer's review cycle and therefore *especially* requiring per-run identity (see P-31's "One TSV per run" rationale).

Today the operator manages run accumulation by hand: remember which `BFFI_DATA_DIR` belongs to which bench, `rm -rf` the ones that are no longer needed. This is fragile in **five** ways:

1. **No registry of what runs exist.** There's no "show me all runs" command — the operator's mental model is the only index. On a machine with a dozen benches, audits, and production trials scattered between `scratchpad/`, `data/`, and ad-hoc paths, the operator has to `find . -name 'stage-events.jsonl'` to enumerate runs.
2. **No "delete runs older than X" pattern.** The closest thing today is `find <BFFI_DATA_DIR> -mtime +30 -delete`, which is dangerous (no preview, no per-run granularity) and doesn't distinguish "old but tagged as the gold-set" from "old and disposable".
3. **No tagging or status concept.** A run from three months ago that the cataloguer reviewed and signed off should stay (as the audit trail); a run from three months ago that was an exploratory bench whose findings are encoded elsewhere can be deleted. Today there's no way to mark the distinction; both look the same to `find -mtime`.
4. **No canonical location.** Runs land wherever `BFFI_DATA_DIR` was pointed: `scratchpad/<descriptive-name>/`, `data/`, occasionally `/tmp/...` for one-off tests, sometimes the repo root if the operator forgot to set the env var. The "where are my runs?" question has no single answer; tooling that wants to enumerate them needs a multi-root search heuristic (P-17's `--watch-glob` pattern applied to a different concern).
5. **No way to keep Prometheus + the dashboard in sync with on-disk reality.** When a run dir is deleted, the exporter's tail loop loses the sidecar, but the Prometheus registry retains the stale `{run_uuid="..."}` series in memory. The dashboard's `$active_run` dropdown still offers the pruned run; the gauges still show the last-known values until they age out via the metric's `stale_seconds`. The pipeline's on-disk state and the dashboard's observability state drift.

P-31's "per-run TSV accumulation" surfaced (3) for one specific artifact, but the underlying issue is wider. A run-lifecycle management system fixes it once, for every run artifact + the dashboard side.

### Why now

- P-31 is sitting in backlog and its hygiene story (R4) depends on the operator having a clean way to prune old TSVs. P-32 is the natural prerequisite.
- The pre-production cycle on the 800 k corpus will multiply the disk problem by ~50x per run — the manual approach scales worse the more we use the pipeline.
- The existing on-disk layout includes already-accumulated runs that don't fit any canonical structure (scratchpad/overnight-sample-2026-05-13/, scratchpad/data-cataloguer-audit-*/, etc.). Going forward without a canonical root means the legacy mess persists *and* new runs keep landing in non-canonical paths. Better to migrate once, then enforce.
- P-30's truth-table catalogue will audit the dashboard state including the run-uuid dropdown. The dashboard-vs-disk drift in (5) is exactly the kind of "the observability surface lies to the operator" pattern P-30 is meant to catch — fixing it here means P-30's catalogue starts clean.

### The structural decision: one canonical runs root

The fix that unifies (1), (4), and the migration story is a **single canonical runs root** with a fixed sub-directory layout:

```
<BFFI_RUNS_ROOT>/                           # default: <repo>/runs/
├── <run_uuid_1>/
│   ├── bffi-run.json                       # manifest (Phase A)
│   ├── stage-events.jsonl
│   ├── bibframe/, bffi/, ...
│   ├── canonical.ttl, canonical-map.jsonl, ...
│   └── cataloguer-source-review-<uuid>.tsv (P-31)
├── <run_uuid_2>/
│   └── ...
└── .archive/                               # (optional, Phase G reserved)
```

Every new run lands at `<BFFI_RUNS_ROOT>/<run_uuid>/`. The directory name *is* the uuid, period. Descriptions and intent live in the manifest's `description` field + the `tags` list, not in the dirname. This is a structural change: `BFFI_DATA_DIR` (operator-picked) becomes derived (`BFFI_DATA_DIR := BFFI_RUNS_ROOT / run_uuid` at pipeline init), with the env var retained only as a one-off escape hatch for unusual cases (and deprecated in the operator runbook).

Trade-off: operator loses the ability to encode "what this run was" in the dirname. Reclaimed via the manifest's `description` field, which is more durable (shows up in `runs list`, survives renames, doesn't get truncated by terminal width) and more accurate (the dirname was always an at-launch guess; the description can be updated post-hoc as the run's purpose clarifies).

## Approach

The shape is a **per-run manifest written by the pipeline** plus a **`bffi-pipeline runs` CLI command tree** that reads the manifests. Manifests live alongside the runs; the CLI discovers runs by walking a configured root and reading manifests. No external registry — the run dirs themselves are the source of truth.

### A. `bffi-run.json` manifest writer

Each pipeline invocation writes / updates `<BFFI_DATA_DIR>/bffi-run.json`. Initial schema sketch (refine on graduation):

```json
{
  "run_uuid": "<uuid4 hex>",
  "started_at": "2026-05-14T08:25:55+00:00",
  "ended_at": "2026-05-14T08:42:42+00:00",
  "bffi_data_dir": "/abs/path/to/run-dir",
  "description": "<BFFI_RUN_DESCRIPTION or empty>",
  "pipeline_git_sha": "38f8e75...",
  "pipeline_version": "<git describe or null>",
  "stages_completed": ["m2", "m3", "m5", "m6", "m8"],
  "stages_observed": ["m2", "m3", "m5", "m6", "m8", "m9"],
  "tags": [],
  "status": "running | completed | aborted | unknown"
}
```

- `started_at` written by `_init_observability()` on pipeline start.
- `stages_observed` appended to as each stage emits its `start` event.
- `stages_completed` appended to as each stage emits its `end` event.
- `ended_at` + `status` written at pipeline finalisation (or on explicit `bffi-pipeline runs mark-complete <uuid>`).
- `tags` written by the tagging CLI (Phase D).
- `pipeline_git_sha` from the runtime via `git rev-parse HEAD` if the repo is available; null otherwise.

The manifest is a small JSON file, written atomically (`.tmp` + rename). All updates re-serialise the whole file — race-free against the single-process pipeline assumption that already applies elsewhere in the codebase.

### B. `bffi-pipeline runs list` CLI

Walks the configured search root (`BFFI_RUNS_ROOT` env var, defaults to a list: `.`, `scratchpad/`, `data/`), finds every `bffi-run.json`, builds an in-memory list, renders a table:

```
$ bffi-pipeline runs list
RUN_UUID                          STARTED              STATUS     SIZE     TAGS    DESCRIPTION
8e7c4d...                        2026-05-13 17:02     completed  1.4 GB   gold    overnight 20k bench
p19-rebench-2026-05-14            2026-05-14 08:25     completed  1.4 GB           p-19 rebench
p19-phaseB-rebench                2026-05-14 09:12     completed  1.4 GB           p-19 phase B rebench
...
```

Sortable / filterable via flags: `--sort=started`, `--status=completed`, `--tag=gold`, `--older-than=30d`, `--limit=20`. Output formats: human-readable table (default), `--json`, `--tsv` for piping.

Legacy run dirs (no manifest) appear as `RUN_UUID=<inferred-from-dirname>`, `STATUS=unknown`, with a `--include-legacy` flag controlling whether they're listed.

### C. `bffi-pipeline runs prune` CLI

Selects runs and deletes their directories. Filters:

- `--older-than <duration>` — e.g. `30d`, `2w`, `6mo`. Compared against `started_at`.
- `--keep-last <N>` — keep the most-recent N runs (by `started_at`), prune the rest.
- `--keep-tagged` — preserve any run with at least one tag.
- `--status <comma-separated>` — only consider runs matching the status filter (e.g. `--status=completed,aborted`).
- `--include-legacy` — also consider directories without a manifest (default: skip; legacy dirs must be explicit).

Safety:

- `--dry-run` is the default. Operator must pass `--apply` to actually delete.
- Pre-flight prints the list of dirs that would be deleted with sizes + the total, plus runs preserved by `--keep-tagged` or `--keep-last`.
- After `--apply`, a single `rm -rf` per selected dir. No soft-delete in v1 — the dry-run preview is the safety net.

The CLI exits non-zero if `--apply` was passed without `--older-than`, `--keep-last`, or `--include-legacy` (i.e. no filter that excludes runs — refuse to prune *everything* without an explicit `--all` flag for that case).

### D. `bffi-pipeline runs tag` / `untag` / `info` CLI

- `bffi-pipeline runs tag <run_uuid> <tag>` — adds a tag to the run's manifest.
- `bffi-pipeline runs untag <run_uuid> <tag>` — removes a tag.
- `bffi-pipeline runs info <run_uuid>` — pretty-prints one manifest plus the dir size + the list of artifact files.

Tags are operator-managed strings. Suggested vocabulary (not enforced):

- `gold` — used in the gold-set or as a regression fixture.
- `audit-bench` — referenced from a `docs/performance/<date>-*.md` snapshot.
- `cataloguer-reviewed` — the run's cataloguer-source / cataloguer-target TSVs (from P-31) have been reviewed and the rows archived.
- `incident-<id>` — preserved for post-mortem of a specific incident.

The `--keep-tagged` flag on `prune` treats any non-empty tag list as "protected".

### E. Canonical runs root + new-run default path

Introduce `BFFI_RUNS_ROOT` env var (default `<repo>/runs/`). At pipeline init, the runtime computes:

```python
data_dir = settings.runs_root / settings.run_uuid
data_dir.mkdir(parents=True, exist_ok=True)
```

and exposes it via the existing `settings.data_dir` field — every stage continues to read `data_dir` exactly as today; only the resolution rule changes.

`BFFI_DATA_DIR` stays as a one-off escape hatch (set it to override the canonical path for a particular invocation, e.g. when reproducing a legacy run's exact paths) but is **deprecated** in the operator runbook. The startup log echoes the resolved path and warns if `BFFI_DATA_DIR` was used to override:

```
[bffi-pipeline] BFFI_RUN_UUID=8e7c4d...
[bffi-pipeline] data_dir=/Users/.../runs/8e7c4d.../ (canonical)
```

or:

```
[bffi-pipeline] BFFI_RUN_UUID=8e7c4d...
[bffi-pipeline] data_dir=/tmp/test-run/ (override via BFFI_DATA_DIR — non-canonical; bffi-pipeline runs list / prune will not see this run unless you adopt it)
```

`.gitignore` gets a `/runs/` entry; the directory contents are local-only by definition.

The `<repo>/runs/` default is convenient for the development setup but assumes the operator runs from the repo root. If the operator prefers a user-level path (`~/.bffi/runs/`) or a separate volume (`/mnt/bffi-runs/`), `BFFI_RUNS_ROOT` overrides. Recommended default stays repo-local until there's a concrete cross-clone use case.

### F. Migration of legacy run dirs

One-time migration command:

```bash
bffi-pipeline runs migrate --from scratchpad/ --from data/
# (dry-run by default; --apply to execute)
```

The command:

1. Walks each `--from` path recursively.
2. Identifies "run-shaped" directories — those containing `stage-events.jsonl` AND at least one of `bffi/`, `bibframe/`, `canonical.ttl`, `judge-decisions.jsonl`. Excludes derived-data dirs (e.g. `scratchpad/merge-cluster-verdicts/`) that have no `stage-events.jsonl`.
3. For each run-shaped dir, derives the `run_uuid`: extracts from the first `stage-events.jsonl` row's `run_uuid` field if present; otherwise generates a fresh uuid4. Logs which uuids are derived vs synthesised so the operator can spot-check.
4. Synthesises `bffi-run.json` from filesystem metadata (mtime → `started_at`; `status="adopted-legacy"`; `description` defaults to the old dirname so the operator's mental mapping survives; `stages_completed` left empty in v1 — Phase F is *adopt-and-stub*, full stages reconstruction from artifact signatures is a follow-up).
5. Moves the dir to `<BFFI_RUNS_ROOT>/<run_uuid>/` with `os.rename` (atomic on the same filesystem; falls back to `shutil.move` cross-filesystem with a warning).
6. Updates internal absolute paths in the manifest (`bffi_data_dir` field reflects the new location).

After migration, the legacy `scratchpad/`, `data/` directories may still contain non-run artifacts (the merge-cluster verdicts dir, the pre-2026-05-14 docs/proposals layout, etc.); those stay where they are. Only run-shaped dirs move.

Post-migration check: `bffi-pipeline runs list` enumerates everything in the new root; the operator can spot-check that the descriptions reflect the right runs, then tag the gold-set / audit-bench / cataloguer-reviewed entries appropriately.

**Rollback:** the migration is reversible while no new runs have landed at the canonical root since the migration: `bffi-pipeline runs migrate --rollback --to scratchpad/` reads each manifest's `description` field (which carries the old dirname for legacy adopts), moves the run back, deletes the synthesised manifest. Drop in v1 if it's not worth the complexity; the operator can manually `mv runs/<uuid> scratchpad/<old-name>` and `rm runs/<uuid>/bffi-run.json` if needed.

### G. Prometheus / exporter reset on prune

When `prune --apply` deletes a run dir, the dashboard should forget it. Two layers:

**G.1 — Exporter restart (in-memory registry reset).** The Prometheus exporter (`bffi-pipeline serve-metrics`) holds an in-memory `prometheus_client` registry that accumulates series across all run_uuids it's ever seen. When a run is pruned, the sidecar JSONL goes with it but the in-memory series stay. Restarting the exporter clears the registry; the next rehydrate-from-sidecar pass sees only the runs that still exist on disk.

`bffi-pipeline runs prune --apply --restart-exporter` reads the exporter's PID file (`<BFFI_RUNS_ROOT>/.exporter.pid`, written at `serve-metrics` startup; new in Phase G), sends `SIGTERM`, waits up to N seconds for clean exit, optionally relaunches with the same args (recorded in `.exporter.argv`).

**G.2 — Prometheus TSDB delete (durable reset).** Even after the exporter restart, the Prometheus instance scraping the exporter retains the deleted-run series in its TSDB until the metric's retention period elapses (Prometheus default: 15 days). The dashboard's `$active_run` dropdown still offers the pruned uuid, with stale values.

Prometheus's admin API can delete series:

```
POST /api/v1/admin/tsdb/delete_series?match[]={run_uuid="<pruned-uuid>"}
POST /api/v1/admin/tsdb/clean_tombstones
```

Requires Prometheus to be started with `--web.enable-admin-api`. The local-only deployment (under `docker-compose.yml`) is the only consumer; enabling the admin API there is safe — the Prometheus instance is bound to `localhost` from the operator's machine and not exposed externally.

`bffi-pipeline runs prune --apply --reset-prometheus` POSTs to the configured Prometheus admin endpoint (`BFFI_PROMETHEUS_URL`, default `http://localhost:9090`) for each pruned `run_uuid`, then triggers the tombstone cleanup. Falls back to a warning + skip if the admin API is disabled or unreachable (operator can rerun with `--no-reset-prometheus` to force the delete to skip).

**Combined: `--reset-exporter --reset-prometheus`** is the recommended flag pair for "make the dashboard forget this run entirely". Either flag alone is partial — exporter restart clears the live registry but not the TSDB history; TSDB delete clears the history but leaves the exporter still emitting (if it had cached state).

### How operators use this

**One-time migration** (operator runs once after Phase F ships):

```bash
# Preview what would move
bffi-pipeline runs migrate --from scratchpad/ --from data/

# Apply
bffi-pipeline runs migrate --from scratchpad/ --from data/ --apply

# Spot-check the post-migration state + tag the gold runs
bffi-pipeline runs list
bffi-pipeline runs tag <overnight-uuid> gold audit-bench
bffi-pipeline runs tag <cataloguer-uuid> cataloguer-reviewed
```

**Typical day-to-day lifecycle** (after migration, with all phases shipped):

```bash
# Start a bench. No BFFI_DATA_DIR needed — defaults to <runs_root>/<auto_uuid>/.
BFFI_RUN_DESCRIPTION="P-22 veto bench" uv run bffi-pipeline ...
# Startup log echoes the resolved path:
# [bffi-pipeline] data_dir=/Users/.../runs/8e7c4d.../ (canonical)

# Survey what's on disk
bffi-pipeline runs list --sort=started

# Mark the run as referenced by a perf snapshot
bffi-pipeline runs tag <uuid> audit-bench

# Six weeks later, sweep old exploratory runs AND clean the dashboard
bffi-pipeline runs prune --older-than 30d --keep-tagged --status=completed
# (dry-run by default — prints the prune set; operator inspects)
bffi-pipeline runs prune --older-than 30d --keep-tagged --status=completed \
  --apply --reset-exporter --reset-prometheus
# Pruned dirs gone; exporter restarted; Prometheus TSDB no longer has the
# stale {run_uuid="..."} series for the deleted runs.
```

## Prerequisites

- The pipeline's existing `BFFI_RUN_UUID` discipline is already in place (every invocation gets a uuid; emitter carries it). Phase A wires the manifest writer to that.
- `BFFI_RUN_DESCRIPTION` may not yet be a settings field; Phase A adds it as an optional env var (empty by default).
- Composes with P-31: P-31's per-run TSVs become part of the artifact set this proposal manages. P-31 doesn't depend on P-32 to ship, but the cleanup story in P-31's R4 references this proposal.
- Composes with P-30: the manifest is a new observability surface; P-30 audits it as part of the truth-table catalogue. Sequence P-32 before P-30.

## Risks

- **R1 — Manifest write races.** Multiple processes writing to the same `BFFI_DATA_DIR` would race on the manifest. Mitigation: same single-process assumption that applies everywhere else in the pipeline; document it. Atomic `.tmp` + rename ensures the file is never half-written from a single writer's perspective.
- **R2 — `prune` deletes the wrong thing.** Catastrophic if it happens. Mitigation: `--dry-run` as the default; explicit `--apply` required; refuse to prune without a filter that excludes at least some runs; print the full list with sizes before deletion; documented runbook section. Consider a `bffi-pipeline runs prune --restore-from-trash` follow-up if soft-delete becomes worth the disk overhead, but ship hard-delete in v1.
- **R3 — Legacy runs without manifests.** Pre-P-32 run dirs have no `bffi-run.json`. The CLI skips them by default (`--include-legacy` opts in). Risk: operator runs `prune --older-than 30d` and is surprised that the 50 legacy runs from before P-32 aren't touched. Mitigation: documentation + an explicit `bffi-pipeline runs adopt <dir>` command that synthesises a minimal manifest from filesystem metadata (mtime → `started_at`, no `stages_completed`, `status=unknown`). Adopt is opt-in, low-risk.
- **R4 — Search root coverage drift.** If the operator's runs land outside the default `BFFI_RUNS_ROOT` paths, `runs list` won't see them. Mitigation: support repeatable `--root` flags + a startup-log echo of the resolved roots. Same shape as P-17's `--watch-glob`.
- **R5 — Tag-based protection bypass.** Operator could pass `--ignore-tags` thinking they're being explicit, then lose a tagged run. Mitigation: don't provide an `--ignore-tags` flag in v1. If the operator wants to delete a tagged run, they untag it first. One-way protection.
- **R6 — Fuseki cross-references.** A pruned run's artifacts may be referenced by `bffi:adminMetadata` blocks loaded into Fuseki; deleting the run's `provenance.ttl` doesn't drop the Fuseki triples. Mitigation: out of scope here — this proposal manages on-disk artifacts only, not Fuseki state. A future "Fuseki garbage collection" plan addresses the cross-reference. Document the caveat in the operator runbook so the operator who pruned the run knows the SPARQL graph still carries (potentially stale) references.
- **R7 — Migration interrupted mid-way.** If `runs migrate --apply` crashes after moving some dirs but not others, the operator is left in a half-migrated state. Mitigation: per-dir move is atomic (`os.rename`); the migration command processes dirs one at a time and prints progress; re-running picks up where it left off because already-moved dirs are no longer in the source path. The dry-run preview lists everything that will move so the operator can scope to a smaller batch if cautious.
- **R8 — Cross-filesystem migration.** If `BFFI_RUNS_ROOT` is on a different filesystem from the legacy dirs (e.g. external SSD vs internal), `os.rename` fails and falls back to `shutil.move` (copy + delete). This temporarily doubles disk for the moving dir. Mitigation: pre-check available space on the destination filesystem; warn if total bytes to move > 80 % of free space. Operator can also work around by moving dirs in batches.
- **R9 — Exporter restart drops in-flight metrics.** A pipeline run that is *currently happening* during `prune --reset-exporter` loses any unscraped events between the previous Prometheus scrape and the restart. Mitigation: don't run `prune --reset-exporter` mid-pipeline. If the operator does it accidentally, the next stage event after the exporter comes back triggers a rehydrate from the run's sidecar — the metric reconstruction is lossy only for the gap between scrapes, which is at most one scrape interval (~15 s). Document the "don't prune mid-run" pattern in the runbook.
- **R10 — Prometheus admin API disabled.** The TSDB delete needs `--web.enable-admin-api` on the Prometheus instance. If the operator's compose config doesn't have it, `--reset-prometheus` warns and skips. Mitigation: Phase G updates `docker-compose.yml` to enable the admin API by default (safe — localhost-bound in the local stack); operators with custom Prometheus configs handle their own flag.
- **R11 — `BFFI_DATA_DIR` ergonomics regression.** Operators who scripted around `BFFI_DATA_DIR=scratchpad/<descriptive-name>` need to learn the new pattern. The env var stays as an escape hatch but loses its "default place" role. Mitigation: clear migration guide in the operator runbook + the startup-log warning when `BFFI_DATA_DIR` is set explicitly so the operator notices their script is still using the old pattern.

## Open questions

- **Soft-delete vs hard-delete in `prune --apply`.** Soft (move to `<root>/.archive/<run_uuid>/`) is recoverable but doubles disk during the transition. Hard (`rm -rf`) is immediate and saves space but unrecoverable. Recommend hard-delete in v1, with `--dry-run` default as the safety net. Soft becomes a future follow-up if the dry-run discipline turns out to be insufficient.
- **Should `runs list` include a "last reviewed" timestamp from the cataloguer-review TSV?** Crossing the P-31 layer to read the TSV's `reviewed_at` column would tell the operator "this run still has 12 un-reviewed rows in the source TSV." Useful but tightly couples P-32 to P-31's schema. Recommend deferring to a follow-up; P-32 v1 just shows the manifest fields.
- **Run manifest in Fuseki?** Loading the manifest's contents into Fuseki's PROV-O graph would make runs SPARQL-queryable across the catalogue. Probably yes long-term, but adds a Fuseki write path; v1 keeps it on-disk only.
- **Should `runs adopt` walk the artifacts in the dir and reconstruct `stages_completed`?** It can: every stage that wrote outputs left signatures. Doing so makes legacy runs first-class participants in `prune`. Trade-off: more code, more risk of getting the inference wrong. Recommend stub `status=adopted-legacy` in v1; full reconstruction is a follow-up if operators ask for it.
- **`description` field length / format.** Free text? Markdown-rendered? Recommend free text up to ~256 chars, no rendering. Long enough for "Q2 production trial after P-22 lands"; short enough to stay on one line in `runs list` output.
- **Pipeline crash mid-run: what does the manifest say?** If `started_at` is set but `ended_at` is never written, the manifest looks like a hung run. Recommend: a `runs status` poll command that detects this (manifest with `started_at` + no `ended_at` + no recent file mtimes inside the dir = `aborted`); the operator can `bffi-pipeline runs mark-complete <uuid> --status=aborted` to clean it up. Or auto-detect on `prune` and treat as eligible for `--include-legacy`.
- **Should `BFFI_RUNS_ROOT` default to `<repo>/runs/` or `~/.bffi/runs/`?** Repo-local is convenient for development (one operator, one clone, runs are co-located with the code that produced them). User-level survives clone-fresh-and-checkout cycles. Recommend repo-local for v1; if multi-clone usage emerges, switch the default and offer a migration command. Either way, `BFFI_RUNS_ROOT` is the operator-overridable single source of truth.
- **`migrate --apply` rollback.** Phase F describes per-dir atomic moves but no automated rollback. Should the migration command keep a `migration.log` of what it did so a `runs migrate --rollback --to <path>` can replay in reverse? Probably yes for v1 if it's cheap — append-only log of `(uuid, old_path, new_path, ts)` rows; rollback iterates in reverse. Defer if the implementation cost is non-trivial; the operator can manually `mv` runs back from the canonical root using `runs list --json | jq ...` to map uuids to old descriptions.
- **Exporter PID file location.** Phase G writes `.exporter.pid` somewhere. `<BFFI_RUNS_ROOT>/.exporter.pid` is cleanest (operator can see it next to the runs they're managing) but means the exporter has to know `BFFI_RUNS_ROOT` (it does — same settings module). Alternative: `~/.bffi/exporter.pid`. Recommend the runs-root location for v1; revisit if the operator manages multiple `BFFI_RUNS_ROOT` instances on one machine (unlikely).
- **Prometheus admin API enable-by-default in compose config.** Phase G's `--reset-prometheus` needs `--web.enable-admin-api`. Should the `docker-compose.yml` change ship as part of P-32 itself, or as a follow-up operator instruction? Recommend ship as part of P-32 G — the local-only deployment context makes "enable admin API" a safe default. Operators with hardened Prometheus configs can disable it themselves.
- **Migration of `judge-cache.sqlite` and `reconcile-cache.sqlite`.** Those caches are per-run today (sit inside each run dir). After migration, do they stay per-run, or do they get promoted to a shared `<BFFI_RUNS_ROOT>/.caches/` location so re-running against the same inputs reuses M6/M9 verdicts across runs? Promoting them is a substantial architectural change and not what this proposal is about — defer. P-32's migration keeps them in their existing per-run location.

## Acceptance criteria (drafted; refine on graduation)

**Phase A — Manifest writer**
- [ ] `bffi-run.json` schema defined in code (`src/bffi_pipeline/run_manifest.py` or similar), with Pydantic or dataclass-backed validation.
- [ ] Pipeline init writes the initial manifest with `started_at`, `run_uuid`, `bffi_data_dir`, `description`, `pipeline_git_sha`, `status="running"`.
- [ ] Each stage's `start` event appends to `stages_observed`; each stage's `end` event appends to `stages_completed`. Idempotent on retries.
- [ ] Pipeline finalisation writes `ended_at` + `status="completed"`.
- [ ] Unit tests pin schema, atomic write, and idempotent stage tracking.

**Phase B — `runs list`**
- [ ] `bffi-pipeline runs list` walks `BFFI_RUNS_ROOT` (with sensible defaults) and renders a table.
- [ ] Sort + filter flags (`--sort`, `--status`, `--tag`, `--older-than`, `--limit`).
- [ ] `--json` / `--tsv` output formats for scripting.
- [ ] Startup-log echo of the resolved roots (mirrors P-17 pattern).

**Phase C — `runs prune`**
- [ ] `bffi-pipeline runs prune` selects runs by `--older-than` / `--keep-last` / `--keep-tagged` / `--status` and prints them.
- [ ] `--dry-run` is default; `--apply` required to delete.
- [ ] CLI refuses to delete unless a filter that excludes at least some runs is set (no accidental "delete everything").
- [ ] Per-run size reported pre-delete + total reported post-delete.

**Phase D — Tagging + info**
- [ ] `bffi-pipeline runs tag` / `untag` / `info` commands.
- [ ] Tag operations are atomic against the manifest.
- [ ] `info` renders manifest + dir size + artifact-file enumeration.

**Phase E — Canonical runs root + new-run default path**
- [ ] `BFFI_RUNS_ROOT` settings field added (default `<repo>/runs/`); `.gitignore` updated.
- [ ] `settings.data_dir` resolution rule changes from "read `BFFI_DATA_DIR` env var" to "compute `runs_root / run_uuid` unless `BFFI_DATA_DIR` is explicitly set". Existing stage code that reads `settings.data_dir` is unchanged.
- [ ] Pipeline init creates the canonical dir (`mkdir -p`) and writes the initial manifest there.
- [ ] Startup-log echoes the resolved `data_dir` + a `canonical` / `override via BFFI_DATA_DIR` marker.
- [ ] Operator runbook documents the deprecation of `BFFI_DATA_DIR` for new runs.

**Phase F — Migration of legacy run dirs**
- [ ] `bffi-pipeline runs migrate` command exists. Discovers run-shaped dirs by the `stage-events.jsonl + (bffi/|bibframe/|canonical.ttl|judge-decisions.jsonl)` heuristic.
- [ ] Each discovered dir gets a synthesised `bffi-run.json` (status="adopted-legacy") and is moved (atomic `os.rename` or fallback `shutil.move`) to `<BFFI_RUNS_ROOT>/<run_uuid>/`.
- [ ] `--dry-run` is default; `--apply` required.
- [ ] Per-dir progress logged; partial-completion is safe (re-running picks up where it left off because moved dirs are no longer in the source paths).
- [ ] Cross-filesystem pre-check: warn if total bytes to move > 80 % of destination free space.
- [ ] Operator successfully runs the migration against this repo's current `scratchpad/` and `data/` dirs without losing data; post-migration `bffi-pipeline runs list` enumerates every previously-run pipeline invocation.

**Phase G — Prometheus / exporter reset on prune**
- [ ] Exporter writes `<BFFI_RUNS_ROOT>/.exporter.pid` + `.exporter.argv` on startup. Cleans them up on graceful exit.
- [ ] `bffi-pipeline runs prune --apply --reset-exporter` reads the PID file, sends SIGTERM, waits up to N seconds, optionally relaunches with the recorded argv.
- [ ] `bffi-pipeline runs prune --apply --reset-prometheus` POSTs to `<BFFI_PROMETHEUS_URL>/api/v1/admin/tsdb/delete_series?match[]={run_uuid="<uuid>"}` for each pruned run, then triggers tombstone cleanup. Falls back to a warning if the admin API returns 405 (not enabled).
- [ ] `docker-compose.yml` updated to start Prometheus with `--web.enable-admin-api` in the local-only stack.
- [ ] Operator runbook documents R9 ("don't `--reset-exporter` mid-run") and the combined `--reset-exporter --reset-prometheus` usage.

**Cross-phase**
- [ ] `make lint && make test` green for each phase landing.
- [ ] Operator runbook section added describing the full prune workflow + the Fuseki-cross-reference caveat (R6) + the migration story.
- [ ] After all phases land, `scratchpad/`, `data/`, and any other legacy locations contain no run-shaped dirs — all runs live under `<BFFI_RUNS_ROOT>/`. Spot-check: `find scratchpad data -name 'stage-events.jsonl' 2>/dev/null` returns empty.

## What this proposal does NOT do

- **Manage Fuseki state.** Pruning a run dir leaves any `bffi:adminMetadata` triples that referenced it in Fuseki untouched. Out of scope; addressed by a future plan if it surfaces as a real cleanup burden.
- **Compress old runs.** A `runs compress <uuid>` command that gzips the run dir's artifacts would save disk without losing data — useful, but not v1 scope. Follow-up if the dry-run-then-`rm -rf` pattern proves too coarse.
- **Cloud / remote storage.** All operations are on local disk. If runs ever land on S3 or NAS, the CLI needs an adapter; not v1.
- **Replace the existing sample-selection `manifest.json`.** That file describes the sample composition, not the pipeline run; it stays as-is. The new file is `bffi-run.json` — distinct filename, distinct purpose.
- **Cross-run artifact references.** If two runs share an `embeddings.faiss` cache by symlink (a possible future optimisation), `prune` doesn't know about the dependency. v1 assumes runs are self-contained; document the assumption.
- **Promote `judge-cache.sqlite` / `reconcile-cache.sqlite` to a shared cross-run location.** Tempting (would amortise M6/M9 cost across runs against similar inputs) but architecturally substantial. Caches stay per-run in v1.
- **Garbage-collect Fuseki triples for pruned runs.** Out of scope; separate plan when it surfaces (see R6).
- **Per-stage TSDB partition reset.** Phase G's `--reset-prometheus` deletes the whole series set for a `run_uuid`. Finer-grained "drop only M9 metrics for run X" isn't supported; the operator drops the whole run or none of it.

## Composition with sibling proposals + plans

- **P-30 (observability + audit-trail critical audit)** — sequence P-32 *before* P-30. The manifest is a new observability surface; P-30's truth-table catalogues it.
- **P-31 (dashboard artifacts panel + per-run cataloguer-review TSVs)** — P-31 sets up the per-run TSVs that motivate part of this proposal. P-31 and P-32 can ship in either order, but P-31's R4 (operator forgets to clean up reviewed TSVs) becomes resolvable once `bffi-pipeline runs prune --keep-tagged` exists. Recommended sequencing: P-31 ships, P-32 ships, P-30 audits both.
- **P-22..29** — orthogonal. They write to a run's `data_dir` like any other run; the manifest treats them no differently. (After Phase E ships, that path is `<BFFI_RUNS_ROOT>/<run_uuid>/`; they don't see the change.)
- **`docs/performance/<date>-*.md` snapshots** — when a snapshot references a specific run, the operator should tag the run with `audit-bench` so it survives subsequent `prune --older-than` sweeps. Worth a runbook note. Existing snapshots that reference `scratchpad/overnight-sample-2026-05-13/` get their paths rewritten as part of Phase F migration (or, less invasively, the snapshot text stays as historical record and the cited path resolves via `git log --follow` against the moved dir — the dir's `run_uuid` is the durable identifier going forward).
