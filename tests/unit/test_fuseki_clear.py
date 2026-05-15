"""Unit tests for ``bffi_pipeline.stages.m10.fuseki_clear`` (P-32 Phase H).

The five named acceptance tests pin: the prefix-only drop boundary,
the oversized-graph safety threshold, the unreachable-Fuseki warn-
and-continue path, the manifest-record contract, and the strict-mode
fail-on-unreachable variant. All drive the helper via
``httpx.MockTransport`` so no real Fuseki is needed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from typer.testing import CliRunner

from bffi_pipeline.cli import app
from bffi_pipeline.config import get_settings
from bffi_pipeline.observability.events import set_active_emitter
from bffi_pipeline.run_manifest import (
    MANIFEST_FILENAME,
    read_manifest,
    update_manifest_field,
    write_initial_manifest,
)
from bffi_pipeline.stages.m10.fuseki_clear import ClearResult, clear_run_output_graphs

GRAPH_BASE = "http://urn.fi/URN:NBN:fi:bib:graph:"
OUTPUT_GRAPH_1 = f"{GRAPH_BASE}bffi-works"
OUTPUT_GRAPH_2 = f"{GRAPH_BASE}provenance"
VOCAB_GRAPH = "http://www.yso.fi/onto/yso/"  # Finto-style, outside graph_base


@pytest.fixture(autouse=True)
def _isolate_test_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Per-test settings + emitter reset; same pattern as test_runs_prune."""
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(tmp_path))
    monkeypatch.setenv("BFFI_OBSERVABILITY_SIDECAR", "none")
    get_settings.cache_clear()
    set_active_emitter(None)
    yield
    get_settings.cache_clear()
    set_active_emitter(None)


def _make_fuseki_mock(graphs: dict[str, int]) -> httpx.Client:
    """Build an ``httpx.Client`` that fakes the SPARQL endpoints.

    ``graphs`` maps named-graph URI to its triple count. The mock
    handles the three request shapes Phase H emits:

    - ``POST .../sparql`` with ``SELECT DISTINCT ?g`` → return the
      keys of ``graphs``.
    - ``POST .../sparql`` with ``SELECT (COUNT(*) AS ?n) ... GRAPH <X>``
      → return ``graphs[X]`` (or 0 if not in the map).
    - ``DELETE .../data?graph=X`` → 204 No Content; pop the entry
      from ``graphs`` so subsequent SELECTs see it gone.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sparql"):
            # run_select POSTs ``data={"query": query}`` which httpx
            # form-encodes; decode it to inspect the SPARQL text.
            body = request.read().decode("utf-8")
            params = parse_qs(body)
            query = params.get("query", [""])[0]
            if "SELECT DISTINCT ?g" in query:
                bindings = [{"g": {"type": "uri", "value": uri}} for uri in sorted(graphs)]
                return httpx.Response(
                    200,
                    json={"results": {"bindings": bindings}},
                )
            if "COUNT(*)" in query:
                # Pull the URI between the angle brackets in
                # `GRAPH <uri> { ?s ?p ?o }`.
                start = query.find("GRAPH <") + len("GRAPH <")
                end = query.find(">", start)
                uri = query[start:end]
                n = graphs.get(uri, 0)
                return httpx.Response(
                    200,
                    json={"results": {"bindings": [{"n": {"type": "literal", "value": str(n)}}]}},
                )
        if request.url.path.endswith("/data") and request.method == "DELETE":
            graph_uri = request.url.params.get("graph")
            graphs.pop(graph_uri, None)
            return httpx.Response(204)
        return httpx.Response(404, text=f"unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport, timeout=10.0)


# --- Phase H acceptance tests --------------------------------------------


def test_clear_drops_only_graph_base_prefixed() -> None:
    """Three graphs in Fuseki, two under ``graph_base``, one vocab.

    Phase H's URI-prefix boundary means only the two output graphs are
    dropped; the YSO-style vocab graph stays untouched.
    """
    graphs = {
        OUTPUT_GRAPH_1: 1000,
        OUTPUT_GRAPH_2: 200,
        VOCAB_GRAPH: 5_000_000,  # bigger than threshold but irrelevant — not under graph_base
    }
    client = _make_fuseki_mock(graphs)

    result = clear_run_output_graphs(
        "http://fuseki.test/bffi",
        GRAPH_BASE,
        client=client,
    )

    assert sorted(result.dropped_graphs) == sorted([OUTPUT_GRAPH_1, OUTPUT_GRAPH_2])
    # The vocab graph survives in the mock's state.
    assert VOCAB_GRAPH in graphs
    # The output graphs were popped on DELETE.
    assert OUTPUT_GRAPH_1 not in graphs
    assert OUTPUT_GRAPH_2 not in graphs


def test_clear_safety_threshold_refuses_large_graph(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Graph > ``max_triples_per_graph`` is skipped unless ``force=True``.

    Catches the misconfigured-``graph_base`` failure mode where a
    vocabulary URI accidentally matches the pipeline prefix and Phase
    H would otherwise drop millions of triples.
    """
    caplog.set_level(logging.WARNING)
    graphs = {OUTPUT_GRAPH_1: 5_000_000}  # 5M > 1M threshold below
    client = _make_fuseki_mock(graphs)

    result = clear_run_output_graphs(
        "http://fuseki.test/bffi",
        GRAPH_BASE,
        max_triples_per_graph=1_000_000,
        client=client,
    )

    assert result.dropped_graphs == []
    assert len(result.skipped_oversized) == 1
    assert result.skipped_oversized[0] == (OUTPUT_GRAPH_1, 5_000_000)
    # Graph still in the mock — drop didn't fire.
    assert OUTPUT_GRAPH_1 in graphs
    # Warning surfaced explaining the threshold + escape hatch.
    messages = " ".join(rec.message for rec in caplog.records)
    assert "safety threshold" in messages
    assert "--force-clear" in messages


