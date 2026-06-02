"""P-41 Phase C — per-run export-synthesis TSV writer.

Mirrors :mod:`bffi_pipeline.cataloguer_review`'s pattern: one TSV per
pipeline run at ``<BFFI_DATA_DIR>/export-synthesis-<run_uuid>.tsv``,
written once on first append, UTF-8 no BOM, tab-delimited, dedup keyed
on ``(bib_id, field, tier)`` so the same synthesis row from two call
sites within one run lands once.

The TSV is the consumer-audit surface — "what did the pipeline make up
on this run?" — and a sibling of the source-review TSV which captures
the cataloguer-action surface ("which records need source-side fixes?").
A record salvaged through B1 will appear in both:

- ``cataloguer-source-review-<run>.tsv`` with ``severity=warning`` and
  the original missing-creator details (the cataloguer signal that the
  source MARCXML should be improved).
- ``export-synthesis-<run>.tsv`` with the synthesised value, tier,
  confidence, and Synthesis Activity URI (the consumer signal that
  this Work's creator came from salvage, not from cataloguing).

Both helpers are no-ops when no active emitter is set (tests +
direct-CLI invocations that don't bootstrap the pipeline emitter fall
through silently). Dedup is per-process — `(bib_id, field, tier)` — so
the same row from two call sites within one run lands once. Tests
reset the dedup state via :func:`_reset_for_tests`.

Phase C.3's retrospective regen CLI re-derives the TSV from
``data/provenance.ttl`` alone; see ``bffi-pipeline export-synthesis-report
--run <uuid>``.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Final

from bffi_pipeline.config import get_settings
from bffi_pipeline.observability.events import get_active_emitter

_HEADER: Final[tuple[str, ...]] = (
    "bib_id",
    "field",
    "marc_source",
    "synthesised_value",
    "tier",
    "method",
    "confidence",
    "activity_uri",
)

#: Per-process dedup state. Module-level singletons by design — one
#: pipeline invocation = one process = one set of dedup keys. Tests
#: reset via :func:`_reset_for_tests`. Key is
#: ``(bib_id, field, tier)``: a record that synthesised two distinct
#: fields (e.g. creator AND a future-tier 008-date) gets two rows;
#: re-entering the salvage path for the same (bib, field, tier)
#: produces one row.
_seen: set[tuple[str, str, str]] = set()
_header_written = False


def _tsv_path() -> tuple[Path, str] | None:
    """Resolve the per-run TSV path + active run_uuid, or ``None`` when
    no emitter is active (tests / standalone use)."""
    emitter = get_active_emitter()
    if emitter is None:
        return None
    data_dir = get_settings().data_dir
    return (
        data_dir / f"export-synthesis-{emitter.run_uuid}.tsv",
        emitter.run_uuid,
    )


def append_synthesis_row(
    *,
    bib_id: str,
    field: str,
    marc_source: str,
    synthesised_value: str,
    tier: str,
    method: str,
    confidence: float,
    activity_uri: str,
) -> None:
    """Append one row to the per-run export-synthesis TSV.

    All eight columns are required (no Optional fields — the
    retrospective regen CLI relies on schema stability). ``confidence``
    is formatted to 4-decimal-place precision in the TSV so consumer-
    side SQL imports get a fixed-width decimal column. Dedup key is
    ``(bib_id, field, tier)``.

    No-op when no emitter is active.
    """
    global _header_written  # noqa: PLW0603 — module-level state by design.
    resolved = _tsv_path()
    if resolved is None:
        return
    path, _run_uuid = resolved

    key = (bib_id, field, tier)
    if key in _seen:
        return
    _seen.add(key)

    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not _header_written and not path.exists()
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t", quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        if write_header:
            writer.writerow(_HEADER)
        writer.writerow(
            [
                bib_id,
                field,
                marc_source,
                synthesised_value,
                tier,
                method,
                f"{confidence:.4f}",
                activity_uri,
            ]
        )
    _header_written = True


def _reset_for_tests() -> None:
    """Clear the per-process dedup state + header flag. Test-only."""
    global _header_written  # noqa: PLW0603 — module-level state by design.
    _seen.clear()
    _header_written = False


__all__ = [
    "append_synthesis_row",
]
