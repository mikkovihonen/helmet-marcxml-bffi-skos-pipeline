"""Dependency health probes for P-11 Phase C.

Each probe is a one-shot ``httpx`` call to a service the pipeline
depends on (Fuseki, mlx-lm, Finto). The probe never raises — failures
return a :class:`ProbeResult` with ``status="degraded"`` (timeout /
HTTP error) or ``status="down"`` (connection refused). The stage that
invoked the probe surfaces the verdict via a ``health`` event on the
P-11 stage-events stream; the status CLI (Phase B) and Grafana
dashboard (Phase D) render it.

Probes are deliberately *not* registered with the stage's existing
``httpx.Client`` — they construct a short-lived client per probe so a
hung Fuseki query in the stage's main client doesn't block the
observational probe (or vice versa). The cost is one extra TCP
connection per probe; probes are infrequent (entry + every N progress
events), so the overhead is negligible.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Final, Literal

import httpx

from bffi_pipeline.stages.observability import emit_if_active

#: Default probe wall-clock budget. 5 s is generous for a healthcheck;
#: a service that can't respond within 5 s is degraded by definition
#: even if it does eventually answer.
DEFAULT_PROBE_TIMEOUT_SECONDS: Final[float] = 5.0

#: Minimum number of slashes in a fully-qualified URL like
#: ``http://host:3030/dataset`` — used by :func:`probe_fuseki` to detect
#: when a dataset suffix is present and needs stripping to hit the
#: server-level ``/$/ping``. Constant so ruff's PLR2004 doesn't flag
#: the magic ``3``.
_DATASET_URL_SLASH_COUNT: Final[int] = 3

#: Probe verdict.
#:
#: - ``up`` — service reachable, healthy response.
#: - ``degraded`` — service reachable but slow / non-2xx / timed out.
#: - ``down`` — service unreachable (connection refused).
#: - ``not_configured`` — probe skipped because the dependency is not
#:   provisioned on this host (e.g. the mlx-lm 32B fallback that
#:   doesn't fit on the M2 Max dev box). Distinguished from ``down``
#:   so the dashboard can grey the cell out instead of colouring it
#:   red. P-12 Phase B.
ProbeStatus = Literal["up", "degraded", "down", "not_configured"]


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one dependency probe.

    ``dep`` is the short tag used in the dashboard / status CLI (e.g.
    ``"fuseki"``, ``"mlx-lm-primary"``, ``"finto"``). ``status`` is the
    ternary up / degraded / down. ``latency_ms`` is the observed
    round-trip; for ``status="down"`` it captures the time-to-fail.
    ``note`` carries an HTTP status code or exception class name so
    forensic audit doesn't need to re-derive what went wrong.
    """

    dep: str
    status: ProbeStatus
    latency_ms: int
    note: str


def _probe_get(
    dep: str,
    url: str,
    *,
    timeout: float,
    client: httpx.Client | None,
    accept_codes: tuple[int, ...] = (200,),
) -> ProbeResult:
    """Shared GET-probe core. Returns a :class:`ProbeResult`; never raises.

    ``accept_codes`` lets a caller widen the "up" set (e.g. some
    healthcheck endpoints respond ``204 No Content``).
    """
    own_client = client is None
    http = client if client is not None else httpx.Client(timeout=timeout)
    start = time.monotonic()
    try:
        response = http.get(url, timeout=timeout)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        if response.status_code in accept_codes:
            return ProbeResult(
                dep=dep,
                status="up",
                latency_ms=elapsed_ms,
                note=f"HTTP {response.status_code}",
            )
        return ProbeResult(
            dep=dep,
            status="degraded",
            latency_ms=elapsed_ms,
            note=f"HTTP {response.status_code}",
        )
    except httpx.ConnectError as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return ProbeResult(
            dep=dep,
            status="down",
            latency_ms=elapsed_ms,
            note=f"ConnectError: {exc!s}",
        )
    except httpx.HTTPError as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        # Timeouts, read errors, decoding failures, protocol errors —
        # the service is *reachable* but didn't respond cleanly. Marked
        # degraded rather than down so the dashboard's down vs. degraded
        # state-timeline panel distinguishes "service crashed" from
        # "service overloaded / wedged."
        return ProbeResult(
            dep=dep,
            status="degraded",
            latency_ms=elapsed_ms,
            note=f"{type(exc).__name__}: {exc!s}",
        )
    finally:
        if own_client:
            http.close()


