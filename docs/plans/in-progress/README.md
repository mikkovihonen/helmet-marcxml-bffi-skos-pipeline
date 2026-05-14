# In progress

Plans where at least one phase has shipped but the plan's
"Definition of done" hasn't been met yet. The plan's `Phase
commits` field should carry concrete commit hashes for the shipped
phases and `<unfilled>` for the ones still ahead.

When the final phase commits and the plan's definition of done is
green, `git mv` the plan into [`../completed/`](../completed/) in
the same commit.

If the plan is dropped before completion, `git mv` it to
[`../abandoned/`](../abandoned/) and add a short
`Abandonment reason` section near the top.

## Current in-progress plans

- [`p-32-run-lifecycle-management.md`](p-32-run-lifecycle-management.md)
  — Phase A (`bffi-run.json` manifest writer + per-stage emit-site
  wiring + `runs mark-complete` CLI + Settings additions + 13 unit
  tests) shipped at `ff83135`. Phase E (canonical
  `<BFFI_RUNS_ROOT>/<run_uuid>/` invariant: `Settings.data_dir`
  derived from `runs_root / run_uuid` by default; `BFFI_DATA_DIR`
  retained as explicit-override escape hatch; startup-log echo
  distinguishes canonical from override; 5 unit tests) shipped at
  `1b9f1f0`. **Phase F (legacy-dir migration) dropped 2026-05-14**
  — post-Phase-E new runs already land canonical; legacy dirs in
  `scratchpad/` / `data/` stay as historical artifact. Phase C
  (`bffi-pipeline runs prune` CLI with --dry-run default,
  --apply-requires-filter guard, --keep-tagged / --keep-last
  preservation, --reset-exporter / --reset-prometheus /
  --reset-fuseki / --reset-all flag plumbing for Phases G + H + 9
  unit tests) shipped. Phase G (exporter PID + argv files written on `serve-metrics` startup with atexit cleanup; SIGTERM + optional relaunch on `--reset-exporter`; Prometheus admin-API delete-series + tombstone-clean on `--reset-prometheus` with graceful 405 / connection-refused fallback; `docker-compose.yml` enables `--web.enable-admin-api`; 8 unit tests) shipped at `bcf803a`. Phase H (`stages/fuseki_clear.py` with prefix-based DROP-graph helper + 100M-triple safety threshold; `bffi-pipeline load --no-clear-fuseki` / `--force-clear` flags; pre-load clear wired into `load_command`; manifest records `pre_run_fuseki_clear` via `update_manifest_field`; manual `bffi-pipeline runs clear-fuseki` CLI for diagnostic + recovery; `reset_fuseki` stub replaced with real implementation; 8 unit tests) shipped. Order continues: B → D.
