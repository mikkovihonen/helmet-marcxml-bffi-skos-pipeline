# Production runbook

End-to-end recipe for running the BFFI pipeline against the
~800 k-record Helmet corpus on the M5 Max. Pre-M10 milestones
(M0-M7) are committed; M8-M11 will extend this document with merge
+ load + Skosmos config when those milestones land.

This is the *canonical* sequence — start here, not from individual
milestone notes.

## Pinned versions

| Component | Pinned | Where |
|---|---|---|
| `marc2bibframe2` | `third_party/marc2bibframe2` (git submodule) | M2 |
| Embedding model | `BAAI/bge-m3` (1024-dim, multilingual) | M5; benchmark via `bffi-pipeline embed-benchmark` |
| FAISS HNSW | `M=32`, `efConstruction=200`, `efSearch=64`, IP metric on L2-normalised vectors | M5 |
| LLM primary | `qwen3:32b-instruct-q4_K_M` (Ollama) / equivalent MLX 4-bit | M6 |
| LLM fallback | `qwen3:72b-instruct-q4_K_M` | M6 cascade |
| Fuseki | `stain/jena-fuseki:5.0.0` | M10 (`docker-compose.yml`) |
| Skosmos | `ghcr.io/natlibfi/skosmos:3.2` | M11 |

Override via environment / `.env`:

```
LLM_BASE_URL=http://localhost:11434/v1     # Ollama default
LLM_MODEL_PRIMARY=qwen3:32b-instruct-q4_K_M
LLM_MODEL_FALLBACK=qwen3:72b-instruct-q4_K_M
BFFI_DATA_DIR=./data
```

## Throughput expectations on the M5 Max

| Stage | Mode | Time on 800 k records | RAM peak |
|---|---|---|---|
| M2 MARCXML → BIBFRAME | one-shot | ~15-30 min (XSLT-bound) | small |
| M3 BIBFRAME → BFFI | one-shot | ~10-20 min | small |
| M4 Stage-1 blocking | one-shot | seconds | small |
| M5 embedding build | sentence-transformers `mps` | 30-60 min | ~5 GB index + 2.5 GB model |
| M5 candidate query | top-k=20 | seconds | reuses index |
| M6 cascade | Ollama serial | 70-170 hours per 50 k pairs | ~20 GB primary; +40 GB if loading fallback |
| M6 cascade | vllm-mlx batched | 10-25 hours per 50 k pairs | same |

Two things to plan around:

1. **Tighten the gray zone before kicking off M6.** Spec § 6 commits
   to ≥ 0.90 / ≤ 0.78 thresholds; the embed-stats output tells you
   how many pairs land in each band. If "escalate" is > 100 k pairs,
   re-tighten before committing to a multi-night run.
2. **vllm-mlx + concurrency for production.** Ollama is fine for
   gold-set runs and the few-hundred-pair test sweeps. Production
   passes use `--concurrency` ≥ 4 and the vllm-mlx server.

## End-to-end command sequence

The whole pipeline against a single record dir:

