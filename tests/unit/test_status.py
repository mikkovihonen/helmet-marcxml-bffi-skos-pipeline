"""Unit tests for ``bffi_pipeline.status`` (P-11 Phase B).

The consumer side of the stage-events.jsonl contract. Synthetic event
streams drive parse → collate → render; the tail() loop is bounded
via the ``iterations`` test hook so the test suite stays
deterministic and fast.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bffi_pipeline.status import (
    PhaseProgress,
    StageEventRow,
    StageStatus,
    collate,
    parse_sidecar,
    render,
    tail,
)


def _write_sidecar(path: Path, rows: list[dict[str, object]]) -> None:
    """Write a synthetic stage-events.jsonl file from a list of dicts."""
    path.write_text(
        "\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n",
        encoding="utf-8",
    )


def _ts(seconds_from_zero: int) -> str:
    """Build an ISO-8601 ts at ``2026-05-13T00:00:00Z + N seconds``."""
    base = datetime(2026, 5, 13, 0, 0, 0, tzinfo=UTC)
    return (base + timedelta(seconds=seconds_from_zero)).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- parse_sidecar -------------------------------------------------------


def test_parse_sidecar_returns_empty_when_file_absent(tmp_path: Path) -> None:
    """No file = empty list; status CLI on a fresh corpus shouldn't crash."""
    assert parse_sidecar(tmp_path / "missing.jsonl") == []


