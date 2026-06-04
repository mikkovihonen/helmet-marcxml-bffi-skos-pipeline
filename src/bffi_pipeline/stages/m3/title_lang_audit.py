"""M3 title-language cascade audit log.

Per-run sidecar that records every LLM title-language fire as a
candidate-shaped JSONL row. The cataloguer-bundle stage consumes
the file when present so a review tab can show the LLM's
per-segment language assignments and let the cataloguer correct
them.

Mirrors :mod:`bffi_pipeline.stages.m3.contrib_audit` in shape so
the cataloguer-bundle dispatcher's auto-discovery loop handles
title-lang like contrib / judge / picker / salvage.

The LLM fires *only* when Lingua collapses (every segment maps to
the same language despite the cataloguer declaring multiple) —
typically Latin-script parallel titles where statistical language
detection can't disambiguate. Each audit row carries the title
text, the cataloguer's declared candidate set, the LLM's
per-segment assignment, and the rationale.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

#: Filename M3 writes the title-lang audit log under in the run dir.
AUDIT_FILENAME: Final[str] = "title-lang-candidates.jsonl"

#: ``added_by`` stamp on every audit row.
ADDED_BY: Final[str] = "m3-title-lang"


@dataclass(frozen=True)
class TitleLangCallTelemetry:
    """Side-channel telemetry the detector exposes after each
    ``.detect()`` call, so the audit writer in ``_retag_pref_labels``
    can stamp the row with the LLM's per-segment decision +
    rationale without breaking ``tag_title``'s existing return
    shape (which is ``list[TaggedSegment]``).
    """

    title: str
    candidates: tuple[str, ...]
    segments: tuple[tuple[str, str | None], ...]  # (text, lang) pairs
    rationale: str


def audit_log_path(run_dir: Path) -> Path:
    """Convention for where M3 writes the title-lang audit log."""
    return run_dir / AUDIT_FILENAME


def reset_audit_log(path: Path) -> None:
    """Truncate the audit log (or create the empty file).

    Called at M3 stage start when the title-lang LLM detector is
    active so re-runs produce a coherent log instead of appending
    duplicates. Parent dir is created if needed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def append_audit_row(
    path: Path,
    *,
    helmet_bib_id: str | None,
    work_uri: str | None,
    telemetry: TitleLangCallTelemetry,
    now: datetime | None = None,
) -> None:
    """Append one audit row for a single title-lang LLM fire.

    ``helmet_bib_id`` may be ``None`` when the bib lookup fails
    (rare — M2 should have populated identifiers); the row still
    lands with ``null`` and the HTML viewer renders a "no MARC
    available" stub for that side.
    """
    seq = _next_seq(path)
    row: dict[str, Any] = {
        "id": f"cg-pending-{seq:04d}",
        "helmet_bib_id": helmet_bib_id,
        "work_uri": work_uri,
        "title": telemetry.title,
        "candidates": list(telemetry.candidates),
        "llm_segments": [{"text": t, "lang": lang} for (t, lang) in telemetry.segments],
        "rationale": telemetry.rationale,
        "added": (now or datetime.now(UTC)).date().isoformat(),
        "added_by": ADDED_BY,
        "notes": "",
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False))
        fh.write("\n")


def telemetry_from_decision(
    *,
    title: str,
    candidates: Iterable[str],
    segments: Iterable[tuple[str, str | None]],
    rationale: str,
) -> TitleLangCallTelemetry:
    """Construct a :class:`TitleLangCallTelemetry` from a fresh
    ``TitleLangDecision`` + the prompt inputs.

    The detector's ``.detect()`` returns the decision but loses the
    inputs by the time control returns to the caller — this helper
    re-attaches them on the side channel.
    """
    return TitleLangCallTelemetry(
        title=title,
        candidates=tuple(sorted(candidates)),
        segments=tuple(segments),
        rationale=rationale,
    )


def _next_seq(path: Path) -> int:
    """Count of non-blank lines already in ``path``, +1."""
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
    "TitleLangCallTelemetry",
    "append_audit_row",
    "audit_log_path",
    "reset_audit_log",
    "telemetry_from_decision",
]
