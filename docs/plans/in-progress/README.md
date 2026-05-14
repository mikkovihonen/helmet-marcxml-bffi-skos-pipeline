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
  tests) shipped. Phase E (canonical `<BFFI_RUNS_ROOT>/<run_uuid>/`
  invariant: `Settings.data_dir` derived from `runs_root / run_uuid`
  by default; `BFFI_DATA_DIR` retained as explicit-override escape
  hatch; startup-log echo distinguishes canonical from override; 5
  unit tests) shipped. Phases F / C / G / H / B / D still ahead in
  recommended order.
