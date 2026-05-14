#!/usr/bin/env bash
# Start the mlx-lm primary server, sourcing configuration from .env.
#
# Reads LLM_BASE_URL_PRIMARY (port) and LLM_MODEL_PRIMARY (model path)
# from the repo's .env file. Other flags follow docs/local-inference.md
# with --prompt-cache-size 100 — the safe budget on M2 Max 64 GB after
# the P-10 Phase C bench attempt OOM'd at 49.86 GB VRAM with size 200.
#
# Foreground by default so stdout streams to the launching terminal.
# Background with '&' or with `nohup ... &`. Any extra arguments are
# forwarded to ``python -m mlx_lm server`` so per-run tweaks (e.g.
# ``--prompt-cache-size 200`` on the M5 Max 128 GB) are one-liners
# without editing this script.
#
# Examples:
#   scripts/start-mlx-lm.sh                              # primary + safe defaults
#   scripts/start-mlx-lm.sh --prompt-cache-size 200      # M5 Max budget
#   scripts/start-mlx-lm.sh > /tmp/mlx-lm-8001.log 2>&1 &  # background

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ ! -f .env ]]; then
    echo "ERROR: .env not found at $REPO_ROOT/.env" >&2
    exit 1
fi

# Export every KEY=VALUE in .env so they're visible to the python child.
# The pipeline reads them via pydantic-settings anyway; we just need the
# port + model path here.
set -a
# shellcheck source=/dev/null
source .env
set +a

# Port comes from LLM_BASE_URL_PRIMARY (preferred) or LLM_BASE_URL.
URL="${LLM_BASE_URL_PRIMARY:-${LLM_BASE_URL:-http://127.0.0.1:8001/v1}}"
if [[ "$URL" =~ :([0-9]+)(/|$) ]]; then
    PORT="${BASH_REMATCH[1]}"
else
    PORT=8001
fi

MODEL="${LLM_MODEL_PRIMARY:-}"
if [[ -z "$MODEL" ]]; then
    echo "ERROR: LLM_MODEL_PRIMARY not set in .env" >&2
    exit 1
fi

# mlx-lm 0.31 reports the absolute path passed to --model as the model
# ID at /v1/models. LangChain 401s when the request's `model` field
# doesn't match what mlx-lm returns there — so .env must hold the
# path, not an Ollama tag (per docs/local-inference.md § Model lookup).
if [[ "$MODEL" == *":"* && "$MODEL" != /* && "$MODEL" != "~"* ]]; then
    echo "WARNING: LLM_MODEL_PRIMARY='$MODEL' looks like Ollama tag syntax." >&2
    echo "         mlx-lm 0.31 wants an absolute path; the picker will 401" >&2
    echo "         at /v1/chat/completions until .env is updated." >&2
fi

# Expand a leading ~ for shell-style paths.
MODEL_PATH="${MODEL/#\~/$HOME}"

if [[ ! -d "$MODEL_PATH" ]]; then
    echo "ERROR: model directory not found at $MODEL_PATH" >&2
    echo "       Convert from a HF checkpoint with:" >&2
    echo "         python -m mlx_lm convert -q --q-bits 4 \\" >&2
    echo "             --hf-path <hf-org>/<hf-name> \\" >&2
    echo "             --mlx-path \"$MODEL_PATH\"" >&2
    echo "       See docs/local-inference.md for the full pull / convert flow." >&2
    exit 1
fi

# Resolve a python interpreter that has ``mlx_lm`` available. Order:
# 1. MLX_LM_PYTHON env override (operator escape hatch).
# 2. The conventional venv at ~/.venvs/mlx-lm/bin/python — matches
#    docs/local-inference.md § Installation.
# 3. The active ``python`` on PATH, if it can import mlx_lm.
# Otherwise abort with a clear hint instead of letting mlx-lm fail
# inside a wrong-venv exec.
mlx_lm_python=""
if [[ -n "${MLX_LM_PYTHON:-}" && -x "$MLX_LM_PYTHON" ]]; then
    mlx_lm_python="$MLX_LM_PYTHON"
elif [[ -x "$HOME/.venvs/mlx-lm/bin/python" ]]; then
    mlx_lm_python="$HOME/.venvs/mlx-lm/bin/python"
elif command -v python >/dev/null 2>&1 && python -c "import mlx_lm" >/dev/null 2>&1; then
    mlx_lm_python="python"
fi

if [[ -z "$mlx_lm_python" ]]; then
    echo "ERROR: no python interpreter with mlx_lm installed was found." >&2
    echo "  Tried: \$MLX_LM_PYTHON, ~/.venvs/mlx-lm/bin/python, \$PATH python." >&2
    echo "  See docs/local-inference.md § Installation to create the venv:" >&2
    echo "    python -m venv ~/.venvs/mlx-lm && \\\\" >&2
    echo "      ~/.venvs/mlx-lm/bin/pip install mlx-lm" >&2
    exit 1
fi

echo "mlx-lm server starting"
echo "  port:   $PORT"
echo "  model:  $MODEL_PATH"
echo "  python: $mlx_lm_python"
echo "  args:   $*"
echo

# Argparse takes the LAST occurrence of any duplicated flag, so trailing
# "$@" cleanly overrides the safe defaults below for per-run tweaks.
exec "$mlx_lm_python" -m mlx_lm server \
    --model "$MODEL_PATH" \
    --host 127.0.0.1 --port "$PORT" \
    --chat-template-args '{"enable_thinking":false}' \
    --decode-concurrency 4 \
    --prompt-concurrency 4 \
    --prompt-cache-size 100 \
    --prompt-cache-bytes 1073741824 \
    "$@"
