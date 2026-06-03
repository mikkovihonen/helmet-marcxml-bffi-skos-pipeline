"""M3 contrib-cascade audit log.

Per-run sidecar that records every cascade fire as a candidate-shaped
JSONL row, so downstream tooling (``review-bundle-build --from-run
<uuid>``, the ``cataloguer-bundle`` pipeline stage) doesn't have to
re-run the LLM cascade against the BIBFRAME files to recover the
review queue.

Each row mirrors :class:`bffi_pipeline.eval.grow_contrib.ContribCandidate`
so the existing bundle-build machinery can consume it without
translation. The cataloguer reviews ``runs/<uuid>/contrib-candidates.jsonl``
the same way they'd review ``gold/grow-candidates-contrib.jsonl`` —
same rows, different scope (a single run's M3 cascade vs. the corpus-
wide mine).

The log is truncated at M3 stage start (:func:`reset_audit_log`) and
appended to per-fire (:func:`append_audit_rows`). Re-running M3 with
``--force-stages m3`` produces a fresh audit; partial runs leave a
partial log, which is what we want — the bundle reflects exactly what
was processed.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

#: Filename M3 writes the audit log under in the run directory. Sits
#: next to ``contrib-variants.jsonl`` (the F2 transliteration sidecar)
#: so the cataloguer-bundle stage can locate it by convention.
AUDIT_FILENAME: Final[str] = "contrib-candidates.jsonl"

#: Value stamped on every audit row's ``added_by`` field. Lets the
#: cataloguer + the import path tell run-mined candidates apart from
#: corpus-mined ones (``grow-gold-contrib``) and from bootstrap rows.
ADDED_BY: Final[str] = "m3-cascade"

#: Auto-suggested category labels. Mirror the values used by
#: ``grow_contrib`` so a per-run audit row and a corpus-mined
#: candidate row carry the same labels when their cascade shapes
#: match. Cataloguer overrides on merge — these are hints.
CATEGORY_PURE_NEW_AGENT: Final[str] = "pure-new-agent"
CATEGORY_TRANSLITERATION: Final[str] = "transliteration"
CATEGORY_ROLE_CLASSIFICATION: Final[str] = "role-classification"
CATEGORY_AMBIGUOUS: Final[str] = "ambiguous-multi-shape"


def suggest_category(contributions: list[dict[str, Any]]) -> str:
    """Heuristic category-suggestion for the cataloguer's review.

    Looks at the LLM-decision shape:

    - All entries have ``transliteration_of`` → ``"transliteration"``.
    - All entries have ``relator_code`` and no transliteration → if a
      single agent, suggest ``"role-classification"`` (the LLM is
      mostly being asked "what's the role here?"); otherwise
      ``"pure-new-agent"`` (multi-agent new extractions).
    - Mixed (some variants + some new) → ``"ambiguous-multi-shape"``.

    The cataloguer overrides on merge — the bootstrap categories
    (``pure-new-agent`` / ``role-classification`` / ``within-record-typo``
    / ``cyrillic-latin-transliteration``) are more nuanced than these
    auto-suggestions can capture without context.
    """
    has_translit = any(c.get("transliteration_of") for c in contributions)
    has_new = any(c.get("relator_code") and not c.get("transliteration_of") for c in contributions)
    if has_translit and not has_new:
        return CATEGORY_TRANSLITERATION
    if has_new and not has_translit:
        if len(contributions) == 1:
            return CATEGORY_ROLE_CLASSIFICATION
        return CATEGORY_PURE_NEW_AGENT
    return CATEGORY_AMBIGUOUS


def audit_log_path(run_dir: Path) -> Path:
    """Convention for where M3 writes the cascade audit log."""
    return run_dir / AUDIT_FILENAME


def flatten_decision_contributions(decision: object) -> list[dict[str, Any]]:
    """Flatten a :class:`ContribExtractDecision` into the JSON shape
    the audit row + the candidate row both carry.

    Preserves ``transliteration_of`` and ``role_text`` when set;
    keeps ``relator_code`` verbatim (the cataloguer may want to see
    hallucinated codes too — surfacing them is the point of the
    review). Used by both the M3 stage's per-fire audit logger and
    the corpus-mining
    :func:`bffi_pipeline.eval.grow_contrib.generate_candidates`
    path, so audit rows and corpus candidates have byte-identical
    shape when their decisions match.
    """
    out: list[dict[str, Any]] = []
    for cand in decision.contributions:  # type: ignore[attr-defined]
        row: dict[str, Any] = {"name": cand.name}
        if cand.relator_code is not None:
            row["relator_code"] = cand.relator_code
        if cand.transliteration_of is not None:
            row["transliteration_of"] = cand.transliteration_of
        if cand.role_text is not None:
            row["role_text"] = cand.role_text
        out.append(row)
    return out


def reset_audit_log(path: Path) -> None:
    """Truncate the audit log (or create the empty file).

    Called at M3 stage start so re-runs produce a coherent log
    instead of appending duplicates. The parent directory is created
    if needed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def append_audit_rows(
    path: Path,
    *,
    helmet_bib_id: str,
    c_subfield: str,
    existing_agents: Iterable[str],
    contributions: list[dict[str, Any]],
    category: str,
    now: datetime | None = None,
) -> None:
    """Append one audit row for a single cascade fire.

    ``contributions`` is the cascade's flattened output (each item has
    ``name`` plus optional ``relator_code`` / ``transliteration_of`` /
    ``role_text``) — the same shape :func:`grow_contrib._decision_to_contributions`
    produces. ``category`` is the auto-suggested label the cataloguer
    can override on merge.

    The row id is a per-position placeholder (``cg-pending-<seq>``)
    derived from the current line count of the log. Writes are
    serial-within-a-process and M3 runs single-threaded per stage, so
    seq monotonically grows; the cataloguer never sees these ids as
    durable identifiers, the import side mints fresh ``cg-NNNN``
    values on KEEP.
    """
    seq = _next_seq(path)
    row: dict[str, Any] = {
        "id": f"cg-pending-{seq:04d}",
        "category": category,
        "helmet_bib_id": helmet_bib_id,
        "c_subfield": c_subfield,
        "existing_agents": list(existing_agents),
        "expected_contributions": contributions,
        "holdout": False,
        "added": (now or datetime.now(UTC)).date().isoformat(),
        "added_by": ADDED_BY,
        "notes": "",
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False))
        fh.write("\n")


def _next_seq(path: Path) -> int:
    """Count of non-blank lines already in ``path``, +1.

    The audit log is appended-to per cascade fire; the next row gets
    ``count + 1`` so its placeholder id reflects insertion order.
    Re-runs that truncate via :func:`reset_audit_log` reset to 1.
    """
    if not path.exists():
        return 1
    n = 0
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            if raw.strip():
                n += 1
    return n + 1


__all__ = [
    "ADDED_BY",
    "AUDIT_FILENAME",
    "CATEGORY_AMBIGUOUS",
    "CATEGORY_PURE_NEW_AGENT",
    "CATEGORY_ROLE_CLASSIFICATION",
    "CATEGORY_TRANSLITERATION",
    "append_audit_rows",
    "audit_log_path",
    "flatten_decision_contributions",
    "reset_audit_log",
    "suggest_category",
]
