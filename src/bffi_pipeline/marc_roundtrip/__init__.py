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

__all__ = [
    "MARC_NAMESPACE",
    "ReconstructedRecord",
    "reconstruct_marc",
    "serialize_marc",
]
