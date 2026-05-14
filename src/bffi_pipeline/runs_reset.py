"""Reset helpers invoked by ``bffi-pipeline runs prune`` (P-32 Phase C plumbing).

Phase C ships the CLI flag plumbing (`--reset-exporter`, `--reset-prometheus`,
`--reset-fuseki`, `--reset-all`); the actual reset implementations land
in Phases G + H. This module exposes the helper surface as stubs that
log warnings and return cleanly, so the prune CLI's behaviour today is
"select + delete dirs; flags are accepted but reset paths are no-ops
pending G/H". The function signatures + their semantic contract are
the ones G/H will fill in — keeps the CLI surface stable across phases.

Phases G + H replace the stub bodies with real implementations:

- Phase G: ``reset_exporter`` (SIGTERM the exporter PID + optionally
  relaunch from recorded argv) and ``reset_prometheus`` (POST to
  Prometheus admin API to delete TSDB series for the pruned runs).
- Phase H: ``reset_fuseki`` (DROP `<graph_base>*` named graphs).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def reset_exporter() -> None:
    """Phase G stub: restart the bffi-pipeline serve-metrics exporter.

    Today: warns and no-ops. Phase G reads ``<BFFI_RUNS_ROOT>/.exporter.pid``,
    sends SIGTERM, optionally relaunches from the recorded argv.
    """
    logger.warning(
        "[bffi-pipeline] --reset-exporter requested; Phase G not yet shipped — no-op. "
        "Manually restart `bffi-pipeline serve-metrics` to clear the in-memory registry."
    )


def reset_prometheus(pruned_run_uuids: list[str]) -> None:
    """Phase G stub: drop pruned run_uuids' series from Prometheus TSDB.

    Today: warns and no-ops. Phase G POSTs to the Prometheus admin API's
    ``/api/v1/admin/tsdb/delete_series?match[]={run_uuid="<uuid>"}``
    endpoint for each pruned uuid, then triggers tombstone cleanup.
    """
    if not pruned_run_uuids:
        return
    logger.warning(
        "[bffi-pipeline] --reset-prometheus requested for %d run_uuid(s); "
        "Phase G not yet shipped — no-op. The dashboard's $active_run dropdown will "
        "still offer these uuids until Prometheus's retention window expires.",
        len(pruned_run_uuids),
    )


def reset_fuseki() -> None:
    """Phase H stub: drop ``<graph_base>*`` named graphs from Fuseki.

    Today: warns and no-ops. Phase H DROPs every named graph whose URI
    starts with ``settings.graph_base`` (preserving vocabulary graphs
    that live in other URI namespaces).
    """
    logger.warning(
        "[bffi-pipeline] --reset-fuseki requested; Phase H not yet shipped — no-op. "
        "Manually run `bffi-pipeline load --rollback` or wait for Phase H's "
        "`runs clear-fuseki` command."
    )


__all__ = [
    "reset_exporter",
    "reset_fuseki",
    "reset_prometheus",
]
