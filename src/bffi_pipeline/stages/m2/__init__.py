"""M2 — MARCXML → BIBFRAME RDF/XML stage.

Public surface is re-exported from :mod:`bffi_pipeline.stages.m2.runner`.
Private helpers (anything prefixed with ``_``) stay reachable via the
submodule path (``from bffi_pipeline.stages.m2.runner import _foo``).
"""

from bffi_pipeline.stages.m2.runner import (
    ConversionErrorRow,
    ConversionSummary,
    HelmetMapRow,
    run,
)

__all__ = [
    "ConversionErrorRow",
    "ConversionSummary",
    "HelmetMapRow",
    "run",
]
