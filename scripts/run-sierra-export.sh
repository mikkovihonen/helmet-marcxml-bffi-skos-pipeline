#!/usr/bin/env bash
# Sierra → MARCXML export driver. Three phases, gated by an explicit
# ack between phases so a botched smoke export never silently rolls
# into the 800 k-record full run:
#
#   1. SMOKE    — export N=${SMOKE_LIMIT} bibs to ${SMOKE_DIR}
#   2. VALIDATE — pipe the smoke slice through M2 (marc-to-bf) + M3
#                 (bf-to-bffi). Confirms the exporter's MARCXML
#                 round-trips marc2bibframe2 cleanly before we spend
#                 the 1-2 h on the full export.
#   3. FULL     — only runs if you pass --confirm-full. Exports the
#                 entire non-suppressed bib corpus to ${FULL_DIR}.
#
# Bundling all three phases into one script means the operator does
# not have to look up every step in the runbook mid-export.
#
# Required environment (typically from .env):
#   DB_HOST DB_PORT DB_USER DB_PASSWORD DB_NAME
# Optional environment:
#   SMOKE_LIMIT  Rows in the smoke export. Default 500.
#   SMOKE_DIR    Output dir for smoke MARCXML. Default /tmp/sierra-smoke.
#   FULL_DIR     Output dir for the full export. Default
#                ./marcxml/sierra.
#   MAX_WORKERS  marcxml-export-sierra --max-workers. Default 10.
#
# Usage:
#   scripts/run-sierra-export.sh                # smoke + validate, stops
#   scripts/run-sierra-export.sh --confirm-full # smoke + validate + full

set -euo pipefail

SMOKE_LIMIT="${SMOKE_LIMIT:-500}"
SMOKE_DIR="${SMOKE_DIR:-/tmp/sierra-smoke}"
FULL_DIR="${FULL_DIR:-./marcxml/sierra}"
MAX_WORKERS="${MAX_WORKERS:-10}"
CONFIRM_FULL=0

for arg in "$@"; do
    case "$arg" in
        --confirm-full) CONFIRM_FULL=1 ;;
        -h|--help)
            sed -n '2,32p' "$0"; exit 0 ;;
        *) echo "Unknown arg: $arg" >&2; exit 2 ;;
    esac
done

for var in DB_HOST DB_PORT DB_USER DB_PASSWORD DB_NAME; do
    if [ -z "${!var:-}" ]; then
        echo "ERROR: $var is not set. Source .env first." >&2
        exit 1
    fi
done

banner() { printf "\n=== %s ===\n" "$*"; }

# --- 1. SMOKE ------------------------------------------------------------
banner "SMOKE  ${SMOKE_LIMIT} rows → ${SMOKE_DIR}"
mkdir -p "$SMOKE_DIR"
uv run marcxml-export-sierra \
    --path "$SMOKE_DIR" \
    --limit "$SMOKE_LIMIT" \
    --max-workers "$MAX_WORKERS"

# Sanity: count files written
smoke_count=$(find "$SMOKE_DIR" -maxdepth 1 -name "*.xml" -type f | wc -l | tr -d ' ')
echo "Smoke wrote $smoke_count MARCXML files."
if [ "$smoke_count" -lt "$SMOKE_LIMIT" ]; then
    echo "WARN: expected $SMOKE_LIMIT files, got $smoke_count." >&2
fi

# Spot-check: every smoke file should carry a 001/003/005/907.
banner "SMOKE check  001 / 003 / 005 / 907 presence"
miss_001=0; miss_003=0; miss_005=0; miss_907=0
for f in "$SMOKE_DIR"/*.xml; do
    grep -q '<controlfield tag="001"' "$f" || miss_001=$((miss_001+1))
    grep -q '<controlfield tag="003"' "$f" || miss_003=$((miss_003+1))
    grep -q '<controlfield tag="005"' "$f" || miss_005=$((miss_005+1))
    grep -q 'tag="907"' "$f" || miss_907=$((miss_907+1))
done
echo "Missing 001: $miss_001 / 003: $miss_003 / 005: $miss_005 / 907: $miss_907 (of $smoke_count)"
if [ "$miss_001" -gt 0 ]; then
    echo "FATAL: smoke export produced files without MARC 001." >&2
    echo "This is the SupaRed-class bug. Do not proceed to full export." >&2
    exit 1
fi

# --- 2. VALIDATE ---------------------------------------------------------
# Pipe the smoke slice through M2 + M3 to confirm marc2bibframe2 + the
# BIBFRAME→BFFI hop are happy with what the exporter produced. No DB,
# no internet — purely local XSLT + SPARQL CONSTRUCT.
banner "VALIDATE  smoke → marc-to-bf → bf-to-bffi"
VALIDATE_OUT="${SMOKE_DIR}-validated"
mkdir -p "$VALIDATE_OUT"
uv run bffi-pipeline marc-to-bf "$SMOKE_DIR" --output-dir "$VALIDATE_OUT"
uv run bffi-pipeline bf-to-bffi --output-dir "$VALIDATE_OUT"

bibframe_count=$(find "$VALIDATE_OUT/bibframe" -name "*.ttl" -type f 2>/dev/null | wc -l | tr -d ' ')
bffi_count=$(find "$VALIDATE_OUT/bffi" -name "*.ttl" -type f 2>/dev/null | wc -l | tr -d ' ')
echo "Validate produced ${bibframe_count} BIBFRAME and ${bffi_count} BFFI Turtle files."
if [ "$bibframe_count" -eq 0 ] || [ "$bffi_count" -eq 0 ]; then
    echo "FATAL: validation produced no output. Inspect $VALIDATE_OUT before proceeding." >&2
    exit 1
fi

# --- 3. FULL (gated) -----------------------------------------------------
if [ "$CONFIRM_FULL" -ne 1 ]; then
    banner "STOP  smoke + validate green; re-run with --confirm-full for the full export"
    echo "Smoke output: $SMOKE_DIR"
    echo "Validation:   $VALIDATE_OUT"
    exit 0
fi

banner "FULL  ~800 k bibs → ${FULL_DIR}"
mkdir -p "$FULL_DIR"
# No --limit — full corpus. Expect 1-2 h on a healthy replica.
uv run marcxml-export-sierra \
    --path "$FULL_DIR" \
    --max-workers "$MAX_WORKERS"

full_count=$(find "$FULL_DIR" -maxdepth 1 -name "*.xml" -type f | wc -l | tr -d ' ')
banner "DONE  full export wrote ${full_count} MARCXML files to ${FULL_DIR}"
echo "Next: switch DB off, internet on, then:"
echo "  MARCXML_DIR=$FULL_DIR BFFI_DATA_DIR=<path> scripts/run-full-pipeline.sh"
