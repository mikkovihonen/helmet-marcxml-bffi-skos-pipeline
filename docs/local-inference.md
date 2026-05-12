# Apple Silicon / local inference

The pipeline runs end-to-end on a MacBook Pro M5 Max with 128 GB unified memory. **No paid LLM APIs.** Plan all LLM-dependent code around the local server.

## What we install and run

The LLM serving layer is [`mlx-lm`](https://github.com/ml-explore/mlx-lm) — Apple MLX team's LLM inference library, published on PyPI as `mlx-lm`. It exposes:

- `mlx_lm.server` — an OpenAI-compatible HTTP server (the production target for M6 + M9 + M3 LLM callers).
- `mlx_lm.convert` — one-shot weight conversion from raw HF checkpoints to MLX 4-bit.
- `mlx_lm.generate`, `mlx_lm.chat` — handy one-shot CLIs for smoke testing.

Apple MLX team maintenance + smaller transitive dep footprint (~15 packages) are the load-bearing reasons we picked mlx-lm over the higher-level `vllm-mlx` wrapper; the decision and trade-off table are recorded in [`plans/in-progress/p-02-inference-stack-tuning.md`](plans/in-progress/p-02-inference-stack-tuning.md) § A1.

For development and gold-set evaluation runs we also keep **Ollama** installed as a fallback / quick-iteration option. P-02 ships the swap toward mlx-lm-by-default.

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

**Fallback if HF download is unreliable**: pull the GGUF blobs Ollama already has at `~/.ollama/models/blobs/`, identify them by manifest, and convert via `mlx_lm.convert --hf-path` against the GGUF path. Flakier; only use if HF download repeatedly fails.

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

## Ollama as the dev fallback

Until P-02 Phase D5 ships, Ollama remains a documented option for fast iteration and gold-set runs:

```bash
brew install --cask ollama && open -a Ollama
ollama pull qwen3:8b-q4_K_M               # primary judge (~5 GB)
ollama pull qwen3:32b-q4_K_M              # fallback (~20 GB)
```

`.env` for Ollama:

```
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=ollama
LLM_MODEL_PRIMARY=qwen3:8b-q4_K_M
LLM_MODEL_FALLBACK=qwen3:32b-q4_K_M
```

After P-02 Phase D6 ships, the Ollama path is removed from the recommended setup but remains usable as a manual fallback for incident triage.

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

Then run the pipeline-side smoke (gold-set eval) once both backends are reachable:

```bash
make eval LABEL=mlx-lm-smoke-$(date +%Y-%m-%d)
```

The eval harness uses whatever `LLM_BASE_URL` / `LLM_MODEL_*` `.env` carries.

## Cross-references

- [`docs/plans/in-progress/p-02-inference-stack-tuning.md`](plans/in-progress/p-02-inference-stack-tuning.md) — the active migration plan from Ollama to mlx-lm.
- [`docs/plans/in-progress/p-03-m6-stall-watchdog.md`](plans/in-progress/p-03-m6-stall-watchdog.md) — per-call + per-pair watchdog around the LLM client. Backend-agnostic, applies under both Ollama and mlx-lm.
- [`docs/archived/local-inference.md`](archived/local-inference.md) — the prior version of this doc, kept for the audit trail of the install-instruction iterations.
