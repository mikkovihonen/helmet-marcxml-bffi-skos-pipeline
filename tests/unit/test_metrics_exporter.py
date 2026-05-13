"""Unit tests for ``bffi_pipeline.metrics_exporter`` (P-11 Phase D).

The exporter applies stage-events to a Prometheus registry. Tests
drive ``apply_event`` directly with synthetic :class:`StageEventRow`
instances and assert against the registry snapshot — no real HTTP
server, no real Prometheus scrape.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path

from prometheus_client import generate_latest

from bffi_pipeline.metrics_exporter import (
    PipelineMetrics,
    _tail_step,
    _TailState,
    apply_event,
    rehydrate,
)
from bffi_pipeline.status import StageEventRow


def _row(
    *,
    event: str,
    stage: str = "m9",
    phase: str | None = None,
    counters: dict[str, int] | None = None,
    extra: dict[str, object] | None = None,
    ts_unix: float | None = None,
    run_uuid: str = "r",
) -> StageEventRow:
    """Factory for synthetic events. Defaults to ``m9`` because that's
    the bench-relevant stage with the richest event vocabulary."""
    ts = (
        datetime.fromtimestamp(ts_unix, tz=UTC)
        if ts_unix is not None
        else datetime(2026, 5, 13, 0, 0, 0, tzinfo=UTC)
    )
    return StageEventRow(
        ts=ts,
        run_uuid=run_uuid,
        stage=stage,
        event=event,
        phase=phase,
        counters=counters or {},
        extra=extra or {},
    )


# --- apply_event: per-event-type behaviour ------------------------------


def test_start_event_sets_started_timestamp() -> None:
    metrics = PipelineMetrics()
    apply_event(metrics, _row(event="start", counters={"total": 12666}))
    text = generate_latest(metrics.registry).decode("utf-8")
    assert "bffi_stage_started_timestamp" in text
    assert 'stage="m9"' in text
    assert "bffi_stage_entities_total" in text


def test_phase_boundary_sets_per_phase_total() -> None:
    metrics = PipelineMetrics()
    apply_event(
        metrics,
        _row(event="phase_boundary", phase="phase1", counters={"total": 12666}),
    )
    text = generate_latest(metrics.registry).decode("utf-8")
    # Per-phase total appears with phase="phase1".
    assert 'phase="phase1"' in text
    assert "12666" in text


def test_progress_updates_processed_and_throughput() -> None:
    metrics = PipelineMetrics()
    # Three progress events 60s apart, each adding 200 processed.
    for n, ts in zip([200, 400, 600], [0, 60, 120], strict=False):
        apply_event(
            metrics,
            _row(
                event="progress",
                phase="phase1",
                counters={"processed": n, "total": 12666},
                ts_unix=1747094400 + ts,
            ),
        )
    text = generate_latest(metrics.registry).decode("utf-8")
    # 400 items over 120s = 200 per minute.
    assert "bffi_stage_throughput_per_minute" in text
    assert "bffi_stage_eta_seconds" in text


def test_end_records_outcome_buckets() -> None:
    metrics = PipelineMetrics()
    apply_event(
        metrics,
        _row(
            event="end",
            counters={
                "total": 12666,
                "local": 7526,
                "lexical": 193,
                "llm_pick": 874,
                "fallback": 474,
                "no_candidate": 2752,
                "fictional": 847,
                "watchdog_aborted": 0,
            },
        ),
    )
    text = generate_latest(metrics.registry).decode("utf-8")
    # Each outcome shows up with its own ``outcome=`` label.
    assert 'outcome="local"' in text
    assert 'outcome="llm_pick"' in text
    assert 'outcome="no_candidate"' in text
    # ``total`` is excluded from the outcomes counter (it's a header,
    # not a bucket).
    assert 'outcome="total"' not in text


def test_health_event_sets_last_probe_timestamp_gauge() -> None:
    """P-12 Phase C: every probe records its event timestamp into
    ``bffi_dependency_last_probe_timestamp`` so the dashboard can
    compute ``time() - probe_ts`` and grey out stale cells.

    Two events for the same (stage, dep): the latest timestamp must
    win (Gauge.set is most-recent-write).
    """
    metrics = PipelineMetrics()
    apply_event(
        metrics,
        _row(
            event="health",
            stage="m9",
            ts_unix=1_700_000_000.0,
            extra={
                "probes": {
                    "fuseki": {
                        "dep": "fuseki",
                        "status": "up",
                        "latency_ms": 12,
                        "note": "HTTP 200",
                    },
                }
            },
        ),
    )
    apply_event(
        metrics,
        _row(
            event="health",
            stage="m9",
            ts_unix=1_700_000_999.0,
            extra={
                "probes": {
                    "fuseki": {
                        "dep": "fuseki",
                        "status": "up",
                        "latency_ms": 14,
                        "note": "HTTP 200",
                    },
                }
            },
        ),
    )
    ts = metrics.dependency_last_probe_timestamp.labels(stage="m9", dep="fuseki")._value.get()
    assert ts == 1_700_000_999.0, f"Expected the latest probe ts (1_700_000_999) to win; got {ts}."


def test_health_event_maps_not_configured_to_nan_gauge() -> None:
    """P-12 Phase B: ``status="not_configured"`` maps to NaN so
    Grafana's default value-mapping greys the cell out instead of
    rendering it as 0 = down (red). Distinguishes "dep not provisioned"
    from "dep failing"."""
    metrics = PipelineMetrics()
    apply_event(
        metrics,
        _row(
            event="health",
            stage="m6",
            extra={
                "probes": {
                    "mlx-lm-fallback": {
                        "dep": "mlx-lm-fallback",
                        "status": "not_configured",
                        "latency_ms": 0,
                        "note": "empty base_url; probe skipped",
                    },
                }
            },
        ),
    )
    gauge_value = metrics.dependency_health.labels(stage="m6", dep="mlx-lm-fallback")._value.get()
    assert math.isnan(gauge_value), (
        f"not_configured should map to NaN; got {gauge_value!r}. "
        "The dashboard's grey-out value-mapping depends on this."
    )
    # Wire-format check too: prometheus_client serialises NaN literally.
    text = generate_latest(metrics.registry).decode("utf-8")
    assert 'bffi_dependency_health{dep="mlx-lm-fallback",stage="m6"} NaN' in text


def test_health_event_maps_status_to_gauge() -> None:
    metrics = PipelineMetrics()
    apply_event(
        metrics,
        _row(
            event="health",
            extra={
                "probes": {
                    "fuseki": {
                        "dep": "fuseki",
                        "status": "up",
                        "latency_ms": 12,
                        "note": "HTTP 200",
                    },
                    "mlx-lm": {
                        "dep": "mlx-lm",
                        "status": "degraded",
                        "latency_ms": 5000,
                        "note": "ReadTimeout",
                    },
                    "finto": {
                        "dep": "finto",
                        "status": "down",
                        "latency_ms": 23,
                        "note": "ConnectError",
                    },
                }
            },
        ),
    )
    text = generate_latest(metrics.registry).decode("utf-8")
    assert "bffi_dependency_health" in text
    # Numeric mapping verified: up=2, degraded=1, down=0.
    assert 'bffi_dependency_health{dep="fuseki",stage="m9"} 2.0' in text
    assert 'bffi_dependency_health{dep="mlx-lm",stage="m9"} 1.0' in text
    assert 'bffi_dependency_health{dep="finto",stage="m9"} 0.0' in text
    # Latency gauge also populated.
    assert "bffi_dependency_probe_latency_ms" in text


def test_watchdog_events_accumulate_via_counter() -> None:
    metrics = PipelineMetrics()
    for _ in range(3):
        apply_event(
            metrics,
            _row(
                event="watchdog",
                stage="watchdog",
                extra={"event": "timeout", "pair_id": "a+b"},
            ),
        )
    apply_event(
        metrics,
        _row(
            event="watchdog",
            stage="watchdog",
            extra={"event": "field_budget_exceeded", "pair_id": "c+d"},
        ),
    )
    text = generate_latest(metrics.registry).decode("utf-8")
    assert 'event="timeout"' in text
    assert 'event="field_budget_exceeded"' in text
    # Counter values are cumulative.
    assert "3.0" in text


# --- rehydrate ----------------------------------------------------------


def test_rehydrate_replays_sidecar_into_registry(tmp_path: Path) -> None:
    """At startup the exporter reads the full sidecar so a mid-run
    start doesn't lose history."""
    sidecar = tmp_path / "stage-events.jsonl"
    rows = [
        {"ts": "2026-05-13T00:00:00Z", "run_uuid": "r", "stage": "m9", "event": "start"},
        {
            "ts": "2026-05-13T00:01:00Z",
            "run_uuid": "r",
            "stage": "m9",
            "event": "phase_boundary",
            "phase": "phase1",
            "counters": {"total": 100},
        },
        {
            "ts": "2026-05-13T00:02:00Z",
            "run_uuid": "r",
            "stage": "m9",
            "event": "progress",
            "phase": "phase1",
            "counters": {"processed": 50, "total": 100},
        },
    ]
    sidecar.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    metrics = PipelineMetrics()
    applied = rehydrate(metrics, sidecar)
    assert applied == len(rows)
    text = generate_latest(metrics.registry).decode("utf-8")
    assert "bffi_stage_entities_total" in text
    assert "bffi_stage_entities_processed_total" in text


