"""Cataloguer review pipeline — M9 picker stage of the review bundle.

The M9 picker writes ``runs/<uuid>/picker-decisions.jsonl`` per
run (via :mod:`bffi_pipeline.stages.m9.picker_audit`) — this
handler consumes it as the candidate pool. Like judge, picker
bundles are per-run only; there's no corpus-wide aggregator.

Each picker audit row carries the full LLM call context (entity
literal + kind + originating work URI, the candidate list, the
``PickerDecision``, the final ``ReconciliationOutcome``). The
sampler stratifies on ``entity_kind x outcome_stage x
confidence_band`` so the cataloguer sees a balanced mix —
especially the low-confidence picks where a human eye matters
most.

KEEP rows (AGREE / PICK-CANDIDATE / NO-MATCH) become
:class:`~picker.PickerGoldCase` rows appended to
``gold/picker.jsonl``. NO-MATCH is a legitimate KEEP outcome —
it signals "the picker should have said uncertain but didn't,
or no KANTO entry exists for this entity" and produces a gold
row with ``expected_uri = null``.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

#: M9 already writes this per-run sidecar. The cataloguer-bundle stage
#: looks for it under ``<run_dir>/`` by name.
AUDIT_FILENAME: Final[str] = "picker-decisions.jsonl"

#: ``added_by`` value stamped on every KEEP row that lands in
#: ``gold/picker.jsonl``.
ADDED_BY_PREFIX: Final[str] = "cataloguer-m9"

#: Entity kinds the picker fires for. Mirrors
#: :data:`bffi_pipeline.stages.m9.schemas.AuthorityKind`.
ENTITY_KINDS: Final[tuple[str, ...]] = (
    "person",
    "corporate_body",
    "subject",
    "genre_form",
    "music_form",
    "fictional_character",
)

#: Decision values the HTML tool emits for picker rows. ``AGREE``
#: keeps the LLM's chosen_uri verbatim; ``PICK-CANDIDATE`` overrides
#: with a candidate the cataloguer picked from the list; ``NO-MATCH``
#: lands a row with ``expected_uri=null``; ``STILL-UNCERTAIN`` /
#: ``DISCARD`` / ``SKIP-FOR-NOW`` don't land.
DECISION_AGREE: Final[str] = "AGREE"
DECISION_PICK_CANDIDATE: Final[str] = "PICK-CANDIDATE"
DECISION_NO_MATCH: Final[str] = "NO-MATCH"
DECISION_UNCERTAIN: Final[str] = "STILL-UNCERTAIN"
DECISION_DISCARD: Final[str] = "DISCARD"
DECISION_SKIP: Final[str] = "SKIP-FOR-NOW"

_LANDING_DECISIONS: Final[frozenset[str]] = frozenset(
    {DECISION_AGREE, DECISION_PICK_CANDIDATE, DECISION_NO_MATCH}
)

_VALID_DECISIONS: Final[frozenset[str]] = frozenset(
    {
        DECISION_AGREE,
        DECISION_PICK_CANDIDATE,
        DECISION_NO_MATCH,
        DECISION_UNCERTAIN,
        DECISION_DISCARD,
        DECISION_SKIP,
    }
)

#: Confidence bands for the stratified sampler. Mirrors the judge
#: handler's bands so the two side-by-side tabs feel consistent.
_CONFIDENCE_BANDS: Final[tuple[tuple[str, float, float], ...]] = (
    ("low", 0.0, 0.6),
    ("mid", 0.6, 0.85),
    ("high", 0.85, 1.0001),
)

_PREVIEW_CAP: Final[int] = 5


class PickerImportError(ValueError):
    """Raised when a cataloguer picker result row fails validation.

    Names the offending row id + the failing field so the operator
    can ask the cataloguer to fix one specific row.
    """


@dataclass
class ImportSummary:
    """Operator-facing summary for a picker import."""

    rows_seen: int = 0
    landed: int = 0
    discarded: int = 0
    skipped: int = 0
    still_uncertain: int = 0
    gold_total_before: int = 0
    gold_total_after: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    by_decision: dict[str, int] = field(default_factory=dict)
    skipped_ids: list[str] = field(default_factory=list)
    flipped_ids: list[tuple[str, str, str | None]] = field(default_factory=list)
    no_match_ids: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            "picker-review-import summary",
            f"  rows in cataloguer file:        {self.rows_seen}",
            f"  Landed in gold/picker.jsonl:    {self.landed}",
            f"  DISCARD (dropped):              {self.discarded}",
            f"  SKIP-FOR-NOW (still pending):   {self.skipped}",
            f"  STILL-UNCERTAIN (no gold row):  {self.still_uncertain}",
        ]
        if self.by_kind:
            lines.append("  Landed by entity_kind:")
            for kind, n in sorted(self.by_kind.items()):
                lines.append(f"    {kind:35} {n}")
        if self.by_decision:
            lines.append("  Landed by cataloguer decision:")
            for dec, n in sorted(self.by_decision.items()):
                lines.append(f"    {dec:35} {n}")
        lines.append(f"  gold/picker.jsonl: {self.gold_total_before} → {self.gold_total_after}")
        if self.no_match_ids:
            lines.append(
                f"  NO-MATCH rows ({len(self.no_match_ids)}) — "
                "picker should have said uncertain, or no KANTO entry exists:"
            )
            preview = ", ".join(self.no_match_ids[:_PREVIEW_CAP])
            extra = len(self.no_match_ids) - _PREVIEW_CAP
            more = "" if extra <= 0 else f" (and {extra} more)"
            lines.append(f"    {preview}{more}")
        if self.skipped_ids:
            preview = ", ".join(self.skipped_ids[:_PREVIEW_CAP])
            extra = len(self.skipped_ids) - _PREVIEW_CAP
            more = "" if extra <= 0 else f" (and {extra} more)"
            lines.append(f"  SKIP-FOR-NOW ids: {preview}{more}")
        if self.flipped_ids:
            lines.append(f"  Cataloguer overrode the picker on {len(self.flipped_ids)} row(s):")
            for new_id, llm_uri, picked_uri in self.flipped_ids[:_PREVIEW_CAP]:
                lines.append(f"    {new_id}: llm={llm_uri} → cataloguer={picked_uri}")
            extra = len(self.flipped_ids) - _PREVIEW_CAP
            if extra > 0:
                lines.append(f"    (and {extra} more)")
        return "\n".join(lines)


# --- Sampler ------------------------------------------------------------


def _band_for(confidence: float | None) -> str:
    if confidence is None:
        return "low"
    for label, lo, hi in _CONFIDENCE_BANDS:
        if lo <= confidence < hi:
            return label
    return "mid"


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
    """Draw a stratified slice of the run's picker decisions.

    Strata are ``entity_kind x outcome_stage x confidence_band``.
    For each stratum, sample up to ``per_category`` rows seeded by
    ``seed`` (default: today's UTC date). Returns a per-stratum
    histogram of the sampled batch.

    Audit rows already carry full context (entity literal, kind,
    candidates, LLM decision, outcome) so the sampler just picks
    rows verbatim — no hydration step needed.
    """
    if seed is None:
        seed = datetime.now(UTC).date().isoformat()
    rng = random.Random(_seed_to_int(seed))

    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    with input_path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            row = json.loads(line)
            key = (
                row.get("entity_kind", "unknown"),
                row.get("outcome_stage", "unknown"),
                _band_for(row.get("llm_confidence")),
            )
            buckets.setdefault(key, []).append(row)

    histogram: dict[str, int] = {}
    picked: list[dict[str, Any]] = []
    for (kind, outcome, band), rows in sorted(buckets.items()):
        chosen = rng.sample(rows, min(per_category, len(rows)))
        histogram[f"{kind}/{outcome}/{band}"] = len(chosen)
        picked.extend(chosen)

    sequence = 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as out_fh:
        for row in picked:
            sequence += 1
            # Preserve every field the audit row carried so the HTML
            # surface has full context; just re-key the id placeholder
            # to the bundle's sequential sequence.
            candidate = dict(row)
            candidate["id"] = f"cg-pending-{sequence:04d}"
            out_fh.write(json.dumps(candidate, ensure_ascii=False))
            out_fh.write("\n")

    return histogram


def bib_ids_for_marc(candidates_jsonl: bytes) -> list[str]:
    """Return one bib id per row (the originating record) so the
    bundle attaches one MARC sidecar per pick. The bundle builder
    dedups across rows that share a bib id."""
    out: list[str] = []
    for raw in candidates_jsonl.decode("utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        row = json.loads(line)
        bib = row.get("originating_bib_id")
        if bib:
            out.append(bib)
    return out


# --- Importer -----------------------------------------------------------


def _validate_keep_row(row: dict[str, Any]) -> None:
    row_id = row.get("id", "<no-id>")
    decision = row.get("decision")
    entity_kind = row.get("entity_kind")
    if entity_kind not in ENTITY_KINDS:
        raise PickerImportError(
            f"Row {row_id!r}: entity_kind must be one of {ENTITY_KINDS}, got {entity_kind!r}."
        )
    if not row.get("entity_label"):
        raise PickerImportError(f"Row {row_id!r}: entity_label is required.")
    expected_uri = row.get("expected_uri")
    if decision == DECISION_NO_MATCH:
        if expected_uri is not None:
            raise PickerImportError(
                f"Row {row_id!r}: NO-MATCH must have expected_uri=null, got {expected_uri!r}."
            )
    else:
        if not isinstance(expected_uri, str) or not expected_uri:
            raise PickerImportError(
                f"Row {row_id!r}: AGREE / PICK-CANDIDATE must carry a non-empty "
                f"expected_uri, got {expected_uri!r}."
            )
        if decision == DECISION_PICK_CANDIDATE:
            # The picked URI must be one of the candidates the LLM
            # saw — otherwise the cataloguer typed something free-form
            # and the gold row's `candidates_seen` won't include it.
            candidate_uris = {c.get("uri") for c in row.get("candidates", [])}
            if expected_uri not in candidate_uris:
                raise PickerImportError(
                    f"Row {row_id!r}: PICK-CANDIDATE expected_uri={expected_uri!r} "
                    "not in the candidate list. Cataloguer should have used "
                    "NO-MATCH if the right authority wasn't among the candidates."
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
            if not isinstance(row_id, str) or not row_id.startswith("gp-"):
                continue
            try:
                seq = int(row_id.removeprefix("gp-"))
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
        "entity_kind": row["entity_kind"],
        "entity_label": row["entity_label"],
        "expected_uri": row.get("expected_uri"),
        "originating_bib_id": row.get("originating_bib_id"),
        "originating_work_uri": row.get("originating_work_uri"),
        "predicate_uri": row.get("predicate_uri"),
        "holdout": bool(row.get("holdout", False)),
        "added": row.get("added") or today,
        "added_by": added_by,
        "notes": row.get("notes") or "",
        "candidates_seen": row.get("candidates", []),
    }


def import_results(*, input_path: Path, output_path: Path) -> ImportSummary:
    """Import a cataloguer's exported picker results.jsonl.

    Two-pass atomic: all KEEP rows (AGREE / PICK-CANDIDATE /
    NO-MATCH) are validated before any are appended. DISCARD /
    SKIP-FOR-NOW / STILL-UNCERTAIN don't land but are counted in
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
            raise PickerImportError(
                f"Row {row_id!r}: decision must be one of {sorted(_VALID_DECISIONS)}, "
                f"got {decision!r}."
            )
        if decision in _LANDING_DECISIONS:
            _validate_keep_row(row)
            landing_rows.append(row)
        elif decision == DECISION_DISCARD:
            summary.discarded += 1
        elif decision == DECISION_UNCERTAIN:
            summary.still_uncertain += 1
        else:  # DECISION_SKIP
            summary.skipped += 1
            summary.skipped_ids.append(str(row.get("id", "<no-id>")))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as fh:
        for row in landing_rows:
            new_id = f"gp-{next_seq:04d}"
            next_seq += 1
            gold_row = _result_row_to_gold_row(row, new_id=new_id)
            fh.write(json.dumps(gold_row, ensure_ascii=False))
            fh.write("\n")
            summary.landed += 1
            summary.by_kind[gold_row["entity_kind"]] = (
                summary.by_kind.get(gold_row["entity_kind"], 0) + 1
            )
            dec = row["decision"]
            summary.by_decision[dec] = summary.by_decision.get(dec, 0) + 1
            llm_uri = row.get("llm_chosen_uri")
            expected = gold_row.get("expected_uri")
            if dec == DECISION_NO_MATCH:
                summary.no_match_ids.append(new_id)
            if (
                dec in (DECISION_PICK_CANDIDATE, DECISION_NO_MATCH)
                and llm_uri is not None
                and llm_uri != expected
            ):
                summary.flipped_ids.append((new_id, llm_uri, expected))

    summary.gold_total_after = summary.gold_total_before + summary.landed
    return summary


__all__ = [
    "ADDED_BY_PREFIX",
    "AUDIT_FILENAME",
    "DECISION_AGREE",
    "DECISION_DISCARD",
    "DECISION_NO_MATCH",
    "DECISION_PICK_CANDIDATE",
    "DECISION_SKIP",
    "DECISION_UNCERTAIN",
    "ENTITY_KINDS",
    "ImportSummary",
    "PickerImportError",
    "bib_ids_for_marc",
    "import_results",
    "sample_stratified",
]
