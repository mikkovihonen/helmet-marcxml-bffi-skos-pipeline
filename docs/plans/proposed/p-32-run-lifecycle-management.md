# P-32 — Run lifecycle management: manifest + list + prune + tagging CLI

**Status**: proposed.
**Scope**: 1-2 weeks across four phases, but each phase is independently shippable: A (manifest writer) is a prerequisite for B/C/D; B (list CLI), C (prune CLI), D (tagging CLI) can ship in any order.
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

Today the operator manages run accumulation by hand: remember which `BFFI_DATA_DIR` belongs to which bench, `rm -rf` the ones that are no longer needed. This is fragile in three ways:

1. **No registry of what runs exist.** There's no "show me all runs" command — the operator's mental model is the only index. On a machine with a dozen benches, audits, and production trials in `scratchpad/`, the operator has to `find . -name 'stage-events.jsonl'` to enumerate runs.
2. **No "delete runs older than X" pattern.** The closest thing today is `find <BFFI_DATA_DIR> -mtime +30 -delete`, which is dangerous (no preview, no per-run granularity) and doesn't distinguish "old but tagged as the gold-set" from "old and disposable".
3. **No tagging or status concept.** A run from three months ago that the cataloguer reviewed and signed off should stay (as the audit trail); a run from three months ago that was an exploratory bench whose findings are encoded elsewhere can be deleted. Today there's no way to mark the distinction; both look the same to `find -mtime`.

P-31's "per-run TSV accumulation" surfaced this for one specific artifact, but the underlying issue is wider. A run-lifecycle management system fixes it once, for every run artifact.

### Why now

- P-31 is sitting in backlog and its hygiene story (R4) depends on the operator having a clean way to prune old TSVs. P-32 is the natural prerequisite.
- The pre-production cycle on the 800 k corpus will multiply this problem by ~50x in disk per run — the manual approach scales worse the more we use the pipeline.
- The existing `manifest.json` in bench dirs is from the sample-selection script (seed, stratum fractions), not from the pipeline. So there's no conflict — P-32 introduces a new `bffi-run.json` per run dir for run-lifecycle metadata.

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

### How operators use this

Typical lifecycle:

```bash
# Start a bench (existing flow; manifest written automatically)
BFFI_DATA_DIR=scratchpad/bench-2026-06-01 \
  BFFI_RUN_DESCRIPTION="P-22 veto bench" \
  uv run bffi-pipeline ...

# Survey what's on disk
bffi-pipeline runs list --sort=started

# Mark the run as referenced by a perf snapshot
bffi-pipeline runs tag <uuid> audit-bench

# Six weeks later, sweep old exploratory runs
bffi-pipeline runs prune --older-than 30d --keep-tagged --status=completed
# (dry-run by default — prints the prune set; operator inspects)
bffi-pipeline runs prune --older-than 30d --keep-tagged --status=completed --apply
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

## Open questions

- **Soft-delete vs hard-delete in `prune --apply`.** Soft (move to `<root>/.archive/<run_uuid>/`) is recoverable but doubles disk during the transition. Hard (`rm -rf`) is immediate and saves space but unrecoverable. Recommend hard-delete in v1, with `--dry-run` default as the safety net. Soft becomes a future follow-up if the dry-run discipline turns out to be insufficient.
- **Should `runs list` include a "last reviewed" timestamp from the cataloguer-review TSV?** Crossing the P-31 layer to read the TSV's `reviewed_at` column would tell the operator "this run still has 12 un-reviewed rows in the source TSV." Useful but tightly couples P-32 to P-31's schema. Recommend deferring to a follow-up; P-32 v1 just shows the manifest fields.
- **Run manifest in Fuseki?** Loading the manifest's contents into Fuseki's PROV-O graph would make runs SPARQL-queryable across the catalogue. Probably yes long-term, but adds a Fuseki write path; v1 keeps it on-disk only.
- **Should `runs adopt` walk the artifacts in the dir and reconstruct `stages_completed`?** It can: every stage that wrote outputs left signatures. Doing so makes legacy runs first-class participants in `prune`. Trade-off: more code, more risk of getting the inference wrong. Recommend stub `status=adopted-legacy` in v1; full reconstruction is a follow-up if operators ask for it.
- **`description` field length / format.** Free text? Markdown-rendered? Recommend free text up to ~256 chars, no rendering. Long enough for "Q2 production trial after P-22 lands"; short enough to stay on one line in `runs list` output.
- **Pipeline crash mid-run: what does the manifest say?** If `started_at` is set but `ended_at` is never written, the manifest looks like a hung run. Recommend: a `runs status` poll command that detects this (manifest with `started_at` + no `ended_at` + no recent file mtimes inside the dir = `aborted`); the operator can `bffi-pipeline runs mark-complete <uuid> --status=aborted` to clean it up. Or auto-detect on `prune` and treat as eligible for `--include-legacy`.

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

**Cross-phase**
- [ ] `make lint && make test` green.
- [ ] Operator runbook section added describing the prune workflow + the Fuseki-cross-reference caveat (R6).

## What this proposal does NOT do

- **Manage Fuseki state.** Pruning a run dir leaves any `bffi:adminMetadata` triples that referenced it in Fuseki untouched. Out of scope; addressed by a future plan if it surfaces as a real cleanup burden.
- **Compress old runs.** A `runs compress <uuid>` command that gzips the run dir's artifacts would save disk without losing data — useful, but not v1 scope. Follow-up if the dry-run-then-`rm -rf` pattern proves too coarse.
- **Cloud / remote storage.** All operations are on local disk. If runs ever land on S3 or NAS, the CLI needs an adapter; not v1.
- **Replace the existing sample-selection `manifest.json`.** That file describes the sample composition, not the pipeline run; it stays as-is. The new file is `bffi-run.json` — distinct filename, distinct purpose.
- **Cross-run artifact references.** If two runs share an `embeddings.faiss` cache by symlink (a possible future optimisation), `prune` doesn't know about the dependency. v1 assumes runs are self-contained; document the assumption.

## Composition with sibling proposals + plans

- **P-30 (observability + audit-trail critical audit)** — sequence P-32 *before* P-30. The manifest is a new observability surface; P-30's truth-table catalogues it.
- **P-31 (dashboard artifacts panel + per-run cataloguer-review TSVs)** — P-31 sets up the per-run TSVs that motivate part of this proposal. P-31 and P-32 can ship in either order, but P-31's R4 (operator forgets to clean up reviewed TSVs) becomes resolvable once `bffi-pipeline runs prune --keep-tagged` exists. Recommended sequencing: P-31 ships, P-32 ships, P-30 audits both.
- **P-22..29** — orthogonal. They write to a run's `BFFI_DATA_DIR` like any other run; the manifest treats them no differently.
- **`docs/performance/<date>-*.md` snapshots** — when a snapshot references a specific run, the operator should tag the run with `audit-bench` so it survives subsequent `prune --older-than` sweeps. Worth a runbook note.
