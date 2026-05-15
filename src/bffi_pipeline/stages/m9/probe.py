"""M9 dependency probes — emit a single ``health`` event at entry + mid-stage.

Wraps :func:`bffi_pipeline.observability.probes.probe_mlx_lm` /
``probe_finto`` / ``probe_fuseki`` (per-test ``conftest.py`` stubs these
out at the module-attribute level — keep the imports here so the patches
take effect).

P-38 Phase D: extracted from m9/runner.py. No logic change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bffi_pipeline.config import get_settings
from bffi_pipeline.observability.probes import (
    emit_health_probes,
    probe_finto,
    probe_fuseki,
    probe_mlx_lm,
)

if TYPE_CHECKING:
    from bffi_pipeline.stages.m9.local_concept_resolver import LocalConceptResolver


def _m9_probe_dependencies(local_resolver: LocalConceptResolver | None) -> None:
    """Run the M9 dependency probes + emit a single ``health`` event."""
    settings = get_settings()
    probes_to_emit = {
        "mlx-lm": probe_mlx_lm(settings.llm_base_url),
        "finto": probe_finto(),
    }
    if local_resolver is not None:
        probes_to_emit["fuseki"] = probe_fuseki(settings.fuseki_url)
    emit_health_probes("m9", probes_to_emit)
