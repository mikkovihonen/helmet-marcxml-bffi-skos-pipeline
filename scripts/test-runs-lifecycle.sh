#!/usr/bin/env bash
# scripts/test-runs-lifecycle.sh — manual smoke driver for P-32 runs CLI.
#
# Exercises every public `bffi-pipeline runs` subcommand against three
# seeded fake run dirs under a throwaway BFFI_RUNS_ROOT. Useful for
# verifying the lifecycle features end-to-end after touching anything in
# src/bffi_pipeline/{cli,run_manifest,runs_reset,stages/fuseki_clear}.py.
#
# What it exercises:
#   - runs list (default table; --json; --tag filter)
#   - runs info (artifact enumeration; JSONL row count; sub-dir count)
#   - runs tag / untag
#   - runs mark-complete (the manual fallback for crashed pipelines)
#   - runs prune (--older-than dry-run; --keep-last preservation; --apply)
#   - runs clear-fuseki --dry-run (best-effort; harmlessly skipped if
#     Fuseki isn't running on the configured BFFI_FUSEKI_URL).
#
# Setup: BFFI_RUNS_ROOT is a throwaway mktemp dir cleaned up on exit;
# BFFI_OBSERVABILITY_SIDECAR=none so per-invocation self-manifests don't
# accumulate alongside the seeded fixtures.
#
# Usage:
#   scripts/test-runs-lifecycle.sh
#
# Override the test root (e.g. to keep it around for inspection):
#   KEEP=1 TEST_ROOT=/tmp/bffi-p32-debug scripts/test-runs-lifecycle.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

TEST_ROOT="${TEST_ROOT:-$(mktemp -d -t bffi-p32-smoke.XXXXXXXX)}"
export BFFI_RUNS_ROOT="$TEST_ROOT"
export BFFI_OBSERVABILITY_SIDECAR=none

cleanup() {
    if [[ "${KEEP:-0}" == "1" ]]; then
        echo
        echo "KEEP=1 — leaving $TEST_ROOT in place for inspection."
    else
        echo
        echo "Cleaning up $TEST_ROOT"
        rm -rf "$TEST_ROOT"
    fi
}
trap cleanup EXIT

mkdir -p "$TEST_ROOT"
echo "Using throwaway BFFI_RUNS_ROOT=$TEST_ROOT"

step() {
    echo
    echo "===== $* ====="
}

# --- Seed three fake runs covering the interesting lifecycle states. -------
step "Seeding three fake runs (60d-old tagged, 1d-old untagged, still-running)"
uv run python - <<'PY'
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bffi_pipeline.run_manifest import (
    MANIFEST_FILENAME,
    RunManifest,
    write_manifest,
)

runs_root = Path(os.environ["BFFI_RUNS_ROOT"])
now = datetime.now(UTC)
fixtures = [
    {
        "run_uuid": "11111111111111111111111111111111",
        "started_at": now - timedelta(days=60),
        "ended_at": now - timedelta(days=60) + timedelta(hours=2),
        "tags": ["nightly"],
        "status": "completed",
        "description": "P-32 smoke — old tagged run",
    },
    {
        "run_uuid": "22222222222222222222222222222222",
        "started_at": now - timedelta(days=1),
        "ended_at": now,
        "tags": [],
        "status": "completed",
        "description": "P-32 smoke — recent untagged run",
    },
    {
        "run_uuid": "33333333333333333333333333333333",
        "started_at": now,
        "ended_at": None,
        "tags": [],
        "status": "running",
        "description": "P-32 smoke — still-running run (mark-complete target)",
    },
]
for f in fixtures:
    run_dir = runs_root / f["run_uuid"]
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = RunManifest(
        run_uuid=f["run_uuid"],
        started_at=f["started_at"],
        ended_at=f["ended_at"],
        bffi_data_dir=str(run_dir),
        description=f["description"],
        tags=f["tags"],
        status=f["status"],
    )
    write_manifest(run_dir / MANIFEST_FILENAME, manifest)
    (run_dir / "payload.bin").write_bytes(b"x" * 1024)
    (run_dir / "stage-events.jsonl").write_bytes(
        b'{"stage":"m2","event":"start"}\n{"stage":"m2","event":"end"}\n'
    )
    bibframe = run_dir / "bibframe"
    bibframe.mkdir()
    (bibframe / "record-1.xml").write_bytes(b"<rdf:RDF/>")
    print(f"  seeded {f['run_uuid'][:12]}... ({f['status']}, tags={f['tags']})")
PY

step "runs list (default table)"
uv run bffi-pipeline runs list

step "runs list --json (machine-readable)"
uv run bffi-pipeline runs list --json

step "runs info on the 60d-old tagged run"
uv run bffi-pipeline runs info 11111111

step "runs tag — add 'manual-smoke' + 'release-candidate' to the recent run"
uv run bffi-pipeline runs tag 22222222 manual-smoke release-candidate

step "runs info — confirm tags landed"
uv run bffi-pipeline runs info 22222222

step "runs untag — remove the 'release-candidate' tag"
uv run bffi-pipeline runs untag 22222222 release-candidate

step "runs list --tag manual-smoke (filter narrows to one row)"
uv run bffi-pipeline runs list --tag manual-smoke

step "runs mark-complete on the still-running run with --status=aborted"
uv run bffi-pipeline runs mark-complete 33333333 --status=aborted
uv run bffi-pipeline runs info 33333333

step "runs prune --older-than 30d (DRY RUN — should preview deleting the 60d run only)"
uv run bffi-pipeline runs prune --older-than 30d

step "runs prune --older-than 30d --apply --keep-last 5 (keep-last rescues every match → no-op)"
uv run bffi-pipeline runs prune --older-than 30d --apply --keep-last 5

step "runs prune --older-than 30d --apply (no preservation flag → 60d run actually deletes)"
uv run bffi-pipeline runs prune --older-than 30d --apply

step "runs list — only the two newer runs should remain"
uv run bffi-pipeline runs list

step "runs clear-fuseki --dry-run (best-effort; harmless skip if Fuseki is down)"
uv run bffi-pipeline runs clear-fuseki || echo "(Fuseki not reachable — non-zero exit is expected here.)"

echo
echo "All P-32 lifecycle commands exercised successfully."
