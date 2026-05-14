"""Prometheus exporter for the P-11 stage-events stream (Phase D).

Architecture: pipeline stages → ``stage-events.jsonl`` (Phase A) →
**this module's tail loop** → ``prometheus_client`` registry →
``/metrics`` HTTP endpoint → Prometheus scrapes → Grafana queries.
All local; no outbound telemetry. See
``docs/plans/completed/p-11-structured-observability.md``.

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

import atexit as _atexit
import contextlib
import glob as _glob
import io
import json as _json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime as _dt
from pathlib import Path
from typing import Any, Final

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
    # _history[(stage, phase, run_uuid)] → list of (ts_unix, processed)
    # for throughput derivation. Bounded at _THROUGHPUT_WINDOW per key.
    # P-13 Phase A: ``run_uuid`` is part of the key so two runs don't
    # share a rolling-window history (which would inflate throughput at
    # run boundaries).
    _history: dict[tuple[str, str, str], list[tuple[float, int]]] = field(default_factory=dict)

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
    dependency_last_probe_timestamp: Gauge = field(init=False)
    watchdog_events_total: Counter = field(init=False)
    stage_errors_total: Counter = field(init=False)
    stage_planned: Gauge = field(init=False)

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
        # P-13 Phase A: every Counter + Gauge carries ``run_uuid`` so
        # the dashboard can scope every panel to the active invocation
        # via ``run_uuid="$active_run"``. The two timestamp gauges
        # above already had ``run_uuid``; this brings the rest in line.
        self.stage_entities_total = Gauge(
            "bffi_stage_entities_total",
            "Total entities the stage / phase is processing.",
            labelnames=("stage", "phase", "run_uuid"),
            registry=self.registry,
        )
        self.stage_entities_processed_total = Counter(
            "bffi_stage_entities_processed_total",
            "Cumulative entities the stage / phase has processed.",
            labelnames=("stage", "phase", "run_uuid"),
            registry=self.registry,
        )
        self.stage_outcomes_total = Counter(
            "bffi_stage_outcomes_total",
            "Per-outcome cumulative count (e.g. M9 tier counts).",
            labelnames=("stage", "outcome", "run_uuid"),
            registry=self.registry,
        )
        self.stage_throughput_per_minute = Gauge(
            "bffi_stage_throughput_per_minute",
            "Recent throughput, derived from a rolling window of progress events.",
            labelnames=("stage", "phase", "run_uuid"),
            registry=self.registry,
        )
        self.stage_eta_seconds = Gauge(
            "bffi_stage_eta_seconds",
            "Estimated seconds to phase boundary (or stage end).",
            labelnames=("stage", "phase", "run_uuid"),
            registry=self.registry,
        )
        self.dependency_health = Gauge(
            "bffi_dependency_health",
            "Health probe verdict: 2 up, 1 degraded, 0 down, NaN not_configured.",
            labelnames=("stage", "dep", "run_uuid"),
            registry=self.registry,
        )
        self.dependency_probe_latency_ms = Gauge(
            "bffi_dependency_probe_latency_ms",
            "Latency of the most recent dependency probe in milliseconds.",
            labelnames=("stage", "dep", "run_uuid"),
            registry=self.registry,
        )
        # P-12 Phase C: timestamp of the most recent health event per
        # (stage, dep) so the dashboard can compute freshness and grey
        # out stale gauges (>60 s) instead of presenting them as if
        # they were live.
        self.dependency_last_probe_timestamp = Gauge(
            "bffi_dependency_last_probe_timestamp",
            "Unix timestamp of the most recent `health` event for this stage / dep.",
            labelnames=("stage", "dep", "run_uuid"),
            registry=self.registry,
        )
        self.watchdog_events_total = Counter(
            "bffi_watchdog_events_total",
            "Cumulative watchdog events emitted by the pipeline.",
            labelnames=("stage", "event", "run_uuid"),
            registry=self.registry,
        )
        # P-12 follow-up (Option B from the 2026-05-13 live dashboard
        # session): per-stage typed-error counter sourced from the on-
        # disk error JSONLs that M2 / M3 already write. One bar per
        # ``(stage, error_type)`` lets the dashboard surface mid-run
        # spikes of e.g. ``marcxml-content-minimum`` failures while
        # the stage is still running, rather than waiting for the
        # stage's ``end`` event to roll up an aggregate. Legacy error
        # rows without a ``run_uuid`` field surface under ``run_uuid=""``.
        self.stage_errors_total = Counter(
            "bffi_stage_errors_total",
            "Cumulative per-stage typed errors (one tick per row in the stage's error JSONL).",
            labelnames=("stage", "error_type", "run_uuid"),
            registry=self.registry,
        )
        # P-12 follow-up: runner-script-emitted "plan" event sets this
        # gauge to 1 for every stage the script intends to run. The
        # dashboard then distinguishes "skipped" (planned=0, no events)
        # from "pending" (planned=1, no start yet) when rendering the
        # 8 stage state tiles. Direct ``bffi-pipeline <subcmd>`` calls
        # don't emit a plan, so non-active stages stay correctly
        # ``skipped`` in their dashboard view.
        self.stage_planned = Gauge(
            "bffi_stage_planned",
            "1 if the runner script declared this stage as part of the planned run; else absent.",
            labelnames=("stage", "run_uuid"),
            registry=self.registry,
        )
        # Free-text run description set via ``bffi-pipeline plan
        # --description "..."`` (forwarded to ``emit_plan(..., description=)``).
        # Stored as a label so the dashboard's templating layer can
        # extract it via ``label_values()``. Value is always 1 — the
        # gauge exists only to carry the label.
        self.run_description = Gauge(
            "bffi_run_description",
            "Free-text description of the run, set by the runner script.",
            labelnames=("run_uuid", "description"),
            registry=self.registry,
        )


#: Per-stage outcome keys that may appear in a ``progress`` event's
#: ``extra`` dict. When present, the exporter mirrors them into
#: ``bffi_stage_outcomes_total`` so the dashboard's outcome bargauge
#: populates live during the run, not only after the ``end`` event.
#: M9 emits these from its Phase 1 and Phase 2 loops; M6 emits its
#: own set via the same mechanism. The set is bounded so a stray
#: ``extra`` key (like ``deferred_to_picker``) doesn't accidentally
#: create new high-cardinality outcome series.
_PROGRESS_OUTCOME_KEYS: Final[frozenset[str]] = frozenset(
    {
        # M9
        "local",
        "lexical",
        "llm_pick",
        "fallback",
        "no_candidate",
        "fictional",
        "watchdog_aborted",
        # M6 (when its progress events grow extra outcome fields)
        "cache_hits",
        "fresh_calls",
        "cascade_used",
        "auto_merged",
    }
)


#: Health-status string → numeric gauge mapping. Matches the Grafana
#: dashboard's state-timeline thresholds: 2 = up (green), 1 = degraded
#: (amber), 0 = down (red). P-12 Phase B adds ``not_configured`` →
#: ``NaN`` so the dashboard can grey out cells for deps that aren't
#: provisioned on this host instead of colouring them red. Default
#: for unknown statuses stays at 0 (down) so a typoed status doesn't
#: silently grey out a real outage.
_HEALTH_STATUS_VALUE: Final[dict[str, float]] = {
    "up": 2.0,
    "degraded": 1.0,
    "down": 0.0,
    "not_configured": float("nan"),
}


def _update_throughput(
    metrics: PipelineMetrics,
    stage: str,
    phase: str,
    processed: int,
    total: int,
    ts_unix: float,
    run_uuid: str,
) -> None:
    """Update the rolling throughput history + the derived gauges."""
    key = (stage, phase, run_uuid)
    history = metrics._history.setdefault(key, [])
    history.append((ts_unix, processed))
    if len(history) > _THROUGHPUT_WINDOW:
        history.pop(0)
    # Phase-complete bypass: when ``processed`` has caught up to ``total``
    # the phase is done and the ETA must read zero. Without this the
    # throughput-driven branch below can return early (e.g. M9 Phase 1's
    # collation loop emits all cadence events within the same wall-clock
    # second so ``elapsed=0``) and leave the gauge stuck at a transient
    # sample value from earlier in the phase.
    if total > 0 and processed >= total:
        metrics.stage_eta_seconds.labels(stage=stage, phase=phase, run_uuid=run_uuid).set(0.0)
    if len(history) < 2:  # noqa: PLR2004 — need two samples to derive a rate
        return
    first_ts, first_processed = history[0]
    last_ts, last_processed = history[-1]
    elapsed = last_ts - first_ts
    delta = last_processed - first_processed
    if elapsed <= 0 or delta <= 0:
        return
    per_second = delta / elapsed
    metrics.stage_throughput_per_minute.labels(stage=stage, phase=phase, run_uuid=run_uuid).set(
        per_second * 60.0
    )
    remaining = total - last_processed
    if remaining > 0:
        metrics.stage_eta_seconds.labels(stage=stage, phase=phase, run_uuid=run_uuid).set(
            remaining / per_second
        )
    else:
        metrics.stage_eta_seconds.labels(stage=stage, phase=phase, run_uuid=run_uuid).set(0.0)


def apply_event(  # noqa: PLR0912 — dispatch table over the StageEvent enum; flattening it into helpers fragments the row-context state needed for throughput / dependency labels.
    metrics: PipelineMetrics,
    row: StageEventRow,
) -> None:
    """Apply one parsed event to the Prometheus registry.

    P-13 Phase A: every metric is labelled with ``row.run_uuid`` so
    dashboards can scope queries to the active invocation via
    ``run_uuid="$active_run"``. Legacy events without a ``run_uuid``
    field surface under ``run_uuid=""`` (Pydantic / StageEventRow's
    empty-string default), visually distinguishable from real runs
    in PromQL queries.
    """
    ts_unix = row.ts.timestamp()
    run = row.run_uuid
    if row.event == "start":
        metrics.stage_started_ts.labels(stage=row.stage, run_uuid=run).set(ts_unix)
        if "total" in row.counters:
            metrics.stage_entities_total.labels(stage=row.stage, phase="_", run_uuid=run).set(
                row.counters["total"]
            )
    elif row.event == "phase_boundary" and row.phase is not None:
        if "total" in row.counters:
            metrics.stage_entities_total.labels(stage=row.stage, phase=row.phase, run_uuid=run).set(
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
        metric = metrics.stage_entities_processed_total.labels(
            stage=row.stage, phase=phase, run_uuid=run
        )
        # prometheus_client doesn't expose a public Counter reset; use
        # the internal API to set it to the cumulative event value.
        metric._value.set(processed)
        if total > 0:
            metrics.stage_entities_total.labels(stage=row.stage, phase=phase, run_uuid=run).set(
                total
            )
        _update_throughput(metrics, row.stage, phase, processed, total, ts_unix, run)
        # Mid-run per-outcome counters — Phase 1 / Phase 2 of M9 emit
        # cumulative tier counts in ``extra`` (local, lexical, llm_pick,
        # fallback, no_candidate, fictional, watchdog_aborted) so the
        # outcome bargauge populates live instead of jumping from empty
        # to fully populated at the ``end`` event. Bounded by
        # ``_PROGRESS_OUTCOME_KEYS`` to keep cardinality fixed.
        for outcome in _PROGRESS_OUTCOME_KEYS:
            if outcome not in row.extra:
                continue
            try:
                value = int(row.extra[outcome])
            except (TypeError, ValueError):
                continue
            metrics.stage_outcomes_total.labels(
                stage=row.stage, outcome=outcome, run_uuid=run
            )._value.set(value)
    elif row.event == "end":
        metrics.stage_ended_ts.labels(stage=row.stage, run_uuid=run).set(ts_unix)
        # Per-outcome buckets — M9 emits a rich counters dict here.
        for outcome, value in row.counters.items():
            if outcome == "total":
                continue
            outcome_metric = metrics.stage_outcomes_total.labels(
                stage=row.stage, outcome=outcome, run_uuid=run
            )
            outcome_metric._value.set(int(value))
    elif row.event == "health":
        probes = row.extra.get("probes") or {}
        for dep, probe in probes.items():
            status_value = _HEALTH_STATUS_VALUE.get(probe.get("status"), 0.0)
            metrics.dependency_health.labels(stage=row.stage, dep=dep, run_uuid=run).set(
                status_value
            )
            metrics.dependency_probe_latency_ms.labels(stage=row.stage, dep=dep, run_uuid=run).set(
                probe.get("latency_ms", 0)
            )
            # P-12 Phase C: per-(stage, dep) freshness gauge for the
            # dashboard's stale-detection overlay.
            metrics.dependency_last_probe_timestamp.labels(
                stage=row.stage, dep=dep, run_uuid=run
            ).set(row.ts.timestamp())
    elif row.event == "watchdog":
        inner_event = row.extra.get("event") or "unknown"
        metrics.watchdog_events_total.labels(stage=row.stage, event=inner_event, run_uuid=run).inc()
    elif row.event == "plan":
        # Runner-script "plan" event: extra.stages lists every stage the
        # invocation intends to run. Set bffi_stage_planned=1 for each
        # so the dashboard's state-tile expression can distinguish
        # ``pending`` (planned, not yet started) from ``skipped`` (not
        # in plan at all).
        planned_stages = row.extra.get("stages") or []
        if isinstance(planned_stages, list):
            for stage in planned_stages:
                if isinstance(stage, str):
                    metrics.stage_planned.labels(stage=stage, run_uuid=run).set(1)
        description = row.extra.get("description") or ""
        if isinstance(description, str) and description:
            metrics.run_description.labels(run_uuid=run, description=description).set(1)


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


# --- Error-file tail (P-12 Phase B follow-up, Option B) -------------------
#
# M2 writes one JSONL row per failed record to ``<data_dir>/bibframe/_errors.jsonl``;
# M3 writes Boundary-3 SHACL failures to ``<data_dir>/bffi/_validation.jsonl``.
# These files already encode error type (``error_type`` for M2; M3
# entries are uniformly ``boundary-3``). The tail loop below
# increments ``bffi_stage_errors_total`` per row, giving the dashboard
# mid-run visibility without waiting for the stage's ``end`` event.


@dataclass(frozen=True)
class _ErrorFileSpec:
    """One on-disk error JSONL file the exporter tails."""

    stage: str
    path: Path
    #: How to derive ``error_type`` for one row. M2 reads its own
    #: ``error_type`` field; M3 doesn't carry one so we hard-code
    #: ``boundary-3``. Future per-stage extensions add their own.
    type_for_row: Callable[[dict[str, Any]], str]


@dataclass
class _ErrorFileTailState:
    """Per-file bookkeeping, mirrors ``_TailState`` for the events sidecar."""

    last_pos: int = 0


def _default_error_specs(data_dir: Path) -> list[_ErrorFileSpec]:
    """Default set of (stage, path, type_for_row) the exporter tails.

    Operators can extend this by passing a custom list to
    :func:`serve` if they introduce new stages with on-disk error
    streams.
    """
    return [
        _ErrorFileSpec(
            stage="m2",
            path=data_dir / "bibframe" / "_errors.jsonl",
            type_for_row=lambda row: str(row.get("error_type") or "unknown"),
        ),
        _ErrorFileSpec(
            stage="m3",
            # Every row in _validation.jsonl is a Boundary-3 SHACL
            # failure by construction (the file is the SHACL validator
            # output). Hard-code the label.
            path=data_dir / "bffi" / "_validation.jsonl",
            type_for_row=lambda _row: "boundary-3",
        ),
    ]


def _error_specs_for_sidecar(sidecar_path: Path) -> list[_ErrorFileSpec]:
    """P-17 — derive a per-sidecar error-spec pair from the sidecar's parent dir.

    The pipeline convention is ``<BFFI_DATA_DIR>/stage-events.jsonl``,
    ``<BFFI_DATA_DIR>/bibframe/_errors.jsonl``,
    ``<BFFI_DATA_DIR>/bffi/_validation.jsonl`` all together. With
    multi-sidecar tailing (one exporter watching multiple bench runs)
    a single global ``data_dir`` is the wrong shape — each sidecar
    has its OWN co-located error files. This derives the pair from
    the sidecar's ``parent``.
    """
    return _default_error_specs(sidecar_path.parent)


def _tail_error_step(
    metrics: PipelineMetrics, spec: _ErrorFileSpec, state: _ErrorFileTailState
) -> int:
    """One iteration of the error-file tail loop.

    Mirrors :func:`_tail_step` but increments
    ``bffi_stage_errors_total`` instead of dispatching to
    ``apply_event``. Returns the number of error rows applied this
    step.
    """
    path = spec.path
    if not path.is_file():
        return 0
    size = path.stat().st_size
    if size < state.last_pos:
        state.last_pos = 0
    if size == state.last_pos:
        return 0
    with path.open("rb") as fh:
        fh.seek(state.last_pos)
        new_bytes = fh.read()
        state.last_pos = fh.tell()
    text = new_bytes.decode("utf-8", errors="replace")
    applied = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = _json.loads(line)
        except ValueError:
            continue
        error_type = spec.type_for_row(row)
        run_uuid = str(row.get("run_uuid") or "")
        metrics.stage_errors_total.labels(
            stage=spec.stage, error_type=error_type, run_uuid=run_uuid
        ).inc()
        applied += 1
    return applied


def rehydrate_error_files(
    metrics: PipelineMetrics, specs: list[_ErrorFileSpec]
) -> dict[str, _ErrorFileTailState]:
    """Replay every error file from byte 0 at startup, returning the
    per-file tail-state map so :func:`serve` can continue from where
    rehydration ended.
    """
    states: dict[str, _ErrorFileTailState] = {}
    for spec in specs:
        st = _ErrorFileTailState(last_pos=0)
        _tail_error_step(metrics, spec, st)
        states[str(spec.path)] = st
    return states


def _rescan_globs(watch_globs: list[str], already_attached: set[Path]) -> list[Path]:
    """P-17 — return glob matches not yet in ``already_attached``.

    Uses ``glob.glob`` (stdlib) so both CWD-relative AND absolute
    patterns are accepted — ``Path('').glob`` rejects the latter.
    ``recursive=True`` enables ``**`` matching. Results are sorted
    for stable startup-log ordering. Best-effort: a glob that walks
    a directory the operator doesn't have read access to silently
    yields nothing for that branch.
    """
    discovered: set[Path] = set()
    for pattern in watch_globs:
        for match_str in _glob.glob(pattern, recursive=True):
            match = Path(match_str)
            try:
                resolved = match.resolve()
            except OSError:
                continue
            if resolved.is_file():
                discovered.add(resolved)
    return sorted(discovered - already_attached)


def _attach_sidecar(
    metrics: PipelineMetrics,
    sidecar_path: Path,
    tail_states: dict[Path, _TailState],
    error_specs_by_sidecar: dict[Path, list[_ErrorFileSpec]],
    error_states: dict[str, _ErrorFileTailState],
) -> None:
    """P-17 — rehydrate one sidecar + its co-located error files; install state."""
    rehydrate(metrics, sidecar_path)
    tail_states[sidecar_path] = _TailState(
        last_pos=sidecar_path.stat().st_size if sidecar_path.is_file() else 0
    )
    specs = _error_specs_for_sidecar(sidecar_path)
    error_specs_by_sidecar[sidecar_path] = specs
    for spec in specs:
        st = _ErrorFileTailState(last_pos=0)
        _tail_error_step(metrics, spec, st)
        error_states[str(spec.path)] = st


def _write_exporter_pid_files(pid_file: Path | None, argv: list[str] | None) -> None:
    """P-32 Phase G: write ``.exporter.pid`` + (optional) ``.exporter.argv``.

    ``bffi-pipeline runs prune --apply --reset-exporter`` reads the
    PID file to SIGTERM this process and the argv file to optionally
    relaunch with the same args. Atexit cleanup removes both files
    on graceful exit.
    """
    if pid_file is None:
        return
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
    if argv is not None:
        argv_file = pid_file.with_name(".exporter.argv")
        argv_file.write_text("\n".join(argv) + "\n", encoding="utf-8")
    _atexit.register(_cleanup_exporter_pid_files, pid_file)


def _cleanup_exporter_pid_files(pid_file: Path) -> None:
    """P-32 Phase G: atexit hook to remove ``.exporter.pid`` + ``.exporter.argv``.

    Best-effort: silently swallows OSError so a missing-file race
    (operator deleted them; we re-init from scratch) doesn't pollute
    the shutdown path.
    """
    for path in (pid_file, pid_file.with_name(".exporter.argv")):
        with contextlib.suppress(OSError):
            path.unlink()


def serve(
    sidecar_paths: list[Path],
    *,
    port: int = 9100,
    poll_seconds: float = 1.0,
    iterations: int | None = None,
    metrics: PipelineMetrics | None = None,
    watch_globs: list[str] | None = None,
    glob_rescan_seconds: float = 30.0,
    pid_file: Path | None = None,
    argv: list[str] | None = None,
) -> None:
    """Run the exporter: rehydrate, then tail forever serving ``/metrics``.

    P-17: accepts a list of sidecars (single-sidecar is the
    ``len == 1`` special case). Each sidecar's co-located error JSONL
    pair (``<sidecar_parent>/bibframe/_errors.jsonl``,
    ``<sidecar_parent>/bffi/_validation.jsonl``) is derived from the
    sidecar's parent dir, not from a single global ``data_dir``.

    ``watch_globs`` is rescanned every ``glob_rescan_seconds`` to
    discover sidecars that didn't exist at launch (e.g. a fresh
    bench run starting under ``scratchpad/<new-bench>/``). New
    matches auto-attach: rehydrate + tail-state install + error-spec
    derivation.

    ``iterations`` is the test hook — bounds the tail loop so the
    test suite stays deterministic. Production CLI passes ``None``
    for unbounded.
    """
    if metrics is None:
        metrics = PipelineMetrics()
    if not sidecar_paths and not watch_globs:
        raise ValueError("serve() requires at least one sidecar path or watch-glob pattern")

    tail_states: dict[Path, _TailState] = {}
    error_specs_by_sidecar: dict[Path, list[_ErrorFileSpec]] = {}
    error_states: dict[str, _ErrorFileTailState] = {}

    for sidecar_path in sidecar_paths:
        _attach_sidecar(
            metrics,
            sidecar_path,
            tail_states,
            error_specs_by_sidecar,
            error_states,
        )

    if watch_globs:
        # Initial glob walk attaches any matches not already in the
        # explicit ``sidecar_paths`` list, so an operator can drop
        # ``--sidecar`` and rely entirely on ``--watch-glob``.
        already = set(tail_states.keys())
        for new_path in _rescan_globs(watch_globs, already):
            _attach_sidecar(
                metrics,
                new_path,
                tail_states,
                error_specs_by_sidecar,
                error_states,
            )

    # ``start_http_server`` spawns a daemon-thread HTTP server bound
    # to 0.0.0.0 by default — perfect for the local-only deployment
    # where Prometheus in the sibling Docker container scrapes the
    # host via ``host.docker.internal``.
    server, server_thread = start_http_server(port, registry=metrics.registry)
    _ = server_thread  # we don't manage the daemon thread; it dies with the process

    _write_exporter_pid_files(pid_file, argv)

    last_glob_rescan = time.monotonic()
    try:
        count = 0
        while iterations is None or count < iterations:
            for sidecar_path, state in tail_states.items():
                _tail_step(metrics, sidecar_path, state)
                for spec in error_specs_by_sidecar[sidecar_path]:
                    _tail_error_step(metrics, spec, error_states[str(spec.path)])

            if watch_globs:
                now_mono = time.monotonic()
                if now_mono - last_glob_rescan >= glob_rescan_seconds:
                    already = set(tail_states.keys())
                    for new_path in _rescan_globs(watch_globs, already):
                        _attach_sidecar(
                            metrics,
                            new_path,
                            tail_states,
                            error_specs_by_sidecar,
                            error_states,
                        )
                    last_glob_rescan = now_mono

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
