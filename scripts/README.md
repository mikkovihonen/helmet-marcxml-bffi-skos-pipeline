# Operator scripts

A small set of convenience wrappers. The pipeline itself is driven via
`bffi-pipeline <subcommand>` (typer CLI) and `make` targets; these
scripts cover cases that don't fit cleanly in either.

## Scripts

| Script | Purpose |
|---|---|
| [`start-mlx-lm.sh`](start-mlx-lm.sh) | Start `mlx_lm.server` with port + model resolved from `.env` (`LLM_BASE_URL_PRIMARY`, `LLM_MODEL_PRIMARY`). Foreground by default; background with `... &`. Extra args pass through to mlx-lm. Defaults match `docs/local-inference.md` with `--prompt-cache-size 100` (the M2 Max 64 GB safe budget per P-10 Phase C). |
| [`select-overnight-sample.py`](select-overnight-sample.py) | Build a stratified MARCXML sub-sample under `scratchpad/overnight-sample-<date>/` for full-pipeline benchmarking. Strata + rationale documented in the module docstring. |
| [`test-runs-lifecycle.sh`](test-runs-lifecycle.sh) | Manual smoke driver for the P-32 run lifecycle CLI (`bffi-pipeline runs list / info / tag / untag / prune / mark-complete / clear-fuseki`). Uses a throwaway `BFFI_RUNS_ROOT` under `/tmp/` so the operator's real runs root is untouched. Set `KEEP=1` to leave the test dir in place for inspection. |

## Driving the pipeline itself

Use the typer CLI directly — there is no shell wrapper:

```bash
uv run bffi-pipeline marc-to-bf <input-dir>     # M2: MARCXML → BIBFRAME
uv run bffi-pipeline bf-to-bffi                 # M3: BIBFRAME → BFFI
uv run bffi-pipeline embed                      # M5: FAISS index + candidates
uv run bffi-pipeline judge --concurrency 4      # M6: cascade judge
uv run bffi-pipeline merge                      # M8: canonical Works
uv run bffi-pipeline reconcile                  # M9: authority reconciliation
uv run bffi-pipeline skosify                    # M10: SKOS output
uv run bffi-pipeline load                       # M11: load into Fuseki
uv run bffi-pipeline runs list                  # P-32: enumerate runs
```

`make lint` and `make test` cover the dev-loop targets; see the
project Makefile for the full set.

## Sierra export

`uv run marcxml-export-sierra` is the entry point; see
`docs/runbook.md` § "Sierra export" for the smoke → validate → full
sequence. No shell wrapper.
