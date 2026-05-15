"""M2 public dataclasses.

Three rows / summary types surfaced to ``cli.py`` callers, the
``helmet-map.jsonl`` writer, and the partial-failure exit-policy tests:

- :class:`HelmetMapRow` — one row of ``helmet-map.jsonl`` per converted
  record. Consumed by M8 to roll raw bib IDs up to canonical Works.
- :class:`ConversionErrorRow` — one row of ``_errors.jsonl`` per failed
  record. ``run_uuid`` carries through to the metrics exporter's error
  tail.
- :class:`ConversionSummary` — end-of-run report; ``render()`` formats
  the CLI summary.

P-38 Phase D: extracted from m2/runner.py to keep the runner focused
on the conversion orchestration. No logic change — moves only.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class HelmetMapRow:
    """One row of ``helmet-map.jsonl`` per converted record."""

    helmet_bib_id: str
    source_file: str
    raw_work_uri: str
    raw_instance_uri: str
    converted_at: str
    marc2bibframe2_version: str


@dataclass(frozen=True)
class ConversionErrorRow:
    """One row of ``_errors.jsonl`` per failed record.

    ``run_uuid`` is populated from the active observability emitter
    so the exporter's error-tail loop (P-12 Option B) can attribute
    each row to its originating pipeline invocation. Empty string
    when no emitter is active (e.g. unit tests that bypass the CLI
    bootstrap) — rows surface under ``run_uuid=""`` in metrics.
    """

    helmet_bib_id: str | None
    filename: str
    error_type: str
    message: str
    run_uuid: str = ""


@dataclass
class ConversionSummary:
    """Aggregate counts for an end-of-run report."""

    succeeded: list[str] = field(default_factory=list)
    skipped_idempotent: list[str] = field(default_factory=list)
    failed: list[ConversionErrorRow] = field(default_factory=list)

    @property
    def total(self) -> int:
        """Total number of input files seen across all outcomes."""
        return len(self.succeeded) + len(self.skipped_idempotent) + len(self.failed)

    def render(self) -> str:
        """Format this summary as paste-ready text for the marc-to-bf CLI."""
        lines = [
            f"MARCXML to BIBFRAME conversion summary ({self.total} input file(s))",
            f"  succeeded: {len(self.succeeded)}",
            f"  skipped (already converted): {len(self.skipped_idempotent)}",
            f"  failed: {len(self.failed)}",
        ]
        if self.failed:
            lines.append("Failures:")
            lines.extend(
                f"  - {row.filename}: [{row.error_type}] {row.message}" for row in self.failed
            )
        return "\n".join(lines)
