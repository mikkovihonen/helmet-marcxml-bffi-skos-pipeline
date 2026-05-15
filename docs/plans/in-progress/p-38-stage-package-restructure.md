# P-38 — Restructure `src/bffi_pipeline/stages/` into per-stage packages and shrink the CLI toward runner-driven operation

**Status**: backlog. Graduated from `proposed/` on 2026-05-15 after eight design questions resolved in conversation; all `Lean:` notes from the proposal hardened into the decisions below. Source-proposal text is recoverable via `git show` once the move + rewrite commits land (the file moved as an untracked rename, so `git log --follow` won't trace through the graduation — the discussion log lives only in the conversation that produced it).

**Plan-base commit**: `cfb7202` (the HEAD at which the proposal was drafted and graduated; current `main` on 2026-05-15). Before executing, run
`git diff cfb7202..HEAD -- src/bffi_pipeline/stages/ src/bffi_pipeline/cli.py src/bffi_pipeline/runner.py tests/ Makefile docs/runbook.md docs/observability.md docs/local-inference.md`
to confirm no in-flight work has reshaped the surface. Particular attention to: P-31 (touches `cataloguer_review.py` callsites in several stages), P-32 (run-manifest surface), P-35 (M3 cascade), P-36 (M3 SPARQL migration deletes ~120 lines of `bf_to_bffi.py`).

**Phase commits**:

- Phase A (per-stage package relocation, Layer 1): shipped 2026-05-15 across `a05ed20` → `2fef135` (10 commits inside the layer: m2, m3, m5, m6, m8, m9, m10 relocations + observability extraction + release/diagnostics extraction + plan-doc sweep).
- Phase B (internal splits of M9 / M8 / M6 / M3, Layer 2): shipped 2026-05-15 across four commits. M3 split at `3cfcb04` (sanitize.py + language_detect.py + contributions.py); M6 split at `07088be` (prompts.py + validation.py + cache.py + clients.py); M8 split at `3f4ea43` (union_find.py + contribution_variants.py); M9 split at `44dfe03` (authority_clients.py + picker_cache.py). Deeper extractions deferred per the "Status 2026-05-15" note under the Phase B heading.
- Phase C (CLI shrink + flag-to-env-var conversion, Layer 3): **deferred to a follow-up plan.** The plan's Phase C deliberately deletes nine CLI commands and converts their flags to env vars — that's a real operator-facing breakage that wants its own sequenced PR with the Phase C-0 snapshot test + Phase C-1 audit doc landing first as a gate. Doing it in the same session as Phase A + B carries too much risk of an incomplete migration. The plan body's Phase C section stays as the spec for the follow-up plan; nothing in it needs revision.

**Owner**: operator (Mikko) + claude solo-then-commit. All three phases are mechanically verifiable — public API of each stage's `runner.py` is preserved exactly through Phases A and B (no signature changes; `__init__.py` re-exports keep the import contract); Phase C is a deliberate CLI breakage with a one-release migration window via hidden error stubs.

**Estimated wall-time**: ~4-5 days end-to-end.

- Phase A: 1-2 days. ~18 file moves + ~86 import-site rewrites + ~30 plan-doc reference updates. Mechanical but tedious; the audit sweep over `docs/plans/**/*.md` for `stages/<old>.py` references is the long pole.
- Phase B: 1-2 days. Four independent sub-module splits (M9 reconcile 2 986 → ~5 files; M8 merge 1 842 → ~5 files; M6 judge 1 754 → ~5 files; M3 bf_to_bffi 1 104 → ~4 files). Each split is moves only, no logic change. **M3 split is conditional on P-36 landing first** — P-36 deletes ~120 lines of `bf_to_bffi.py` post-process helpers, so splitting before P-36 forces redoing M3's split.
- Phase C: 1-2 days. Phase-0 snapshot + Phase-1 audit doc + Phase-2 deletion execution + Phase-3 diagnostics/evaluation relocation. Phase 1 is the long pole (catalogue every flag, every test callsite, every Makefile reference).

**Phase priority**: **Phase A ships first, unconditionally.** Phase B can interleave with Phase C *for stages where neither layer depends on the other* (which is true for all four B-eligible stages — Layer 2 splits the stage runner internals, Layer 3 deletes CLI commands that import from the public surface). M3's Layer-2 split waits for P-36. Within Phase C, the sub-phases run in order (0 → 1 → 2 → 3) — Phase 1's audit is the contract Phase 2 executes against.

**Sequencing prerequisites** (in-flight plans this one must coordinate with):

- **P-35** (M3 cascade follow-ups, **in-progress**). Touches `bf_to_bffi.py`. Phase A's M3 file rename is mechanically compatible with P-35's edits — coordinate so Phase A lands after P-35's remaining phases to keep the rebase cost on P-35. The M3 Layer-2 split (Phase B) waits for P-36 in any case.
- **P-36** (M3 SPARQL migration, **backlog**). **Phase B's M3 sub-split waits for P-36 to land.** Phase A's rename can ship before P-36 — the rename moves the file, P-36 then edits the renamed file. P-36's Definition of Done bullets reference `src/bffi_pipeline/stages/bf_to_bffi.py`; either Phase A runs after P-36 (preferred) or Phase A's commit updates P-36's path references in the same commit.

**Recently landed in this surface area** (already part of `main`; the Plan-base drift-check command at the top of this document will surface any callsite changes — listed here so the executor knows where to look if the drift check shows movement):

- **P-31** (dashboard artifacts panel, **completed**) — touched `cataloguer_review.py` and `append_*_row` callsites in `bf_to_bffi.py`, `marc_to_bf.py`, `merge.py`, `reconcile.py`.
- **P-32** (run lifecycle management, **completed**) — introduced the run-manifest surface.
- **P-34** (M8 mint anonymous main-entry works, **completed**) — touched `merge.py`'s mint logic.

## Motivation

`src/bffi_pipeline/stages/` is the canonical home for pipeline-stage code, but its layout has drifted from the live convention in four ways:

1. **Filenames don't carry the stage ID.** The codebase has settled on M-numbers as live stage identifiers (`runner.py:73-86`, `cli.py` prose, dashboard panels, plan docs). Filenames like `marc_to_bf.py`, `bf_to_bffi.py`, `embeddings.py`, `judge.py`, `merge.py`, `reconcile.py`, `load.py`, `skosify_run.py`, `load_finto.py` don't. Reading a stack trace or a plan reference forces an indirection through prose to know which stage broke.

2. **Some files have grown sprawling.** Line counts at `cfb7202`:

   | File | Lines | Stage |
   |---|---:|---|
   | `reconcile.py` | 2 986 | M9 |
   | `merge.py` | 1 842 | M8 |
   | `judge.py` | 1 754 | M6 |
   | `bf_to_bffi.py` | 1 104 | M3 |
   | `marc_to_bf.py` | 742 | M2 |
   | `embeddings.py` | 711 | M5 |
   | `load.py` | 460 | M10 |
   | `load_finto.py` | 420 | M10 |
   | `observability.py` | 357 | (shared) |
   | `ysa_disambiguation_report.py` | 347 | diagnostic |
   | `export.py` | 344 | release tooling |
   | `local_concept_resolver.py` | 275 | M9 satellite |
   | `probes.py` | 230 | (shared) |
   | `workkey.py` | 203 | diagnostic |
   | `skosify_run.py` | 201 | M10 |
   | `fuseki_clear.py` | 191 | M10 |
   | `watchdog.py` | 137 | (shared) |
   | `preprocess.py` | 1 | (stub) |

   The four files over 1 000 lines collectively hold 64 % of the directory's body and mix sub-concerns (M9 has candidate-pair iteration, KANTO/VIAF clients, picker logic, SPARQL builders, cache; M8 mixes corpus loading with mint logic with contribution variants).

3. **Non-stage utilities sit in the stage namespace.** `observability.py`, `watchdog.py`, `probes.py`, `fuseki_clear.py`, `export.py`, the empty `preprocess.py` are imported by stages but aren't stages themselves. `workkey.py` is a diagnostic (the `workkey-stats` command — `runner.py:73-76` explicitly notes it isn't on the canonical chain).

