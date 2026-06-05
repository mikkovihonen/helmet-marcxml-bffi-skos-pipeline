#!/usr/bin/env bash
# Three-way picker eval: start mlx-lm with each model in turn,
# run eval-picker against the 10-case set, capture JSON, stop server.
#
# Output: eval-results/picker-2026-06-05/<slug>.json (one per model)
#
# Usage:
#   scripts/eval-picker-shootout.sh
#
# Knobs:
#   PORT=8001                        — mlx-lm server port
#   LOAD_TIMEOUT_SECONDS=180         — how long to wait for /v1/models
#   LLM_CALL_TIMEOUT_SECONDS=180     — picker's watchdog (overrides .env)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PORT="${PORT:-8001}"
LOAD_TIMEOUT_SECONDS="${LOAD_TIMEOUT_SECONDS:-180}"
OUT_DIR="eval-results/picker-2026-06-05"
mkdir -p "$OUT_DIR"

MODELS=(
    "qwen3-8b:/Users/mikkovihonen/.mlx_models/Qwen3-8B-4bit"
    "gemma-4-26B-A4B:/Users/mikkovihonen/.mlx_models/gemma-4-26B-A4B-it-4bit"
    "gemma-4-31B:/Users/mikkovihonen/.mlx_models/gemma-4-31B-it-4bit"
)

run_one() {
    local slug="$1" model_path="$2"
    local server_log="/tmp/mlx-eval-${slug}.log"
    local eval_log="/tmp/eval-picker-${slug}.log"
    local out_json="${OUT_DIR}/${slug}.json"

    echo "===================================="
    echo "  ${slug}"
    echo "  model:  ${model_path}"
    echo "  port:   ${PORT}"
    echo "===================================="

    # Refuse to start a new server if port is already held by another
    # process. Otherwise the new server bind-fails silently and the
    # subsequent eval queries route to whatever stale server holds
    # the port — producing eval numbers for the wrong model.
    if lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "  ERROR: port ${PORT} already in use by:" >&2
        lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >&2
        echo "  Refuse to proceed; eval would hit the wrong model." >&2
        return 1
    fi

    "$HOME/.venvs/mlx-lm/bin/python" -m mlx_lm server \
        --model "$model_path" \
        --host 127.0.0.1 --port "$PORT" \
        --chat-template-args '{"enable_thinking":false}' \
        --decode-concurrency 4 \
        --prompt-concurrency 4 \
        --prompt-cache-size 100 \
        --prompt-cache-bytes 1073741824 \
        > "$server_log" 2>&1 &
    local server_pid=$!
    echo "  server pid: $server_pid  log: $server_log"

    echo -n "  waiting for /v1/models … "
    local served_model=""
    for i in $(seq 1 "$LOAD_TIMEOUT_SECONDS"); do
        if curl -sf "http://127.0.0.1:${PORT}/v1/models" -o /tmp/models-probe.json 2>/dev/null; then
            served_model="$(python3 -c 'import json,sys; print(json.load(open("/tmp/models-probe.json"))["data"][0]["id"])' 2>/dev/null || echo "")"
            if [[ "$served_model" == "$model_path" ]]; then
                echo "ready after ${i}s (model=${served_model})"
                break
            fi
            # Server is up but serving a different model — abort.
            echo "FAIL — port ${PORT} serves wrong model: ${served_model}"
            echo "       expected: ${model_path}"
            kill "$server_pid" 2>/dev/null || true
            return 1
        fi
        if ! kill -0 "$server_pid" 2>/dev/null; then
            echo "FAIL — server exited during startup. tail of log:"
            tail -20 "$server_log"
            return 1
        fi
        sleep 1
    done

    if ! curl -sf "http://127.0.0.1:${PORT}/v1/models" >/dev/null 2>&1; then
        echo "  timeout after ${LOAD_TIMEOUT_SECONDS}s"
        tail -20 "$server_log"
        kill "$server_pid" 2>/dev/null || true
        return 1
    fi

    echo "  running eval-picker..."
    LLM_BASE_URL="http://127.0.0.1:${PORT}/v1" \
    LLM_BASE_URL_PRIMARY="http://127.0.0.1:${PORT}/v1" \
    LLM_MODEL_PRIMARY="$model_path" \
    LLM_CALL_TIMEOUT_SECONDS="${LLM_CALL_TIMEOUT_SECONDS:-180}" \
        uv run bffi-pipeline eval-picker \
            --model "$model_path" \
            --json \
            > "$out_json" 2> "$eval_log" || {
        echo "  eval-picker FAILED. tail of stderr:"
        tail -20 "$eval_log"
    }

    echo "  result: $out_json"

    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true

    # Give the OS a beat to release the port + VRAM before the next model.
    sleep 3
    echo
}

for entry in "${MODELS[@]}"; do
    slug="${entry%%:*}"
    model="${entry#*:}"
    run_one "$slug" "$model" || {
        echo "WARN: $slug failed; continuing with next model" >&2
    }
done

echo "==== all done ===="
echo "results in: $OUT_DIR"
ls -la "$OUT_DIR"
