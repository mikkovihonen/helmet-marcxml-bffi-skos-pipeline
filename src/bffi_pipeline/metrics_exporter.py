"""Prometheus exporter for the P-11 stage-events stream (Phase D).

Architecture: pipeline stages → ``stage-events.jsonl`` (Phase A) →
**this module's tail loop** → ``prometheus_client`` registry →
``/metrics`` HTTP endpoint → Prometheus scrapes → Grafana queries.
All local; no outbound telemetry. See
``docs/proposals/prop-11-structured-observability.md``.

The exporter is opt-in via a separate ``bffi-pipeline serve-metrics``
CLI subcommand. Pipeline stages never import this module — the cost
of the ``prometheus_client`` dep is paid only when an operator
explicitly starts the exporter alongside their pipeline run.

Metric vocabulary maps one-to-one with the Phase A event payload:
- ``bffi_stage_started_timestamp{stage, run_uuid}`` ← ``start``
- ``bffi_stage_entities_total{stage, phase}`` ← ``start`` /
  ``phase_boundary``
- ``bffi_stage_entities_processed_total{stage, phase}`` ← ``progress``
- ``bffi_stage_outcomes_total{stage, outcome}`` ← ``end`` (M9-specific
  outcome bucket counters)
- ``bffi_stage_throughput_per_minute{stage, phase}`` ← derived
- ``bffi_stage_eta_seconds{stage, phase}`` ← derived
- ``bffi_dependency_health{dep, port}`` ← ``health``
- ``bffi_dependency_probe_latency_ms{dep, port}`` ← ``health``
- ``bffi_watchdog_events_total{stage, event}`` ← ``watchdog``
- ``bffi_stage_ended_timestamp{stage, run_uuid}`` ← ``end``

The :class:`PipelineMetrics` dataclass owns the registry; the
``apply_event`` function takes one parsed :class:`StageEventRow` and
updates the right metric. Tests drive ``apply_event`` directly with
synthetic events; the production CLI wires it up to a tail loop.
"""

from __future__ import annotations

import io
import json as _json
import time
from dataclasses import dataclass, field
from datetime import datetime as _dt
from pathlib import Path
from typing import Final

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    start_http_server,
)

from bffi_pipeline.status import (
    StageEventRow,
    parse_sidecar,
)

#: Mapping of (stage, phase) → derived throughput/ETA history. Used to
#: compute per-minute throughput and ETA across successive progress
#: events for the same phase. Sized at the same default as the status
#: CLI's window.
_THROUGHPUT_WINDOW: Final[int] = 5


@dataclass
class PipelineMetrics:
    """Owns the Prometheus registry + all named metrics.

    One instance per ``serve-metrics`` invocation. Tests create their
    own instance to assert against the registry snapshot; the CLI
    creates one and threads it through the tail loop.
    """

    registry: CollectorRegistry = field(default_factory=CollectorRegistry)
    # _history[(stage, phase)] → list of (ts_unix, processed) for
    # throughput derivation. Bounded at _THROUGHPUT_WINDOW per key.
    _history: dict[tuple[str, str], list[tuple[float, int]]] = field(default_factory=dict)

    # Per-metric handles wired up in __post_init__.
    stage_started_ts: Gauge = field(init=False)
    stage_ended_ts: Gauge = field(init=False)
    stage_entities_total: Gauge = field(init=False)
    stage_entities_processed_total: Counter = field(init=False)
    stage_outcomes_total: Counter = field(init=False)
    stage_throughput_per_minute: Gauge = field(init=False)
    stage_eta_seconds: Gauge = field(init=False)
    dependency_health: Gauge = field(init=False)
    dependency_probe_latency_ms: Gauge = field(init=False)
    watchdog_events_total: Counter = field(init=False)

    def __post_init__(self) -> None:
        self.stage_started_ts = Gauge(
            "bffi_stage_started_timestamp",
            "Unix timestamp when the stage emitted its `start` event.",
            labelnames=("stage", "run_uuid"),
            registry=self.registry,
        )
        self.stage_ended_ts = Gauge(
            "bffi_stage_ended_timestamp",
            "Unix timestamp when the stage emitted its `end` event.",
            labelnames=("stage", "run_uuid"),
            registry=self.registry,
        )
        self.stage_entities_total = Gauge(
            "bffi_stage_entities_total",
            "Total entities the stage / phase is processing.",
            labelnames=("stage", "phase"),
            registry=self.registry,
        )
        self.stage_entities_processed_total = Counter(
            "bffi_stage_entities_processed_total",
            "Cumulative entities the stage / phase has processed.",
            labelnames=("stage", "phase"),
            registry=self.registry,
        )
        self.stage_outcomes_total = Counter(
            "bffi_stage_outcomes_total",
            "Per-outcome cumulative count (e.g. M9 tier counts).",
            labelnames=("stage", "outcome"),
            registry=self.registry,
        )
        self.stage_throughput_per_minute = Gauge(
            "bffi_stage_throughput_per_minute",
            "Recent throughput, derived from a rolling window of progress events.",
            labelnames=("stage", "phase"),
            registry=self.registry,
        )
        self.stage_eta_seconds = Gauge(
            "bffi_stage_eta_seconds",
            "Estimated seconds to phase boundary (or stage end).",
            labelnames=("stage", "phase"),
            registry=self.registry,
        )
        self.dependency_health = Gauge(
            "bffi_dependency_health",
            "Health probe verdict: 2 up, 1 degraded, 0 down.",
            labelnames=("stage", "dep"),
            registry=self.registry,
        )
        self.dependency_probe_latency_ms = Gauge(
            "bffi_dependency_probe_latency_ms",
            "Latency of the most recent dependency probe in milliseconds.",
            labelnames=("stage", "dep"),
            registry=self.registry,
        )
        self.watchdog_events_total = Counter(
            "bffi_watchdog_events_total",
            "Cumulative watchdog events emitted by the pipeline.",
            labelnames=("stage", "event"),
            registry=self.registry,
        )