4. **`cli.py` has accumulated commands that should be runner-driven.** At 3 007 lines, it holds 22 top-level `@app.command(...)` definitions plus two sub-typers. Per-stage commands (`marc-to-bf`, `bf-to-bffi`, `embed`, `judge`, `merge`, `reconcile`, `skosify`, `load`, `export`) sit at the top level next to load-bearing operator commands (`run`, `plan`, `status`, `serve-metrics`) with no syntactic marker of their escape-hatch status. `docs/runbook.md:109` already encodes the intended direction ("For routine runs, use `bffi-pipeline run`"), and `runner.py` exposes `--from-stage` and `--force-stages` for stage-level control; the code lags the doc.

The user's directive that prompted this plan: stage-specific code must live in `src/bffi_pipeline/stages/m<N>/`, each stage's core workflow stays in a single file inside that package, and `cli.py` shrinks toward code- and runner-driven operation.

## Out of scope (deliberately not done in this plan)

- **No signature changes** to any stage's public `run(...)` function or its returned dataclasses through Phases A and B. The `__init__.py` re-export shim preserves the import contract bit-identically. Anyone refactoring stage signatures opens a separate plan.
- **No logic deduplication** during the Layer-2 splits. Splitting `reconcile.py` and `merge.py` is moves-only — even if Phase B exposes duplication between `bffi_pipeline.contrib_variants` and `m8/contribution_variants.py`, that consolidation becomes a follow-up plan.
- **No back-compat shim for the deleted CLI commands.** Phase C's hidden error stubs print a one-line migration message; they are not aliases that still work. Operators must migrate to `bffi-pipeline run --from-stage <stage> --force-stages <stage>`.
- **No CLI surface change for surviving commands.** The `runs_app` and `provenance_app` sub-typers stay flat under their existing names; `lookup-helmet` and `load-finto` stay at the top level; `bffi-pipeline workkey-stats` still works (just lives in a different module after Phase C-3).
- **No env-var promotion for `--from-stage` / `--force-stages` themselves.** These stay as CLI flags on `bffi-pipeline run` — they're the operator's primary stage-level lever, not tuning knobs.

