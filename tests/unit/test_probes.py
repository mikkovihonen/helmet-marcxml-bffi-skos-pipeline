"""Unit tests for ``bffi_pipeline.observability.probes`` (P-11 Phase C).

All HTTP traffic goes through ``httpx.MockTransport``; the live
network is never contacted. Probes must never raise — every error
class maps to a :class:`ProbeResult` with the right status.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from bffi_pipeline.observability.events import (
    StageEventEmitter,
    set_active_emitter,
)
from bffi_pipeline.observability.probes import (
    ProbeResult,
    emit_health_probes,
    probe_finto,
    probe_fuseki,
    probe_mlx_lm,
)


@pytest.fixture(autouse=True)
def _reset_emitter() -> None:
    """Phase A's module-level singleton bleed-prevention."""
    set_active_emitter(None)


def _client(handler: Any) -> httpx.Client:
    """One-line MockTransport client constructor."""
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- probe_fuseki -------------------------------------------------------


def test_probe_fuseki_200_is_up() -> None:
    """Healthy Fuseki returns 200 to /$/ping → status=up."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/$/ping"
        return httpx.Response(200, text="pong")

    result = probe_fuseki("http://localhost:3030/bffi", client=_client(handler))
    assert isinstance(result, ProbeResult)
    assert result.dep == "fuseki"
    assert result.status == "up"
    assert result.note == "HTTP 200"
    assert result.latency_ms >= 0


def test_probe_fuseki_strips_dataset_suffix() -> None:
    """The probe targets the server root, not the dataset path. Given
    ``/bffi`` it should hit ``/$/ping`` at the host root."""
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return httpx.Response(200, text="pong")

    probe_fuseki("http://localhost:3030/bffi", client=_client(handler))
    assert seen_paths == ["/$/ping"]


def test_probe_fuseki_503_is_degraded() -> None:
    """Service responding but unhealthy (503 Service Unavailable) →
    status=degraded, not down."""
    result = probe_fuseki(
        "http://localhost:3030/bffi",
        client=_client(lambda _: httpx.Response(503)),
    )
    assert result.status == "degraded"
    assert result.note == "HTTP 503"


def test_probe_fuseki_connect_error_is_down() -> None:
    """Connection refused → status=down. Distinguishes ``service crashed``
    from ``service overloaded`` (which would be degraded)."""

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    result = probe_fuseki("http://localhost:3030/bffi", client=_client(handler))
    assert result.status == "down"
    assert "ConnectError" in result.note


def test_probe_fuseki_timeout_is_degraded() -> None:
    """Read timeout → status=degraded (service is reachable but
    not responding fast enough)."""

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated 5s timeout")

    result = probe_fuseki(
        "http://localhost:3030/bffi",
        timeout=0.01,
        client=_client(handler),
    )
    assert result.status == "degraded"
    assert "ReadTimeout" in result.note


# --- probe_mlx_lm -------------------------------------------------------


def test_probe_mlx_lm_default_dep_is_mlx_lm() -> None:
    """Default dep tag is ``mlx-lm``."""
    handler = lambda _: httpx.Response(  # noqa: E731 — concise mock handler
        200, json={"object": "list", "data": []}
    )
    result = probe_mlx_lm("http://127.0.0.1:8001/v1", client=_client(handler))
    assert result.dep == "mlx-lm"
    assert result.status == "up"


def test_probe_mlx_lm_custom_dep_tag_for_cascade_ports() -> None:
    """Cascade callers pass dep="mlx-lm-primary" / "mlx-lm-fallback" so
    the dashboard's state-timeline panel separates the two ports."""
    result = probe_mlx_lm(
        "http://127.0.0.1:8002/v1",
        dep="mlx-lm-fallback",
        client=_client(lambda _: httpx.Response(200, json={"data": []})),
    )
    assert result.dep == "mlx-lm-fallback"


