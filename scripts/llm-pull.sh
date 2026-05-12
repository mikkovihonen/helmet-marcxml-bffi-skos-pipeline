#!/usr/bin/env bash
# P-02 Phase D2 — one-command model conversion via mlx-lm.
#
# Restores the `ollama pull qwen3:8b` one-command UX without the rest
# of Ollama: pass a Hugging Face slug `<org>/<name>`; the script
# fetches + 4-bit-quantises the weights via `mlx_lm.convert` and
# writes the MLX-format checkpoint under `~/.mlx_models/<name>-4bit/`,
# which is the path the `mlx_lm.server` examples in
# `docs/local-inference.md` expect.
#
# Requires the mlx-lm venv to be on PATH (see local-inference.md §
# Installation). Activate it first:
#
#   source ~/.venvs/mlx-lm/bin/activate
#
# Usage:
#   scripts/llm-pull.sh Qwen/Qwen3-8B
#   scripts/llm-pull.sh Qwen/Qwen3-32B
#   scripts/llm-pull.sh Qwen/Qwen3-1.7B               # draft model (P-02 Phase C)
#
# Note: Qwen3 (released 2025) dropped the "-Instruct" suffix; the bare
# Qwen/Qwen3-<size> repo is the chat-tuned variant. For pre-quantised
# MLX checkpoints (faster than local conversion), see
# docs/local-inference.md § "Model acquisition".
#
# Override the output root with MLX_MODELS_DIR if you don't want
# ~/.mlx_models (e.g. for an external drive).
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <hf-org>/<hf-name>" >&2
    echo "example: $0 Qwen/Qwen3-8B" >&2
    exit 2
fi

slug="$1"
if [[ "$slug" != */* ]]; then
    echo "error: expected <hf-org>/<hf-name>, got '$slug'" >&2
    exit 2
fi

if ! python -c "import mlx_lm" 2>/dev/null; then
    echo "error: mlx_lm not importable. Activate the mlx-lm venv first:" >&2
    echo "  source ~/.venvs/mlx-lm/bin/activate" >&2
    exit 3
fi

models_dir="${MLX_MODELS_DIR:-$HOME/.mlx_models}"
mkdir -p "$models_dir"

name=$(basename "$slug")
out="$models_dir/${name}-4bit"

if [[ -d "$out" && -n "$(ls -A "$out" 2>/dev/null)" ]]; then
    echo "$out already exists and is non-empty; skipping." >&2
    echo "Remove the directory to re-convert, or pass a different MLX_MODELS_DIR." >&2
    exit 0
fi

echo "converting $slug → $out (4-bit quantisation, ~5-90 min depending on model size)"
python -m mlx_lm convert --hf-path "$slug" -q --q-bits 4 --mlx-path "$out"
echo "done: $out"
