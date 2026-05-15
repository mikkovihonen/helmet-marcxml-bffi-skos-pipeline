"""Diagnostic / reporting tooling for the bffi pipeline.

These modules are operator-only tools that emit reports against
pipeline artefacts; they're not on ``CANONICAL_STAGES`` and aren't
invoked by the per-pipeline-run chain. Currently houses
:mod:`bffi_pipeline.diagnostics.blocking_stats` (the M4 ``workkey-stats``
command's backing logic). P-38 Phase C-3 will move the
ysa-disambiguation-report module here as well.
"""

from bffi_pipeline.diagnostics import blocking_stats

__all__ = ["blocking_stats"]
