# P-17 — Exporter tails multiple stage-events sidecars + glob-based auto-discovery

**Status**: in-progress (started 2026-05-14).
**Source proposal**: `prop-17-exporter-multi-sidecar-discovery` (deleted on graduation; recover via `git show 9a0601d:docs/plans/proposed/prop-17-exporter-multi-sidecar-discovery.md`).
**Plan-base commit**: `18f53bf`. To gauge drift before re-executing or backporting, run
`git diff 18f53bf..HEAD --
src/bffi_pipeline/metrics_exporter.py
src/bffi_pipeline/cli.py`.
**Phase commits**:

- Phase A (multi-sidecar + watch-glob + per-sidecar error specs + startup-log echo): `9a0601d` (code + 5 unit tests, 2026-05-14).
- Phase B (bench smoke test): `<unfilled>` — operator-side verification on the next overnight bench launch.

**Owner**: shipped this session.
**Estimated wall-time**: half- to one-day. Actual: ~2 h including tests.

## Goal

The 2026-05-13 overnight bench surfaced two related observability gotchas:

1. The exporter resolves its sidecar path from `BFFI_DATA_DIR` / `BFFI_OBSERVABILITY_SIDECAR` at process startup. When the pipeline runs against a different `BFFI_DATA_DIR` than the exporter was launched with, the dashboard silently tails the stale sidecar. The bench's new run was invisible for ~5 hours.
2. Same shape of gotcha for M2 + M3 error JSONLs: `_default_error_specs` derives paths from a single global `data_dir` at startup, separately from `--sidecar`. The bench's `bibframe/_errors.jsonl` stayed invisible despite the panel having correct PromQL.

Eliminate both gotchas via four small additions to the exporter, all opt-in (defaults unchanged).

## Definition of done

- [x] `--sidecar` accepts multiple invocations (repeatable typer option). Default behaviour (single path from `BFFI_DATA_DIR` or `BFFI_OBSERVABILITY_SIDECAR`) preserved.
- [x] `--watch-glob` accepts a glob pattern; rescanned every `--glob-rescan-seconds` (default 30 s). New matches auto-attach. Uses `glob.glob(recursive=True)` so both CWD-relative and absolute patterns are accepted.
- [x] Each attached sidecar synthesises its own pair of error-spec paths from the sidecar's **parent dir**, not from a single global `data_dir` (step C of the proposal). Helper: `_error_specs_for_sidecar(sidecar_path)`.
- [x] Startup-log echoes the resolved sidecar set + each sidecar's co-located error JSONLs + active globs.
- [x] Unit test: two synthetic sidecars in a temp dir; rehydrate + tail picks up events from both. `bffi_stage_started_timestamp{run_uuid=...}` is set for run_uuids from each file.
- [x] Unit test: a sidecar in dir X with an `_errors.jsonl` in `X/bibframe/`; `bffi_stage_errors_total{run_uuid=...}` is set from that file (not from a separate `data/` dir). Pins step C's per-sidecar error-spec derivation.
- [x] Unit test: a glob that matches multiple sidecars at launch attaches all of them.
- [x] Unit test: `serve` with neither sidecars nor globs raises `ValueError` (user error, not silent empty endpoint).
- [x] `make lint && make test` green (ruff + mypy strict + 966 pytest passed).
- [ ] **Phase B — Smoke test on the bench**: launch exporter with `--watch-glob 'scratchpad/**/stage-events.jsonl'`, run a pipeline against any `BFFI_DATA_DIR`; dashboard shows the new run AND the M2+M3 failure-mode bargauge populates without an exporter restart. Operator action, pending the next bench launch.

## What shipped at 9a0601d

- `serve()` signature changed from `sidecar_path: Path` to `sidecar_paths: list[Path]`. Single-sidecar is the `len == 1` special case. Test wiring updated.
- New `_rescan_globs(watch_globs, already_attached) -> list[Path]` walks each glob via stdlib `glob.glob(pattern, recursive=True)`. `Path('').glob` rejected the absolute form — caught early by the watch-glob unit test.
- New `_attach_sidecar(...)` factors the per-sidecar rehydrate + tail-state install + error-spec derivation so explicit `--sidecar` and glob auto-discovery share the same code path.
- CLI `serve-metrics` command in `src/bffi_pipeline/cli.py` updated to accept repeatable `--sidecar`, repeatable `--watch-glob`, and `--glob-rescan-seconds`. Default-sidecar resolution gated on "neither `--sidecar` nor `--watch-glob` given" — operator who passes `--watch-glob` alone doesn't get the implicit default attached too.
- Startup log enumerates every attached sidecar's path + its derived error files + every active glob pattern. The silent-stale gotcha becomes a one-line diagnostic.

## Risks (residual)

- **R1 — duplicate-event ingestion** (from the proposal). Not deduplicated in this phase. Acceptable per the proposal's R1 mitigation note: the cumulative-counter pattern means most metrics still produce correct final values even on duplicate apply.
- **R2 — glob noise**. Mitigated by conservative defaults (no glob unless operator opts in) + startup-log echo.
- **R3 — file-handle proliferation**. Theoretical; in practice an operator has 2-5 concurrent sidecars.
- **R4 — race on file rotation**. Existing `_tail_step` truncation branch already handles this.

## What this plan does NOT do (deferred)

- **`bffi_exporter_watched_sidecar` gauge** (Phase B follow-up in the proposal's open questions): not shipped. The startup-log echo addresses the immediate "what's being watched?" question; a Prometheus gauge would be a nice dashboard panel but isn't load-bearing. Re-open if the operator wants it.
- **`--config exporter.yaml`**: not shipped per the proposal's open question. Env-var + CLI flags are sufficient until configuration grows beyond a handful of paths.
- **Dedupe of `(run_uuid, stage, event, ts)` across sidecars**: R1 stays open per the proposal.
