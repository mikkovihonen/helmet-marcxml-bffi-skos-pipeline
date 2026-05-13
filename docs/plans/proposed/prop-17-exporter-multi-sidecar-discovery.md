# P-17 — Exporter tails multiple stage-events sidecars + glob-based auto-discovery

**Status**: proposed.
**Scope**: half- to one-day. Extend the exporter's tail loop to handle a list of sidecar paths; add `--watch-glob` for auto-discovery; switch error-spec derivation from a single global `data_dir` to per-sidecar parent-dir resolution.
**Proposal-base commit**: `11691bf`. To gauge drift before acting, run
`git diff 11691bf..HEAD --
src/bffi_pipeline/metrics_exporter.py
src/bffi_pipeline/cli.py`.

## Motivation

The 2026-05-13 overnight-bench launch surfaced an operator gotcha: the metrics exporter (`bffi-pipeline serve-metrics`) takes its sidecar path from `BFFI_DATA_DIR` or `BFFI_OBSERVABILITY_SIDECAR` at **process startup**. When the pipeline runs against a different `BFFI_DATA_DIR` than the exporter was launched with — a common pattern for bench runs, per-corpus working directories, or focused audits — the exporter silently keeps tailing the stale sidecar. The dashboard shows the old run's data; the new run is invisible until the operator notices the gap and restarts the exporter with `--sidecar <new-path>`.

Concrete trace from the 2026-05-13 overnight run:

- Exporter launched at 16:05 with default `BFFI_DATA_DIR=./data/` → tails `data/stage-events.jsonl`.
- Bench pipeline launched at 17:24 with `BFFI_DATA_DIR=scratchpad/overnight-sample-2026-05-13/` → writes events to `scratchpad/overnight-sample-2026-05-13/stage-events.jsonl`.
- Grafana dashboard's `$active_run` dropdown only sees the *old* runs from `data/stage-events.jsonl`. The new run (`c64d1207b7c6443dbcd6e1cbd5d6da15`) is invisible from 17:24 until ~22:00 when the operator noticed and manually restarted the exporter with `--sidecar` pointing at the right file.

The current rollback — restart the exporter with `--sidecar` — works but is friction. Two more-foot-gunny variants:

1. The operator forgets the gotcha entirely; an overnight run lands with the dashboard apparently empty and they conclude "observability is broken." This happened today.
2. Multiple bench runs in parallel (e.g. an audit pipeline + a production publish from different `BFFI_DATA_DIR`s) — the operator has to pick which one the exporter watches.

**Same gotcha, different symptom — error JSONLs.** ~22:30 on the same overnight run the operator noticed the **M2+M3 failure-mode bargauge was empty** despite the panel having refIds `topk(10, sum by (error_type) (bffi_stage_errors_total{stage=m2|m3, run_uuid="$active_run"}))`. Root cause: the exporter's `_default_error_specs(data_dir)` derives `bibframe/_errors.jsonl` + `bffi/_validation.jsonl` paths from `get_settings().data_dir` at startup — separately from the `--sidecar` flag. So `--sidecar` correctly switched the event stream but error rows from the bench's `scratchpad/overnight-sample-2026-05-13/bibframe/_errors.jsonl` stayed invisible. Rollback: also set `BFFI_DATA_DIR=<bench-dir>` on the exporter's environment. Same shape of gotcha as the sidecar one — the operator has to remember to align two settings instead of one.

## Approach

Four small additions to the exporter, all opt-in (defaults unchanged):

### A. `--sidecar` becomes repeatable

Today the CLI takes one `--sidecar` path. Change it to accept multiple:

```bash
bffi-pipeline serve-metrics \
    --sidecar data/stage-events.jsonl \
    --sidecar scratchpad/overnight-sample-2026-05-13/stage-events.jsonl
```

Internally the exporter maintains a `dict[Path, _TailState]` (one tail-state per sidecar) and the polling loop iterates over all entries. Existing single-sidecar code becomes the `len == 1` special case.