#: Health-status string → numeric gauge mapping. Matches the Grafana
#: dashboard's state-timeline thresholds: 2 = up (green), 1 = degraded
#: (amber), 0 = down (red).
_HEALTH_STATUS_VALUE: Final[dict[str, int]] = {"up": 2, "degraded": 1, "down": 0}


def _update_throughput(
    metrics: PipelineMetrics,
    stage: str,
    phase: str,
    processed: int,
    total: int,
    ts_unix: float,
) -> None:
    """Update the rolling throughput history + the derived gauges."""
    key = (stage, phase)
    history = metrics._history.setdefault(key, [])
    history.append((ts_unix, processed))
    if len(history) > _THROUGHPUT_WINDOW:
        history.pop(0)
    if len(history) < 2:  # noqa: PLR2004 — need two samples to derive a rate
        return
    first_ts, first_processed = history[0]
    last_ts, last_processed = history[-1]
    elapsed = last_ts - first_ts
    delta = last_processed - first_processed
    if elapsed <= 0 or delta <= 0:
        return
    per_second = delta / elapsed
    metrics.stage_throughput_per_minute.labels(stage=stage, phase=phase).set(per_second * 60.0)
    remaining = total - last_processed
    if remaining > 0:
        metrics.stage_eta_seconds.labels(stage=stage, phase=phase).set(remaining / per_second)
    else:
        metrics.stage_eta_seconds.labels(stage=stage, phase=phase).set(0.0)


def apply_event(
    metrics: PipelineMetrics,
    row: StageEventRow,
) -> None:
    """Apply one parsed event to the Prometheus registry."""
    ts_unix = row.ts.timestamp()
    if row.event == "start":
        metrics.stage_started_ts.labels(stage=row.stage, run_uuid=row.run_uuid).set(ts_unix)
        if "total" in row.counters:
            metrics.stage_entities_total.labels(stage=row.stage, phase="_").set(
                row.counters["total"]
            )
    elif row.event == "phase_boundary" and row.phase is not None:
        if "total" in row.counters:
            metrics.stage_entities_total.labels(stage=row.stage, phase=row.phase).set(
                row.counters["total"]
            )
    elif row.event == "progress":
        phase = row.phase or "_"
        processed = int(row.counters.get("processed", 0))
        total = int(row.counters.get("total", 0))
        # ``processed`` from the event is cumulative within the phase;
        # Counter.inc() is incremental, so set the gauge instead and
        # let Prometheus compute rate() at query time. We use the
        # underlying ``_value`` setter so the cumulative semantics are
        # preserved across scrapes.
        metric = metrics.stage_entities_processed_total.labels(stage=row.stage, phase=phase)
        # prometheus_client doesn't expose a public Counter reset; use
        # the internal API to set it to the cumulative event value.
        metric._value.set(processed)
        if total > 0:
            metrics.stage_entities_total.labels(stage=row.stage, phase=phase).set(total)
        _update_throughput(metrics, row.stage, phase, processed, total, ts_unix)
    elif row.event == "end":
        metrics.stage_ended_ts.labels(stage=row.stage, run_uuid=row.run_uuid).set(ts_unix)
        # Per-outcome buckets — M9 emits a rich counters dict here.
        for outcome, value in row.counters.items():
            if outcome == "total":
                continue
            outcome_metric = metrics.stage_outcomes_total.labels(stage=row.stage, outcome=outcome)
            outcome_metric._value.set(int(value))
    elif row.event == "health":
        probes = row.extra.get("probes") or {}
        for dep, probe in probes.items():
            status_value = _HEALTH_STATUS_VALUE.get(probe.get("status"), 0)
            metrics.dependency_health.labels(stage=row.stage, dep=dep).set(status_value)
            metrics.dependency_probe_latency_ms.labels(stage=row.stage, dep=dep).set(
                probe.get("latency_ms", 0)
            )
    elif row.event == "watchdog":
        inner_event = row.extra.get("event") or "unknown"
        metrics.watchdog_events_total.labels(stage=row.stage, event=inner_event).inc()


