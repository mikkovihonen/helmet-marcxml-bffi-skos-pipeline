# Apple Silicon / local inference

The pipeline runs end-to-end on a MacBook Pro M5 Max with 128 GB unified memory. **No paid LLM APIs.** Plan all LLM-dependent code around the local server.

## Installation

The pipeline talks to any OpenAI-compatible server via `LLM_BASE_URL`. The two committed options are Ollama (default for development and gold-set evaluation) and vllm-mlx (production batches). Pick one to start; switching is just an env-var change.

### Ollama — development + gold-set runs

One-time host install on macOS:

```bash
brew install --cask ollama          # or download the DMG from https://ollama.com
open -a Ollama                      # starts the background service on :11434
```

Pull the two judge models (~20 GB and ~40 GB on disk; first-pull takes 10-30 min on a fast connection):

```bash
ollama pull qwen3:32b-q4_K_M             # primary judge
ollama pull qwen2.5:72b-instruct-q4_K_M  # cascade fallback (Qwen3 has no 72B size)
```

Note on the cascade fallback: Qwen3 sizes go `0.6b / 1.7b / 14b / 32b / 235b` — no 72B. The cascade therefore steps to the previous-generation Qwen2.5 72B, which is still Apache 2.0 and similar quality. If you want a same-generation cascade, use `qwen3:235b` instead, but that needs a much larger memory envelope than the 128 GB M5 Max.

Verify the OpenAI-compatible endpoint is up:

```bash
curl -s http://localhost:11434/v1/models | jq '.data[].id'
# → "qwen3:32b-q4_K_M"
# → "qwen2.5:72b-instruct-q4_K_M"
```

Configure the pipeline. Copy `.env.example` to `.env` (first time only) and edit if your install differs:

```bash
cp .env.example .env
# .env carries the committed defaults:
#   LLM_BASE_URL=http://localhost:11434/v1
#   LLM_API_KEY=ollama
#   LLM_MODEL_PRIMARY=qwen3:32b-q4_K_M
#   LLM_MODEL_FALLBACK=qwen2.5:72b-instruct-q4_K_M
```

The `LLM_API_KEY` value is unused by Ollama but the `langchain-openai` client requires a non-empty string. Any literal works; `ollama` is the convention.

A quick end-to-end probe through the pipeline (no Fuseki / no gold set required):

```bash
uv run python - <<'EOF'
from bffi_pipeline.stages.judge import judge_pair, WorkRecord
a = WorkRecord(record_id="probe-a", creator="Tolstoy, Leo", preferred_title="War and Peace", expression_language="eng")
b = WorkRecord(record_id="probe-b", creator="Tolstoy, Leo", preferred_title="Sota ja rauha", expression_language="fin", original_language="rus")
decision, cache_hit, latency = judge_pair(a, b, sim=0.84)
print(decision.decision, decision.confidence, f"{latency:.1f}s", "(cached)" if cache_hit else "(fresh)")
EOF
```

Expected: `same_work` with confidence ≥ 0.85 and latency 3-6 s on the M5 Max. If the call hangs or times out, check that the Ollama service is running (`ollama list`) and that the pulled tags match `LLM_MODEL_PRIMARY`.

### vllm-mlx — production batches

Switch to vllm-mlx for the full-corpus judge pass. Continuous batching gives 4-8x throughput on bulk runs (see "Throughput planning" below), which is the difference between a 70-hour Ollama-serial run and a 10-hour batched one.

**About the inference stack.** Three layered projects:

