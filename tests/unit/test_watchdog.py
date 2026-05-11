"""Unit tests for ``bffi_pipeline.stages.watchdog``.

Covers the structured event emitter in isolation; the wiring into
``judge.judge_pair`` is exercised separately in ``test_judge.py``
under the ``# --- watchdog`` section.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bffi_pipeline.stages.watchdog import (
    WATCHDOG_STDERR_PREFIX,
    emit_watchdog_event,
)


def _read_sidecar(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_emit_writes_one_jsonl_line_to_sidecar(tmp_path: Path, capsys: Any) -> None:
    sidecar = tmp_path / "watchdog-events.jsonl"
    emit_watchdog_event(
        pair_id="r1+r2",
        event="timeout",
        model_name="qwen3:8b-q4_K_M",
        elapsed_seconds=90.123456,
        retry_n=0,
        sidecar_path=sidecar,
    )
    events = _read_sidecar(sidecar)
    assert len(events) == 1
    e = events[0]
    assert e["pair_id"] == "r1+r2"
    assert e["event"] == "timeout"
    assert e["model"] == "qwen3:8b-q4_K_M"
    assert e["elapsed_s"] == 90.123  # 3-decimal rounding pinned
    assert e["retry_n"] == 0
    # The ts field is always present and starts with the year prefix.
    assert e["ts"].startswith("20")  # any 21st-century year is fine for the test
    assert e["ts"].endswith("Z")


def test_emit_prints_prefixed_payload_to_stderr(tmp_path: Path, capsys: Any) -> None:
    emit_watchdog_event(
        pair_id="r1+r2",
        event="give_up",
        model_name="qwen3:32b-q4_K_M",
        elapsed_seconds=270.0,
        retry_n=3,
        sidecar_path=None,
    )
    out = capsys.readouterr()
    assert out.out == ""  # stdout stays clean
    assert out.err.startswith(WATCHDOG_STDERR_PREFIX)
    payload_str = out.err[len(WATCHDOG_STDERR_PREFIX) :].rstrip()
    payload = json.loads(payload_str)
    assert payload["event"] == "give_up"
    assert payload["retry_n"] == 3


def test_emit_appends_across_multiple_calls(tmp_path: Path) -> None:
    sidecar = tmp_path / "watchdog-events.jsonl"
    for i, event in enumerate(["timeout", "retry", "give_up"]):
        emit_watchdog_event(
            pair_id=f"r{i}+r{i + 1}",
            event=event,  # type: ignore[arg-type]
            model_name="m",
            elapsed_seconds=float(i),
            retry_n=i,
            sidecar_path=sidecar,
        )
    events = _read_sidecar(sidecar)
    assert [e["event"] for e in events] == ["timeout", "retry", "give_up"]


def test_emit_creates_parent_directory_if_missing(tmp_path: Path) -> None:
    """``data/`` may not exist on a fresh repo; the emitter should create it."""
    sidecar = tmp_path / "nonexistent" / "subdir" / "watchdog-events.jsonl"
    assert not sidecar.parent.exists()
    emit_watchdog_event(
        pair_id="r1+r2",
        event="timeout",
        model_name="m",
        elapsed_seconds=1.0,
        retry_n=0,
        sidecar_path=sidecar,
    )
    assert sidecar.exists()
