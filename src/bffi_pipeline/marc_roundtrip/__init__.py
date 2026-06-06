"""MARC round-trip reconstruction.

Reconstructs a MARCXML record from the BFFI graph (per-Manifestation) so
cataloguers can see what the migration to BFFI preserves vs loses.
Pairs with :mod:`bffi_pipeline.marc_roundtrip.diff` (field-by-field
comparator) and the M10 round-trip stage that runs both per record into
``runs/<uuid>/marc-roundtrip/``.

Not a production export path — purely a cataloguer-review surface.
"""

from __future__ import annotations

from bffi_pipeline.marc_roundtrip.converter import (
    MARC_NAMESPACE,
    ReconstructedRecord,
    reconstruct_marc,
    serialize_marc,
)
from bffi_pipeline.marc_roundtrip.diff import RecordDiff, diff_records
from bffi_pipeline.marc_roundtrip.runner import RoundtripSummary, run

__all__ = [
    "MARC_NAMESPACE",
    "ReconstructedRecord",
    "RecordDiff",
    "RoundtripSummary",
    "diff_records",
    "reconstruct_marc",
    "run",
    "serialize_marc",
]
