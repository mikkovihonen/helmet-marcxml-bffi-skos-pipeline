#!/bin/bash
# Run the full pipeline end-to-end on a MARCXML input directory.
#
# Stages: M2 (marc-to-bf) → M3 (bf-to-bffi) → M5 (embed) → M6 (judge)
# → M8 (merge) → M9 (reconcile) → M10 phase 1 (skosify) → M10 phase 2 (load).
#
# Emits ``STAGE_<name>_START`` and ``STAGE_<name>_DONE <elapsed>s`` to
# stdout + the log file so `bffi-pipeline status` (or a tail -F | grep)
# can show progress without polling. Each stage flushes its summary to
# the log before moving on, so a kill -TERM at any point leaves the
# disk state recoverable via the resume flags of the underlying CLI
# subcommands (M2/M3 idempotent; M6 checkpoint+resume).
#
# Required env:
#   MARCXML_DIR  Directory of *.xml MARCXML files (one record per file).
#
# Optional env (with defaults):
#   BFFI_DATA_DIR     Output base (./data).
#   PIPELINE_LOG      Per-stage log (<BFFI_DATA_DIR>/pipeline.log).
#   M6_CONCURRENCY    Judge concurrency. Defaults to 1 (safe for any
#                     server config). For mlx-lm with the recommended
#                     --decode-concurrency 4 --prompt-concurrency 4
#                     --prompt-cache-size 200 server flags, the P-02
#                     § A6 sweep on M2 Max picked 4. Re-measure on
#                     M5 Max before production batches — see
#                     docs/local-inference.md § "Throughput findings".
#   SKIP_M5_M6        Set to "1" to skip embeddings + LLM judge (writes an
#                     empty judge-decisions.jsonl so M8 still has its
#                     expected input). Useful for fast pattern-mining runs.
#   SKIP_RECONCILE    Set to "1" to skip M9 (no Finto/VIAF/LLM-picker calls).
#                     M10 then loads the un-reconciled canonical.
set -euo pipefail

if [[ -z "${MARCXML_DIR:-}" ]]; then
    echo "ERROR: MARCXML_DIR is required." >&2
    echo "Usage: MARCXML_DIR=path/to/marcxml [BFFI_DATA_DIR=path/to/out] $0" >&2
    exit 2
fi
if [[ ! -d "$MARCXML_DIR" ]]; then
    echo "ERROR: MARCXML_DIR not a directory: $MARCXML_DIR" >&2
    exit 2
fi

BFFI_DATA_DIR="${BFFI_DATA_DIR:-./data}"
PIPELINE_LOG="${PIPELINE_LOG:-$BFFI_DATA_DIR/pipeline.log}"
M6_CONCURRENCY="${M6_CONCURRENCY:-1}"

mkdir -p "$BFFI_DATA_DIR"
mkdir -p "$(dirname "$PIPELINE_LOG")"

export BFFI_DATA_DIR

# Pin BFFI_RUN_UUID so every subcommand in the chain emits its
# stage-events under the same run_uuid. The Grafana dashboard's
# ``$active_run`` filter then sees one coherent run across stages
# instead of N independent invocations. Generated upfront so the
# planner event below can attach to it.
if [[ -z "${BFFI_RUN_UUID:-}" ]]; then
    BFFI_RUN_UUID="$(uuidgen | tr 'A-Z' 'a-z' | tr -d '-')"
fi
export BFFI_RUN_UUID

log() { echo "$@" | tee -a "$PIPELINE_LOG"; }

T0=$(date +%s)
log "PIPELINE_START $(date -u +%Y-%m-%dT%H:%M:%SZ)"
log "  MARCXML_DIR=$MARCXML_DIR"
log "  BFFI_DATA_DIR=$BFFI_DATA_DIR"
log "  BFFI_RUN_UUID=$BFFI_RUN_UUID"

# Declare the planned stage set up-front. The dashboard's 4-state
# tile expression uses ``bffi_stage_planned`` to distinguish
# ``pending`` (planned, not yet started) from ``skipped`` (not in
# plan at all). Subtract skipped stages so they show as skipped.
PLAN=(m2 m3)
if [[ "${SKIP_M5_M6:-0}" != "1" ]]; then PLAN+=(m5 m6); fi
PLAN+=(m8)
if [[ "${SKIP_RECONCILE:-0}" != "1" ]]; then PLAN+=(m9); fi
PLAN+=(skosify load)
RUN_DESCRIPTION="Full pipeline run · MARCXML=$MARCXML_DIR"
if [[ "${SKIP_M5_M6:-0}" == "1" || "${SKIP_RECONCILE:-0}" == "1" ]]; then
    RUN_DESCRIPTION="${RUN_DESCRIPTION} (skipping:"
    [[ "${SKIP_M5_M6:-0}" == "1" ]] && RUN_DESCRIPTION+=" M5+M6"
    [[ "${SKIP_RECONCILE:-0}" == "1" ]] && RUN_DESCRIPTION+=" M9"
    RUN_DESCRIPTION+=")"
