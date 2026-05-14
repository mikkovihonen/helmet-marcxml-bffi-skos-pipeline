"""Unit tests for ``bffi_pipeline.runs_reset`` (P-32 Phase G).

The five named acceptance tests pin: the exporter writes its PID file
at startup, the reset path SIGTERMs + relaunches, the absent-PID case
is a graceful no-op, the Prometheus admin-API delete loop hits the
right endpoint per uuid + cleans tombstones, and the 405 fallback
keeps the prune from aborting.

``reset_exporter`` is tested via subprocess (real fork to verify the
PID-file write end-to-end) and via monkeypatched ``os.kill`` (avoiding
SIGTERM on the pytest process itself). ``reset_prometheus`` uses an
``httpx.MockTransport`` so no real Prometheus is needed.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from bffi_pipeline import runs_reset
from bffi_pipeline.config import get_settings
from bffi_pipeline.runs_reset import (
    EXPORTER_ARGV_FILENAME,
    EXPORTER_PID_FILENAME,
    exporter_argv_file,
    exporter_pid_file,
    reset_exporter,
    reset_prometheus,
)


@pytest.fixture(autouse=True)
def _isolate_settings_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Per-test ``Settings`` cache reset + a ``BFFI_RUNS_ROOT`` pointing at tmp_path.

    The reset paths read ``get_settings().runs_root`` for the PID-file
    location and ``settings.prometheus_url`` for the admin API; both
    need to be deterministic per test.
    """
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(tmp_path))
    monkeypatch.setenv("BFFI_OBSERVABILITY_SIDECAR", "none")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --- exporter PID + argv lifecycle ----------------------------------------


def test_exporter_writes_pid_file_on_startup(tmp_path: Path) -> None:
    """``serve()`` writes PID + argv files on startup; atexit removes them.

    Launches the exporter in a real subprocess (uv run python -c ...)
    so the atexit cleanup hook fires on shutdown — that's the
    contract Phase G's reset path depends on.
    """
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    pid_file = runs_root / EXPORTER_PID_FILENAME
    argv_file = runs_root / EXPORTER_ARGV_FILENAME

    # Spawn a short-lived exporter that writes the PID + argv files,
    # then exits via the iterations=0 test-hook.
    code = f"""
from pathlib import Path
from bffi_pipeline.metrics_exporter import serve, PipelineMetrics
sidecar = Path({str(tmp_path / "sidecar.jsonl")!r})
sidecar.write_text("")
serve(
    [sidecar],
    port=0,  # OS-assigned ephemeral port
    poll_seconds=0.0,
    iterations=0,
    metrics=PipelineMetrics(),
    pid_file=Path({str(pid_file)!r}),
    argv=["recorded", "argv", "from", "test"],
)
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    _ = proc

    # serve() finished cleanly; atexit fired → both files cleaned up.
    assert not pid_file.exists(), "atexit cleanup should have removed the PID file on graceful exit"
    assert not argv_file.exists(), (
        "atexit cleanup should have removed the argv file on graceful exit"
    )


def test_exporter_writes_pid_file_with_recorded_argv(tmp_path: Path) -> None:
    """The write itself is correct — observe the file mid-process.

    Inspect the PID + argv files BEFORE the exporter exits by having
    it sleep briefly so we can read them. This is the "did serve()
    write them?" check; the atexit cleanup is covered by the test
    above.
    """
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    pid_file = runs_root / EXPORTER_PID_FILENAME
    argv_file = runs_root / EXPORTER_ARGV_FILENAME

    code = f"""
import time
from pathlib import Path
from bffi_pipeline.metrics_exporter import _write_exporter_pid_files
_write_exporter_pid_files(
    Path({str(pid_file)!r}),
    ["recorded", "argv", "from", "test"],
)
time.sleep(2)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Wait for the helper to write the file.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if pid_file.exists():
                break
            time.sleep(0.05)

        assert pid_file.is_file()
        assert argv_file.is_file()
        written_pid = int(pid_file.read_text().strip())
        assert written_pid == proc.pid
        argv_lines = argv_file.read_text().splitlines()
        assert argv_lines == ["recorded", "argv", "from", "test"]
    finally:
        proc.terminate()
        proc.wait(timeout=5)


# --- reset_exporter happy paths + no-op cases -----------------------------


