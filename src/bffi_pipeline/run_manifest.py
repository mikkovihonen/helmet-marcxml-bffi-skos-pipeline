"""Per-run manifest (`bffi-run.json`) — minimal stub on the rewrite branch.

The full manifest model on `main` carries run identification (run_uuid,
started_at, ended_at, description) plus per-stage lifecycle markers
(stages_observed, stages_completed) the dashboard ingests via the metrics
exporter. The rewrite branch will reimplement that surface when the
observability event-sidecar wiring lands; for now the public API is two
no-op helpers so the observability event emitter can compile.

When a runs-lifecycle subcommand lands, lift the full implementation from
``main:src/bffi_pipeline/run_manifest.py`` and replace this stub.
"""

from __future__ import annotations

from pathlib import Path


def append_stage_observed(manifest_path: Path, stage: str) -> None:
    """Append `stage` to the manifest's `stages_observed` list. No-op stub."""
    del manifest_path, stage


def append_stage_completed(manifest_path: Path, stage: str) -> None:
    """Append `stage` to the manifest's `stages_completed` list. No-op stub."""
    del manifest_path, stage
