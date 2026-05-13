"""Unit tests for ``bffi_pipeline.stages.observability`` (P-11 Phase A).

The emitter is a small surface but its contract — stderr line + JSONL
append, thread-safe, watchdog absorption forwarding — is what Phase B
(status CLI) and Phase D (Prometheus exporter) consume. These tests
pin every observable property of the contract; downstream consumers
can read them as the spec.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from bffi_pipeline.stages.observability import (
    STAGE_EVENT_STDERR_PREFIX,
    StageEventEmitter,
    emit_if_active,
    get_active_emitter,
    set_active_emitter,
)
from bffi_pipeline.stages.watchdog import emit_watchdog_event


@pytest.fixture(autouse=True)
def _reset_emitter() -> None:
    """Clear the module-level singleton between tests so they don't bleed."""
    set_active_emitter(None)


def _load_sidecar(path: Path) -> list[dict[str, object]]:
    """Parse the JSONL sidecar into a list of dicts."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# --- emit shape ----------------------------------------------------------


def test_emit_writes_stderr_prefix_and_sidecar_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sidecar = tmp_path / "stage-events.jsonl"
    emitter = StageEventEmitter(sidecar_path=sidecar, run_uuid="run-1")

    emitter.emit(
        stage="m9",
        event="start",
        counters={"total": 12666},
    )

    captured = capsys.readouterr()
    assert captured.err.startswith(STAGE_EVENT_STDERR_PREFIX)
    rows = _load_sidecar(sidecar)
    assert len(rows) == 1
    row = rows[0]
    assert row["stage"] == "m9"
    assert row["event"] == "start"
    assert row["run_uuid"] == "run-1"
    assert row["counters"] == {"total": 12666}
    # ts is always present, ISO-8601 with second precision.
    assert "ts" in row
    assert isinstance(row["ts"], str)


def test_emit_includes_phase_and_extra_when_set(tmp_path: Path) -> None:
    sidecar = tmp_path / "stage-events.jsonl"
    emitter = StageEventEmitter(sidecar_path=sidecar, run_uuid="run-2")

    emitter.emit(
        stage="m9",
        event="progress",
        phase="phase1",
        counters={"processed": 200, "total": 12666},
        extra={"tier0_local": 187, "no_candidate": 7},
    )

    rows = _load_sidecar(sidecar)
    assert rows[0]["phase"] == "phase1"
    assert rows[0]["extra"] == {"tier0_local": 187, "no_candidate": 7}


def test_emit_omits_optional_fields_when_unset(tmp_path: Path) -> None:
    sidecar = tmp_path / "stage-events.jsonl"
    emitter = StageEventEmitter(sidecar_path=sidecar, run_uuid="run-3")

    emitter.emit(stage="load", event="end")

    rows = _load_sidecar(sidecar)
    row = rows[0]
    # No phase / counters / extra keys when not passed — keeps the JSONL
    # tight for high-volume runs.
    assert "phase" not in row
    assert "counters" not in row
    assert "extra" not in row


def test_emit_with_none_sidecar_only_writes_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emitter = StageEventEmitter(sidecar_path=None, run_uuid="run-4")
    emitter.emit(stage="m9", event="start", counters={"total": 100})
    captured = capsys.readouterr()
    assert STAGE_EVENT_STDERR_PREFIX in captured.err
    # Nothing crashed; sidecar_path None is the documented test path.


# --- active-emitter singleton --------------------------------------------


def test_set_and_get_active_emitter_round_trip(tmp_path: Path) -> None:
    assert get_active_emitter() is None
    emitter = StageEventEmitter(sidecar_path=tmp_path / "stage-events.jsonl", run_uuid="r")
    set_active_emitter(emitter)
    assert get_active_emitter() is emitter
    set_active_emitter(None)
    assert get_active_emitter() is None


def test_emit_if_active_no_op_when_no_emitter(tmp_path: Path) -> None:
    """Calling emit_if_active without an active emitter does nothing
    (no crash, no stderr) — the pattern that makes the per-stage
    call-sites None-safe."""
    sidecar = tmp_path / "stage-events.jsonl"
    # No set_active_emitter; module slot is None.
    emit_if_active(stage="m9", event="progress", counters={"processed": 1})
    assert not sidecar.exists()


def test_emit_if_active_forwards_to_active_emitter(tmp_path: Path) -> None:
    sidecar = tmp_path / "stage-events.jsonl"
    emitter = StageEventEmitter(sidecar_path=sidecar, run_uuid="r")
    set_active_emitter(emitter)
    emit_if_active(stage="m9", event="end", counters={"total": 12666})
    rows = _load_sidecar(sidecar)
    assert len(rows) == 1
    assert rows[0]["event"] == "end"


# --- thread safety -------------------------------------------------------


def test_emit_is_thread_safe_under_concurrent_callers(tmp_path: Path) -> None:
    """M9's c=4 + phase1=8 means up to 12 workers can call emit
    concurrently. Each emit's stderr line + JSONL append must land
    intact (no interleaving)."""
    sidecar = tmp_path / "stage-events.jsonl"
    emitter = StageEventEmitter(sidecar_path=sidecar, run_uuid="r")

    n_workers = 12
    emits_per_worker = 50

    def worker(worker_idx: int) -> None:
        for i in range(emits_per_worker):
            emitter.emit(
                stage="m9",
                event="progress",
                counters={"processed": i},
                extra={"worker": worker_idx},
            )

    threads = [threading.Thread(target=worker, args=(idx,)) for idx in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = _load_sidecar(sidecar)
    assert len(rows) == n_workers * emits_per_worker
    # Each row parses as valid JSON (no torn writes from interleaving).
    for row in rows:
        assert "extra" in row
        assert "worker" in row["extra"]


# --- watchdog absorption ------------------------------------------------


def test_watchdog_event_propagates_to_active_emitter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the active emitter is set, ``emit_watchdog_event`` forwards
    a copy to stage-events.jsonl while still writing its dedicated
    watchdog-events.jsonl. The watchdog's existing forensic sidecar
    contract is preserved; the new unified stream gets a copy under
    ``event="watchdog"``."""
    stage_sidecar = tmp_path / "stage-events.jsonl"
    watchdog_sidecar = tmp_path / "watchdog-events.jsonl"

    emitter = StageEventEmitter(sidecar_path=stage_sidecar, run_uuid="r")
    set_active_emitter(emitter)

    emit_watchdog_event(
        pair_id="raw_a+raw_b",
        event="timeout",
        model_name="qwen3:8b",
        elapsed_seconds=92.4,
        retry_n=1,
        sidecar_path=watchdog_sidecar,
    )

    # Both sidecars carry the event.
    wd_rows = _load_sidecar(watchdog_sidecar)
    assert len(wd_rows) == 1
    assert wd_rows[0]["event"] == "timeout"
    assert wd_rows[0]["pair_id"] == "raw_a+raw_b"

    stage_rows = _load_sidecar(stage_sidecar)
    assert len(stage_rows) == 1
    forwarded = stage_rows[0]
    assert forwarded["event"] == "watchdog"
    assert forwarded["stage"] == "watchdog"
    # The watchdog payload is nested under ``extra`` so consumers can
    # filter on the inner event type without parsing the message text.
    assert forwarded["extra"]["event"] == "timeout"
    assert forwarded["extra"]["pair_id"] == "raw_a+raw_b"


def test_watchdog_event_no_op_on_stage_stream_when_no_active_emitter(
    tmp_path: Path,
) -> None:
    """Without an active emitter, the watchdog's existing
    ``watchdog-events.jsonl`` behaviour is unchanged — no
    stage-events.jsonl side-effect, no crash."""
    watchdog_sidecar = tmp_path / "watchdog-events.jsonl"

    # No set_active_emitter — singleton is None.
    emit_watchdog_event(
        pair_id="raw_a+raw_b",
        event="timeout",
        model_name="qwen3:8b",
        elapsed_seconds=92.4,
        retry_n=1,
        sidecar_path=watchdog_sidecar,
    )

    wd_rows = _load_sidecar(watchdog_sidecar)
    assert len(wd_rows) == 1  # forensic sidecar still works
    assert not (tmp_path / "stage-events.jsonl").exists()  # no stage stream