def test_probe_mlx_lm_hits_models_endpoint() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return httpx.Response(200, json={"data": []})

    probe_mlx_lm("http://127.0.0.1:8001/v1", client=_client(handler))
    assert seen_paths == ["/v1/models"]


def test_probe_mlx_lm_down_when_connect_refused() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nothing on :8002")

    result = probe_mlx_lm("http://127.0.0.1:8002/v1", client=_client(handler))
    assert result.status == "down"


def test_probe_mlx_lm_empty_url_is_not_configured() -> None:
    """P-12 Phase B: empty ``base_url`` short-circuits to
    ``status="not_configured"`` without an HTTP attempt.

    Convention used by callers to express "this dependency is not
    provisioned on this host" — e.g. the unstarted 32B fallback on a
    dev box where the cascade collapses to a single tier. The
    exporter maps this status to a ``NaN`` gauge so Grafana greys
    the cell out instead of colouring it red.
    """
    http_attempted = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal http_attempted
        http_attempted = True
        return httpx.Response(200, json={"data": []})

    result = probe_mlx_lm("", dep="mlx-lm-fallback", client=_client(handler))
    assert result.status == "not_configured"
    assert result.dep == "mlx-lm-fallback"
    assert result.latency_ms == 0
    assert "skipped" in result.note
    assert http_attempted is False  # short-circuit before any HTTP


# --- probe_finto --------------------------------------------------------


def test_probe_finto_default_targets_api_finto_fi() -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json={"vocabularies": []})

    probe_finto(client=_client(handler))
    assert seen_urls == ["https://api.finto.fi/rest/v1/vocabularies"]


def test_probe_finto_accepts_base_url_override_for_tests() -> None:
    """Callers (and integration tests) can point the probe at a local
    Finto mirror without monkey-patching the default."""
    result = probe_finto(
        base_url="http://localhost:9999",
        client=_client(lambda _: httpx.Response(200, json={"vocabularies": []})),
    )
    assert result.status == "up"


def test_probe_finto_503_is_degraded() -> None:
    result = probe_finto(
        client=_client(lambda _: httpx.Response(503, text="overloaded")),
    )
    assert result.status == "degraded"
    assert result.note == "HTTP 503"


# --- emit_health_probes -------------------------------------------------


def _load_sidecar(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_emit_health_probes_writes_one_event_with_all_probes(
    tmp_path: Path,
) -> None:
    """One event per probe-cycle (not one per probe) so the status CLI
    reads the latest health snapshot from a single row."""
    sidecar = tmp_path / "stage-events.jsonl"
    set_active_emitter(StageEventEmitter(sidecar_path=sidecar, run_uuid="r"))

    emit_health_probes(
        "m9",
        {
            "fuseki": ProbeResult(dep="fuseki", status="up", latency_ms=12, note="HTTP 200"),
            "mlx-lm": ProbeResult(dep="mlx-lm", status="up", latency_ms=9, note="HTTP 200"),
            "finto": ProbeResult(
                dep="finto", status="degraded", latency_ms=5000, note="ReadTimeout"
            ),
        },
    )

    rows = _load_sidecar(sidecar)
    assert len(rows) == 1
    row = rows[0]
    assert row["stage"] == "m9"
    assert row["event"] == "health"
    assert set(row["extra"]["probes"].keys()) == {"fuseki", "mlx-lm", "finto"}
    finto = row["extra"]["probes"]["finto"]
    assert finto["status"] == "degraded"
    assert finto["latency_ms"] == 5000


def test_emit_health_probes_no_op_without_active_emitter(tmp_path: Path) -> None:
    """No emitter set → silent no-op. Stages that emit probes should
    work whether or not the CLI's bootstrap initialised an emitter
    (e.g. running a stage directly in a script for debugging)."""
    sidecar = tmp_path / "stage-events.jsonl"
    # No set_active_emitter — singleton is None.
    emit_health_probes(
        "m9",
        {"fuseki": ProbeResult(dep="fuseki", status="up", latency_ms=12, note="OK")},
    )
    assert not sidecar.exists()