def test_rehydrate_on_missing_sidecar_returns_zero(tmp_path: Path) -> None:
    """No sidecar yet (the pipeline hasn't run) — exporter must not
    crash; it just exposes an empty registry."""
    metrics = PipelineMetrics()
    assert rehydrate(metrics, tmp_path / "missing.jsonl") == 0


# --- _tail_step (P-12 Phase A regression pins) --------------------------


def _watchdog_event_row(ts: str, inner: str) -> dict[str, object]:
    """Synthetic watchdog row for the tail-loop double-count tests.

    Watchdog events are useful here because they're keyed off the
    `Counter` family — the gauge family masks the bug since
    ``Gauge.set`` is idempotent. Counter inflation is the bug
    signature.
    """
    return {
        "ts": ts,
        "run_uuid": "r",
        "stage": "watchdog",
        "event": "watchdog",
        "extra": {"event": inner, "elapsed_s": 0.0, "retry_n": 0},
    }


def _watchdog_total(metrics: PipelineMetrics, inner: str) -> float:
    """Helper: read the ``bffi_watchdog_events_total`` counter for one inner event."""
    return float(metrics.watchdog_events_total.labels(stage="watchdog", event=inner)._value.get())


def test_tail_step_idle_polls_do_not_double_count(tmp_path: Path) -> None:
    """Regression pin for the P-12 Phase A fix.

    Pre-fix: ``_tail_step`` used ``size <= state.last_pos`` and
    reset ``last_pos`` to 0 even when nothing was appended, then
    re-applied the entire sidecar on every idle poll. Counter
    inflation in mid-bench dashboards (~1 165x on the 2026-05-13
    run) was the symptom.

    Post-fix: idle polls (size == last_pos) return 0 with no
    re-application. Counters stay stable.
    """
    sidecar = tmp_path / "stage-events.jsonl"
    rows = [
        _watchdog_event_row("2026-05-13T00:00:00Z", "timeout"),
        _watchdog_event_row("2026-05-13T00:00:01Z", "timeout"),
        _watchdog_event_row("2026-05-13T00:00:02Z", "give_up"),
    ]
    sidecar.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    metrics = PipelineMetrics()
    rehydrate(metrics, sidecar)
    state = _TailState(last_pos=sidecar.stat().st_size)

    timeout_before = _watchdog_total(metrics, "timeout")
    give_up_before = _watchdog_total(metrics, "give_up")
    assert timeout_before == 2.0
    assert give_up_before == 1.0

    # 50 idle polls. Pre-fix this inflated counters by 50x;
    # post-fix every step returns 0 and counters stay frozen.
    for _ in range(50):
        applied = _tail_step(metrics, sidecar, state)
        assert applied == 0

    assert _watchdog_total(metrics, "timeout") == timeout_before
    assert _watchdog_total(metrics, "give_up") == give_up_before