Surface: ~40 lines in `metrics_exporter.py` (the rehydrate + tail-loop functions both need to accept a list). Existing `_tail_step` / `_TailState` are reused as-is.

### B. `--watch-glob` for auto-discovery

A glob pattern (or several) that the exporter rescans periodically (default every 30 s; `--glob-rescan-seconds` to tune) to discover sidecars that didn't exist at launch time:

```bash
bffi-pipeline serve-metrics \
    --watch-glob '**/stage-events.jsonl' \
    --watch-glob '/tmp/bffi-*/stage-events.jsonl'
```

New matches → new `_TailState` entries. Disappeared matches → state retained (the tail-step already handles "file shrunk" via the truncation branch); next rescan re-attaches if the file reappears.

Surface: ~30 lines. The rescan is best-effort — a glob walk over a few hundred top-level directories is sub-millisecond.

### C. Error-spec paths derive from each sidecar's parent dir

Today `_default_error_specs(data_dir)` is computed once at process startup against `get_settings().data_dir`. With per-sidecar tailing from steps A + B this is the wrong shape: each watched sidecar has its OWN co-located `bibframe/_errors.jsonl` and `bffi/_validation.jsonl` (the pipeline convention is `<BFFI_DATA_DIR>/stage-events.jsonl`, `<BFFI_DATA_DIR>/bibframe/_errors.jsonl`, `<BFFI_DATA_DIR>/bffi/_validation.jsonl` all together).

Change the resolution: when the exporter attaches a new sidecar (whether via explicit `--sidecar` or glob auto-discovery), it also synthesises the matching error specs from the sidecar's parent dir. Concretely:

```python
def _error_specs_for_sidecar(sidecar_path: Path) -> list[_ErrorFileSpec]:
    data_dir = sidecar_path.parent
    return [
        _ErrorFileSpec(stage="m2", path=data_dir / "bibframe" / "_errors.jsonl", ...),
        _ErrorFileSpec(stage="m3", path=data_dir / "bffi" / "_validation.jsonl", ...),
    ]
```

The exporter's tail loop iterates over `{sidecar → [error_spec, error_spec, ...]}` pairs. Per-sidecar `_TailState` for the events JSONL pairs with per-error-file `_ErrorFileTailState` for the two error JSONLs.

A `--error-spec <stage>:<path>` flag stays available as an escape hatch for operators who want to point at error JSONLs from a directory layout that doesn't follow the standard pipeline convention (e.g. tests, ad-hoc post-mortem analysis).

Surface: ~20 lines (factor out the spec construction into `_error_specs_for_sidecar`; update the tail loop to iterate over the per-sidecar dict).

### D. Echo the resolved sidecar set on startup

The exporter today prints `[bffi-pipeline] metrics exporter listening on :9100`. Extend to also print:

```
[bffi-pipeline] tailing sidecars (3):
  /path/to/data/stage-events.jsonl
    + bibframe/_errors.jsonl, bffi/_validation.jsonl
  /path/to/scratchpad/overnight-sample-2026-05-13/stage-events.jsonl
    + bibframe/_errors.jsonl, bffi/_validation.jsonl
  /path/to/scratchpad/data-cataloguer-audit-2026-05-13-v2/stage-events.jsonl
    + bibframe/_errors.jsonl, bffi/_validation.jsonl
[bffi-pipeline] watch-glob: '**/stage-events.jsonl' (rescan every 30s, will pick up new sidecars automatically)
```

So an operator reading the exporter logs can immediately see whether their run's sidecar AND its co-located error JSONLs are being watched.

Surface: ~10 lines in `cli.py`.

## Prerequisites

- The exporter already rehydrates from a sidecar on startup (`rehydrate()` in `metrics_exporter.py`). Multi-sidecar rehydrate is a loop over the existing single-sidecar code.
- The current `_TailState` keys on byte position, not file identity; it's reusable per path.
- Prometheus scrape model: one exporter exposes one `/metrics` endpoint with metrics from all watched sidecars. No Prometheus-side change.