def rehydrate(metrics: PipelineMetrics, sidecar_path: Path) -> int:
    """Replay the full sidecar into the registry at startup.

    Operators commonly start the exporter mid-pipeline-run (e.g. after
    seeing the operator's first ``what's the progress?`` instinct
    fire). Rehydration lets the dashboard show the run's history-to-
    date rather than only events after the exporter started.

    Returns the number of events applied.
    """
    rows = parse_sidecar(sidecar_path)
    for row in rows:
        apply_event(metrics, row)
    return len(rows)


@dataclass
class _TailState:
    """Bookkeeping for the file-tail loop."""

    last_size: int = 0
    last_pos: int = 0


def _tail_step(metrics: PipelineMetrics, sidecar_path: Path, state: _TailState) -> int:
    """One iteration of the tail loop. Returns the number of events
    applied this step.

    Reads only the bytes appended since the last call (using stored
    ``last_pos``). Lines that don't parse are skipped (lenient — Phase
    A's writer is atomic per-line but a torn write would still leave
    a partial last line during scrape).
    """
    if not sidecar_path.is_file():
        return 0
    size = sidecar_path.stat().st_size
    if size < state.last_pos:
        # File was truncated / rotated. Reset and re-read from the
        # start so we don't miss the new content. ``<`` (not ``<=``)
        # because ``size == last_pos`` is the *idle poll* case where
        # nothing was appended — using ``<=`` here would re-apply the
        # whole sidecar on every quiet tick and ``Counter.inc()`` is
        # cumulative, so the bug surfaced as ~1 165x inflation in the
        # mid-bench dashboard on 2026-05-13. See P-12 Phase A.
        state.last_pos = 0
    if size == state.last_pos:
        return 0
    with sidecar_path.open("rb") as fh:
        fh.seek(state.last_pos)
        new_bytes = fh.read()
        state.last_pos = fh.tell()
    text = new_bytes.decode("utf-8", errors="replace")
    state.last_size = size
    applied = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            data = _json.loads(line)
            ts = _dt.fromisoformat(data["ts"].replace("Z", "+00:00"))
        except (ValueError, KeyError):
            continue
        row = StageEventRow(
            ts=ts,
            run_uuid=str(data.get("run_uuid", "")),
            stage=str(data.get("stage", "")),
            event=str(data.get("event", "")),
            phase=data.get("phase"),
            counters=dict(data.get("counters") or {}),
            extra=dict(data.get("extra") or {}),
        )
        apply_event(metrics, row)
        applied += 1
    _ = io  # reserved for future bytes-streaming refactor
    return applied


def serve(
    sidecar_path: Path,
    *,
    port: int = 9100,
    poll_seconds: float = 1.0,
    iterations: int | None = None,
    metrics: PipelineMetrics | None = None,
) -> None:
    """Run the exporter: rehydrate, then tail forever serving ``/metrics``.

    ``iterations`` is the test hook — bounds the tail loop so the
    test suite stays deterministic. Production CLI passes ``None``
    for unbounded.
    """
    if metrics is None:
        metrics = PipelineMetrics()
    rehydrate(metrics, sidecar_path)
    state = _TailState(last_pos=sidecar_path.stat().st_size if sidecar_path.is_file() else 0)

    # ``start_http_server`` spawns a daemon-thread HTTP server bound
    # to 0.0.0.0 by default — perfect for the local-only deployment
    # where Prometheus in the sibling Docker container scrapes the
    # host via ``host.docker.internal``.
    server, server_thread = start_http_server(port, registry=metrics.registry)
    _ = server_thread  # we don't manage the daemon thread; it dies with the process
    try:
        count = 0
        while iterations is None or count < iterations:
            _tail_step(metrics, sidecar_path, state)
            time.sleep(poll_seconds)
            count += 1
    finally:
        server.shutdown()


__all__ = [
    "PipelineMetrics",
    "apply_event",
    "rehydrate",
    "serve",
]