def test_tail_step_handles_truncation_and_re_reads(tmp_path: Path) -> None:
    """Rotation / truncation must still trigger a full re-read.

    Pre-fix the ``<=`` branch handled this incidentally (and broke
    idle polls); post-fix the strict ``<`` branch handles it
    intentionally without breaking the idle case.
    """
    sidecar = tmp_path / "stage-events.jsonl"
    rows_before = [_watchdog_event_row("2026-05-13T00:00:00Z", "timeout") for _ in range(3)]
    sidecar.write_text("\n".join(json.dumps(r) for r in rows_before) + "\n", encoding="utf-8")
    metrics = PipelineMetrics()
    rehydrate(metrics, sidecar)
    state = _TailState(last_pos=sidecar.stat().st_size)
    assert _watchdog_total(metrics, "timeout") == 3.0

    # Truncate + replace with fewer-but-different events.
    rows_after = [
        _watchdog_event_row("2026-05-13T01:00:00Z", "pair_budget_exceeded"),
        _watchdog_event_row("2026-05-13T01:00:01Z", "pair_budget_exceeded"),
    ]
    sidecar.write_text("\n".join(json.dumps(r) for r in rows_after) + "\n", encoding="utf-8")

    applied = _tail_step(metrics, sidecar, state)
    # Truncation re-reads from byte 0 so both new events get applied.
    assert applied == 2
    assert _watchdog_total(metrics, "pair_budget_exceeded") == 2.0


