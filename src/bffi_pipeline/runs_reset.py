"""Reset helpers invoked by ``bffi-pipeline runs prune`` (P-32 Phases G + H).

Phase C shipped the CLI flag plumbing (`--reset-exporter`,
`--reset-prometheus`, `--reset-fuseki`, `--reset-all`). Phase G fills
in ``reset_exporter`` and ``reset_prometheus`` with real
implementations; Phase H replaces the ``reset_fuseki`` stub.

The CLI surface stays stable across the transition — the stubs were
replaced in-place, the call sites in cli.py didn't change.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import time
from pathlib import Path

import httpx

from bffi_pipeline.config import get_settings

logger = logging.getLogger(__name__)

#: Where ``bffi-pipeline serve-metrics`` writes its PID + argv files.
#: Located under ``BFFI_RUNS_ROOT`` so the file lives alongside the runs
#: it observes; ``runs prune --reset-exporter`` reads it from the same
#: place. Per-runs-root, not per-run.
EXPORTER_PID_FILENAME = ".exporter.pid"
EXPORTER_ARGV_FILENAME = ".exporter.argv"


def exporter_pid_file(runs_root: Path | None = None) -> Path:
    """Return the canonical exporter PID-file path under ``BFFI_RUNS_ROOT``."""
    root = runs_root if runs_root is not None else get_settings().runs_root
    return root / EXPORTER_PID_FILENAME


def exporter_argv_file(runs_root: Path | None = None) -> Path:
    """Return the canonical exporter argv-file path under ``BFFI_RUNS_ROOT``."""
    root = runs_root if runs_root is not None else get_settings().runs_root
    return root / EXPORTER_ARGV_FILENAME


def reset_exporter(*, relaunch: bool = True, sigterm_wait_seconds: float = 10.0) -> None:
    """Send ``SIGTERM`` to the running ``serve-metrics`` exporter; optionally relaunch.

    Reads ``<BFFI_RUNS_ROOT>/.exporter.pid`` for the target PID and
    ``.exporter.argv`` for the recorded launch argv:

    - PID file absent → warn ("exporter not running; nothing to reset"), return.
    - PID file present + process alive → ``SIGTERM``; wait up to
      ``sigterm_wait_seconds`` for clean exit; if ``relaunch=True`` and
      the argv file is readable, launch a fresh detached child with
      those args; else leave restart to the operator.
    - PID file present + process dead → clean up stale file, warn, return.

    Pruning a run-uuid from disk while the exporter is still running
    leaves the in-memory ``prometheus_client`` registry holding stale
    ``{run_uuid="..."}`` series; restarting the exporter forces it to
    rehydrate from the (now-pruned) sidecars, which omits the deleted
    runs cleanly.
    """
    settings = get_settings()
    pid_path = exporter_pid_file(settings.runs_root)
    pid = _read_exporter_pid_or_warn(pid_path)
    if pid is None:
        return

    if not _send_sigterm_to_exporter(pid):
        return

    _wait_for_exit(pid, sigterm_wait_seconds)

    if not relaunch:
        logger.warning(
            "[bffi-pipeline] --reset-exporter: relaunch=False; "
            "operator is expected to restart `bffi-pipeline serve-metrics` manually."
        )
        return

    _maybe_relaunch_exporter(exporter_argv_file(settings.runs_root))


def _read_exporter_pid_or_warn(pid_path: Path) -> int | None:
    """Resolve the exporter's PID from ``pid_path``; warn + return None on any issue."""
    if not pid_path.is_file():
        logger.warning(
            "[bffi-pipeline] --reset-exporter: %s does not exist; "
            "exporter not running (or never wrote its PID). Skipping.",
            pid_path,
        )
        return None

    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as exc:
        logger.warning(
            "[bffi-pipeline] --reset-exporter: couldn't parse %s (%s); "
            "removing stale file and skipping.",
            pid_path,
            exc,
        )
        with contextlib.suppress(OSError):
            pid_path.unlink()
        return None

    if not _process_alive(pid):
        logger.warning(
            "[bffi-pipeline] --reset-exporter: PID %d from %s is not alive; "
            "removing stale file and skipping.",
            pid,
            pid_path,
        )
        with contextlib.suppress(OSError):
            pid_path.unlink()
        return None
    return pid


