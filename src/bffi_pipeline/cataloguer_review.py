"""Cataloguer-facing unified review TSVs (P-31 Phase B + C).

Two per-run files at ``<BFFI_DATA_DIR>/``:

- ``cataloguer-source-review-<run_uuid>.tsv`` — bib_ids the pipeline
  refused or partially refused because the MARCXML itself was wrong or
  incomplete (M2 errors, M3 SHACL fails, M8 mint failures). Cataloguer
  fixes the source, re-runs.
- ``cataloguer-target-review-<run_uuid>.tsv`` — canonical Works the
  pipeline transformed in a way that warrants a cataloguer
  sanity-check (M8 conflicts, M9 fallback / no-candidate / fictional,
  FP-veto classes once those land). Cataloguer verifies + records
  verdict; the fix is in the pipeline, not the source or Skosmos.

Both helpers are no-ops when no active emitter is set (tests +
direct-CLI invocations that don't bootstrap the pipeline emitter
fall through silently). Dedup is per-process — `(bib_id, stage,
details)` for source, `(canonical_work_uri, reason)` for target —
so the same row from two call sites within one run lands once.
Tests reset the dedup state via :func:`_reset_for_tests`.

Header conventions: UTF-8 without BOM, tab-delimited, written once on
first append. Per-stage TSVs (`bibframe/_errors.tsv`,
`bffi/_validation.tsv`, `canonical-mint-failures.tsv`) stay as-is —
the unified TSVs are derived cataloguer-handoff views alongside them.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Final

from bffi_pipeline.config import get_settings
from bffi_pipeline.observability.events import get_active_emitter

_SOURCE_HEADER: Final[tuple[str, ...]] = (
    "bib_id",
    "stage",
    "severity",
    "details",
)

_TARGET_HEADER: Final[tuple[str, ...]] = (
    "member_bib_ids",
    "reason",
    "confidence",
    "details",
)

#: Max length for the ``details`` / ``expected_behavior`` free-text
#: columns. Bibliographic data + SHACL reports can run to multiple
#: KB; truncating keeps the spreadsheet readable in Excel/Numbers.
_FREE_TEXT_MAX_LEN: Final[int] = 240

# Per-process state. Module-level singletons are intentional: one
# pipeline invocation = one process = one set of dedup keys. Tests
# reset via :func:`_reset_for_tests`.
_source_seen: set[tuple[str, str, str]] = set()  # (bib_id, stage, details)
_target_seen: set[tuple[str, str]] = set()
_source_header_written = False
_target_header_written = False


def _truncate(value: str) -> str:
    if len(value) <= _FREE_TEXT_MAX_LEN:
        return value
    return value[:_FREE_TEXT_MAX_LEN] + "…"


def _source_tsv_path() -> tuple[Path, str] | None:
    """Resolve the source-review TSV path + active run_uuid, or ``None``."""
    emitter = get_active_emitter()
    if emitter is None:
        return None
    data_dir = get_settings().data_dir
    return (
        data_dir / f"cataloguer-source-review-{emitter.run_uuid}.tsv",
        emitter.run_uuid,
    )


def _target_tsv_path() -> tuple[Path, str] | None:
    """Resolve the target-review TSV path + active run_uuid, or ``None``."""
    emitter = get_active_emitter()
    if emitter is None:
        return None
    data_dir = get_settings().data_dir
    return (
        data_dir / f"cataloguer-target-review-{emitter.run_uuid}.tsv",
        emitter.run_uuid,
    )


def append_source_row(
    *,
    bib_id: str,
    stage: str,
    severity: str,
    details: str,
) -> None:
    """Append one row to the unified source-review TSV.

    ``stage`` ∈ {``"m2"``, ``"m3"``, ``"m8"``} — the upstream stage
    that flagged the row. ``severity`` ∈ {``"blocking"``,
    ``"warning"``} — drives cataloguer triage. ``details`` is the
    human-readable message, truncated to 240 chars.

    No-op when no emitter is active.
    """
    global _source_header_written  # noqa: PLW0603 — module-level state by design.
    resolved = _source_tsv_path()
    if resolved is None:
        return
    path, _run_uuid = resolved

    truncated_details = _truncate(details)
    key = (bib_id, stage, truncated_details)
    if key in _source_seen:
        return
    _source_seen.add(key)

    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not _source_header_written and not path.exists()
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t", quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        if write_header:
            writer.writerow(_SOURCE_HEADER)
        writer.writerow([bib_id, stage, severity, truncated_details])
    _source_header_written = True


def append_target_row(
    *,
    member_bib_ids: list[str],
    reason: str,
    confidence: float | None,
    details: str,
    dedup_key: str,
) -> None:
    """Append one row to the unified target-review TSV.

    ``reason`` is the pipeline's flag for why this canonical Work
    needs review — ``"m8-conflict"``, ``"m9-fallback"``,
    ``"m9-no-candidate"``, ``"fictional-character"``, or
    ``"fp-<class>"`` once the FP veto plans land.

    ``member_bib_ids`` pipe-separates as ``a|b|c`` in the TSV (empty
    list → empty string).

    ``details`` is a free-text column the caller builds per reason:
    M9 packs the request literal + top considered candidates (URI,
    label, source, similarity) + picker rationale; M8-conflict
    describes the contradiction (conflicting pair + same-work path).
    Truncated to 240 chars.

    ``dedup_key`` is the per-process dedup token: the caller picks a
    value that uniquely identifies this surface so the same row from
    two call sites within one run lands once. Typical values:
    canonical_work_uri (M8-conflict), request.work_uri (M9 outcomes).

    No-op when no emitter is active.
    """
    global _target_header_written  # noqa: PLW0603
    resolved = _target_tsv_path()
    if resolved is None:
        return
    path, _run_uuid = resolved

    key = (dedup_key, reason)
    if key in _target_seen:
        return
    _target_seen.add(key)

    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not _target_header_written and not path.exists()
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t", quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        if write_header:
            writer.writerow(_TARGET_HEADER)
        writer.writerow(
            [
                "|".join(member_bib_ids),
                reason,
                "" if confidence is None else f"{confidence:.4f}",
                _truncate(details),
            ]
        )
    _target_header_written = True


def _reset_for_tests() -> None:
    """Clear the per-process dedup state + header flags. Test-only."""
    global _source_header_written, _target_header_written  # noqa: PLW0603
    _source_seen.clear()
    _target_seen.clear()
    _source_header_written = False
    _target_header_written = False


__all__ = [
    "append_source_row",
    "append_target_row",
]