## Decisions hardened from the proposal (no longer open)

| # | Decision | Rationale |
|---|---|---|
| 1 | Observability becomes its own package: `src/bffi_pipeline/observability/{events.py, watchdog.py, probes.py}`. | 724 lines of cohesive operator-facing telemetry deserves a package boundary; co-locates the surface as a unit. |
| 2 | `workkey.py` moves to `src/bffi_pipeline/diagnostics/blocking_stats.py` (out of `stages/`). | Not on `CANONICAL_STAGES`; only invoked from the `workkey-stats` CLI command (`cli.py:1736-1737`). |
| 3 | Every stage gets an umbrella `runner.py` as the single core-workflow file. | Symmetric directory shape; the umbrella is the natural mount point for the (now-deleted) per-stage CLI. |
| 4 | CLI surface stays flat for surviving commands (no `bffi-pipeline m9 reconcile` form). | The per-stage commands are deleted anyway, making the flat-vs-nested question mostly moot. |
| 5 | `eval`, `grow-gold`, `embed-benchmark`, `embed-stats` move to `src/bffi_pipeline/evaluation/commands.py`. | These are evaluation/benchmarking tools, not pipeline-stage invocations; a parallel `evaluation/` package alongside `diagnostics/` keeps the M12 work findable. |
| 6 | `provenance_app` stays inline in `cli.py`. Sharpens the rule: `cli.py` holds commands that operate on the pipeline as a whole, on runs, or on persisted state — never on a single stage invocation. | Provenance compaction is M7 graph maintenance, not a per-pipeline-run stage. |
| 7 | One PR per layer (three PRs total). Per-commit structure inside each PR carries the bisection burden; no back-compat shim including in transitional commit states. | Each PR's diff is large but each PR's commits are small; bisection always resolves to a single stage. |
| 8 | Per-stage canonical-chain CLI commands are *deleted*, not relocated. Their tuning flags become namespaced env vars (`M5_*`, `M6_*`, `M9_*` precedent — `cli.py:2380-2453`). Operator path for stage-level work is `bffi-pipeline run --from-stage <stage> --force-stages <stage>`. | The runner already plumbs env vars and `--from-stage`; the wrappers duplicate that surface. Trade-off: env vars are global per process — fine because stages run serially in the canonical chain. |