def _send_sigterm_to_exporter(pid: int) -> bool:
    """SIGTERM ``pid``; return False on signal failure."""
    logger.warning("[bffi-pipeline] --reset-exporter: SIGTERM-ing exporter PID %d.", pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        logger.warning("[bffi-pipeline] --reset-exporter: SIGTERM failed (%s); skipping.", exc)
        return False
    return True


def _wait_for_exit(pid: int, sigterm_wait_seconds: float) -> None:
    """Poll until ``pid`` exits or ``sigterm_wait_seconds`` elapses; warn on timeout."""
    deadline = time.monotonic() + sigterm_wait_seconds
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return
        time.sleep(0.2)
    logger.warning(
        "[bffi-pipeline] --reset-exporter: PID %d did not exit within %.1fs; "
        "leaving it to the OS. Manually verify the new exporter has a fresh registry.",
        pid,
        sigterm_wait_seconds,
    )


def _maybe_relaunch_exporter(argv_path: Path) -> None:
    """Launch a fresh detached exporter from the recorded argv if available."""
    if not argv_path.is_file():
        logger.warning(
            "[bffi-pipeline] --reset-exporter: %s missing; can't relaunch. "
            "Restart `bffi-pipeline serve-metrics` manually.",
            argv_path,
        )
        return

    argv = [line.rstrip("\n") for line in argv_path.read_text(encoding="utf-8").splitlines()]
    if not argv:
        logger.warning("[bffi-pipeline] --reset-exporter: %s is empty; can't relaunch.", argv_path)
        return

    logger.warning("[bffi-pipeline] --reset-exporter: relaunching with %s", argv)
    # Detached so the new exporter survives this CLI's exit. stdout /
    # stderr go to /dev/null — the new exporter writes its own JSONL
    # tail logs and per-stage events to disk.
    subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def reset_prometheus(pruned_run_uuids: list[str]) -> None:
    """Drop pruned ``run_uuid``s' series from Prometheus's TSDB via the admin API.

    For each ``run_uuid`` in ``pruned_run_uuids``, POSTs to
    ``<BFFI_PROMETHEUS_URL>/api/v1/admin/tsdb/delete_series?match[]={run_uuid="<uuid>"}``;
    then POSTs to ``/api/v1/admin/tsdb/clean_tombstones`` once at the
    end to free the deleted blocks immediately. Both endpoints require
    Prometheus to be started with ``--web.enable-admin-api``; the
    local docker-compose stack enables it by default (Phase G).

    Falls back gracefully on operator-side failure modes:

    - HTTP 405 → admin API disabled. Logs a warning + skips.
    - Connection refused → Prometheus unreachable. Same.
    - Per-uuid 4xx/5xx → log and continue with the next uuid.

    Doesn't abort the prune on any of these — the on-disk runs are
    already gone; Prometheus residue is a tidiness issue, not a
    correctness one.
    """
    if not pruned_run_uuids:
        return

    settings = get_settings()
    base_url = settings.prometheus_url.rstrip("/")
    delete_url = f"{base_url}/api/v1/admin/tsdb/delete_series"
    clean_url = f"{base_url}/api/v1/admin/tsdb/clean_tombstones"

    deleted_count = 0
    try:
        with httpx.Client(timeout=10.0) as client:
            try:
                for run_uuid in pruned_run_uuids:
                    if _post_delete_series(client, delete_url, run_uuid):
                        deleted_count += 1
            except _AdminApiDisabledError:
                # First 405 short-circuits the loop; the warning was
                # logged at the call site.
                return
            if deleted_count > 0:
                _post_clean_tombstones(client, clean_url)
    except httpx.ConnectError as exc:
        logger.warning(
            "[bffi-pipeline] --reset-prometheus: connection refused at %s (%s); "
            "skipping. The dashboard's $active_run dropdown will still offer the "
            "pruned uuids until Prometheus's retention window expires.",
            base_url,
            exc,
        )
        return

    logger.warning(
        "[bffi-pipeline] --reset-prometheus: deleted series for %d / %d pruned run_uuid(s).",
        deleted_count,
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


# --- Internals ----------------------------------------------------------


def _process_alive(pid: int) -> bool:
    """Return True iff a process with ``pid`` is currently alive.

    Uses ``os.kill(pid, 0)`` — sends no signal; raises if the PID
    doesn't exist OR we don't have permission to signal it. The
    "no permission" case is treated as alive (the process exists; we
    just can't poke it), which is the conservative choice for the
    pre-SIGTERM check.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists in a different uid; signal would be rejected.
        # Treat as alive — the operator wanted us to send SIGTERM, and
        # the subsequent ``os.kill(pid, SIGTERM)`` will raise the same
        # PermissionError with a clearer message.
        return True
    return True


def _post_delete_series(client: httpx.Client, url: str, run_uuid: str) -> bool:
    """POST one delete_series call; return True on 204 No Content."""
    try:
        response = client.post(
            url,
            params={"match[]": f'{{run_uuid="{run_uuid}"}}'},
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "[bffi-pipeline] --reset-prometheus: %s failed for run_uuid=%s (%s); continuing.",
            url,
            run_uuid,
            exc,
        )
        return False
    if response.status_code == 405:  # noqa: PLR2004 — HTTP status literal
        logger.warning(
            "[bffi-pipeline] --reset-prometheus: %s returned 405 — admin API not enabled "
            "(start Prometheus with --web.enable-admin-api). Skipping further deletes.",
            url,
        )
        # 405 is a global "feature off" signal; abort the loop by
        # raising and letting the caller catch.
        raise _AdminApiDisabledError
    if response.status_code != 204:  # noqa: PLR2004 — HTTP status literal
        logger.warning(
            "[bffi-pipeline] --reset-prometheus: %s returned %d for run_uuid=%s; continuing.",
            url,
            response.status_code,
            run_uuid,
        )
        return False
    return True


def _post_clean_tombstones(client: httpx.Client, url: str) -> None:
    """POST clean_tombstones; warn on non-204 but otherwise ignore."""
    try:
        response = client.post(url)
    except httpx.HTTPError as exc:
        logger.warning(
            "[bffi-pipeline] --reset-prometheus: tombstone-clean call to %s failed (%s); "
            "leaving the freshly-deleted series tombstoned. They age out per "
            "Prometheus's retention.",
            url,
            exc,
        )
        return
    if response.status_code != 204:  # noqa: PLR2004 — HTTP status literal
        logger.warning(
            "[bffi-pipeline] --reset-prometheus: tombstone-clean returned %d; ignoring.",
            response.status_code,
        )


class _AdminApiDisabledError(Exception):
    """Sentinel raised on first 405 to short-circuit the delete loop."""


__all__ = [
    "EXPORTER_ARGV_FILENAME",
    "EXPORTER_PID_FILENAME",
    "exporter_argv_file",
    "exporter_pid_file",
    "reset_exporter",
    "reset_fuseki",
    "reset_prometheus",
]
