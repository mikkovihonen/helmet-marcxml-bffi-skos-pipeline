#!/bin/bash
# Fast pattern-mining run: M2 → M3 → M8 (empty decisions) → ysa-disambiguation-report.
#
# Skips M5 (embeddings) and M6 (LLM judge) entirely. M8 receives no
# same_work edges so each raw Work stays in its own canonical group —
# this is correct for the YSA disambiguation report (which walks
# subject literals, independent of merge quality) and for surface-
# level corpus characterisation runs (no-candidate counts, source-tag
# distribution, etc.).
#
# NOT suitable for the Skosmos publish path — that needs real M5+M6
# merge decisions to consolidate same-Work bibs. Use
# scripts/run-full-pipeline.sh for that.
#
# Required env:
#   MARCXML_DIR  Directory of *.xml MARCXML files.
#
# Optional env:
#   BFFI_DATA_DIR     Output base (./data).
#   PIPELINE_LOG      Per-stage log (<BFFI_DATA_DIR>/pipeline.log).
set -euo pipefail

if [[ -z "${MARCXML_DIR:-}" ]]; then
    echo "ERROR: MARCXML_DIR is required." >&2
    echo "Usage: MARCXML_DIR=path/to/marcxml [BFFI_DATA_DIR=path/to/out] $0" >&2
    exit 2
fi

BFFI_DATA_DIR="${BFFI_DATA_DIR:-./data}"
PIPELINE_LOG="${PIPELINE_LOG:-$BFFI_DATA_DIR/pipeline.log}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export BFFI_DATA_DIR PIPELINE_LOG SKIP_M5_M6=1 SKIP_RECONCILE=1

mkdir -p "$BFFI_DATA_DIR"

# Delegate to the full pipeline; SKIP_M5_M6 + SKIP_RECONCILE flags
# turn it into the fast-export shape (M2 → M3 → M8 empty → load).
# Then layer the disambiguation report on top.
log() { echo "$@" | tee -a "$PIPELINE_LOG"; }

"$SCRIPT_DIR/run-full-pipeline.sh"

# YSA disambiguation report runs against the (already-loaded) Fuseki
# canonical so cataloguers get the worklist for free at the end of
# the run.
TS=$(date +%s); log "STAGE_YSA_REPORT_START"
uv run bffi-pipeline ysa-disambiguation-report \
    --canonical-path "$BFFI_DATA_DIR/canonical-reconciled.ttl" \
    --output-path "$BFFI_DATA_DIR/ysa-disambiguation-report.csv" >>"$PIPELINE_LOG" 2>&1
TE=$(date +%s); log "STAGE_YSA_REPORT_DONE $((TE-TS))s"
log "REPORT_PATH $BFFI_DATA_DIR/ysa-disambiguation-report.csv"