## Definition of done

### Phase A — Per-stage package relocation (Layer 1, one PR)

Mechanical relocation only — no logic change, no public-surface change. Each commit inside the PR moves one stage + rewrites every callsite + updates plan-doc references in lockstep, so bisecting to a broken commit resolves to a single stage.

- [ ] **Stage packages created.** Each of the following relocations lands as its own commit. Inside each commit: `mv` the file to its new location, add `m<N>/__init__.py` re-exporting the public surface (`run`, dataclasses, anything `cli.py` / `runner.py` / tests import today), rewrite every `from bffi_pipeline.stages.<old> import …` callsite in `src/` and `tests/`, update `docs/plans/**/*.md` references that name the old path, run `make lint && make test` green.

  - [ ] `marc_to_bf.py` → `m2/runner.py` (commit 1)
  - [ ] `bf_to_bffi.py` → `m3/runner.py` (commit 2)
  - [ ] `embeddings.py` → `m5/runner.py` (commit 3)
  - [ ] `judge.py` → `m6/runner.py` (commit 4)
  - [ ] `merge.py` → `m8/runner.py` (commit 5)
  - [ ] `reconcile.py` → `m9/runner.py` (commit 6)
  - [ ] `local_concept_resolver.py` → `m9/local_concept_resolver.py` (commit 6 or 7)
  - [ ] `ysa_disambiguation_report.py` → `m9/ysa_disambiguation_report.py` (commit 6 or 7 — *will move again in Phase C-3* to `diagnostics/`, but Phase A keeps it adjacent to its M9 dependency chain to minimise import churn)
  - [ ] `load.py` → `m10/load.py` (commit 8)
  - [ ] `skosify_run.py` → `m10/skosify_run.py` (commit 9)
  - [ ] `load_finto.py` → `m10/load_finto.py` (commit 10)
  - [ ] `fuseki_clear.py` → `m10/fuseki_clear.py` (commit 11)