| Layer | Project | Role |
|---|---|---|
| Framework | `mlx` (Apple) | Apple-Silicon array/ML framework. Transitive dep. |
| LLM runtime | `mlx-lm` ([`ml-explore/mlx-lm`](https://github.com/ml-explore/mlx-lm)) | Apple's plain LLM inference + basic OpenAI-compatible server. Transitive dep. |
| Serving layer | **`vllm-mlx`** ([`waybarrios/vllm-mlx`](https://github.com/waybarrios/vllm-mlx)) | **This is what we install and run.** vLLM-style continuous-batching server with OpenAI + Anthropic compat, prefix caching, prompt cache, and speculative decoding. Builds on top of mlx-lm. |

(An earlier draft of these docs referenced `Blaizzy/mlx_lm.git` — a
typo. That repo does not exist; the closest match `Blaizzy/mlx-vlm`
is for vision-language models, not text-only LLMs. Corrected during
P-02 Phase A1.)

One-time install via PyPI:

```bash
mkdir -p ~/Workspace/vendor/vllm-mlx && cd ~/Workspace/vendor/vllm-mlx
uv venv .venv-mlx --python 3.12
source .venv-mlx/bin/activate
uv pip install vllm-mlx
python -c "import vllm_mlx; print(vllm_mlx.__version__)"
```

Download pre-quantised MLX checkpoints (one-time; cached under `~/.cache/huggingface/hub/`):

```bash
vllm-mlx download mlx-community/Qwen3-8B-Instruct-4bit
vllm-mlx download mlx-community/Qwen3-32B-Instruct-4bit
```

(`vllm-mlx model --help` covers from-scratch conversion if a pre-quantised checkpoint isn't published yet.)

Start the OpenAI-compatible server. For multi-model serving (primary + fallback under one endpoint), use `--models-config`:

```bash
# Single model (simple case):
vllm-mlx serve mlx-community/Qwen3-8B-Instruct-4bit \
    --host 127.0.0.1 --port 8001 \
    --continuous-batching --enable-prefix-cache

# Multi-model (matches Ollama's per-request model-selection UX):
vllm-mlx serve --models-config ~/.config/vllm-mlx/models.yaml \
    --host 127.0.0.1 --port 8001 \
    --continuous-batching --enable-prefix-cache
```

Point the pipeline at the new endpoint for the production batch:

```bash
LLM_BASE_URL=http://localhost:8000/v1 \
    bffi-pipeline judge --concurrency 16
```

The `--concurrency` value is the per-batch parallel-request count. The committed sweep range is `{4, 8, 16, 32}`; pick the value that maximises throughput without OOMing — see the runbook's `--concurrency` tuning sweep section.

### Memory headroom

Don't run the 32B and 72B models concurrently on a 64 GB Mac. The cascade only needs the primary model loaded most of the time; the fallback is invoked for ~10-20 % of pairs. On the 128 GB M5 Max both fit, which is the only configuration the production timings below assume. Stop both LLM and Docker services before swapping models in unified-memory-tight setups.

## Memory budget

| Component | Approx. resident size |
|---|---|
| FAISS HNSW index (800k × 1024 dim) | ~5 GB |
| BGE-M3 embedding model (loaded only during M5) | ~2.5 GB |
| Qwen3 32B 4-bit (primary judge) | ~18–20 GB |
| Qwen3 72B 4-bit (cascade fallback) | ~40 GB |
| Fuseki + Skosmos containers | ~4–6 GB |
| OS + working memory | ~10–15 GB |
| **Total typical concurrent peak** | **~40–50 GB** |

Comfortable on 128 GB. Loading both judge models simultaneously is possible (~60 GB of models alone) if you want to run a cascade in one process, but most operations only need one at a time.

## Server choice

- **Default: Ollama.** Simplest setup, OpenAI-compatible API on `:11434`, MLX backend in preview. Good throughput for serial requests. Use for development and gold-set runs.
- **Production batch: vllm-mlx.** Continuous batching gives 4–8x throughput on bulk judge runs. Worth switching to for the production pass over 50k+ pairs. Same OpenAI-compatible interface.

The code talks to either through `langchain-openai` pointed at `LLM_BASE_URL`. Don't write server-specific code.

## Model expectations

Benchmark all model choices against the gold set before committing. Treat the numbers below as starting points, not commitments:

- **Qwen3 32B Q4 MLX:** ~3–6 s per judge call (with system-prompt KV cache hit). Multilingual quality is strong but uneven on hard cases (common-title collisions, abridgments). Use as primary.
- **Qwen3 72B Q4 MLX:** ~6–12 s per judge call. Better on ambiguous cases. Use as second-opinion in a cascade, or as primary if throughput allows.
- **Llama 3.3 70B Q4 MLX:** Comparable to Qwen3 72B in size; weaker on Finnish/Russian. Use only if Qwen3 has problems on the gold set.

## Throughput planning

A single-pass judge run on 50k–100k gray-zone pairs takes:

| Model | Server mode | Time |
|---|---|---|
| Qwen3 32B | Ollama (serial) | 70–170 hours |
| Qwen3 32B | vllm-mlx (batched) | 10–25 hours |
| Qwen3 72B | Ollama (serial) | 140–340 hours |
| Qwen3 72B | vllm-mlx (batched) | 20–50 hours |

Two consequences for the design:

**Tighten the gray zone aggressively** — push auto-merge from ≥0.92 to ≥0.90 and rejection from ≤0.75 to ≤0.78 once you've validated thresholds against the gold set. This roughly halves the LLM workload.

**Plan for vllm-mlx in production** — Ollama is fine for development and gold-set runs, but the batched run for the full corpus should use vllm-mlx.

## Cascade strategy

For production runs, use a two-stage cascade:

1. Run all gray-zone pairs through **Qwen3 32B**.
2. Re-run pairs where 32B returned `uncertain` OR `same_work` with confidence < 0.85 through **Qwen3 72B**.

The 72B handles ~10–20% of the workload and catches the cases where the 32B was wobbly. Both decisions get logged to provenance with distinct `bffi-prov:stage` values (`"llm-judge-primary"` and `"llm-judge-second-opinion"`).

## Quality risks — be honest about these

Open-source judge models will not match Claude-class quality on hard bibliographic cases. Plan for:

- A larger human-review queue. Lower the auto-commit confidence threshold (e.g., 0.95 instead of 0.90 — though if the model is well-calibrated above 0.90, this can be relaxed).
- More gold-set growth, especially in the categories where the model struggles. The override-feedback loop matters more here than it would with a frontier API.
- Longer prompt iteration. Test multiple few-shot configurations before declaring M6 done.

If after careful tuning the gold-set per-category accuracy isn't acceptable in some category (say, common-title collisions consistently below 75%), the answer is **not** to over-trust the model. It's to send those category candidates straight to human review without an LLM decision, and document this in the provenance with `bffi-prov:stage = "human-only"`.
