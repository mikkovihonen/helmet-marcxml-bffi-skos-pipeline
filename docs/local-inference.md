# Apple Silicon / local inference

The pipeline runs end-to-end on a MacBook Pro M5 Max with 128 GB unified memory. **No paid LLM APIs.** Plan all LLM-dependent code around the local server.

## What we install and run

The LLM serving layer is [`mlx-lm`](https://github.com/ml-explore/mlx-lm) — Apple MLX team's LLM inference library, published on PyPI as `mlx-lm`. It exposes:

- `mlx_lm.server` — an OpenAI-compatible HTTP server (the production target for M6 + M9 + M3 LLM callers).
- `mlx_lm.convert` — one-shot weight conversion from raw HF checkpoints to MLX 4-bit.
- `mlx_lm.generate`, `mlx_lm.chat` — handy one-shot CLIs for smoke testing.

Apple MLX team maintenance + smaller transitive dep footprint (~15 packages) are the load-bearing reasons we picked mlx-lm over the higher-level `vllm-mlx` wrapper; the decision and trade-off table are recorded in [`plans/completed/p-02-inference-stack-tuning.md`](plans/completed/p-02-inference-stack-tuning.md) § A1.

### What we don't install (and why)

| Project | Why not |
|---|---|
| `mlx-vlm` ([Blaizzy/mlx-vlm](https://github.com/Blaizzy/mlx-vlm)) | Vision-language models; M6's judge is text-only. |
| `vllm-mlx` ([waybarrios/vllm-mlx](https://github.com/waybarrios/vllm-mlx)) | Richer feature set (continuous batching, paged KV cache, `--models-config` multi-model registry, MTP, MCP) but single-contributor maintenance + ~50 transitive deps. The maintenance argument tips toward mlx-lm for a multi-year pro-bono pipeline. Re-evaluated only if a P-02 phase reveals a load-bearing feature mlx-lm can't deliver. |

## Installation

mlx-lm is on PyPI. No source clone needed; a standalone venv keeps its deps out of the bffi_pipeline venv:

```bash
# Pick any host directory; ~/.venvs/mlx-lm is the suggested location.
mkdir -p ~/.venvs && uv venv ~/.venvs/mlx-lm --python 3.12
source ~/.venvs/mlx-lm/bin/activate
uv pip install mlx-lm
python -c "import mlx_lm; print(mlx_lm.__version__)"   # expect 0.31+
```

Activate the venv any time you want to run `python -m mlx-lm.*` commands. The bffi_pipeline CLI itself talks to mlx-lm over HTTP and doesn't need this venv on its PATH.

## Model acquisition

One-time. The pipeline's two judge models are `Qwen3-8B` (primary) and `Qwen3-32B` (fallback); a smaller `Qwen3-1.7B` is the draft model for speculative decoding (P-02 Phase C). Qwen3 (released 2025) dropped the `-Instruct` suffix from the conversational variants — the base `Qwen/Qwen3-<size>` repo *is* the chat-tuned model; `Qwen/Qwen3-<size>-Base` is the pretrained-only variant.

**Recommended: pull pre-quantised MLX checkpoints.** Qwen now publishes their own MLX 4-bit at `Qwen/Qwen3-8B-MLX-4bit`; mlx-community covers the 32B at `mlx-community/Qwen3-32B-4bit`. Faster than local conversion (network-bound, no quant pass) and the 8B is shipped by the model authors themselves. The Hugging Face CLI replaces `huggingface-cli` with `hf` in 2026:

```bash
source ~/.venvs/mlx-lm/bin/activate
hf download Qwen/Qwen3-8B-MLX-4bit       --local-dir ~/.mlx_models/Qwen3-8B-4bit
hf download mlx-community/Qwen3-32B-4bit --local-dir ~/.mlx_models/Qwen3-32B-4bit
# Optional, only if you're shipping P-02 Phase C:
hf download mlx-community/Qwen3-1.7B-4bit --local-dir ~/.mlx_models/Qwen3-1.7B-4bit
```

Disk: ~4 GB / ~17 GB / ~1 GB respectively after 4-bit quantisation.

**Convert from source instead** if you need a different quantisation, a non-`mlx-community` variant, or pinned reproducibility. The one-shot wrapper [`scripts/llm-pull.sh`](../scripts/llm-pull.sh) (P-02 § D2) calls `python -m mlx_lm convert -q --q-bits 4` and writes to `~/.mlx_models/<name>-4bit/`:

```bash
source ~/.venvs/mlx-lm/bin/activate
scripts/llm-pull.sh Qwen/Qwen3-8B
scripts/llm-pull.sh Qwen/Qwen3-32B
scripts/llm-pull.sh Qwen/Qwen3-1.7B
```

Times on the M5 Max: ~30-45 min for the 8B, ~1-2 h for the 32B, ~10 min for the 1.7B.

**Fallback if HF download is unreliable**: retry with `--max-workers 1` and / or set `HF_TOKEN` for higher rate limits. The Hugging Face hub-cache at `~/.cache/huggingface/` keeps partial downloads, so a flaky session resumes cleanly.

## Running the server

`mlx_lm.server` serves one model per process. For the cascade (primary + fallback) we run two processes on different ports.

`--chat-template-args '{"enable_thinking":false}'` is **load-bearing for Qwen3**: without it, Qwen3 emits its chain-of-thought into a non-standard `message.reasoning` field while `message.content` stays empty until the reasoning budget is exhausted — which breaks any client (including the pipeline's `langchain_openai.ChatOpenAI`) that reads `content`. Discovered during P-02 § A3.

```bash
# Primary on 8001
python -m mlx_lm server \
    --model ~/.mlx_models/Qwen3-8B-4bit \
    --host 127.0.0.1 --port 8001 \
    --chat-template-args '{"enable_thinking":false}' \
    --prompt-cache-size 200 \
    --prompt-cache-bytes 1073741824 &

# Fallback on 8002 (skip if RAM is tight; the cascade tolerates a single-port setup)
python -m mlx_lm server \
    --model ~/.mlx_models/Qwen3-32B-4bit \
    --host 127.0.0.1 --port 8002 \
    --chat-template-args '{"enable_thinking":false}' \
    --prompt-cache-size 200 \
    --prompt-cache-bytes 1073741824 &
```

Useful flags (run `python -m mlx_lm server --help` for the full list):

| Flag | Purpose |
|---|---|
| `--chat-template-args '{"enable_thinking":false}'` | Disable Qwen3 thinking mode server-side so generation goes to `message.content`, not `message.reasoning`. Required for our judge prompts. |
| `--prompt-cache-size 200` | Cache up to 200 distinct prompt prefixes (entry count). |
| `--prompt-cache-bytes 1073741824` | 1 GB byte budget for the prefix cache (raised so the entry-count cap binds first on our workload). |
| `--draft-model PATH` | Speculative decoding (P-02 Phase C). |
| `--num-draft-tokens 5` | Speculative-decoding lookahead depth. Upstream default. |
| `--decode-concurrency N` | Decode-side concurrency (continuous-batching-style). |
| `--prompt-concurrency N` | Prefill-side concurrency. |
| `--prefill-step-size N` | Chunk size for prefill. |

## Pointing the pipeline at the server

Once the server(s) are up, the pipeline reads `LLM_BASE_URL` (and, post-P-02 Phase D1, `LLM_BASE_URL_PRIMARY` + `LLM_BASE_URL_FALLBACK`) from `.env`.

`LLM_MODEL_*` must be the **absolute path** that was passed to `mlx_lm.server --model` — mlx-lm 0.31 reports the path as the model ID at `/v1/models` and rejects the basename (it falls through to a Hugging Face fetch and 401s). Update the home-dir prefix per operator.

```
# Single-port setup (cascade falls back to primary if no fallback URL set)
LLM_BASE_URL=http://127.0.0.1:8001/v1
LLM_MODEL_PRIMARY=/Users/<you>/.mlx_models/Qwen3-8B-4bit
LLM_MODEL_FALLBACK=/Users/<you>/.mlx_models/Qwen3-32B-4bit

# Two-port setup (post-P-02 D1)
LLM_BASE_URL_PRIMARY=http://127.0.0.1:8001/v1
LLM_BASE_URL_FALLBACK=http://127.0.0.1:8002/v1
LLM_MODEL_PRIMARY=/Users/<you>/.mlx_models/Qwen3-8B-4bit
LLM_MODEL_FALLBACK=/Users/<you>/.mlx_models/Qwen3-32B-4bit
```

Then any pipeline CLI call (`bffi-pipeline judge`, `bffi-pipeline reconcile`, etc.) picks up the new endpoint.

## Memory budget

| Component | Approx. resident size |
|---|---|
| FAISS HNSW index (800k × 1024 dim) | ~5 GB |
| BGE-M3 embedding model (loaded only during M5) | ~2.5 GB |
| Qwen3 8B 4-bit (primary judge) | ~5 GB |
| Qwen3 32B 4-bit (fallback judge) | ~18-20 GB |
| Qwen3 1.7B 4-bit (draft model, P-02 Phase C only) | ~1 GB |
| Fuseki + Skosmos containers | ~4-6 GB |
| OS + working memory | ~10-15 GB |
| **Typical concurrent peak (M5 Max, both judge models + draft + Fuseki + Skosmos)** | **~45-55 GB** |

Comfortable on 128 GB. **On smaller dev boxes** (64 GB), drop the 32B fallback during dev iteration — the cascade tolerates a single-port setup where the primary handles every pair.

## Throughput findings — P-02 § A6

Measured on **M2 Max, 64 GB unified memory** (the current dev box,
NOT the production target M5 Max with 128 GB). All M5 Max
recommendations below carry a *re-measure on the target hardware*
qualifier — these numbers are a floor, not a contract.

### Sweep results

Synthetic 32-pair slice constructed from `gold/gold.jsonl` (real
M6 prompt template + `_build_chain` from `src/bffi_pipeline/stages/judge.py`,
unique `(record_a, record_b)` per call). Bench script:
[`scripts/p02-a6-concurrency-bench.py`](../scripts/p02-a6-concurrency-bench.py).

| Configuration | c=1 | c=4 | c=8 | c=16 |
|---|---|---|---|---|
| Defaults, no prefix cache | 25.5 /min | 26.7 /min | 26.4 /min | 26.1 /min |
| `--decode-concurrency 8 --prompt-concurrency 8` only | 25.5 /min | 25.9 /min | 25.9 /min | 26.0 /min |
| `--decode-concurrency 8 --prompt-concurrency 8 --prompt-cache-size 200 --prompt-cache-bytes 1073741824` | **28.4 /min** | **31.2 /min** | **31.6 /min** | 30.2 /min |

Median per-call latency at the best configuration: 1.94 s (c=1) →
7.73 s (c=4) → 15.0 s (c=8). Latency scales roughly linearly with
concurrency — i.e. **mlx-lm queues** rather than truly batches on
the M2 Max for this workload. The throughput ceiling at c≈4-8
(~31 pairs/min) is bandwidth-bound, not memory-bound (peak resident
on the 8B server is ~4.7 GB).

### Why the gains are modest

- Each M6 pair has substantial unique user content (`record_a` /
  `record_b` differ on every call). The prompt-prefix cache only
  helps the shared SYSTEM section, not the per-pair tail.
- Decode dominates total wall-time (~200-token rationale per call);
  prefix caching saves only prefill.
- mlx-lm 0.31's `--decode-concurrency` batching gain on the M5 Max
  family for this workload pattern appears bandwidth-limited.

### M2 Max operational defaults

| Setting | Value |
|---|---|
| `M6_CONCURRENCY` (client `bffi-pipeline judge --concurrency`) | **4** |
| `mlx_lm.server --decode-concurrency` | **4** |
| `mlx_lm.server --prompt-concurrency` | **4** |
| `mlx_lm.server --prompt-cache-size` | 200 |
| `mlx_lm.server --prompt-cache-bytes` | 1073741824 (1 GiB) |
| `mlx_lm.server --chat-template-args` | `'{"enable_thinking":false}'` (Qwen3) |

The throughput knee is at `c=4`; `c=8` adds essentially nothing
(+1 %) and `c=16` starts to degrade. Picking `c=4` keeps the
server-side KV-cache pre-allocation tight (lower memory footprint
on the 64 GB box) at the cost of <2 % throughput.

### M5 Max re-measurement gates

Before kicking off a production batch on the M5 Max, re-run the
bench script and update this section. Specifically check whether:

- Higher `--decode-concurrency` (16, 24) actually pays off — the
  M5 Max has more memory bandwidth and may finally show true
  batched-decode parallelism this workload should support.
- The throughput ceiling stays at ~30 /min or moves materially.
  At 50 k escalate-band pairs and the current ceiling, a full M6
  run is ~28 hours; doubling that throughput cuts a production
  run to ~14 hours, which is a real operational win.

### TTFT measurement (P-02 § B4)

Phase B's acceptance criterion is *≥ 3× TTFT speedup* (time-to-
first-token) from prefix caching, not 3× end-to-end throughput.
Bench: [`scripts/p02-b-ttft-bench.py`](../scripts/p02-b-ttft-bench.py)
streams 8 sequential chat completions against the byte-stable
``_M6_PROMPT_PREFIX_FULL`` (~875 tokens) on a freshly-restarted 8B
server with the cache flags above. Call 0 is the cold-cache
reference; calls 1-7 hit the warm prefix.

Measured (M2 Max 64 GB):

| | TTFT |
|---|---|
| Cold (call 0) | **2.87 s** |
| Warm (calls 1-7, median) | **0.36 s** |
| **Speedup** | **7.99×** |

Comfortably above the ≥ 3× target. The cold-warm gap of ~2.5 s
matches the rough estimate of prefilling 875 tokens at ~350
tokens/s — i.e. **the warm path skips essentially all of the
system-prefix prefill**, and only the per-pair user delta is
processed before generation starts.

End-to-end throughput numbers above don't reflect this 8×
TTFT win because decode (~1.5-2 s for ~100-200 token rationales)
dominates the rest of total wall-time. For batched production
runs the TTFT savings still compound: at the A6 ceiling of
~31 pairs/min, ~80 % of every request's TTFT is now saved, which
matters more if a stage downstream (e.g. M9 picker) ever needs to
fire on every M6 verdict in real time.

### Speculative decoding — not enabled (P-02 § C abandoned)

The recommended production server **does not pass `--draft-model`**.
P-02 Phase C tried `--draft-model ~/.mlx_models/Qwen3-1.7B-4bit
--num-draft-tokens 5` on top of the Phase B prefix-cache config and
measured a ~50 % regression on the M2 Max 64 GB (Phase B
31.6 pairs/min → Phase C 14.0 pairs/min at c=8). Likely the
8B-target / 1.7B-draft ratio (~5×) is too small to amortise the
per-step draft-model overhead on Apple Silicon's memory-bandwidth
profile. The 1.7B model stays on disk under `~/.mlx_models/` for
future re-evaluation with different `--num-draft-tokens` settings
or a different draft-model size on the M5 Max. See P-02 § C5 in
the plan for the abandon trail.

## Verification

After install + conversion, smoke-test the server:

```bash
# Should print at least one model — whichever was loaded on :8001.
curl -s http://127.0.0.1:8001/v1/models | jq

# A one-shot generation against the loaded model.
source ~/.venvs/mlx-lm/bin/activate
python -m mlx_lm generate \
    --model ~/.mlx_models/Qwen3-8B-4bit \
    --prompt "Say 'hello'" --max-tokens 16
```

Then run the pipeline-side smoke (gold-set eval) once the server is reachable:

```bash
make eval LABEL=mlx-lm-smoke-$(date +%Y-%m-%d)
```

The eval harness uses whatever `LLM_BASE_URL` / `LLM_MODEL_*` `.env` carries.

## Cross-references

- [`docs/plans/completed/p-02-inference-stack-tuning.md`](plans/completed/p-02-inference-stack-tuning.md) — the plan of record for the mlx-lm inference stack.
- [`docs/plans/in-progress/p-03-m6-stall-watchdog.md`](plans/in-progress/p-03-m6-stall-watchdog.md) — per-call + per-pair watchdog around the LLM client.
- [`docs/archived/local-inference.md`](archived/local-inference.md) — the prior version of this doc, kept for the audit trail of the install-instruction iterations.