- [ ] **Non-stage code extracted from `stages/`.** Each commit:

  - [ ] `observability.py` → `src/bffi_pipeline/observability/events.py` (rename within the move; module's body becomes the package's core event-emission module)
  - [ ] `watchdog.py` → `src/bffi_pipeline/observability/watchdog.py`
  - [ ] `probes.py` → `src/bffi_pipeline/observability/probes.py`
  - [ ] `src/bffi_pipeline/observability/__init__.py` re-exports `emit_if_active`, `set_active_emitter`, `get_active_emitter`, `emit_plan`, `emit_failed`, `emit_skipped`, `emit_health_probes`, `probe_fuseki`, `probe_mlx_lm`, `emit_watchdog_event` (the surface today's callsites import).
  - [ ] `export.py` → `src/bffi_pipeline/release/export.py` (new `release/` package).
  - [ ] `workkey.py` → `src/bffi_pipeline/diagnostics/blocking_stats.py` (new `diagnostics/` package; preserves the `compute_blocks` / `BlockStats` surface).
  - [ ] `preprocess.py` deleted (empty stub, no callers — verified with `grep -rn "preprocess" src/ tests/` returning only the file itself).

- [ ] **`stages/__init__.py` shim.** Left as a one-line file (empty docstring) — no back-compat re-exports per Decision 7. Any callsite that still imports from `bffi_pipeline.stages.<old>` fails loudly; the rewrite sweep is complete.

- [ ] **Plan-doc reference audit.** `git grep -l "stages/[a-z_]*\.py" docs/plans/` → for each match, replace with the new path. Lands as its own commit at the end of Phase A. Reviewers can audit this commit in isolation.

- [ ] **CI verification.** `make lint && make test` green at every commit inside the PR (the per-commit move-plus-callsite-rewrite discipline is exactly what makes this bisectable).

- [ ] **No new behaviour, anywhere.** `git diff cfb7202..HEAD --stat -- src/` shows only rename + import-statement changes (and the small set of `from … import` rewrites needed to reach the new paths). Anything else is a smell — reviewer asks for it to be split out.

### Phase B — Internal sub-module splits for the four sprawling stages (Layer 2, one PR)

Splits each of M9 reconcile, M8 merge, M6 judge, M3 bf_to_bffi into cohesive sub-modules within its package. Public surface stays bit-identical between commits — `__init__.py` re-exports the same names from the new file locations.

**Conditional:** **M3 split waits for P-36 to land.** P-36 deletes ~120 lines of `bf_to_bffi.py` post-process helpers; splitting before P-36 forces redoing the M3 split. If P-36 hasn't landed when Phase B starts, ship M9 + M8 + M6 splits and defer M3 to a follow-up.

**Status 2026-05-15:** All four stage splits shipped — M3 first (the safe pattern), then M6 / M8 / M9. Each split lifts cohesive sub-concerns out of `m<N>/runner.py` into sibling modules under the same package, with public surface preserved bit-identically via `# noqa: F401` re-imports in runner.py so tests reaching for private symbols through `m<N>.runner._private` keep working without changes.

Final shapes per stage:

- **M3** (`bf_to_bffi`, was 1104 lines): `runner.py` + `sanitize.py` (URI / date scrubbing) + `language_detect.py` (`prefLabel` retagging) + `contributions.py` (245$c cascade emitter).
- **M6** (`judge`, was 1754 lines → runner now 1259): `runner.py` + `prompts.py` (loading / hashing / section parsing) + `validation.py` (Pydantic schemas + Boundary-4 validators + `STUB_PHRASES` / `UNCERTAIN_MAX_CONFIDENCE` / `MIN_RATIONALE_CHARS`) + `cache.py` (SQLite judge-cache) + `clients.py` (LangChain chain builder + retry-error classifiers + byte-stable `_M6_PROMPT_PREFIX_*` constants).
- **M8** (`merge`, was 1842 lines): `runner.py` + `union_find.py` (path-compressed disjoint-set with lex-smallest-root union) + `contribution_variants.py` (F2 `skos:altLabel` binding pass). Other prospective splits (the ~470-line BFFI graph reader, the canonical-Turtle emitter, `_load_work_records_from_corpus`) sit on top of dataclasses `apply_merge` weaves together; extracting them needs splitting the dataclasses too — left for follow-up.
- **M9** (`reconcile`, was 2986 lines → runner now 2628): `runner.py` + `authority_clients.py` (Finto / VIAF HTTP clients + `AuthorityClient` Protocol) + `picker_cache.py` (P-10 Phase B persistent picker-decision cache). Picker schema + picker-pool orchestration sit too close to `apply_reconciliation`'s shared state for safe in-session extraction; left for follow-up.

Each per-file-ignores entry in `pyproject.toml` got updated as files moved.

- [ ] **M9 (`reconcile.py`, 2 986 lines) split.** Single commit moving the body into sub-modules within `m9/`. Final shape:

  - [ ] `m9/runner.py` — top-level `reconcile()` loop, decision orchestration, public `run()` entry point
  - [ ] `m9/kanto_viaf_client.py` — HTTP clients + SPARQL templates against KANTO/VIAF
  - [ ] `m9/picker.py` — candidate-picker logic
  - [ ] `m9/cache.py` — sqlite picker-cache layer
  - [ ] `m9/subject_requests.py` — subject/genreForm request iteration (already reached into by `ysa_disambiguation_report.py`)
  - [ ] `m9/local_concept_resolver.py` (from Phase A — unchanged)
  - [ ] `m9/ysa_disambiguation_report.py` (from Phase A — unchanged; moves to `diagnostics/` in Phase C-3)
  - [ ] `m9/__init__.py` re-exports the surface `cli.py` / `runner.py` / tests import today

- [ ] **M8 (`merge.py`, 1 842 lines) split.** Single commit:

  - [ ] `m8/runner.py` — per-bib mint orchestration, public `run()`
  - [ ] `m8/corpus_loader.py` — the P-19 throughput-optimised loader
  - [ ] `m8/contribution_variants.py` — local contribution-variant logic (NB: do *not* consolidate with `bffi_pipeline.contrib_variants` here — that's out of scope per "Out of scope" §2)
  - [ ] `m8/mint.py` — Work + Expression URI minting
  - [ ] `m8/post_process.py` — final-pass transformations
  - [ ] `m8/__init__.py` re-exports

- [ ] **M6 (`judge.py`, 1 754 lines) split.** Single commit:

  - [ ] `m6/runner.py` — cascade entrypoint, public `run()`
  - [ ] `m6/prompts.py` — prompt builders + hash logging (prompts themselves stay in `prompts/` per CLAUDE.md)
  - [ ] `m6/cache.py` — sqlite judge-cache layer
  - [ ] `m6/clients.py` — mlx-lm + fallback wiring
  - [ ] `m6/validation.py` — Pydantic verdict models + repair
  - [ ] `m6/__init__.py` re-exports

- [ ] **M3 (`bf_to_bffi.py`, 1 104 lines) split. CONDITIONAL on P-36.** Single commit (if executed):

  - [ ] `m3/runner.py` — convert-one + post_process orchestration, public `run()`
  - [ ] `m3/language_detect.py` — the `_retag_pref_labels` cascade
  - [ ] `m3/contributions.py` — `_propagate_non_primary_roles` + `_emit_extracted_contributions` (assuming P-36 hasn't already deleted these — if P-36 Phase A landed, these helpers no longer exist and this sub-module shrinks or disappears)
  - [ ] `m3/sanitize.py` — `_sanitize_uri_whitespace` + `_sanitize_date_literals`
  - [ ] `m3/__init__.py` re-exports

- [ ] **CI verification.** `make lint && make test` green at every commit. No public-API change — verifiable via `grep -rn "from bffi_pipeline.stages.m[0-9]" src/ tests/` returning the same set of imported names as before the PR.

- [ ] **Bench regression check.** Run the canonical chain against the curated dev sample (`memory/curated_dev_sample.md`); compare per-record output diffs to a pre-Phase-B run. Expect *zero* diffs — Phase B is moves only.

### Phase C — CLI shrink toward runner-driven operation (Layer 3, one PR)

Deletes per-stage canonical-chain CLI commands; converts their tuning flags to namespaced env vars; relocates diagnostic/evaluation commands to their own packages. `cli.py` shrinks from 3 007 lines to a projected 400-600 lines holding only orchestration, lifecycle, observability, query, and graph-maintenance commands.

Sub-phases run in order inside the PR — Phase 0 first, then 1, 2, 3.

#### Phase C-0 — Snapshot the pre-change CLI surface

- [ ] Add `tests/integration/test_cli_surface.py` capturing `bffi-pipeline --help` and `bffi-pipeline <cmd> --help` byte-for-byte for every command currently in `cli.py`. Golden snapshots under `tests/fixtures/cli-surface-baseline/` (one file per command + one for the root).
- [ ] Test asserts byte-equality against the baseline on every run. Lands as its own commit at the start of Phase C — establishes the baseline before any deletion.

#### Phase C-1 — Flag-to-env-var conversion audit (doc-only commit)

Produces a markdown table committed under `docs/plans/backlog/p-38-phase-c1-cli-audit.md` (or equivalent) capturing:

- [ ] Every CLI flag on every Class-B command (the nine canonical-chain commands: `marc-to-bf`, `bf-to-bffi`, `embed`, `judge`, `merge`, `reconcile`, `skosify`, `load`, `export`), with proposed env-var name and existing precedent if any (`M6_CONCURRENCY` already exists per `docs/local-inference.md:187`; `M9_CONCURRENCY`, `LLM_M9_FIELD_TIMEOUT_SECONDS`, `BFFI_M9_CACHE_DISABLED`, `BFFI_M9_PICKER_ORDERING` already exist per `cli.py:2380-2453`).
- [ ] Every test under `tests/` that invokes `bffi-pipeline <Class-B-cmd>` directly (sourced from `grep -rn "bffi-pipeline " tests/`). For each: target umbrella-runner call to replace the CLI invocation.
- [ ] Every `Makefile` target that invokes a Class-B command directly. For each: target replacement (`bffi-pipeline run --from-stage <stage>` form).
- [ ] Every doc reference under `docs/` that names a Class-B command (`docs/runbook.md:109`, `docs/local-inference.md:127`, `docs/observability.md:90`, `docs/tech-stack.md:110`). For each: target rewrite.
- [ ] **No code change in this commit.** The doc is the contract Phase C-2 executes against.

#### Phase C-2 — Execute the conversion (delete Class-B commands; rewrite callers)

Per the Phase C-1 table:

- [ ] Convert each Class-B command's flags to env vars in `src/bffi_pipeline/config.py` (the `get_settings()` source). Each conversion's commit body cites the C-1 table row it's executing.
- [ ] Rewrite every test callsite to use the umbrella runner. Commit per stage: `m2` callers, then `m3`, etc.
- [ ] Rewrite every `Makefile` target. One commit.
- [ ] Rewrite every doc reference. One commit (`docs/runbook.md` is the largest; `docs/local-inference.md:127`, `docs/observability.md:90`, `docs/tech-stack.md:110` are small).
- [ ] Delete the Class-B command functions from `cli.py` (commit per stage, in the same commit that adds the hidden error stub for the same command).
- [ ] **Hidden error stubs for one release.** For each deleted command, register a typer command with `hidden=True` that prints: `"Error: 'bffi-pipeline <name>' has been removed. Use 'bffi-pipeline run --from-stage <stage> --force-stages <stage>' instead. See docs/runbook.md."` and exits with code `2`. These stubs are deleted in a follow-up PR after operators have migrated.
- [ ] **Update the Phase C-0 snapshot test.** Each deletion commit also regenerates the golden snapshot for the affected command (now showing the error-stub help text) and the root `--help` (now showing one fewer command). The snapshot test stays green at every commit.
- [ ] `make lint && make test` green at every commit.

#### Phase C-3 — Relocate Class-C diagnostic + evaluation commands

- [ ] Create `src/bffi_pipeline/diagnostics/commands.py`. Move `workkey-stats` and `ysa-disambiguation-report` command bodies into it (their backing modules moved in Phase A: `blocking_stats.py` already in `diagnostics/`, `ysa_disambiguation_report.py` moves from `m9/` to `diagnostics/` here).
- [ ] Create `src/bffi_pipeline/evaluation/commands.py`. Move `embed-benchmark`, `embed-stats`, `eval`, `grow-gold` command bodies into it.
- [ ] `cli.py` mounts both as flat sub-typers — `bffi-pipeline workkey-stats`, `bffi-pipeline eval`, etc. continue to work bit-identically (golden snapshot stays green for these).
- [ ] Backing modules in `m5/` and elsewhere update their imports if needed; the public-API surface of each `m<N>/` stays bit-identical.

#### Phase C wrap-up

- [ ] `cli.py` final size in the 400-600 line range. Contents: `run`, `plan`, `status`, `serve-metrics`, `lookup-helmet`, `load-finto`, the `runs_app` + `provenance_app` mounts, the `diagnostics` + `evaluation` sub-app mounts, the hidden error-stub commands.
- [ ] `tests/integration/test_cli_surface.py` snapshots reflect the final state (Class-B commands appear as hidden error stubs; Class-C commands work as before; Class-A commands unchanged).
- [ ] `docs/runbook.md` rewritten to remove line 109's "individual stage commands are still available" claim — they're not.
- [ ] `make lint && make test` green.
- [ ] Bench regression: run the canonical chain against the curated dev sample. Expect identical output to a pre-Phase-C run (Phase C changes the CLI surface, not the pipeline behaviour).

## Risk register

1. **Import-cycle reintroduction inside `m9/`.** Today, `local_concept_resolver.py` and `ysa_disambiguation_report.py` import from `reconcile.py` (and vice versa is *not* the case — grep-verified at base). Moving all three into `m9/` keeps the existing direction; the `__init__.py` re-export must not introduce `m9/__init__.py → reconcile.py → m9/local_concept_resolver.py → m9/__init__.py` cycles.

   **Mitigation:** `__init__.py` re-exports use `from .runner import …` lines at the bottom of the file (after any module-load side effects) and never via the package name (`from bffi_pipeline.stages.m9 import …`). Sub-modules import siblings directly, not via the package.

2. **Plan-doc reference rot.** ~30 active plan docs name `src/bffi_pipeline/stages/<file>.py` paths in their Definition-of-Done bullets. Missed references mean a future executor of P-36 (say) follows the plan to `bf_to_bffi.py` and finds nothing.

   **Mitigation:** Phase A's plan-doc reference audit is its own commit (`git grep -l "stages/[a-z_]*\.py" docs/plans/` → replace each). Reviewers audit the commit in isolation.

3. **Layer-2 scope creep.** Splitting `reconcile.py` and `merge.py` without behaviour change is non-trivial; "concise" is a direction, not a finishing line, and the temptation to consolidate the contribution-variants logic between `m8/` and `bffi_pipeline.contrib_variants` is real.

   **Mitigation:** Phase B is strictly moves — no deletions, no consolidation, no signature changes. "Out of scope" §2 says so. Anything that requires touching `cli.py` / `runner.py` callsites is a follow-up plan.

4. **`stages/__init__.py` shim depth.** Today this file is one line. Adding back-compat re-exports for the old paths would defeat the point (filenames should carry stage ID; consumers should import from the new path).

   **Mitigation:** No back-compat shim. Update every callsite in the same PR. 86 import sites at base, all in our own `src/` + `tests/`, none in third-party code (there isn't any — this is a pro-bono solo-operator pipeline).

5. **Phase C deliberately breaks the per-stage CLI surface.** Scripts and Makefile targets invoking the nine deleted commands stop working. The risk isn't silent drift (the commands stop existing — loud) but *incomplete migration*: a callsite missed during Phase C-1's audit ships in Phase C-2 and breaks in production.

   **Mitigation:** Phase C-0's golden-snapshot test catches *new* divergence in CI; Phase C-1's audit is comprehensive (every test, every Makefile target, every doc reference); Phase C-2's hidden error stubs print a discoverable migration message (`Use 'bffi-pipeline run --from-stage <stage>' instead`) for one release window so any missed callsite fails with guidance rather than typer's default "unknown command" error.

6. **Env-var contention between concurrently-running stages.** Phase C-2 promotes stage tuning flags to env vars. Env vars are global per process — two stages running in the same `bffi-pipeline run` invocation cannot have different values of e.g. `M5_BATCH_SIZE`.

   **Mitigation:** Stages run serially in `CANONICAL_STAGES`; the only concurrency point is M5, which already spawns as a subprocess (`runner.py:150-151`) so even mutated env wouldn't leak to other stages. The namespaced naming convention (`M5_*`, `M6_*`, `M9_*`) means cross-stage collision is structurally impossible unless someone reuses a name.

7. **P-36 lands while Phase A is in flight.** Phase A renames `bf_to_bffi.py` to `m3/runner.py`; P-36 edits `bf_to_bffi.py`. If both land near the same time, the merge produces conflicts in the moved file.

   **Mitigation:** Either ship Phase A after P-36 lands (preferred — see Sequencing prerequisites), or in the same merge window coordinate which lands first and rebase the other across the rename. Phase A's M3 commit is a single file move; rebasing P-36 across it is mechanical.

## Rollback procedure

Each layer's PR is independently revertible. Inside a layer's PR, the per-commit structure makes per-stage rollback possible (revert the commit for the broken stage, keep the others).

**Phase A rollback:** `git revert` the PR commit. The 86 callsite rewrites revert with it; `stages/__init__.py` returns to its one-line state. Mid-PR rollback (one stage broken, others fine): `git revert` the specific stage's commit; that commit revert restores the old file path and rewrites callsites back. The single-commit-per-stage discipline is what makes this surgical.

**Phase B rollback:** Same shape — `git revert` the PR, or the specific sub-module-split commit. Because Phase B is moves only with bit-identical public surface, revert has no behaviour impact beyond moving the file body back to one file.

**Phase C rollback:** More disruptive because Phase C-2 deletes commands and converts flags to env vars. Revert restores: (1) the deleted command bodies in `cli.py`; (2) the original flag-handling code; (3) the rewritten tests / Makefile / docs back to CLI invocations. The env-var settings landed in `config.py` stay (they're additive and harmless if unused). Mid-PR rollback (Phase C-2 broken for one stage): `git revert` the specific stage's deletion commit, restoring that stage's CLI command while leaving the others deleted. The hidden error stubs go with the revert.

**Cross-layer rollback:** Phase B and Phase C don't depend on each other beyond Phase A being landed. Phase A being in place is required for both — reverting Phase A while Phase B or Phase C is landed produces import errors. If Phase A must be reverted, revert Phase B and Phase C first (in that order).