fi
uv run bffi-pipeline plan "${PLAN[@]}" --description "$RUN_DESCRIPTION" >>"$PIPELINE_LOG" 2>&1 || true
log "  plan: ${PLAN[*]}"
log "  description: $RUN_DESCRIPTION"

# --- M2 ------------------------------------------------------------------
TS=$(date +%s); log "STAGE_M2_START"
uv run bffi-pipeline marc-to-bf "$MARCXML_DIR" --output-dir "$BFFI_DATA_DIR" >>"$PIPELINE_LOG" 2>&1
TE=$(date +%s); log "STAGE_M2_DONE $((TE-TS))s"

# --- M3 ------------------------------------------------------------------
TS=$(date +%s); log "STAGE_M3_START"
uv run bffi-pipeline bf-to-bffi --output-dir "$BFFI_DATA_DIR" >>"$PIPELINE_LOG" 2>&1
TE=$(date +%s); log "STAGE_M3_DONE $((TE-TS))s"

# --- M5 + M6 -------------------------------------------------------------
if [[ "${SKIP_M5_M6:-0}" == "1" ]]; then
    # Empty decisions file so M8 still has its expected input. M5
    # auto-merge band is the only thing that would have produced
    # same_work edges without M6, but skipping M5 implies the operator
    # is doing a pattern-mining run where merge quality isn't the goal.
    : > "$BFFI_DATA_DIR/judge-decisions.jsonl"
    log "STAGE_M5_M6_SKIPPED (SKIP_M5_M6=1)"
else
    TS=$(date +%s); log "STAGE_M5_START"
    uv run bffi-pipeline embed --corpus-dir "$BFFI_DATA_DIR" --output-dir "$BFFI_DATA_DIR" >>"$PIPELINE_LOG" 2>&1
    TE=$(date +%s); log "STAGE_M5_DONE $((TE-TS))s"

    TS=$(date +%s); log "STAGE_M6_START"
    uv run bffi-pipeline judge --concurrency "$M6_CONCURRENCY" >>"$PIPELINE_LOG" 2>&1
    TE=$(date +%s); log "STAGE_M6_DONE $((TE-TS))s"
fi

# --- M8 ------------------------------------------------------------------
TS=$(date +%s); log "STAGE_M8_START"
uv run bffi-pipeline merge \
    --bffi-corpus-dir "$BFFI_DATA_DIR" \
    --decisions-path "$BFFI_DATA_DIR/judge-decisions.jsonl" \
    --helmet-map-path "$BFFI_DATA_DIR/helmet-map.jsonl" \
    --output-path "$BFFI_DATA_DIR/canonical.ttl" >>"$PIPELINE_LOG" 2>&1
TE=$(date +%s); log "STAGE_M8_DONE $((TE-TS))s"

# --- M9 ------------------------------------------------------------------
if [[ "${SKIP_RECONCILE:-0}" == "1" ]]; then
    cp "$BFFI_DATA_DIR/canonical.ttl" "$BFFI_DATA_DIR/canonical-reconciled.ttl"
    log "STAGE_M9_SKIPPED (SKIP_RECONCILE=1)"
else
    TS=$(date +%s); log "STAGE_M9_START"
    uv run bffi-pipeline reconcile \
        --canonical-path "$BFFI_DATA_DIR/canonical.ttl" \
        --output-path "$BFFI_DATA_DIR/canonical-reconciled.ttl" >>"$PIPELINE_LOG" 2>&1
    TE=$(date +%s); log "STAGE_M9_DONE $((TE-TS))s"
fi

# --- M10 phase 1 (skosify) ----------------------------------------------
TS=$(date +%s); log "STAGE_SKOSIFY_START"
uv run bffi-pipeline skosify \
    --canonical-path "$BFFI_DATA_DIR/canonical-reconciled.ttl" \
    --output-path "$BFFI_DATA_DIR/canonical-skosified.ttl" >>"$PIPELINE_LOG" 2>&1
TE=$(date +%s); log "STAGE_SKOSIFY_DONE $((TE-TS))s"

# --- M10 phase 2 (load) -------------------------------------------------
TS=$(date +%s); log "STAGE_LOAD_START"
uv run bffi-pipeline load \
    --skosified-path "$BFFI_DATA_DIR/canonical-skosified.ttl" >>"$PIPELINE_LOG" 2>&1
TE=$(date +%s); log "STAGE_LOAD_DONE $((TE-TS))s"

TFINAL=$(date +%s)
log "PIPELINE_TOTAL $((TFINAL-T0))s"
log "PIPELINE_DONE"
