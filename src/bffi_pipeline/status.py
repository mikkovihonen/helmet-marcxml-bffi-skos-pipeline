"""``bffi-pipeline status`` — read the P-11 stage-events.jsonl sidecar
and render the pipeline's current state (P-11 Phase B).

This module is the *consumer* counterpart to
:mod:`bffi_pipeline.stages.observability`. The producer side emits
events; this side parses the JSONL stream, collates per-stage state,
and renders paste-ready text. Same module can also tail the sidecar
for live re-rendering.

Per the plan, this consumer is deliberately separate from the
``stages/`` package — the events module produces, status consumes.
Keeping the boundary explicit lets Phase D's Prometheus exporter
reuse the parse + collate without dragging in the stages package.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

#: Window for throughput / ETA derivation — the last N progress events
#: in each (stage, phase) bucket. Sparse cadences (M9 at 200 entities
#: per progress event) mean N=5 covers ~1k items of recent history; at
#: M2's 100-cadence that's ~500 records — enough to smooth jitter
#: without being so wide it lags real throughput changes.
_PROGRESS_WINDOW: Final[int] = 5

#: Time-format unit constants used by ``_format_elapsed``. Avoid magic
#: ``60`` literals in the comparisons so ruff's PLR2004 doesn't flag
#: every wall-clock-formatting helper as suspect.
_SECONDS_PER_MINUTE: Final[int] = 60
_MINUTES_PER_HOUR: Final[int] = 60

#: Minimum events needed to derive a throughput / ETA from a phase's
#: progress history (first/last subtraction). Below this, leave both
#: as ``None``.
_MIN_PROGRESS_EVENTS_FOR_THROUGHPUT: Final[int] = 2


@dataclass(frozen=True)
class StageEventRow:
    """One parsed row of ``stage-events.jsonl``.

    Mirrors the payload shape :class:`StageEventEmitter` writes. All
    optional fields default to empty / None for ergonomic access at
    the call site.
    """

    ts: datetime
    run_uuid: str
    stage: str
    event: str
    phase: str | None = None
    counters: dict[str, int] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PhaseProgress:
    """Per-stage, per-phase progress snapshot.

    M9 has phases (``phase1`` / ``phase2`` / ``phase3``); other stages
    use ``phase=None`` and store a single PhaseProgress under the key
    ``"_"`` in :class:`StageStatus.phases`.
    """

    phase: str | None
    processed: int = 0
    total: int = 0
    throughput_per_minute: float | None = None
    eta_seconds: float | None = None
    # Most recent few progress events for throughput derivation. Bounded
    # at _PROGRESS_WINDOW.
    recent: list[StageEventRow] = field(default_factory=list)


@dataclass
class StageStatus:
    """Collated state for one stage at the snapshot moment."""

    stage: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    elapsed_seconds: float | None = None
    phases: dict[str, PhaseProgress] = field(default_factory=dict)
    latest_phase: str | None = None
    watchdog_events: dict[str, int] = field(default_factory=dict)
    last_health: dict[str, Any] | None = None
    final_counters: dict[str, int] = field(default_factory=dict)


def parse_sidecar(
    sidecar_path: Path,
    *,
    since: datetime | None = None,
    run_uuid: str | None = None,
) -> list[StageEventRow]:
    """Parse the JSONL sidecar into typed rows, oldest-first.

    ``since`` filters out rows with ``ts < since``. ``run_uuid``
    filters to a single run (None = all runs in the file). Lines that
    fail to parse are skipped silently — the sidecar is JSONL but
    tools writing to it can leave a partial last line during tail.
    """
    if not sidecar_path.is_file():
        return []
    rows: list[StageEventRow] = []
    for line in sidecar_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            ts = datetime.fromisoformat(data["ts"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if since is not None and ts < since:
            continue
        if run_uuid is not None and data.get("run_uuid") != run_uuid:
            continue
        rows.append(
            StageEventRow(
                ts=ts,
                run_uuid=str(data.get("run_uuid", "")),
                stage=str(data.get("stage", "")),
                event=str(data.get("event", "")),
                phase=data.get("phase"),
                counters=dict(data.get("counters") or {}),
                extra=dict(data.get("extra") or {}),
            )
        )
    return rows


def _compute_throughput(recent: list[StageEventRow]) -> tuple[float | None, float | None]:
    """Return ``(throughput_per_minute, eta_seconds)`` from recent events.

    Throughput is derived from the first/last events in the window:
    ``(processed_last - processed_first) / (ts_last - ts_first)``. ETA
    is ``(total - processed_last) / throughput``. Both are ``None``
    when fewer than 2 events or zero elapsed (avoid divide-by-zero).
    """
    if len(recent) < _MIN_PROGRESS_EVENTS_FOR_THROUGHPUT:
        return None, None
    first = recent[0]
    last = recent[-1]
    elapsed = (last.ts - first.ts).total_seconds()
    if elapsed <= 0:
        return None, None
    processed_first = int(first.counters.get("processed", 0))
    processed_last = int(last.counters.get("processed", 0))
    delta = processed_last - processed_first
    if delta <= 0:
        return None, None
    per_second = delta / elapsed
    per_minute = per_second * 60.0
    total = int(last.counters.get("total", 0))
    remaining = total - processed_last
    eta = remaining / per_second if remaining > 0 and per_second > 0 else 0.0
    return per_minute, eta


def collate(events: list[StageEventRow]) -> dict[str, StageStatus]:  # noqa: PLR0912 — single switch over event type; splitting into per-event handlers would scatter the StageStatus mutation state and obscure the reading order.
    """Reduce a list of events to per-stage status snapshots.

    The grouping rule for M9-style multi-phase stages: each
    ``phase_boundary`` event opens a new phase; subsequent
    ``progress`` events go into that phase's bucket until the next
    ``phase_boundary`` or ``end``. Single-phase stages use the
    sentinel phase key ``"_"``.

    Watchdog events (forwarded by P-11 Phase A's absorption layer)
    accumulate under the originating stage. M6/M9 events appear under
    ``"m6"`` / ``"m9"``; the absorption layer sets ``stage="watchdog"``,
    so the count lives there for now. Future iteration: read the
    nested ``extra["pair_id"]`` to attribute to the correct stage.
    """
    statuses: dict[str, StageStatus] = {}
    for row in events:
        status = statuses.setdefault(row.stage, StageStatus(stage=row.stage))
        if row.event == "start":
            status.started_at = row.ts
            # Don't create a phase bucket from the start event itself —
            # the first phase_boundary / progress row decides which
            # bucket gets the total. Stages without phases (M2, M3,
            # M6 etc.) lazily seed the sentinel ``"_"`` bucket via
            # their progress events.
        elif row.event == "phase_boundary" and row.phase is not None:
            status.latest_phase = row.phase
            phase = status.phases.setdefault(row.phase, PhaseProgress(phase=row.phase))
            if "total" in row.counters:
                phase.total = int(row.counters["total"])
        elif row.event == "progress":
            phase_key = row.phase or "_"
            if phase_key == "_" and status.latest_phase is not None:
                # M9 progress carries phase; M2/M3 progress doesn't. The
                # ``or "_"`` guards the M2/M3 path so they don't accidentally
                # collide with M9's phase-keyed bucket.
                phase_key = status.latest_phase
            phase = status.phases.setdefault(phase_key, PhaseProgress(phase=row.phase))
            if "processed" in row.counters:
                phase.processed = int(row.counters["processed"])
            if "total" in row.counters:
                phase.total = int(row.counters["total"])
            phase.recent.append(row)
            if len(phase.recent) > _PROGRESS_WINDOW:
                phase.recent = phase.recent[-_PROGRESS_WINDOW:]
            phase.throughput_per_minute, phase.eta_seconds = _compute_throughput(phase.recent)
        elif row.event == "end":
            status.ended_at = row.ts
            if row.counters:
                status.final_counters = dict(row.counters)
        elif row.event == "health":
            status.last_health = dict(row.extra)
        elif row.event == "watchdog":
            inner_event = row.extra.get("event") or "unknown"
            status.watchdog_events[inner_event] = status.watchdog_events.get(inner_event, 0) + 1

    # Elapsed seconds for every stage that has a started_at; uses
    # ended_at when set (stage finished) or "now" otherwise.
    now = datetime.now(UTC)
    for status in statuses.values():
        if status.started_at is None:
            continue
        endpoint = status.ended_at or now
        status.elapsed_seconds = (endpoint - status.started_at).total_seconds()
    return statuses


def _format_elapsed(seconds: float) -> str:
    """Render seconds as ``HhMMmSSs`` or ``MMm:SSs`` (short and parseable)."""
    if seconds < _SECONDS_PER_MINUTE:
        return f"{seconds:.0f}s"
    minutes, sec = divmod(int(seconds), _SECONDS_PER_MINUTE)
    if minutes < _MINUTES_PER_HOUR:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, _MINUTES_PER_HOUR)
    return f"{hours}h{minutes:02d}m{sec:02d}s"


def _progress_bar(processed: int, total: int, width: int = 20) -> str:
    """ASCII progress bar like ``████████░░░░`` plus ``N / M`` and ``%``."""
    if total <= 0:
        return f"({processed} done; total unknown)"
    pct = max(0.0, min(1.0, processed / total))
    filled = int(pct * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"{bar}  {processed:,} / {total:,}  ({pct * 100:.0f}%)"


def _render_stage(status: StageStatus) -> list[str]:
    """One stage's section of the rendered output."""
    lines: list[str] = []
    if status.started_at is None:
        lines.append(f"{status.stage} (no events seen)")
        return lines

    started_text = status.started_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    elapsed_text = _format_elapsed(status.elapsed_seconds or 0.0)
    final_marker = " — ended" if status.ended_at is not None else ""
    lines.append(f"{status.stage} (started {started_text}, elapsed {elapsed_text}{final_marker})")

    # Per-phase progress.
    for phase_key, phase in status.phases.items():
        label = "  " + (phase_key if phase_key != "_" else "progress").ljust(8)
        bar = _progress_bar(phase.processed, phase.total)
        eta_text = ""
        if phase.eta_seconds is not None and phase.eta_seconds > 0:
            eta_text = f", ~{_format_elapsed(phase.eta_seconds)} ETA"
        thru_text = ""
        if phase.throughput_per_minute is not None:
            thru_text = f", {phase.throughput_per_minute:.0f}/min"
        lines.append(f"{label} {bar}{thru_text}{eta_text}")

    # Final counters (when the stage has ended) — render compactly.
    if status.ended_at is not None and status.final_counters:
        summary = ", ".join(f"{k}={v:,}" for k, v in status.final_counters.items())
        lines.append(f"  summary  {summary}")

    # Health probe verdict (Phase C populates this).
    if status.last_health is not None:
        health_text = json.dumps(status.last_health, separators=(",", ":"))
        lines.append(f"  health   {health_text}")

    # Watchdog event tally — even zero is useful information (says
    # the wiring fires).
    if status.watchdog_events:
        wd = ", ".join(f"{k}={v:,}" for k, v in status.watchdog_events.items())
        lines.append(f"  watchdog {wd}")
    return lines


def render(statuses: dict[str, StageStatus]) -> str:
    """Render the full multi-stage status as paste-ready text."""
    if not statuses:
        return "(no stage events recorded yet)"
    # Render in deterministic order: by start time (oldest first), then
    # by stage name as tie-breaker.
    items = sorted(
        statuses.values(),
        key=lambda s: (s.started_at or datetime(9999, 12, 31, tzinfo=UTC), s.stage),
    )
    sections: list[str] = []
    for status in items:
        sections.append("\n".join(_render_stage(status)))
    return "\n\n".join(sections)


def tail(
    sidecar_path: Path,
    *,
    since: datetime | None = None,
    run_uuid: str | None = None,
    poll_seconds: float = 0.2,
    iterations: int | None = None,
) -> Iterator[str]:
    """Yield a freshly-rendered status string each time new events appear.

    Polling-based (no ``inotify`` / ``fsevents`` dependency); 200 ms
    cadence by default. Picks up appended-to files via length growth.

    ``iterations`` is the test hook — pass an int to bound the loop;
    production CLI passes ``None`` for unbounded.
    """
    last_size = -1
    last_rendered: str | None = None
    count = 0
    while iterations is None or count < iterations:
        size = sidecar_path.stat().st_size if sidecar_path.is_file() else 0
        if size != last_size:
            last_size = size
            rows = parse_sidecar(sidecar_path, since=since, run_uuid=run_uuid)
            rendered = render(collate(rows))
            if rendered != last_rendered:
                last_rendered = rendered
                yield rendered
        count += 1
        time.sleep(poll_seconds)


__all__ = [
    "PhaseProgress",
    "StageEventRow",
    "StageStatus",
    "collate",
    "parse_sidecar",
    "render",
    "tail",
]
