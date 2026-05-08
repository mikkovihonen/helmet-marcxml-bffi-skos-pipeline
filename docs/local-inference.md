# Apple Silicon / local inference

The pipeline runs end-to-end on a MacBook Pro M5 Max with 128 GB unified memory. **No paid LLM APIs.** Plan all LLM-dependent code around the local server.

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
