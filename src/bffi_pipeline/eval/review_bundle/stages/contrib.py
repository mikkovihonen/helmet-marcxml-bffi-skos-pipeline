"""Cataloguer review pipeline — contrib stage of the review bundle.

This is the contrib :data:`~bffi_pipeline.eval.review_bundle.stages.STAGE_REGISTRY`
entry: the per-stage logic for sampling candidates from
``gold/grow-candidates-contrib.jsonl`` and importing the cataloguer's
KEEP decisions back into ``gold/contrib.jsonl``. The bundle wrapper
(:mod:`bffi_pipeline.eval.review_bundle.bundle`) attaches the per-row
MARCXML sidecar files and writes the zip the cataloguer opens in
``gold/cataloguer-review.html``.

Provides:

- :func:`sample_stratified` — draws a stratified-by-category slice of
  the full candidate pool. The result lands in the bundle as
  ``contrib/candidates.jsonl``.
- :func:`import_results` — consumes the cataloguer's exported results
  (the ``contrib/results.jsonl`` inside their results zip). KEEP rows
  are validated + minted with sequential ``cg-NNNN`` IDs + appended to
  ``gold/contrib.jsonl`` in the canonical bootstrap schema. DISCARD
  rows are dropped silently. SKIP-FOR-NOW rows print a warning so the
  operator knows the cataloguer isn't done.

The cataloguer's exported rows carry the original candidate fields plus
the cataloguer's decision fields (``decision``, ``category``,
``holdout``, ``vetted_contributions``, ``notes``, ``reviewed_by``,
``reviewed_at``); :func:`import_results` strips the cataloguer-only
fields and maps ``vetted_contributions`` → ``expected_contributions``
so the appended rows match ``gold/contrib.jsonl``'s existing 4
bootstrap rows byte-for-byte in shape.

P-39's M9 reconciliation work is gated on ``gold/contrib.jsonl``
reaching ≥ 30 cataloguer-vetted rows. This module is the engineering
support for the cataloguer-side bottleneck.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from bffi_pipeline.contrib_extract_llm import VALID_RELATOR_CODES
from bffi_pipeline.eval.grow_contrib import ContribCandidate as ContribCandidate  # noqa: PLC0414

#: Regex matching the language-separator forms that suggest a single
#: ``name`` field actually carries multiple authors. Matches at word
#: boundaries to avoid false positives like "Andersson" containing
#: "and". Covers Finnish ("ja"), Swedish ("och"), German ("und"),
#: French ("et"), English ("and"), the ampersand, and trailing
#: " et al" / ".. et al" variants.
_COMPOSITE_NAME_RX: Final[re.Pattern[str]] = re.compile(
    r"\s+(?:ja|och|und|et|and|&)\s+|\.{1,2}\s*et\s+al\.?|\s+et\s+al\.?",
    re.IGNORECASE,
)

#: Default path the operator's stratified-sample output lands at. Lives
#: under ``scratchpad/`` (gitignored) because each batch is per-day
#: ephemera the operator emails to the cataloguer, not a checked-in
#: artefact. Use ``--output-path`` to override.
DEFAULT_BATCH_OUTPUT_DIR: Final[Path] = Path(__file__).resolve().parents[5] / "scratchpad"

#: Canonical category labels the cataloguer can pick from. Mirrors
#: ``gold/contrib.jsonl``'s 4 bootstrap rows; expanded only when a
#: cataloguer proposes a new label in ``notes`` and the operator
#: graduates it into the canonical set.
CANONICAL_CATEGORIES: Final[frozenset[str]] = frozenset(
    {
        "pure-new-agent",
        "role-classification",
        "within-record-typo",
        "cyrillic-latin-transliteration",
    }
)

#: Decision values the HTML tool emits. ``SKIP-FOR-NOW`` is for cases
#: the cataloguer wants to come back to later; the import skips them
#: without flagging an error (the candidate stays in
#: ``grow-candidates-contrib.jsonl`` for a future batch).
DECISION_KEEP: Final[str] = "KEEP"
DECISION_DISCARD: Final[str] = "DISCARD"
DECISION_SKIP: Final[str] = "SKIP-FOR-NOW"
_VALID_DECISIONS: Final[frozenset[str]] = frozenset(
    {DECISION_KEEP, DECISION_DISCARD, DECISION_SKIP}
)

#: Cap on the number of SKIP-FOR-NOW ids the summary previews inline.
#: Beyond this the summary prints "(and N more)" so a giant skip list
#: doesn't dominate the operator's terminal.
_SKIPPED_ID_PREVIEW: Final[int] = 5


class GoldReviewImportError(ValueError):
    """Raised when a cataloguer result row fails schema validation.

    The error message names the offending row by ``id`` + the failing
    field so the operator can ask the cataloguer to fix one specific
    row rather than re-doing the whole batch.
    """


@dataclass
class ImportSummary:
    """Operator-facing summary printed by the CLI after import.

    Tracks both the cataloguer's decision distribution and the
    pipeline-side outcome (rows appended, new total, gap-to-gate
    against P-39's 30-row Phase A gate).
    """

    rows_seen: int = 0
    kept: int = 0
    discarded: int = 0
    skipped: int = 0
    appended_to_gold: int = 0
    gold_total_before: int = 0
    gold_total_after: int = 0
    p39_gate_threshold: int = 30
    by_category: dict[str, int] = field(default_factory=dict)
    skipped_ids: list[str] = field(default_factory=list)
    #: KEEP rows that landed in gold/contrib.jsonl with a name field
    #: containing a recognised multi-author separator. The HTML tool
    #: offers a split-button before the cataloguer hits KEEP; this
    #: list is the safety net for rows where the cataloguer chose to
    #: keep the composite intentionally (or missed the warning). The
    #: operator surfaces this for follow-up; the import doesn't block.
    composite_name_kept: list[tuple[str, str]] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            "gold-review-import summary",
            f"  rows in cataloguer file:        {self.rows_seen}",
            f"  KEEP (appended to gold):        {self.kept}",
            f"  DISCARD (dropped):              {self.discarded}",
            f"  SKIP-FOR-NOW (still pending):   {self.skipped}",
        ]
        if self.by_category:
            lines.append("  KEEP by category:")
            for cat, n in sorted(self.by_category.items()):
                lines.append(f"    {cat:35} {n}")
        lines.append(f"  gold/contrib.jsonl: {self.gold_total_before} → {self.gold_total_after}")
        gap = max(0, self.p39_gate_threshold - self.gold_total_after)
        if gap > 0:
            lines.append(f"  P-39 gate: still {gap} row(s) short of {self.p39_gate_threshold}.")
        else:
            lines.append(
                f"  P-39 gate cleared: {self.gold_total_after} ≥ {self.p39_gate_threshold}."
            )
        if self.skipped_ids:
            preview = ", ".join(self.skipped_ids[:_SKIPPED_ID_PREVIEW])
            extra = len(self.skipped_ids) - _SKIPPED_ID_PREVIEW
            more = "" if extra <= 0 else f" (and {extra} more)"
            lines.append(f"  SKIP-FOR-NOW ids: {preview}{more}")
        if self.composite_name_kept:
            lines.append(
                f"  Composite names retained on {len(self.composite_name_kept)} row(s) — "
                "consider asking the cataloguer to split:"
            )
            for row_id, name in self.composite_name_kept[:_SKIPPED_ID_PREVIEW]:
                lines.append(f"    {row_id}: {name!r}")
            extra = len(self.composite_name_kept) - _SKIPPED_ID_PREVIEW
            if extra > 0:
                lines.append(f"    (and {extra} more)")
        return "\n".join(lines)


# --- Stratified sampling --------------------------------------------------


def _seed_to_int(seed: str) -> int:
    """Hash a string seed to a 64-bit int. Deterministic across
    Python versions (uses SHA-256 of the UTF-8 bytes) so the same
    seed string always produces the same sample on any machine —
    `hash()` is salted per-process and not safe here."""
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def _read_candidates(path: Path) -> list[dict[str, Any]]:
    """Load a candidate JSONL into a list of dicts. Skips blank lines."""
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def bib_ids_for_marc(candidates_jsonl: bytes) -> list[str]:
    """Return the ``helmet_bib_id`` of every row in the candidate JSONL.

    Takes raw bytes so the bundle builder can pass its in-memory
    buffer (no temp file round-trip). Order matches the file;
    duplicates (a bib appearing in multiple candidate rows) are
    preserved — the bundle builder dedups before reading from disk.
    """
    out: list[str] = []
    for raw in candidates_jsonl.decode("utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        out.append(json.loads(line)["helmet_bib_id"])
    return out


def sample_stratified(
    *,
    input_path: Path,
    output_path: Path,
    per_category: int = 25,
    seed: str | None = None,
) -> dict[str, int]:
    """Draw a stratified-by-``category`` slice of ``input_path``.

    Picks ``per_category`` rows from each distinct ``category`` value
    in the candidate pool, seeded by ``seed`` (default: today's date
    as a string, so two operators on the same day generate the same
    batch). Writes the sampled rows to ``output_path`` as JSONL.

    Returns a per-category histogram of the sampled batch.

    Categories with fewer than ``per_category`` candidates contribute
    all their rows (the function doesn't over-sample). The result is
    therefore not always exactly ``4 * per_category`` total.
    """
    rows = _read_candidates(input_path)
    if seed is None:
        seed = datetime.now(UTC).date().isoformat()
    rng = random.Random(_seed_to_int(seed))

    by_category: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_category.setdefault(row.get("category") or "uncategorised", []).append(row)

    picked: list[dict[str, Any]] = []
    histogram: dict[str, int] = {}
    for category in sorted(by_category):
        pool = by_category[category]
        # ``per_category <= 0`` means "no cap" — see picker.py for the
        # full-audit motivation.
        if per_category <= 0:
            sampled = pool
        else:
            sample_size = min(per_category, len(pool))
            sampled = rng.sample(pool, sample_size)
        picked.extend(sampled)
        histogram[category] = len(sampled)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        for row in picked:
            fh.write(json.dumps(row, ensure_ascii=False))
            fh.write("\n")

    return histogram


# --- Result-row validation ------------------------------------------------


def _require_field(row: dict[str, Any], field_name: str, row_id: str) -> Any:
    """Pull ``field_name`` from ``row`` or raise a typed error naming
    both the row id and the missing field."""
    value = row.get(field_name)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise GoldReviewImportError(f"Row {row_id!r} is missing required field {field_name!r}.")
    return value


def _validate_keep_row(row: dict[str, Any]) -> None:
    """Validate a KEEP result row against the gold/contrib.jsonl
    contract. Raises :class:`GoldReviewImportError` with a clear
    message naming the offending row + field. Called for every
    KEEP-decision row before it lands in ``gold/contrib.jsonl``.
    """
    row_id = str(row.get("id", "<no-id>"))

    _require_field(row, "helmet_bib_id", row_id)
    _require_field(row, "c_subfield", row_id)
    _require_field(row, "reviewed_by", row_id)
    _require_field(row, "reviewed_at", row_id)

    category = _require_field(row, "category", row_id)
    if category not in CANONICAL_CATEGORIES:
        raise GoldReviewImportError(
            f"Row {row_id!r} has unknown category {category!r}. "
            f"Expected one of: {sorted(CANONICAL_CATEGORIES)}."
        )

    holdout = row.get("holdout", False)
    if not isinstance(holdout, bool):
        raise GoldReviewImportError(
            f"Row {row_id!r}: holdout must be true or false, got {holdout!r}."
        )

    contribs = row.get("vetted_contributions")
    if not isinstance(contribs, list) or not contribs:
        raise GoldReviewImportError(
            f"Row {row_id!r}: vetted_contributions must be a non-empty list."
        )
    for i, c in enumerate(contribs):
        if not isinstance(c, dict):
            raise GoldReviewImportError(
                f"Row {row_id!r}: vetted_contributions[{i}] is not an object."
            )
        if not c.get("name") or not isinstance(c["name"], str):
            raise GoldReviewImportError(f"Row {row_id!r}: vetted_contributions[{i}] missing name.")
        relator = c.get("relator_code")
        translit = c.get("transliteration_of")
        if relator is None and translit is None:
            raise GoldReviewImportError(
                f"Row {row_id!r}: vetted_contributions[{i}] has neither "
                "relator_code nor transliteration_of."
            )
        # Allow pipe-separated alternative codes (e.g. "aft|aui|wpr|ctb")
        # following the bootstrap precedent on ``cg-0002``.
        if relator is not None:
            for code in str(relator).split("|"):
                if code and code not in VALID_RELATOR_CODES:
                    raise GoldReviewImportError(
                        f"Row {row_id!r}: vetted_contributions[{i}] has unknown "
                        f"relator_code {code!r}. Expected one of: "
                        f"{sorted(VALID_RELATOR_CODES)}."
                    )


# --- Gold-file append -----------------------------------------------------


def _next_gold_id(gold_path: Path) -> int:
    """Scan ``gold_path`` for the highest ``cg-NNNN`` id and return
    the next sequence number. Returns 1 when the file is missing or
    empty."""
    if not gold_path.is_file():
        return 1
    max_seq = 0
    with gold_path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            row = json.loads(line)
            row_id = str(row.get("id", ""))
            if row_id.startswith("cg-"):
                tail = row_id[3:]
                if tail.isdigit():
                    max_seq = max(max_seq, int(tail))
    return max_seq + 1


def _result_row_to_gold_row(
    row: dict[str, Any],
    *,
    new_id: str,
) -> dict[str, Any]:
    """Map a cataloguer result row → a ``gold/contrib.jsonl`` row.

    Drops the cataloguer-decision fields (``decision``, ``reviewed_by``,
    ``reviewed_at``, ``vetted_contributions``) and renames the agent
    list back to ``expected_contributions`` so the appended rows
    match the bootstrap schema byte-for-byte. Preserves
    ``helmet_bib_id``, ``c_subfield``, ``existing_agents``,
    ``category``, ``holdout``, ``notes``; mints fresh ``id``,
    ``added``, ``added_by``.
    """
    today = datetime.now(UTC).date().isoformat()
    reviewer = str(row.get("reviewed_by", "cataloguer")).strip() or "cataloguer"
    # Cataloguer-only fields stripped; agent list renamed.
    return {
        "id": new_id,
        "category": row["category"],
        "helmet_bib_id": row["helmet_bib_id"],
        "c_subfield": row["c_subfield"],
        "existing_agents": list(row.get("existing_agents", [])),
        "expected_contributions": list(row["vetted_contributions"]),
        "holdout": bool(row.get("holdout", False)),
        "added": today,
        "added_by": f"cataloguer:{reviewer}",
        "notes": str(row.get("notes", "")),
    }


def _count_gold_rows(gold_path: Path) -> int:
    """Count lines in ``gold_path`` (with a 0 short-circuit for
    missing files)."""
    if not gold_path.is_file():
        return 0
    return sum(1 for line in gold_path.read_text(encoding="utf-8").splitlines() if line.strip())


def import_results(
    *,
    input_path: Path,
    output_path: Path,
    p39_gate_threshold: int = 30,
) -> ImportSummary:
    """Import a cataloguer's results JSONL into ``gold/contrib.jsonl``.

    Walks ``input_path`` row-by-row:

    - ``decision == "KEEP"`` → validate, mint ``cg-NNNN`` id, append
      to ``output_path`` in the bootstrap schema.
    - ``decision == "DISCARD"`` → drop silently.
    - ``decision == "SKIP-FOR-NOW"`` → record the id in the summary
      so the operator knows the cataloguer isn't done with them yet.

    Validation raises :class:`GoldReviewImportError` with a row-id +
    field reference; the import does **not** silently drop bad rows.
    The caller decides whether to fix the cataloguer's file and
    re-run.
    """
    rows = _read_candidates(input_path)
    summary = ImportSummary(
        rows_seen=len(rows),
        gold_total_before=_count_gold_rows(output_path),
        p39_gate_threshold=p39_gate_threshold,
    )
    next_seq = _next_gold_id(output_path)

    # Two-pass: validate every KEEP row first, then append. This way a
    # single bad row doesn't leave gold/contrib.jsonl in a half-written
    # state — either all KEEP rows land or none do.
    keep_rows: list[dict[str, Any]] = []
    for row in rows:
        decision = row.get("decision")
        if decision not in _VALID_DECISIONS:
            row_id = row.get("id", "<no-id>")
            raise GoldReviewImportError(
                f"Row {row_id!r}: decision must be one of {sorted(_VALID_DECISIONS)}, "
                f"got {decision!r}."
            )
        if decision == DECISION_KEEP:
            _validate_keep_row(row)
            keep_rows.append(row)
        elif decision == DECISION_DISCARD:
            summary.discarded += 1
        else:  # SKIP-FOR-NOW
            summary.skipped += 1
            summary.skipped_ids.append(str(row.get("id", "<no-id>")))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as fh:
        for row in keep_rows:
            new_id = f"cg-{next_seq:04d}"
            next_seq += 1
            gold_row = _result_row_to_gold_row(row, new_id=new_id)
            fh.write(json.dumps(gold_row, ensure_ascii=False))
            fh.write("\n")
            summary.kept += 1
            summary.appended_to_gold += 1
            cat = gold_row["category"]
            summary.by_category[cat] = summary.by_category.get(cat, 0) + 1
            for contrib in gold_row.get("expected_contributions", []):
                name = contrib.get("name", "")
                if _COMPOSITE_NAME_RX.search(name):
                    summary.composite_name_kept.append((new_id, name))

    summary.gold_total_after = summary.gold_total_before + summary.appended_to_gold
    return summary


# --- Re-exports (kept for symmetry with grow_contrib.py) ------------------


__all__ = [
    "CANONICAL_CATEGORIES",
    "DECISION_DISCARD",
    "DECISION_KEEP",
    "DECISION_SKIP",
    "DEFAULT_BATCH_OUTPUT_DIR",
    "ContribCandidate",
    "GoldReviewImportError",
    "ImportSummary",
    "import_results",
    "sample_stratified",
]