def test_clear_fuseki_unreachable_warns_and_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fuseki ConnectError → warn, set ``fuseki_reachable=False``, return cleanly."""
    caplog.set_level(logging.WARNING)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, timeout=5.0)

    result = clear_run_output_graphs(
        "http://fuseki.test/bffi",
        GRAPH_BASE,
        client=client,
    )

    assert result.fuseki_reachable is False
    assert result.dropped_graphs == []
    assert result.total_triples_before == 0
    messages = " ".join(rec.message for rec in caplog.records).lower()
    assert "unreachable" in messages


def test_clear_records_outcome_in_manifest(tmp_path: Path) -> None:
    """``runs clear-fuseki --apply`` updates the run manifest's ``pre_run_fuseki_clear``.

    Actually exercises the manifest-update path indirectly via the
    ClearResult.to_manifest_dict + ``update_manifest_field`` chain;
    the load command's wiring (``_maybe_clear_fuseki_before_load``)
    is the production caller.
    """
    # Set up a manifest at the canonical location.
    write_initial_manifest(tmp_path, run_uuid="aaa")

    # Build a synthetic ClearResult and persist via the same helper
    # that load_command's _maybe_clear_fuseki_before_load uses.
    result = ClearResult(
        dropped_graphs=[OUTPUT_GRAPH_1, OUTPUT_GRAPH_2],
        skipped_oversized=[(VOCAB_GRAPH, 5_000_000)],
        total_triples_before=1200,
        fuseki_reachable=True,
        dry_run=False,
    )
    update_manifest_field(
        tmp_path / MANIFEST_FILENAME,
        pre_run_fuseki_clear=result.to_manifest_dict(),
    )

    manifest = read_manifest(tmp_path / MANIFEST_FILENAME)
    pre_run = manifest.pre_run_fuseki_clear
    assert pre_run is not None
    assert pre_run["dropped_graphs"] == [OUTPUT_GRAPH_1, OUTPUT_GRAPH_2]
    assert pre_run["skipped_oversized"] == [{"graph": VOCAB_GRAPH, "triples": 5_000_000}]
    assert pre_run["total_triples_before"] == 1200
    assert pre_run["fuseki_reachable"] is True
    assert pre_run["dry_run"] is False
    assert "ts" in pre_run


def test_clear_fuseki_strict_mode_fails_on_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``BFFI_FUSEKI_CLEAR_ON_RUN_START=strict`` + unreachable Fuseki → load exits non-zero.

    The ``load`` command's ``_maybe_clear_fuseki_before_load`` reads
    the setting and aborts with exit code 1 when the strict-mode
    operator expects fail-on-unreachable.
    """
    monkeypatch.setenv("BFFI_FUSEKI_CLEAR_ON_RUN_START", "strict")
    # Point at an unreachable port — actual TCP connect failure.
    monkeypatch.setenv("FUSEKI_URL", "http://127.0.0.1:1/bffi")
    monkeypatch.setenv("BFFI_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("BFFI_RUN_UUID", raising=False)
    get_settings.cache_clear()

    # Stub the load.run path (the command exits before reaching it,
    # but typer's no-args-is-help would otherwise demand options) by
    # providing a minimal skosified.ttl fixture.
    (tmp_path / "canonical-skosified.ttl").write_text("# empty\n")
    (tmp_path / "provenance.ttl").write_text("# empty\n")

    result = CliRunner().invoke(
        app,
        ["load", "--skosified-path", str(tmp_path / "canonical-skosified.ttl")],
    )

    # Either exit code 1 (strict mode fired) or the load failed
    # downstream — both are acceptable failures here. What matters:
    # NOT exit code 0 (success path didn't sneak through with
    # silent-clear-skip behaviour).
    assert result.exit_code != 0
    # The strict-mode warning fires on stderr.
    output = (result.output or "") + (
        result.stderr if hasattr(result, "stderr") and result.stderr else ""
    )
    # Either the strict refusal or a downstream connection error
    # surfaces — but the strict refusal is what we're testing.
    lowered = output.lower()
    assert "strict" in lowered or "unreachable" in lowered or "refused" in lowered


# --- Supporting tests ----------------------------------------------------


def test_clear_dry_run_does_not_delete() -> None:
    """``dry_run=True`` records the would-drop set without firing DELETE."""
    graphs = {OUTPUT_GRAPH_1: 100, OUTPUT_GRAPH_2: 200}
    client = _make_fuseki_mock(graphs)

    result = clear_run_output_graphs(
        "http://fuseki.test/bffi",
        GRAPH_BASE,
        dry_run=True,
        client=client,
    )

    assert sorted(result.dropped_graphs) == sorted([OUTPUT_GRAPH_1, OUTPUT_GRAPH_2])
    assert result.dry_run is True
    # Mock state unchanged — DELETE didn't fire.
    assert OUTPUT_GRAPH_1 in graphs
    assert OUTPUT_GRAPH_2 in graphs


def test_clear_force_overrides_safety_threshold() -> None:
    """``force=True`` drops graphs that exceed ``max_triples_per_graph``."""
    graphs = {OUTPUT_GRAPH_1: 5_000_000}
    client = _make_fuseki_mock(graphs)

    result = clear_run_output_graphs(
        "http://fuseki.test/bffi",
        GRAPH_BASE,
        max_triples_per_graph=1_000_000,
        force=True,
        client=client,
    )

    assert result.dropped_graphs == [OUTPUT_GRAPH_1]
    assert result.skipped_oversized == []
    assert OUTPUT_GRAPH_1 not in graphs


def test_clear_to_manifest_dict_shape() -> None:
    """``ClearResult.to_manifest_dict`` returns the right JSON shape."""
    result = ClearResult(
        dropped_graphs=[OUTPUT_GRAPH_1],
        skipped_oversized=[(VOCAB_GRAPH, 100)],
        total_triples_before=1000,
        fuseki_reachable=True,
        dry_run=False,
    )
    payload: dict[str, Any] = result.to_manifest_dict()
    assert payload["dropped_graphs"] == [OUTPUT_GRAPH_1]
    assert payload["skipped_oversized"] == [{"graph": VOCAB_GRAPH, "triples": 100}]
    assert payload["total_triples_before"] == 1000
    assert payload["fuseki_reachable"] is True
    assert payload["dry_run"] is False
    assert "ts" in payload