def test_tail_step_mixed_idle_and_append_pattern(tmp_path: Path) -> None:
    """Bench-realistic pattern: events arrive sporadically across many
    polls. Total counter delta must equal exactly the appended
    events, not appended-times-idle-polls.
    """
    sidecar = tmp_path / "stage-events.jsonl"
    sidecar.write_text("", encoding="utf-8")
    metrics = PipelineMetrics()
    state = _TailState(last_pos=0)

    # 5 idle polls before anything happens.
    for _ in range(5):
        assert _tail_step(metrics, sidecar, state) == 0

    # Append one event, poll, then 5 idle polls.
    with sidecar.open("a", encoding="utf-8") as f:
        f.write(json.dumps(_watchdog_event_row("2026-05-13T00:00:00Z", "timeout")) + "\n")
    assert _tail_step(metrics, sidecar, state) == 1
    for _ in range(5):
        assert _tail_step(metrics, sidecar, state) == 0

    # Append two more events, poll, then 5 more idle polls.
    with sidecar.open("a", encoding="utf-8") as f:
        f.write(json.dumps(_watchdog_event_row("2026-05-13T00:00:01Z", "timeout")) + "\n")
        f.write(json.dumps(_watchdog_event_row("2026-05-13T00:00:02Z", "give_up")) + "\n")
    assert _tail_step(metrics, sidecar, state) == 2
    for _ in range(5):
        assert _tail_step(metrics, sidecar, state) == 0

    # Exactly 3 events appended; no inflation from the 15 idle polls.
    assert _watchdog_total(metrics, "timeout") == 2.0
    assert _watchdog_total(metrics, "give_up") == 1.0


# --- dashboard JSON schema sanity ---------------------------------------


