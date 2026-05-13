#!/bin/bash
# Re-run the downstream half of the pipeline after a stage's logic
# changed (e.g. M6 auto-merge wiring, M9 reconciler routing, M10
# skosify config). Assumes M2 + M3 outputs (bibframe/ + bffi/)
# already exist under BFFI_DATA_DIR.
#
# Stages: M5 → M6 → M8 → M9 → skosify → load. Override the entry
# point with --from-stage if you only need a subset.
#
# Required env:
#   BFFI_DATA_DIR  Base directory holding existing bibframe/ + bffi/.
#
# Optional flags:
#   --from-stage <STAGE>  One of m5 (default), m6, m8, m9, skosify, load.
#                         Stages strictly before this are skipped — their
#                         outputs are assumed up to date on disk.
#   --m6-concurrency <N>  Judge concurrency (default 1).
set -euo pipefail

FROM_STAGE=m5
M6_CONCURRENCY=1
while [[ $# -gt 0 ]]; do
    case "$1" in
        --from-stage) FROM_STAGE="$2"; shift 2 ;;
        --m6-concurrency) M6_CONCURRENCY="$2"; shift 2 ;;
        *) echo "Unknown flag: $1" >&2; exit 2 ;;
    esac
done

if [[ -z "${BFFI_DATA_DIR:-}" ]]; then
    echo "ERROR: BFFI_DATA_DIR is required." >&2
    echo "Usage: BFFI_DATA_DIR=path/to/data $0 [--from-stage m6|m8|m9|skosify|load]" >&2
    exit 2
fi

PIPELINE_LOG="${PIPELINE_LOG:-$BFFI_DATA_DIR/pipeline.log}"
export BFFI_DATA_DIR

# Pin BFFI_RUN_UUID so every subcommand emits stage-events under the
# same run_uuid. Generated upfront so the plan event below can attach.
if [[ -z "${BFFI_RUN_UUID:-}" ]]; then
    BFFI_RUN_UUID="$(uuidgen | tr 'A-Z' 'a-z' | tr -d '-')"
fi
export BFFI_RUN_UUID

log() { echo "$@" | tee -a "$PIPELINE_LOG"; }

# Order matters; ordinal lets us "skip stages strictly before --from-stage".
declare -A STAGE_ORDER=([m5]=1 [m6]=2 [m8]=3 [m9]=4 [skosify]=5 [load]=6)
if [[ -z "${STAGE_ORDER[$FROM_STAGE]:-}" ]]; then
    echo "ERROR: unknown --from-stage $FROM_STAGE (expected m5|m6|m8|m9|skosify|load)" >&2
    exit 2
fi
FROM_ORD="${STAGE_ORDER[$FROM_STAGE]}"

# Plan: every stage from $FROM_STAGE onwards. Stages strictly before
# stay absent from the plan — dashboard shows them ``skipped`` (they
# were intentionally not in this re-publish's scope).
PLAN=()
for s in m5 m6 m8 m9 skosify load; do
    if (( ${STAGE_ORDER[$s]} >= FROM_ORD )); then PLAN+=("$s"); fi
done
uv run bffi-pipeline plan "${PLAN[@]}" >>"$PIPELINE_LOG" 2>&1 || true

T0=$(date +%s)
log "PIPELINE_START $(date -u +%Y-%m-%dT%H:%M:%SZ) (from $FROM_STAGE)"
log "  BFFI_RUN_UUID=$BFFI_RUN_UUID"
log "  plan: ${PLAN[*]}"

run_stage() {
    local name="$1" ord="${STAGE_ORDER[$1]}"
    shift
    if (( ord < FROM_ORD )); then
        log "STAGE_${name^^}_SKIPPED (--from-stage $FROM_STAGE)"
        return
    fi
    local ts="$(date +%s)"
    log "STAGE_${name^^}_START"
    "$@" >>"$PIPELINE_LOG" 2>&1
    local te="$(date +%s)"
    log "STAGE_${name^^}_DONE $((te-ts))s"
}

run_stage m5 uv run bffi-pipeline embed --corpus-dir "$BFFI_DATA_DIR" --output-dir "$BFFI_DATA_DIR"

# M6 must --restart when re-running because the auto-merge synthetic
# rows write only on a fresh judge-decisions.jsonl; resume would skip
# the auto-merge pre-loop.
run_stage m6 uv run bffi-pipeline judge --concurrency "$M6_CONCURRENCY" --restart

run_stage m8 uv run bffi-pipeline merge \
    --bffi-corpus-dir "$BFFI_DATA_DIR" \
    --decisions-path "$BFFI_DATA_DIR/judge-decisions.jsonl" \
    --helmet-map-path "$BFFI_DATA_DIR/helmet-map.jsonl" \
    --output-path "$BFFI_DATA_DIR/canonical.ttl"

run_stage m9 uv run bffi-pipeline reconcile \
    --canonical-path "$BFFI_DATA_DIR/canonical.ttl" \
    --output-path "$BFFI_DATA_DIR/canonical-reconciled.ttl"

run_stage skosify uv run bffi-pipeline skosify \
    --canonical-path "$BFFI_DATA_DIR/canonical-reconciled.ttl" \
    --output-path "$BFFI_DATA_DIR/canonical-skosified.ttl"

run_stage load uv run bffi-pipeline load \
    --skosified-path "$BFFI_DATA_DIR/canonical-skosified.ttl"

TFINAL=$(date +%s)
log "PIPELINE_TOTAL $((TFINAL-T0))s"
log "PIPELINE_DONE"
