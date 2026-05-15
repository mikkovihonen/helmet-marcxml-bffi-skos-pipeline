"""Unit-test isolation guards.

Unit tests must never touch the network. Two leak paths today:

1. ``stages/reconcile.py:_m9_probe_dependencies`` fires
   ``probe_mlx_lm(settings.llm_base_url)`` + ``probe_finto()`` +
   ``probe_fuseki(settings.fuseki_url)`` at the start (and every
   1000-entity cadence) of ``apply_reconciliation``. Tests in
   ``test_local_concept_resolver.py`` and ``test_reconcile.py``
   transitively invoke ``apply_reconciliation`` against the
   operator's live ``LLM_BASE_URL`` / ``FUSEKI_URL`` / Finto.
2. ``stages/judge.py`` fires ``probe_mlx_lm(primary_url, ...)`` +
   ``probe_mlx_lm(fallback_url, ...)`` at the entry of the judge
   stage's ``run()``. Direct ``test_judge.py`` invocations hit the
   live LLM endpoints.

Both probes are side-effect-only — they emit a ``health`` event
that downstream consumers don't actually inspect. Stubbing them in
unit tests is byte-stable against the production code path.

Two layers of defence:

- **Probe stubs** patched onto the *consumer module* namespace
  (``reconcile.probe_mlx_lm`` etc., not ``probes.probe_mlx_lm``)
  so ``test_probes.py`` — which exercises the probe layer directly
  via the ``probes`` module — keeps working unchanged.
- **Socket-level outbound block** on non-loopback ``connect()`` calls
  catches future regressions where new code adds a network probe
  that bypasses the patched probe functions. Loopback connections
  stay open because some tests legitimately run an HTTP server
  bound to ``127.0.0.1`` and consume it in-process.

Both layers install as autouse session-scoped fixtures so every
unit test inherits the protection without per-test boilerplate.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest

from bffi_pipeline.stages import probes
from bffi_pipeline.stages.m6 import runner as judge
from bffi_pipeline.stages.m9 import runner as reconcile


def _stub_probe(
    *args: object,
    dep: str = "stub",
    **_kwargs: object,
) -> probes.ProbeResult:
    """Return a ``not_configured`` ProbeResult without touching the network.

    Matches the shape ``probe_mlx_lm("")`` produces for empty URLs
    (the operator-side convention for "dependency not provisioned").
    Downstream callers either emit it as a dashboard cell (greyed out)
    or ignore the result entirely.
    """
    # ``probe_mlx_lm`` callers pass ``dep`` as a kwarg; ``probe_finto`` and
    # ``probe_fuseki`` don't. Default to "stub" when the caller hasn't
    # told us which dependency this is — the test never reads the value.
    _ = args  # signature compatibility only
    return probes.ProbeResult(
        dep=dep,
        status="not_configured",
        latency_ms=0,
        note="unit test stub; real probe disabled",
    )


@pytest.fixture(autouse=True, scope="session")
def _block_unit_test_network() -> Iterator[None]:
    """Install probe stubs + socket-level outbound block for every unit test.

    Probe layer patches:

    - ``reconcile.probe_mlx_lm`` / ``reconcile.probe_finto`` /
      ``reconcile.probe_fuseki`` — the imports the M9 entry-point
      health probes resolve against.
    - ``judge.probe_mlx_lm`` — the import the M6 entry-point health
      probes resolve against.

    Socket layer: ``socket.socket.connect`` raises on non-loopback
    targets. Loopback (``127.0.0.1`` / ``::1`` / ``localhost``) stays
    open because in-process HTTP servers (Prometheus exporter tests,
    etc.) legitimately bind + consume there. ``httpx.MockTransport``
    short-circuits BEFORE socket.connect, so existing mock-using
    tests don't trip the guard.
    """
    # Layer 1: probe stubs on the consumer modules.
    saved_reconcile = (
        reconcile.probe_mlx_lm,
        reconcile.probe_finto,
        reconcile.probe_fuseki,
    )
    saved_judge_probe_mlx_lm = judge.probe_mlx_lm
    reconcile.probe_mlx_lm = _stub_probe  # type: ignore[assignment]
    reconcile.probe_finto = _stub_probe  # type: ignore[assignment]
    reconcile.probe_fuseki = _stub_probe  # type: ignore[assignment]
    judge.probe_mlx_lm = _stub_probe  # type: ignore[assignment]

    # Layer 2: socket-level guard. Loopback stays open; everything
    # else raises a clear error pointing at this file.
    saved_socket_connect = socket.socket.connect

    def _guarded_connect(self: socket.socket, address: object) -> None:
        host: str | None = None
        if isinstance(address, tuple) and address:
            first = address[0]
            host = first if isinstance(first, str) else None
        if host is not None and host not in ("127.0.0.1", "::1", "localhost"):
            raise RuntimeError(
                f"Unit test attempted outbound connect to {address!r}. "
                f"Mock the call (httpx.MockTransport) or extend "
                f"tests/unit/conftest.py to stub the new code path. "
                f"Loopback (127.0.0.1 / ::1 / localhost) is allowed."
            )
        saved_socket_connect(self, address)

    socket.socket.connect = _guarded_connect  # type: ignore[assignment, method-assign]

    try:
        yield
    finally:
        (
            reconcile.probe_mlx_lm,
            reconcile.probe_finto,
            reconcile.probe_fuseki,
        ) = saved_reconcile
        judge.probe_mlx_lm = saved_judge_probe_mlx_lm
        socket.socket.connect = saved_socket_connect  # type: ignore[method-assign]
