"""M8 ``helmet-map.jsonl`` loader — raw Work URI → HelmetMapEntry.

M2 emits the helmet-map.jsonl as a side effect of each MARCXML →
BIBFRAME conversion. M8 reads it to seed
``bffi:descriptionCreationDate`` on each canonical Work via the
``earliest_converted_at`` selector.

P-38 Phase D: extracted from m8/runner.py. No logic change.
"""

from __future__ import annotations

import json
from pathlib import Path

from bffi_pipeline.stages.m8.schemas import HelmetMapEntry


def _load_helmet_map(path: Path) -> dict[str, HelmetMapEntry]:
    """Return ``raw_work_uri → HelmetMapEntry``."""
    if not path.is_file():
        return {}
    out: dict[str, HelmetMapEntry] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        out[row["raw_work_uri"]] = HelmetMapEntry(
            raw_work_uri=row["raw_work_uri"],
            helmet_bib_id=row["helmet_bib_id"],
            converted_at=row["converted_at"],
        )
    return out
