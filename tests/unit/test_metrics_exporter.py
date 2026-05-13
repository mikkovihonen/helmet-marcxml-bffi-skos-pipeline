"""Unit tests for ``bffi_pipeline.metrics_exporter`` (P-11 Phase D).

The exporter applies stage-events to a Prometheus registry. Tests
drive ``apply_event`` directly with synthetic :class:`StageEventRow`
instances and assert against the registry snapshot — no real HTTP
server, no real Prometheus scrape.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from prometheus_client import generate_latest

from bffi_pipeline.metrics_exporter import (
    PipelineMetrics,
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


# --- dashboard JSON schema sanity ---------------------------------------


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