def test_parse_sidecar_skips_blank_and_malformed_lines(tmp_path: Path) -> None:
    """Tail-during-write can leave a partial last line; parser is lenient."""
    sidecar = tmp_path / "events.jsonl"
    sidecar.write_text(
        "\n".join(
            [
                json.dumps({"ts": _ts(0), "run_uuid": "r", "stage": "m9", "event": "start"}),
                "",
                "{not valid json",
                json.dumps({"ts": _ts(1), "run_uuid": "r", "stage": "m9", "event": "end"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    rows = parse_sidecar(sidecar)
    assert [r.event for r in rows] == ["start", "end"]


def test_parse_sidecar_filters_by_since(tmp_path: Path) -> None:
    sidecar = tmp_path / "events.jsonl"
    _write_sidecar(
        sidecar,
        [
            {"ts": _ts(0), "run_uuid": "r", "stage": "m9", "event": "start"},
            {"ts": _ts(60), "run_uuid": "r", "stage": "m9", "event": "end"},
        ],
    )
    cutoff = datetime(2026, 5, 13, 0, 0, 30, tzinfo=UTC)
    rows = parse_sidecar(sidecar, since=cutoff)
    assert [r.event for r in rows] == ["end"]


def test_parse_sidecar_filters_by_run_uuid(tmp_path: Path) -> None:
    sidecar = tmp_path / "events.jsonl"
    _write_sidecar(
        sidecar,
        [
            {"ts": _ts(0), "run_uuid": "run-a", "stage": "m9", "event": "start"},
            {"ts": _ts(10), "run_uuid": "run-b", "stage": "m9", "event": "start"},
        ],
    )
    rows = parse_sidecar(sidecar, run_uuid="run-b")
    assert [r.run_uuid for r in rows] == ["run-b"]


# --- collate -------------------------------------------------------------


def test_collate_tracks_start_phases_progress_end_for_m9(tmp_path: Path) -> None:
    """The full M9 event vocabulary collates into one StageStatus
    with three phases. Progress events under a phase update that
    phase's counters; the end event populates ``final_counters``."""
    sidecar = tmp_path / "events.jsonl"
    _write_sidecar(
        sidecar,
        [
            {
                "ts": _ts(0),
                "run_uuid": "r",
                "stage": "m9",
                "event": "start",
                "counters": {"total": 12666},
            },
            {
                "ts": _ts(1),
                "run_uuid": "r",
                "stage": "m9",
                "event": "phase_boundary",
                "phase": "phase1",
                "counters": {"total": 12666},
            },
            {
                "ts": _ts(60),
                "run_uuid": "r",
                "stage": "m9",
                "event": "progress",
                "phase": "phase1",
                "counters": {"processed": 200, "total": 12666},
            },
            {
                "ts": _ts(120),
                "run_uuid": "r",
                "stage": "m9",
                "event": "progress",
                "phase": "phase1",
                "counters": {"processed": 400, "total": 12666},
            },
            {
                "ts": _ts(180),
                "run_uuid": "r",
                "stage": "m9",
                "event": "phase_boundary",
                "phase": "phase2",
                "counters": {"deferred_to_picker": 1348},
            },
            {
                "ts": _ts(180),
                "run_uuid": "r",
                "stage": "m9",
                "event": "phase_boundary",
                "phase": "phase3",
                "counters": {"total": 12666},
            },
            {
                "ts": _ts(200),
                "run_uuid": "r",
                "stage": "m9",
                "event": "end",
                "counters": {
                    "total": 12666,
                    "local": 7526,
                    "lexical": 193,
                    "llm_pick": 874,
                    "fallback": 474,
                    "no_candidate": 2752,
                    "fictional": 847,
                    "watchdog_aborted": 0,
                },
            },
        ],
    )
    rows = parse_sidecar(sidecar)
    statuses = collate(rows)
    assert set(statuses.keys()) == {"m9"}
    s = statuses["m9"]
    assert s.started_at is not None
    assert s.ended_at is not None
    assert s.final_counters["llm_pick"] == 874
    assert set(s.phases.keys()) == {"phase1", "phase2", "phase3"}
    phase1 = s.phases["phase1"]
    assert phase1.processed == 400
    assert phase1.total == 12666
    # Throughput: 200 items over 60s = 200/min.
    assert phase1.throughput_per_minute == pytest.approx(200.0, rel=0.01)
    # ETA: (12666 - 400) / (200/60) seconds.
    assert phase1.eta_seconds is not None
    assert phase1.eta_seconds == pytest.approx((12666 - 400) / (200 / 60), rel=0.01)


def test_collate_watchdog_events_accumulate(tmp_path: Path) -> None:
    """Watchdog absorption forwards events with ``event="watchdog"``
    and ``stage="watchdog"``; the inner event type is in
    ``extra.event``. The collator tallies by inner type."""
    sidecar = tmp_path / "events.jsonl"
    _write_sidecar(
        sidecar,
        [
            {
                "ts": _ts(10),
                "run_uuid": "r",
                "stage": "watchdog",
                "event": "watchdog",
                "extra": {"event": "timeout", "pair_id": "a+b"},
            },
            {
                "ts": _ts(20),
                "run_uuid": "r",
                "stage": "watchdog",
                "event": "watchdog",
                "extra": {"event": "timeout", "pair_id": "c+d"},
            },
            {
                "ts": _ts(30),
                "run_uuid": "r",
                "stage": "watchdog",
                "event": "watchdog",
                "extra": {"event": "field_budget_exceeded", "pair_id": "e+f"},
            },
        ],
    )
    statuses = collate(parse_sidecar(sidecar))
    assert statuses["watchdog"].watchdog_events == {
        "timeout": 2,
        "field_budget_exceeded": 1,
    }


def test_collate_uses_now_for_elapsed_when_stage_not_ended(tmp_path: Path) -> None:
    """A stage with ``start`` but no ``end`` still gets a sensible
    elapsed (now - started_at)."""
    sidecar = tmp_path / "events.jsonl"
    _write_sidecar(
        sidecar,
        [{"ts": _ts(0), "run_uuid": "r", "stage": "m9", "event": "start"}],
    )
    statuses = collate(parse_sidecar(sidecar))
    assert statuses["m9"].elapsed_seconds is not None
    assert statuses["m9"].elapsed_seconds > 0


# --- render --------------------------------------------------------------


def test_render_empty_statuses_returns_human_message() -> None:
    """Renderer doesn't crash on no events; status CLI on fresh data
    shows a sensible message."""
    assert render({}) == "(no stage events recorded yet)"


def test_render_includes_progress_bar_eta_and_summary(tmp_path: Path) -> None:
    sidecar = tmp_path / "events.jsonl"
    _write_sidecar(
        sidecar,
        [
            {
                "ts": _ts(0),
                "run_uuid": "r",
                "stage": "m9",
                "event": "start",
                "counters": {"total": 1000},
            },
            {
                "ts": _ts(1),
                "run_uuid": "r",
                "stage": "m9",
                "event": "phase_boundary",
                "phase": "phase1",
                "counters": {"total": 1000},
            },
            {
                "ts": _ts(60),
                "run_uuid": "r",
                "stage": "m9",
                "event": "progress",
                "phase": "phase1",
                "counters": {"processed": 100, "total": 1000},
            },
            {
                "ts": _ts(120),
                "run_uuid": "r",
                "stage": "m9",
                "event": "progress",
                "phase": "phase1",
                "counters": {"processed": 200, "total": 1000},
            },
        ],
    )
    rendered = render(collate(parse_sidecar(sidecar)))
    assert "m9" in rendered
    assert "phase1" in rendered
    assert "200" in rendered  # processed shown
    assert "1,000" in rendered  # total shown (thousands separator)
    assert "20%" in rendered  # 200/1000
    assert "100/min" in rendered  # throughput
    assert "ETA" in rendered


def test_render_ordering_oldest_started_first(tmp_path: Path) -> None:
    """Multi-stage runs render in start-time order so the operator
    reads the pipeline left-to-right."""
    sidecar = tmp_path / "events.jsonl"
    _write_sidecar(
        sidecar,
        [
            {"ts": _ts(100), "run_uuid": "r", "stage": "m9", "event": "start"},
            {"ts": _ts(0), "run_uuid": "r", "stage": "m2", "event": "start"},
            {"ts": _ts(50), "run_uuid": "r", "stage": "m6", "event": "start"},
        ],
    )
    rendered = render(collate(parse_sidecar(sidecar)))
    pos_m2 = rendered.index("m2 (")
    pos_m6 = rendered.index("m6 (")
    pos_m9 = rendered.index("m9 (")
    assert pos_m2 < pos_m6 < pos_m9


# --- tail() --------------------------------------------------------------


def test_tail_yields_on_new_event_then_stops_at_iterations(
    tmp_path: Path,
) -> None:
    """The bounded test hook stops the loop after N polls so the test
    suite stays deterministic without races."""
    sidecar = tmp_path / "events.jsonl"
    _write_sidecar(
        sidecar,
        [{"ts": _ts(0), "run_uuid": "r", "stage": "m9", "event": "start"}],
    )

    # Wrap the iterator so we can pull lazily. The tail loop renders on
    # each poll where the size changed; we append once and pull.
    iterator = tail(sidecar, poll_seconds=0.01, iterations=20)
    first = next(iterator)
    assert "m9" in first

    # Append a new event in another thread; the next yield reflects it.
    def appender() -> None:
        time.sleep(0.05)
        with sidecar.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {"ts": _ts(60), "run_uuid": "r", "stage": "m9", "event": "end"},
                    separators=(",", ":"),
                )
                + "\n"
            )

    thread = threading.Thread(target=appender)
    thread.start()
    try:
        # Drain until we see "ended" or run out of iterations.
        for rendered in iterator:
            if "ended" in rendered:
                break
    finally:
        thread.join()


# --- dataclass smoke -----------------------------------------------------


def test_phaseprogress_default_recent_is_independent() -> None:
    """Per-instance default factories don't share state across
    instances (a common dataclass pitfall)."""
    a = PhaseProgress(phase="phase1")
    b = PhaseProgress(phase="phase2")
    a.recent.append(
        StageEventRow(
            ts=datetime.now(UTC),
            run_uuid="r",
            stage="m9",
            event="progress",
        )
    )
    assert b.recent == []


def test_stagestatus_default_phases_is_independent() -> None:
    a = StageStatus(stage="m9")
    b = StageStatus(stage="m6")
    a.phases["phase1"] = PhaseProgress(phase="phase1")
    assert b.phases == {}
