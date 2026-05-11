#!/usr/bin/env bash
# P-02 Phase A5 — vllm-mlx parity bench against Ollama baseline.
#
# Runs the gold-set eval (`bffi-pipeline eval`) twice — once against
# the Ollama-backed defaults, once against the vllm-mlx-backed ones
# — then diffs the per-case verdicts and reports parity.
#
# Two named env files drive the swap:
#
#   .env.ollama-baseline   — the operator's current Ollama setup
#                            (created by `cp .env .env.ollama-baseline`
#                            at the start of Phase A4).
#   .env.vllm-mlx          — same shape, but LLM_BASE_URL points at
#                            the vllm-mlx server port (e.g. 8001) and
#                            LLM_MODEL_PRIMARY / _FALLBACK use the
#                            MLX-style identifiers (e.g. Qwen3-8B-4bit).
#
# Usage:
#   scripts/p02-parity-bench.sh                  # default labels
#   scripts/p02-parity-bench.sh <baseline-label> <candidate-label>
#
# Output:
#   eval-runs/<baseline-label>.json
#   eval-runs/<candidate-label>.json
#   stdout: side-by-side aggregate metrics + per-case verdict diff.
#
# Exit codes:
#   0  parity confirmed (same accuracy + identical failure set + identical predicted values).
#   1  drift detected (any of: accuracy mismatch, failure-set mismatch, predicted-value mismatch).
#   2  setup error (missing env files, eval failure, etc.).

set -euo pipefail

BASELINE_LABEL="${1:-p02-phase-a-ollama-baseline}"
CANDIDATE_LABEL="${2:-p02-phase-a-vllm-mlx-parity}"
BASELINE_ENV="${BASELINE_ENV:-.env.ollama-baseline}"
CANDIDATE_ENV="${CANDIDATE_ENV:-.env.vllm-mlx}"

for env_file in "$BASELINE_ENV" "$CANDIDATE_ENV"; do
    if [ ! -f "$env_file" ]; then
        echo "ERROR: env file '$env_file' missing." >&2
        echo "Create it with the Phase A4 commands: cp .env $env_file" >&2
        echo "and edit LLM_BASE_URL / LLM_MODEL_* to point at the right backend." >&2
        exit 2
    fi
done

run_eval() {
    local env_file="$1"
    local label="$2"
    echo
    echo "=== Running eval: $label (env=$env_file) ==="
    # Run inside a subshell so the env-var swap doesn't pollute the
    # outer shell. Use `set -a` so all sourced vars get exported.
    (
        set -a
        # shellcheck source=/dev/null
        . "$env_file"
        set +a
        uv run bffi-pipeline eval --run-label "$label"
    )
}

run_eval "$BASELINE_ENV" "$BASELINE_LABEL"
run_eval "$CANDIDATE_ENV" "$CANDIDATE_LABEL"

echo
echo "=== Parity diff: $BASELINE_LABEL vs $CANDIDATE_LABEL ==="
uv run python <<EOF
import json
import sys
from pathlib import Path

repo = Path(".").resolve()
b = json.loads((repo / "eval-runs" / "$BASELINE_LABEL.json").read_text())
c = json.loads((repo / "eval-runs" / "$CANDIDATE_LABEL.json").read_text())

print(f"  accuracy:                  baseline={b['accuracy']:.4f}  candidate={c['accuracy']:.4f}")
print(f"  decided_accuracy:          baseline={b['decided_accuracy']:.4f}  candidate={c['decided_accuracy']:.4f}")
print(f"  uncertain_rate:            baseline={b['uncertain_rate']:.4f}  candidate={c['uncertain_rate']:.4f}")
print(f"  median_latency_ms:         baseline={b['median_latency_ms']}  candidate={c['median_latency_ms']}")
print(f"  failures count:            baseline={len(b['failures'])}  candidate={len(c['failures'])}")

# Strict parity: same accuracy + same failure-id set + same predicted per failure.
issues = []
if b["accuracy"] != c["accuracy"]:
    issues.append(f"accuracy mismatch ({b['accuracy']} vs {c['accuracy']})")
b_fail = {f["id"]: f["predicted"] for f in b["failures"]}
c_fail = {f["id"]: f["predicted"] for f in c["failures"]}
b_ids = set(b_fail)
c_ids = set(c_fail)
only_b = b_ids - c_ids
only_c = c_ids - b_ids
both = b_ids & c_ids
if only_b:
    issues.append(f"baseline fails {len(only_b)} cases candidate does not: {sorted(only_b)}")
if only_c:
    issues.append(f"candidate fails {len(only_c)} cases baseline does not: {sorted(only_c)}")
predicted_diffs = [
    (cid, b_fail[cid], c_fail[cid]) for cid in sorted(both) if b_fail[cid] != c_fail[cid]
]
if predicted_diffs:
    issues.append("predicted-value mismatch on shared failures:")
    for cid, bp, cp in predicted_diffs:
        issues.append(f"    {cid}: baseline={bp!r}  candidate={cp!r}")

if not issues:
    print()
    print("PARITY OK — every case produced identical verdicts on both backends.")
    sys.exit(0)

print()
print("DRIFT DETECTED:")
for line in issues:
    print(f"  - {line}")
sys.exit(1)
EOF
