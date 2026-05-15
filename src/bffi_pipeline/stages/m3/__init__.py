"""M3 — BIBFRAME RDF/XML → BFFI Turtle stage.

Public surface is re-exported from :mod:`bffi_pipeline.stages.m3.runner`.
Private helpers (anything prefixed with ``_``) stay reachable via the
submodule path (``from bffi_pipeline.stages.m3.runner import _foo``).
"""

from bffi_pipeline.stages.m3.runner import (
    BFFI_CORPUS_FILENAME,
    BffiSummary,
    ValidationRow,
    construct_bffi,
    post_process,
    run,
)

__all__ = [
    "BFFI_CORPUS_FILENAME",
    "BffiSummary",
    "ValidationRow",
    "construct_bffi",
    "post_process",
    "run",
]