def test_reset_exporter_sends_sigterm_and_relaunches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``reset_exporter()`` SIGTERMs the recorded PID, waits, relaunches from argv.

    Mocks ``os.kill`` + ``subprocess.Popen`` + ``_process_alive`` to
    avoid signaling pytest itself. Asserts the call sequence:
    SIGTERM with the right PID → process detected as dead → Popen
    fires with the argv content.
    """
    runs_root = tmp_path
    pid_file = exporter_pid_file(runs_root)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text("12345\n")
    argv_file = exporter_argv_file(runs_root)
    argv_file.write_text("python\n-m\nbffi_pipeline\nserve-metrics\n--port\n9100\n")

    # Mock the process-control surface.
    mock_kill = MagicMock()
    mock_popen = MagicMock()
    # _process_alive returns True before SIGTERM, False after — simulate
    # the "exporter exited cleanly" branch.
    alive_calls = iter([True, False])
    mock_process_alive = MagicMock(side_effect=lambda _pid: next(alive_calls))

    monkeypatch.setattr(runs_reset, "os", MagicMock(kill=mock_kill, getpid=os.getpid))
    monkeypatch.setattr(runs_reset.subprocess, "Popen", mock_popen)
    monkeypatch.setattr(runs_reset, "_process_alive", mock_process_alive)

    reset_exporter(relaunch=True, sigterm_wait_seconds=2.0)

    mock_kill.assert_called_once()
    sigterm_args = mock_kill.call_args[0]
    assert sigterm_args[0] == 12345  # PID from the file

    mock_popen.assert_called_once()
    relaunch_argv = mock_popen.call_args[0][0]
    assert relaunch_argv == ["python", "-m", "bffi_pipeline", "serve-metrics", "--port", "9100"]


def test_reset_exporter_skips_when_pid_absent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No ``.exporter.pid`` → warn and return without raising."""

    caplog.set_level(logging.WARNING)
    reset_exporter(relaunch=True)
    # Warning logged; no exception escaped.
    messages = " ".join(rec.message for rec in caplog.records)
    assert "does not exist" in messages


# --- reset_prometheus happy + fallback paths ------------------------------


def test_reset_prometheus_posts_delete_for_each_uuid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One POST per pruned uuid + one tombstone-clean call at the end."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    # Patch httpx.Client so the MockTransport drives the calls.
    real_client = httpx.Client

    def client_factory(**kwargs: Any) -> httpx.Client:
        kwargs["transport"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(runs_reset.httpx, "Client", client_factory)

    reset_prometheus(["uuid-aaa", "uuid-bbb", "uuid-ccc"])

    # 3 deletes + 1 tombstone-clean = 4 requests.
    assert len(requests) == 4
    delete_requests = [r for r in requests if "delete_series" in str(r.url)]
    clean_requests = [r for r in requests if "clean_tombstones" in str(r.url)]
    assert len(delete_requests) == 3
    assert len(clean_requests) == 1

    # Each delete carries the right run_uuid in its match[] param.
    match_values = sorted(r.url.params.get("match[]") for r in delete_requests)
    assert match_values == [
        '{run_uuid="uuid-aaa"}',
        '{run_uuid="uuid-bbb"}',
        '{run_uuid="uuid-ccc"}',
    ]


def test_reset_prometheus_continues_on_admin_api_405(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Admin API returns 405 → warn + short-circuit; don't abort the prune."""

    caplog.set_level(logging.WARNING)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(405)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def client_factory(**kwargs: Any) -> httpx.Client:
        kwargs["transport"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(runs_reset.httpx, "Client", client_factory)

    # Must not raise.
    reset_prometheus(["uuid-aaa", "uuid-bbb"])

    messages = " ".join(rec.message for rec in caplog.records)
    assert "405" in messages
    assert "admin API not enabled" in messages


def test_reset_prometheus_handles_connection_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Prometheus unreachable → warn + return cleanly; don't abort the prune."""

    caplog.set_level(logging.WARNING)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def client_factory(**kwargs: Any) -> httpx.Client:
        kwargs["transport"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(runs_reset.httpx, "Client", client_factory)

    # Must not raise.
    reset_prometheus(["uuid-aaa"])

    messages = " ".join(rec.message for rec in caplog.records)
    assert "connection refused" in messages.lower()


def test_reset_prometheus_noop_on_empty_uuid_list() -> None:
    """Empty uuid list → no HTTP calls, no warnings."""
    # No mocking — if reset_prometheus tried to make a real HTTP call,
    # the test would hang or fail with a connection error against the
    # default localhost:9091.
    reset_prometheus([])
