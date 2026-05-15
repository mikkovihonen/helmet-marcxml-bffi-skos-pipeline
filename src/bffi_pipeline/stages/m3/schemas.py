"""M3 public dataclasses.

Two row / summary shapes surfaced to ``cli.py``, the
``bffi/_validation.jsonl`` writer, and the partial-failure exit-policy
tests:

- :class:`ValidationRow` — one row of ``bffi/_validation.jsonl`` per
  Boundary-3-failing record. SHACL failures don't halt the pipeline;
  they're logged here and flagged in the end-of-run summary.
- :class:`BffiSummary` — end-of-run report; ``render()`` formats the
  CLI summary with converted / skipped / shape-failing / errored
  counts.

P-38 Phase D: extracted from m3/runner.py to keep the runner focused
on the conversion orchestration. No logic change — moves only.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ValidationRow:
    """One row of ``_validation.jsonl`` per (Boundary-3-failing) record.

    ``run_uuid`` is populated from the active observability emitter
    so the exporter's error-tail loop (P-12 Option B) can attribute
    each row to its originating pipeline invocation. Empty string
    when no emitter is active (e.g. unit tests that bypass the CLI
    bootstrap) — rows surface under ``run_uuid=""`` in metrics.
    """

    helmet_bib_id: str
    output_file: str
    conforms: bool
    report_text: str
    run_uuid: str = ""


@dataclass
class BffiSummary:
    """Aggregate counts for an end-of-run report."""

    converted: list[str] = field(default_factory=list)
    skipped_idempotent: list[str] = field(default_factory=list)
    failed_shape: list[str] = field(default_factory=list)
    errored: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        """Total number of input files seen, excluding shape-only flags."""
        return len(self.converted) + len(self.skipped_idempotent) + len(self.errored)

    def render(self) -> str:
        """Format this summary as paste-ready text for the bf-to-bffi CLI."""
        lines = [
            f"BIBFRAME to BFFI conversion summary ({self.total} input file(s))",
            f"  converted: {len(self.converted)}",
            f"  skipped (already converted): {len(self.skipped_idempotent)}",
            f"  shape-failing (kept; flagged): {len(self.failed_shape)}",
            f"  errored: {len(self.errored)}",
        ]
        if self.failed_shape:
            lines.append("Shape-failing records:")
            lines.extend(f"  - {bib}" for bib in self.failed_shape)
        if self.errored:
            lines.append("Hard errors (record skipped):")
            lines.extend(f"  - {bib}: {msg}" for bib, msg in self.errored)
        return "\n".join(lines)