## Risks

- **R1 — duplicate-event ingestion.** If the same `(run_uuid, stage, event, ts)` appears in two sidecars (e.g. operator copied a sidecar around), the exporter would double-apply. **Mitigation**: don't dedupe in v1 (adds complexity for an unlikely case); document the gotcha. The cumulative-counter pattern means most metrics still produce correct final values even on duplicate apply — only rate / throughput would be inflated.
- **R2 — glob noise.** A too-broad glob (e.g. `~/**/stage-events.jsonl`) might pick up files from unrelated projects or stale data dirs. **Mitigation**: glob defaults are conservative (no default; only set when operator explicitly opts in). Echoed at startup so the operator sees what's matched.
- **R3 — file-handle proliferation.** Each watched sidecar holds a Python file handle during the tail step. With 10+ sidecars the handle count grows linearly. **Mitigation**: in practice an operator has 2-5 concurrent sidecars; 10+ is a misuse case. If it becomes a real problem, cap with a `--max-watched` flag.
- **R4 — race on file rotation.** If a glob match disappears AND a new file at the same path appears between rescans, the exporter's `_TailState` retains the old byte position and might miss events. **Mitigation**: the existing `_tail_step` truncation branch already handles this — when `current_size < last_pos`, it re-reads from byte 0.

## Open questions

- Should the exporter ALSO accept a config file (`--config exporter.yaml`) listing sidecars and globs? Probably no — env-var + CLI flag is enough until the configuration grows beyond a handful of paths.
- Should the dashboard expose a "currently-tailed sidecars" panel? Worth doing as a Phase B follow-up, sourced from a new `bffi_exporter_watched_sidecar` gauge with one entry per path. Small surface; could ride along with this proposal.

## Acceptance criteria

- [ ] `--sidecar` accepts multiple invocations (repeatable typer option). Default behaviour (single path from `BFFI_DATA_DIR` or `BFFI_OBSERVABILITY_SIDECAR`) preserved.
- [ ] `--watch-glob` accepts a glob pattern; rescanned every `--glob-rescan-seconds` (default 30 s). New matches auto-attach.
- [ ] Each attached sidecar synthesises its own pair of error-spec paths from the sidecar's **parent dir**, not from a single global `data_dir` (step C). `--error-spec` escape hatch available for non-standard layouts.
- [ ] Startup-log echoes the resolved sidecar set + each sidecar's co-located error JSONLs + active globs.
- [ ] Unit test: two synthetic sidecars in a temp dir; rehydrate + tail picks up events from both. Assert that `bffi_stage_started_timestamp{run_uuid=...}` is set for run_uuids from each file.
- [ ] Unit test: a sidecar in dir X with an `_errors.jsonl` in `X/bibframe/`; `bffi_stage_errors_total{run_uuid=...}` is set from that file (not from a separate `data/` dir). Pins step C's per-sidecar error-spec derivation.
- [ ] Unit test: a glob that matches no files at launch but matches one mid-run; assert the new file's events surface within one rescan interval.
- [ ] Smoke test on the bench: launch exporter with `--watch-glob '**/stage-events.jsonl'`, run a pipeline against any `BFFI_DATA_DIR`; dashboard shows the new run AND the M2+M3 failure-mode bargauge populates without an exporter restart.
- [ ] `make lint && make test` green.

## What this proposal does NOT do

- Doesn't change the default sidecar resolution. Operators who only ever use the default `data/stage-events.jsonl` see no behaviour change.
- Doesn't add dedupe logic for `(run_uuid, stage, event, ts)` collisions across sidecars (R1). The simple add-everything approach is correct as long as one run_uuid lives in exactly one sidecar — which is the normal case.
- Doesn't add inter-process signalling (e.g. pipeline POSTs "I'm using sidecar X" to the exporter). Glob discovery covers the same operator workflow with strictly less coupling.
- Doesn't extend to log files, watchdog sidecars, or any other JSONL stream the pipeline writes. Scope is the `stage-events.jsonl` family only.
