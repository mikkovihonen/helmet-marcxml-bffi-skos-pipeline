"""Cataloguer review pipeline — M2 salvage stage of the review bundle.

The M2 salvage cascade (P-41) writes ``runs/<uuid>/salvage-candidates.jsonl``
per run (via :mod:`bffi_pipeline.stages.m2.salvage_audit`) when the
``--llm-salvage-cascade`` runner flag is on. This handler consumes
that audit log as the candidate pool. Like judge + picker, salvage
bundles are per-run only.

Each audit row carries:

- ``helmet_bib_id`` — the record being salvaged
- ``c_subfield`` — the MARC 245$c the LLM parsed
- ``salvaged_agents`` — post-substring-check ``LlmAgent`` list
- ``rationale`` — the LLM's free-text reasoning
- ``cache_hit`` — bool indicating fresh LLM call vs cache replay

The cataloguer reviews whether the salvaged creator(s) are right;
KEEP rows become :class:`~salvage.SalvageGoldCase` rows appended to
``gold/salvage.jsonl`` (new file, bootstrap empty).
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from bffi_pipeline.contrib_extract_llm import VALID_RELATOR_CODES

#: M2 already writes this per-run sidecar when the LLM cascade is
#: enabled. Auto-discovered by the cataloguer-bundle dispatcher.
AUDIT_FILENAME: Final[str] = "salvage-candidates.jsonl"

#: ``added_by`` prefix stamped on KEEP rows that land in
#: ``gold/salvage.jsonl``. Lets the eval harness tell salvage gold
#: apart from contrib / judge / picker gold.
ADDED_BY_PREFIX: Final[str] = "cataloguer-m2"

#: Decision values the HTML tool emits. Contrib-style 3-way:
#: KEEP / DISCARD / SKIP-FOR-NOW.
DECISION_KEEP: Final[str] = "KEEP"
DECISION_DISCARD: Final[str] = "DISCARD"
DECISION_SKIP: Final[str] = "SKIP-FOR-NOW"

_VALID_DECISIONS: Final[frozenset[str]] = frozenset(
    {DECISION_KEEP, DECISION_DISCARD, DECISION_SKIP}
)

#: Agent-count bands for stratified sampling. Single-agent salvages
#: are the most common; multi-agent salvages benefit from extra
#: cataloguer eyes (the LLM might have over-extracted from a
#: capitalised list).
_AGENT_COUNT_BANDS: Final[tuple[tuple[str, int, int], ...]] = (
    ("empty", 0, 1),
    ("single", 1, 2),
    ("multi", 2, 999),
)

_PREVIEW_CAP: Final[int] = 5


class SalvageImportError(ValueError):
    """Raised when a cataloguer salvage result row fails validation.

    Names the offending row id + the failing field so the operator
    can ask the cataloguer to fix one specific row.
    """


@dataclass
class ImportSummary:
    """Operator-facing summary for a salvage import."""

    rows_seen: int = 0
    landed: int = 0
    discarded: int = 0
    skipped: int = 0
    gold_total_before: int = 0
    gold_total_after: int = 0
    by_band: dict[str, int] = field(default_factory=dict)
    cache_hit_kept: int = 0
    skipped_ids: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            "salvage-review-import summary",
            f"  rows in cataloguer file:        {self.rows_seen}",
            f"  Landed in gold/salvage.jsonl:   {self.landed}",
            f"  DISCARD (dropped):              {self.discarded}",
            f"  SKIP-FOR-NOW (still pending):   {self.skipped}",
        ]
        if self.by_band:
            lines.append("  Landed by agent-count band:")
            for band, n in sorted(self.by_band.items()):
                lines.append(f"    {band:35} {n}")
        if self.cache_hit_kept > 0:
            lines.append(f"  KEEP rows from cache hits:      {self.cache_hit_kept}")
        lines.append(f"  gold/salvage.jsonl: {self.gold_total_before} → {self.gold_total_after}")
        if self.skipped_ids:
            preview = ", ".join(self.skipped_ids[:_PREVIEW_CAP])
            extra = len(self.skipped_ids) - _PREVIEW_CAP
            more = "" if extra <= 0 else f" (and {extra} more)"
            lines.append(f"  SKIP-FOR-NOW ids: {preview}{more}")
        return "\n".join(lines)


# --- Sampler ------------------------------------------------------------


def _band_for(agent_count: int) -> str:
    for label, lo, hi in _AGENT_COUNT_BANDS:
        if lo <= agent_count < hi:
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
    """Draw a stratified slice of the run's salvage decisions.

    Strata are ``cache_hit x agent_count_band`` (2 x 3 = 6 cells).
    For each stratum, sample up to ``per_category`` rows seeded by
    ``seed`` (default: today's UTC date). Returns a per-stratum
    histogram.

    Audit rows already carry full context (bib id, c_subfield,
    agents, rationale, cache flag) so the sampler just picks rows
    verbatim — no hydration step needed.
    """
    if seed is None:
        seed = datetime.now(UTC).date().isoformat()
    rng = random.Random(_seed_to_int(seed))

    buckets: dict[tuple[bool, str], list[dict[str, Any]]] = {}
    with input_path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            row = json.loads(line)
            count = len(row.get("salvaged_agents") or [])
            key = (bool(row.get("cache_hit")), _band_for(count))
            buckets.setdefault(key, []).append(row)

    histogram: dict[str, int] = {}
    picked: list[dict[str, Any]] = []
    for (cache_hit, band), rows in sorted(buckets.items()):
        # ``per_category <= 0`` means "no cap" — see picker.py.
        chosen = rows if per_category <= 0 else rng.sample(rows, min(per_category, len(rows)))
        cache_tag = "cache-hit" if cache_hit else "fresh"
        histogram[f"{cache_tag}/{band}"] = len(chosen)
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
    """Return one bib id per row (the originating record). The
    bundle builder dedups across rows that share a bib id."""
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
    if not row.get("helmet_bib_id"):
        raise SalvageImportError(f"Row {row_id!r}: helmet_bib_id is required for KEEP.")
    agents = row.get("expected_agents") or []
    if not isinstance(agents, list) or not agents:
        raise SalvageImportError(
            f"Row {row_id!r}: expected_agents must be a non-empty list for KEEP. "
            "If the LLM's salvage was wrong / there are no agents to keep, use DISCARD."
        )
    for i, agent in enumerate(agents):
        if not isinstance(agent, dict) or not agent.get("name"):
            raise SalvageImportError(
                f"Row {row_id!r}: expected_agents[{i}] must carry a non-empty 'name'."
            )
        # ``relator_code`` is required (M3-style — uses the 20-value
        # MARC ``VALID_RELATOR_CODES`` list). Pipe-separated combos
        # like ``aft|aui|ctb`` are accepted for genuinely ambiguous
        # roles, mirroring contrib's gold contract.
        relator = agent.get("relator_code")
        if not isinstance(relator, str) or not relator:
            raise SalvageImportError(
                f"Row {row_id!r}: expected_agents[{i}] must carry a non-empty "
                "'relator_code' (MARC relator from VALID_RELATOR_CODES)."
            )
        for raw_piece in relator.split("|"):
            piece = raw_piece.strip()
            if piece and piece not in VALID_RELATOR_CODES:
                raise SalvageImportError(
                    f"Row {row_id!r}: expected_agents[{i}] relator_code piece "
                    f"{piece!r} not in VALID_RELATOR_CODES."
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
            if not isinstance(row_id, str) or not row_id.startswith("sv-"):
                continue
            try:
                seq = int(row_id.removeprefix("sv-"))
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
        "helmet_bib_id": row["helmet_bib_id"],
        "c_subfield": row.get("c_subfield"),
        "expected_agents": row["expected_agents"],
        "holdout": bool(row.get("holdout", False)),
        "added": row.get("added") or today,
        "added_by": added_by,
        "notes": row.get("notes") or "",
    }


def import_results(*, input_path: Path, output_path: Path) -> ImportSummary:
    """Import a cataloguer's exported salvage results.jsonl.

    Two-pass atomic: every KEEP row is validated before any is
    appended. DISCARD / SKIP-FOR-NOW don't land but are counted in
    the summary.
    """
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
            raise SalvageImportError(
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
            new_id = f"sv-{next_seq:04d}"
            next_seq += 1
            gold_row = _result_row_to_gold_row(row, new_id=new_id)
            fh.write(json.dumps(gold_row, ensure_ascii=False))
            fh.write("\n")
            summary.landed += 1
            band = _band_for(len(gold_row["expected_agents"]))
            summary.by_band[band] = summary.by_band.get(band, 0) + 1
            if row.get("cache_hit"):
                summary.cache_hit_kept += 1

    summary.gold_total_after = summary.gold_total_before + summary.landed
    return summary


__all__ = [
    "ADDED_BY_PREFIX",
    "AUDIT_FILENAME",
    "DECISION_DISCARD",
    "DECISION_KEEP",
    "DECISION_SKIP",
    "ImportSummary",
    "SalvageImportError",
    "bib_ids_for_marc",
    "import_results",
    "sample_stratified",
]
