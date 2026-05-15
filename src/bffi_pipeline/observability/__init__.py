"""Operator-facing observability surface for the bffi pipeline.

Bundles three concerns that share the same downstream consumer (the
StageEventEmitter / Prometheus exporter / Grafana dashboard):

- :mod:`bffi_pipeline.observability.events` — per-stage start/end/
  health/skipped/failed event emission and the active-emitter
  registry. The core module of the package.
- :mod:`bffi_pipeline.observability.watchdog` — long-running stage
  watchdog events (M6 / M9).
- :mod:`bffi_pipeline.observability.probes` — Fuseki + mlx-lm + Finto
  liveness probes used at stage entry.

Re-exports the operator-facing surface so callers can write
``from bffi_pipeline.observability import emit_if_active`` rather
than reaching into the submodules. Heavy consumers that need module-
level access (the unit-test conftest patches probe functions on the
consumer module's namespace, not on this package) keep using the
explicit submodule path.
"""

from bffi_pipeline.observability.events import (
    StageEventEmitter,
    emit_failed,
    emit_if_active,
    emit_plan,
    emit_skipped,
    get_active_emitter,
    set_active_emitter,
)
from bffi_pipeline.observability.probes import (
    ProbeResult,
    emit_health_probes,
    probe_finto,
    probe_fuseki,
    probe_mlx_lm,
)
from bffi_pipeline.observability.watchdog import emit_watchdog_event

__all__ = [
    "ProbeResult",
    "StageEventEmitter",
    "emit_failed",
    "emit_health_probes",
    "emit_if_active",
    "emit_plan",
    "emit_skipped",
    "emit_watchdog_event",
    "get_active_emitter",
    "probe_finto",
    "probe_fuseki",
    "probe_mlx_lm",
    "set_active_emitter",
]
