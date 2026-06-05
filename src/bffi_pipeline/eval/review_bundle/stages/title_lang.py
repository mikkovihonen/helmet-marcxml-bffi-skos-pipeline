"""Cataloguer review pipeline — M3 title-language stage.

Consumes ``runs/<uuid>/title-lang-candidates.jsonl`` (per-fire
audit log written by ``stages/m3/title_lang_audit``) as the
candidate pool. Like judge / picker / salvage, title-lang
bundles are per-run only — there's no corpus-wide aggregator.

Each audit row carries the title text, the cataloguer's declared
language candidate set, the LLM's per-segment assignment, and
the rationale. The cataloguer reviews whether each segment
landed in the right language; KEEP rows become title-lang gold
cases appended to ``gold/title-lang.jsonl`` (new file, bootstrap
empty).
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

#: M3 writes this per-run sidecar when the title-language LLM
#: cascade is enabled. Auto-discovered by the cataloguer-bundle
#: dispatcher.
AUDIT_FILENAME: Final[str] = "title-lang-candidates.jsonl"

#: ``added_by`` prefix stamped on KEEP rows that land in
#: ``gold/title-lang.jsonl``.
ADDED_BY_PREFIX: Final[str] = "cataloguer-m3-title"

#: Decision values the HTML tool emits — contrib-style 3-way.
DECISION_KEEP: Final[str] = "KEEP"
DECISION_DISCARD: Final[str] = "DISCARD"
DECISION_SKIP: Final[str] = "SKIP-FOR-NOW"

_VALID_DECISIONS: Final[frozenset[str]] = frozenset(
    {DECISION_KEEP, DECISION_DISCARD, DECISION_SKIP}
)

#: Segment-count bands for stratified sampling. The interesting
#: cases are 2- and 3-segment parallel titles; longer titles are
#: rare but the bundle should still surface them.
_SEGMENT_BANDS: Final[tuple[tuple[str, int, int], ...]] = (
    ("single", 1, 2),
    ("2seg", 2, 3),
    ("3seg", 3, 4),
    ("multi", 4, 999),
)

_PREVIEW_CAP: Final[int] = 5


class TitleLangImportError(ValueError):
    """Raised when a cataloguer title-lang result row fails validation."""


@dataclass
class ImportSummary:
    """Operator-facing summary for a title-lang import."""

    rows_seen: int = 0
    landed: int = 0
    discarded: int = 0
    skipped: int = 0
    gold_total_before: int = 0
    gold_total_after: int = 0
    by_band: dict[str, int] = field(default_factory=dict)
    skipped_ids: list[str] = field(default_factory=list)
    flipped_ids: list[tuple[str, str]] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            "title-lang-review-import summary",
            f"  rows in cataloguer file:        {self.rows_seen}",
            f"  Landed in gold/title-lang.jsonl: {self.landed}",
            f"  DISCARD (dropped):              {self.discarded}",
            f"  SKIP-FOR-NOW (still pending):   {self.skipped}",
        ]
        if self.by_band:
            lines.append("  Landed by segment-count band:")
            for band, n in sorted(self.by_band.items()):
                lines.append(f"    {band:35} {n}")
        lines.append(f"  gold/title-lang.jsonl: {self.gold_total_before} → {self.gold_total_after}")
        if self.skipped_ids:
            preview = ", ".join(self.skipped_ids[:_PREVIEW_CAP])
            extra = len(self.skipped_ids) - _PREVIEW_CAP
            more = "" if extra <= 0 else f" (and {extra} more)"
            lines.append(f"  SKIP-FOR-NOW ids: {preview}{more}")
        if self.flipped_ids:
            lines.append(
                f"  Cataloguer overrode per-segment language on {len(self.flipped_ids)} title(s):"
            )
            for new_id, title in self.flipped_ids[:_PREVIEW_CAP]:
                lines.append(f"    {new_id}: {title!r}")
            extra = len(self.flipped_ids) - _PREVIEW_CAP
            if extra > 0:
                lines.append(f"    (and {extra} more)")
        return "\n".join(lines)


# --- Sampler --------------------------------------------------------------


def _band_for(segment_count: int) -> str:
    for label, lo, hi in _SEGMENT_BANDS:
        if lo <= segment_count < hi:
            return label
    return "multi"


def _seed_to_int(seed: str) -> int:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def sample_stratified(
    *,
    input_path: Path,
    output_path: Path,
    per_category: int = 25,
    seed: str | None = None,
) -> dict[str, int]:
    """Stratify on ``segment_count_band x len(candidates)`` so the
    cataloguer sees both 2-language and 3+language records.
    """
    if seed is None:
        seed = datetime.now(UTC).date().isoformat()
    rng = random.Random(_seed_to_int(seed))

    buckets: dict[tuple[str, int], list[dict[str, Any]]] = {}
    with input_path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            row = json.loads(line)
            segs = row.get("llm_segments") or []
            cand_count = len(row.get("candidates") or [])
            key = (_band_for(len(segs)), cand_count)
            buckets.setdefault(key, []).append(row)

    histogram: dict[str, int] = {}
    picked: list[dict[str, Any]] = []
    for (band, cand_count), rows in sorted(buckets.items()):
        # ``per_category <= 0`` means "no cap" — see picker.py.
        chosen = rows if per_category <= 0 else rng.sample(rows, min(per_category, len(rows)))
        histogram[f"{band}/{cand_count}cand"] = len(chosen)
        picked.extend(chosen)

    sequence = 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as out_fh:
        for row in picked:
            sequence += 1
            candidate = dict(row)
            candidate["id"] = f"cg-pending-{sequence:04d}"
            out_fh.write(json.dumps(candidate, ensure_ascii=False))
            out_fh.write("\n")

    return histogram


def bib_ids_for_marc(candidates_jsonl: bytes) -> list[str]:
    """Return one bib id per row (originating record)."""
    out: list[str] = []
    for raw in candidates_jsonl.decode("utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        row = json.loads(line)
        bib = row.get("helmet_bib_id")
        if bib:
            out.append(bib)
    return out


# --- Importer -----------------------------------------------------------


def _validate_keep_row(row: dict[str, Any]) -> None:
    row_id = row.get("id", "<no-id>")
    if not row.get("title"):
        raise TitleLangImportError(f"Row {row_id!r}: title is required for KEEP.")
    segments = row.get("expected_segments") or []
    if not isinstance(segments, list) or not segments:
        raise TitleLangImportError(
            f"Row {row_id!r}: expected_segments must be a non-empty list for KEEP. "
            "Use DISCARD if the LLM's split was wrong / unsalvageable."
        )
    candidates = set(row.get("candidates") or [])
    for i, seg in enumerate(segments):
        if not isinstance(seg, dict) or not seg.get("text"):
            raise TitleLangImportError(
                f"Row {row_id!r}: expected_segments[{i}] must carry a non-empty 'text'."
            )
        lang = seg.get("lang")
        if lang is not None and lang not in candidates:
            raise TitleLangImportError(
                f"Row {row_id!r}: expected_segments[{i}].lang {lang!r} must be null or "
                f"a member of candidates {sorted(candidates)}."
            )


def _next_gold_id(gold_path: Path) -> int:
    if not gold_path.is_file():
        return 1
    max_seq = 0
    with gold_path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            row = json.loads(line)
            row_id = row.get("id", "")
            if not isinstance(row_id, str) or not row_id.startswith("tl-"):
                continue
            try:
                seq = int(row_id.removeprefix("tl-"))
            except ValueError:
                continue
            max_seq = max(max_seq, seq)
    return max_seq + 1


def _result_row_to_gold_row(row: dict[str, Any], *, new_id: str) -> dict[str, Any]:
    reviewer = row.get("reviewed_by") or ""
    added_by = f"{ADDED_BY_PREFIX}:{reviewer}" if reviewer else ADDED_BY_PREFIX
    today = datetime.now(UTC).date().isoformat()
    return {
        "id": new_id,
        "helmet_bib_id": row.get("helmet_bib_id"),
        "work_uri": row.get("work_uri"),
        "title": row["title"],
        "candidates": row.get("candidates") or [],
        "expected_segments": row["expected_segments"],
        "holdout": bool(row.get("holdout", False)),
        "added": row.get("added") or today,
        "added_by": added_by,
        "notes": row.get("notes") or "",
    }


def _segments_match(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> bool:
    if len(a) != len(b):
        return False
    for sa, sb in zip(a, b, strict=True):
        if sa.get("text") != sb.get("text") or sa.get("lang") != sb.get("lang"):
            return False
    return True


def import_results(*, input_path: Path, output_path: Path) -> ImportSummary:
    """Import a cataloguer's exported title-lang results.jsonl."""
    rows: list[dict[str, Any]] = []
    with input_path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if line:
                rows.append(json.loads(line))

    summary = ImportSummary(rows_seen=len(rows))
    if output_path.is_file():
        summary.gold_total_before = sum(
            1 for line in output_path.read_text(encoding="utf-8").splitlines() if line.strip()
        )

    next_seq = _next_gold_id(output_path)
    landing_rows: list[dict[str, Any]] = []

    for row in rows:
        decision = row.get("decision")
        if decision not in _VALID_DECISIONS:
            row_id = row.get("id", "<no-id>")
            raise TitleLangImportError(
                f"Row {row_id!r}: decision must be one of {sorted(_VALID_DECISIONS)}, "
                f"got {decision!r}."
            )
        if decision == DECISION_KEEP:
            _validate_keep_row(row)
            landing_rows.append(row)
        elif decision == DECISION_DISCARD:
            summary.discarded += 1
        else:  # DECISION_SKIP
            summary.skipped += 1
            summary.skipped_ids.append(str(row.get("id", "<no-id>")))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as fh:
        for row in landing_rows:
            new_id = f"tl-{next_seq:04d}"
            next_seq += 1
            gold_row = _result_row_to_gold_row(row, new_id=new_id)
            fh.write(json.dumps(gold_row, ensure_ascii=False))
            fh.write("\n")
            summary.landed += 1
            band = _band_for(len(gold_row["expected_segments"]))
            summary.by_band[band] = summary.by_band.get(band, 0) + 1
            llm_segs = row.get("llm_segments") or []
            if not _segments_match(llm_segs, row["expected_segments"]):
                summary.flipped_ids.append((new_id, row["title"]))

    summary.gold_total_after = summary.gold_total_before + summary.landed
    return summary


__all__ = [
    "ADDED_BY_PREFIX",
    "AUDIT_FILENAME",
    "DECISION_DISCARD",
    "DECISION_KEEP",
    "DECISION_SKIP",
    "ImportSummary",
    "TitleLangImportError",
    "bib_ids_for_marc",
    "import_results",
    "sample_stratified",
]