def test_grafana_dashboard_has_active_run_templating_variable() -> None:
    """P-12 Phase E: the top-of-dashboard overview row filters every
    stage tile by ``run_uuid=$active_run``, where ``$active_run`` is
    a Grafana templating variable that auto-tracks the latest start
    event. The variable must exist and target ``run_uuid``."""
    dashboard_path = (
        Path(__file__).resolve().parents[2]
        / "config"
        / "grafana"
        / "dashboards"
        / "bffi-pipeline.json"
    )
    data = json.loads(dashboard_path.read_text(encoding="utf-8"))
    variables = data.get("templating", {}).get("list", [])
    assert any(v.get("name") == "active_run" for v in variables), (
        "Missing $active_run templating variable; overview row needs it "
        "to filter tiles to the currently-active pipeline invocation."
    )
    active_run = next(v for v in variables if v.get("name") == "active_run")
    # Must read from the Prometheus datasource and produce a run_uuid.
    assert "bffi_stage_started_timestamp" in active_run["query"]
    assert "run_uuid" in active_run["query"] + active_run.get("regex", "")


def test_grafana_dashboard_pipeline_overview_row_covers_every_stage() -> None:
    """P-12 Phase E: the top row has one stat tile per pipeline stage.

    Pins the eight-stage set so a future stage addition (or accidental
    deletion) shows up in CI rather than as a silent dashboard
    regression. The 'Pipeline stages (last start / end timestamps)'
    panel that this row replaces is also asserted gone.
    """
    dashboard_path = (
        Path(__file__).resolve().parents[2]
        / "config"
        / "grafana"
        / "dashboards"
        / "bffi-pipeline.json"
    )
    data = json.loads(dashboard_path.read_text(encoding="utf-8"))
    # Overview row sits at y=0 with width-3 stat tiles.
    overview_tiles = [
        p
        for p in data["panels"]
        if p.get("type") == "stat"
        and p.get("gridPos", {}).get("y") == 0
        and p.get("gridPos", {}).get("w") == 3
    ]
    assert len(overview_tiles) == 8, (
        f"Expected 8 overview tiles at the top of the dashboard; "
        f"got {len(overview_tiles)} ({[t['title'] for t in overview_tiles]})."
    )
    # The 8 cover the M2 → load span.
    expected_stages = {"m2", "m3", "m5", "m6", "m8", "m9", "skosify", "load"}
    seen_stages = {
        s
        for tile in overview_tiles
        for s in expected_stages
        if f'stage="{s}"' in tile["targets"][0]["expr"]
    }
    assert seen_stages == expected_stages, (
        f"Overview tiles missing stages: {expected_stages - seen_stages}"
    )
    # The noisy 'Pipeline stages' panel is gone.
    titles = {p["title"] for p in data["panels"]}
    assert not any("last start / end timestamps" in t for t in titles), (
        "The unreadable 'Pipeline stages (last start / end timestamps)' "
        "panel should have been removed in P-12 Phase E."
    )


def test_grafana_dashboard_json_parses() -> None:
    """The bundled dashboard JSON must parse as valid JSON and carry
    the load-bearing top-level fields. A schema-level bug here
    breaks Grafana's auto-provisioning at container startup."""
    dashboard_path = (
        Path(__file__).resolve().parents[2]
        / "config"
        / "grafana"
        / "dashboards"
        / "bffi-pipeline.json"
    )
    data = json.loads(dashboard_path.read_text(encoding="utf-8"))
    assert data["uid"] == "bffi-pipeline"
    assert data["schemaVersion"] >= 30
    assert isinstance(data["panels"], list)
    assert len(data["panels"]) >= 5
    # Every panel references the provisioned ``bffi-prometheus`` UID
    # so it works on first start without operator clicks.
    for panel in data["panels"]:
        if "datasource" in panel:
            assert panel["datasource"]["uid"] == "bffi-prometheus"