```bash
# 0. One-time benchmark to lock in the embedding model.
#    Takes ~10 min for first model download; the comparison itself
#    is seconds against gold/gold.jsonl (~13 pairs at bootstrap).
bffi-pipeline embed-benchmark
# → reports same_work / different_work mean similarity per
#   {BGE-M3, e5-large, jina-v3}, ranks by widest gap, names the winner.

# 1. M2 — MARCXML to BIBFRAME RDF/XML.
bffi-pipeline marc-to-bf <input-dir>
# → writes <BFFI_DATA_DIR>/bibframe/<bib_id>.rdf,
#   <BFFI_DATA_DIR>/helmet-map.jsonl, and
#   <BFFI_DATA_DIR>/bibframe/_errors.jsonl for any rejected files.

# 2. M3 — BIBFRAME RDF/XML to BFFI Turtle.
bffi-pipeline bf-to-bffi
# → writes <BFFI_DATA_DIR>/bffi/<bib_id>.ttl plus
#   <BFFI_DATA_DIR>/bffi/_validation.jsonl for SHACL flags.

# 3. M4 — block-size statistics (no output file; prints histogram).
bffi-pipeline workkey-stats <BFFI_DATA_DIR>

# 4. M5 — build the FAISS HNSW index and emit candidate pairs.
#    First run downloads BGE-M3 (~2.3 GB); ~30-60 min on the M5 Max.
bffi-pipeline embed
# → writes <BFFI_DATA_DIR>/embeddings.faiss,
#   <BFFI_DATA_DIR>/embeddings.idmap.json,
#   <BFFI_DATA_DIR>/embed-candidates.jsonl,
#   prints band counts and similarity histogram.
#
# Re-runs without --force are idempotent: skipped when both files
# are newer than the input BFFI Turtle.

# 5. M6 — cascade judge over the escalate band.
#    Default --concurrency=1 (Ollama serial). Crash-safe: --resume
#    is the default and picks up from <output>.checkpoint.
bffi-pipeline judge
# Production batch (vllm-mlx on :8000):
LLM_BASE_URL=http://localhost:8000/v1 \
    bffi-pipeline judge --concurrency 16
# → writes <BFFI_DATA_DIR>/judge-decisions.jsonl,
#   <BFFI_DATA_DIR>/judge-decisions.jsonl.checkpoint,
#   <BFFI_DATA_DIR>/provenance.ttl per spec § 8 (every cascade step
#   is one bffi-prov:WorkMergeDecision Activity), and
#   <BFFI_DATA_DIR>/judge-cache.sqlite (post-validation cache).

# 6. M7 — periodic provenance compaction (every ~90 days).
bffi-pipeline provenance compact --older-than 90d
# → strips bffi-prov:rawResponse from old Activities, refreshes
#   <BFFI_DATA_DIR>/provenance-meta.ttl#lastCompactedAt. CLI startup
#   prints a stderr nag once that sentinel is older than 90 days.
```

Each `bffi-pipeline ...` invocation runs the stale-provenance check
at startup; the warning fires once `provenance.ttl` exists *and* the
last compaction is missing or older than 90 days.

## --concurrency tuning sweep (one-time, before the production batch)

Per spec § 7 / BUILD_PLAN M6, sweep `{4, 8, 16, 32}` against a fixed
1 k-pair sample on vllm-mlx, measure throughput, and record the
chosen value here. Until the sweep runs, treat the 16-concurrency
default in the example above as a placeholder.

The recommended approach:

```bash
# Capture a 1 k-pair slice of escalate-band candidates.
head -1000 data/embed-candidates.jsonl > data/embed-candidates.sample.jsonl

for c in 4 8 16 32; do
    rm -rf data/judge-decisions.sample.jsonl* data/judge-cache.sqlite
    time LLM_BASE_URL=http://localhost:8000/v1 \
        bffi-pipeline judge \
            --candidates-path data/embed-candidates.sample.jsonl \
            --output-path data/judge-decisions.sample.jsonl \
            --concurrency $c \
            --no-provenance
done
```

Pick the value that maximises throughput without OOMing. Update this
section + the example command sequence above with the chosen value.

## Compaction cron suggestion

Once production data is flowing:

```cron
# crontab -e
0 4 1 */3 *  cd /path/to/bffi-pipeline && uv run bffi-pipeline provenance compact --older-than 90d >> ~/Library/Logs/bffi-compact.log 2>&1
```

(Quarterly is a comfortable margin under the 90-day staleness floor.)

## What's still missing

This runbook stops at M7. The end-to-end "Skosmos UI shows the
canonical Works" path needs:

- **M8** — apply judge decisions to mint canonical Work URIs and
  union `bf:identifiedBy` sets.
- **M9** — reconcile against KANTO / VIAF / YSO / KAUNO / MUSO.
- **M10** — Skosify overlay + Fuseki load.
- **M11** — Skosmos config + verified UI.

Update this runbook as each lands. Pinned versions stay in the table
above so the runbook is the one place the production stack is named.