def probe_fuseki(
    fuseki_url: str,
    *,
    timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    client: httpx.Client | None = None,
) -> ProbeResult:
    """Probe an Apache Jena Fuseki instance via ``/$/ping``.

    ``fuseki_url`` is the dataset endpoint (e.g.
    ``http://localhost:3030/bffi``); the probe targets the
    server-level ``/$/ping`` which lives outside the dataset path. We
    derive the server root by stripping the dataset suffix.
    """
    # Strip dataset suffix to get server root. ``http://host:3030/bffi``
    # → ``http://host:3030``. Idempotent on already-rooted URLs.
    base = fuseki_url.rstrip("/")
    if base.count("/") >= _DATASET_URL_SLASH_COUNT:
        base = base.rsplit("/", 1)[0]
    return _probe_get(
        "fuseki",
        f"{base}/$/ping",
        timeout=timeout,
        client=client,
    )


def probe_mlx_lm(
    base_url: str,
    *,
    dep: str = "mlx-lm",
    timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    client: httpx.Client | None = None,
) -> ProbeResult:
    """Probe a mlx-lm OpenAI-compat server via ``/v1/models``.

    ``base_url`` is the same URL the picker / judge would call
    (e.g. ``http://127.0.0.1:8001/v1``). ``dep`` defaults to
    ``"mlx-lm"`` but callers can pass ``"mlx-lm-primary"`` /
    ``"mlx-lm-fallback"`` when probing the two cascade ports.

    P-12 Phase B: an empty ``base_url`` (the convention callers use
    to signal "this dependency isn't provisioned on this host" — e.g.
    the unstarted M2 Max 32B fallback) short-circuits to
    ``status="not_configured"`` without an HTTP attempt. The dashboard
    then greys out the cell instead of colouring it red.
    """
    if not base_url:
        return ProbeResult(
            dep=dep,
            status="not_configured",
            latency_ms=0,
            note="empty base_url; probe skipped",
        )
    url = f"{base_url.rstrip('/')}/models"
    return _probe_get(dep, url, timeout=timeout, client=client)


def probe_finto(
    *,
    timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    client: httpx.Client | None = None,
    base_url: str = "https://api.finto.fi",
) -> ProbeResult:
    """Probe Finto's REST API via the vocabularies index.

    Finto doesn't publish a dedicated healthcheck endpoint, so we hit
    ``/rest/v1/vocabularies`` which returns the small vocabularies
    listing. Cheap on the server side; if the service is wedged the
    request times out into ``status="degraded"``.
    """
    url = f"{base_url.rstrip('/')}/rest/v1/vocabularies"
    return _probe_get("finto", url, timeout=timeout, client=client)


def emit_health_probes(stage: str, probes: dict[str, ProbeResult]) -> None:
    """Emit a single ``health`` event carrying every probe's verdict.

    One event per probe-cycle (not one per probe) so the status CLI /
    Prometheus exporter can read "the latest health snapshot for stage
    M9" from a single row rather than reconstructing it from N
    interleaved rows.

    No-op when no emitter is active (tests that don't go through the
    CLI bootstrap).
    """
    emit_if_active(
        stage=stage,
        event="health",
        extra={"probes": {dep: asdict(probe) for dep, probe in probes.items()}},
    )


__all__ = [
    "DEFAULT_PROBE_TIMEOUT_SECONDS",
    "ProbeResult",
    "ProbeStatus",
    "emit_health_probes",
    "probe_finto",
    "probe_fuseki",
    "probe_mlx_lm",
]
