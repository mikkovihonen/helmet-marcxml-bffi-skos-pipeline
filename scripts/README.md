# Pipeline driver scripts

End-to-end runners that chain the BFFI pipeline stages together with
per-stage milestone logging. Each emits `STAGE_<NAME>_START` and
`STAGE_<NAME>_DONE <elapsed>s` lines to stdout + a per-run log so a
`tail -F | grep STAGE_` streams progress without polling. Per-stage
stdout/stderr (rdflib warnings, summary tables) goes only to the log
file — the milestone stream stays uncluttered.

## When to use which

| Script | Stages run | Typical use | LLM calls |
|---|---|---|---|
| [`run-full-pipeline.sh`](run-full-pipeline.sh) | M2 → M3 → M5 → M6 → M8 → M9 → skosify → load | Bootstrap a fresh corpus → publish to Skosmos. | M6 judge + M9 picker. Hours on full corpus with Ollama serial. |
| [`run-fast-export.sh`](run-fast-export.sh) | M2 → M3 → M8 (empty decisions) → skosify → load → `ysa-disambiguation-report` | Surface cataloguer-side data-quality residue (YSA disambiguation worklist; source-tag distribution; no-candidate counts) without LLM cost. **Not for production publish** — no merge consolidation. | None. |
| [`republish.sh`](republish.sh) | M5 → M6 → M8 → M9 → skosify → load (subsettable via `--from-stage`) | Re-run downstream half after a stage's logic changed (e.g. an M6 auto-merge wiring fix). M2 + M3 outputs are assumed up to date. | Subset of M6 + M9 depending on entry point. |

## Required input

All three scripts read from a `MARCXML_DIR` (input) and write to
`BFFI_DATA_DIR` (output). Defaults:

- `MARCXML_DIR` — **required** for `run-full-pipeline.sh` and
  `run-fast-export.sh`; `republish.sh` doesn't need it (M2 already
  ran on a previous invocation).
- `BFFI_DATA_DIR` — defaults to `./data` for all three.
- `PIPELINE_LOG` — defaults to `<BFFI_DATA_DIR>/pipeline.log`.

## Examples

**Bootstrap the dev sample end-to-end** (the 13 curated records under
`tests/data/sample-marcxml/curated/`):

```bash
MARCXML_DIR=tests/data/sample-marcxml/curated \
BFFI_DATA_DIR=./data \
    scripts/run-full-pipeline.sh
```

**Fast pattern-mining on a random 200-record sample**:

```bash
MARCXML_DIR=/tmp/bffi-200-smoke/marcxml \
BFFI_DATA_DIR=/tmp/bffi-200-smoke/data \
    scripts/run-fast-export.sh
# → tail /tmp/bffi-200-smoke/data/pipeline.log
# → open /tmp/bffi-200-smoke/data/ysa-disambiguation-report.csv
```

**Re-publish after an M6 logic change** (e.g. tweaking the auto-merge
band threshold). M2 + M3 outputs from the previous run are kept:

```bash
BFFI_DATA_DIR=/tmp/bffi-200-smoke/data \
    scripts/republish.sh --from-stage m6
```

**Skosmos-only update** (canonical-reconciled.ttl is current; just
re-skosify and reload):

```bash
BFFI_DATA_DIR=./data \
    scripts/republish.sh --from-stage skosify
```

## Production-scale env overrides

For the full 800k Helmet corpus the LLM stages dominate. Sensible
overrides for `run-full-pipeline.sh`:

```bash
MARCXML_DIR=/path/to/helmet-marcxml \
BFFI_DATA_DIR=/path/to/production-out \
LLM_BASE_URL=http://localhost:8000/v1 \
M6_CONCURRENCY=16 \
    scripts/run-full-pipeline.sh
```

`LLM_BASE_URL` points at vllm-mlx (per `docs/runbook.md`);
`M6_CONCURRENCY` should match the value you locked in via the
runbook's `--concurrency` tuning sweep on your hardware.

## Skipping LLM-bound stages

Both `run-full-pipeline.sh` and `republish.sh` accept env-level
overrides to drop LLM work when not needed:

- `SKIP_M5_M6=1` — writes an empty `judge-decisions.jsonl` so M8 still
  has its expected input but no merging happens. Birds-of-fire-style
  duplicates will stay as separate canonical Works.
- `SKIP_RECONCILE=1` — copies `canonical.ttl` to
  `canonical-reconciled.ttl` unchanged so M10 has its expected
  input. Cataloguer literals stay as unresolved labels.

`run-fast-export.sh` always sets both.

## Idempotency + recovery

Each underlying CLI subcommand is idempotent:

- M2 / M3 skip records whose output is newer than the input unless
  `--force`.
- M5 skips its index rebuild when both faiss+idmap are newer than the
  BFFI corpus.
- M6 has crash-safe `--resume` (default) keyed on a sibling
  `judge-decisions.jsonl.checkpoint`.
- M8 / M9 / skosify / load run fully every time but at modest cost.

If a stage fails midway, fix the underlying issue and re-run the
script — completed stages are skipped where idempotent, partial work
is recovered where checkpointed.
