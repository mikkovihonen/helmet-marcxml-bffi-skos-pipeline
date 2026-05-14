"""Pre-run Fuseki clear — drop every named graph under ``graph_base`` (P-32 Phase H).

The pipeline writes its canonical Works + provenance graph into the
Fuseki named graphs ``<graph_base>bffi-works`` and
``<graph_base>provenance``. Without intervention, two runs against
the same input layer their triples on top of each other, mixing
prov:Activity URIs and (depending on M9 fallback decisions)
producing duplicate canonical Works for the same source records.
Phase H drops every named graph whose URI starts with
``settings.graph_base`` at the start of each pipeline run.

The preserve-vs-wipe boundary is encoded in the URI namespace, not
in a hand-curated whitelist:

- Pipeline output graphs live under ``settings.graph_base`` (default
  ``http://urn.fi/URN:NBN:fi:bib:graph:``).
- Vocabulary graphs (YSO, KANTO, allars, SLM, MUSO) use their own
  Finto URIs, outside ``graph_base``. They're not touched.

Safety net: ``BFFI_FUSEKI_CLEAR_MAX_TRIPLES`` (default 100 M)
refuses to drop a graph larger than the threshold unless the
operator passes ``force=True``. Catches misconfigured ``graph_base``
where a vocab URI accidentally matches the prefix.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from bffi_pipeline.stages.load import delete_graph, run_select

logger = logging.getLogger(__name__)


@dataclass
class ClearResult:
    """Outcome of one ``clear_run_output_graphs`` invocation.

    Surfaced both for the pipeline-init manifest record and for the
    operator-facing ``runs clear-fuseki`` CLI's pretty-print output.
    """

    dropped_graphs: list[str] = field(default_factory=list)
    skipped_oversized: list[tuple[str, int]] = field(default_factory=list)
    total_triples_before: int = 0
    fuseki_reachable: bool = True
    dry_run: bool = False

    def to_manifest_dict(self) -> dict[str, Any]:
        """Serialise the result for the run manifest's ``pre_run_fuseki_clear``."""
        return {
            "dropped_graphs": list(self.dropped_graphs),
            "skipped_oversized": [{"graph": g, "triples": n} for g, n in self.skipped_oversized],
            "total_triples_before": self.total_triples_before,
            "fuseki_reachable": self.fuseki_reachable,
            "dry_run": self.dry_run,
            "ts": datetime.now(UTC).isoformat(),
        }


def clear_run_output_graphs(
    fuseki_url: str,
    graph_base: str,
    *,
    max_triples_per_graph: int = 100_000_000,
    dry_run: bool = False,
    force: bool = False,
    client: httpx.Client | None = None,
) -> ClearResult:
    """Drop every named graph whose URI starts with ``graph_base``.

    Flow:

    1. SPARQL ``SELECT DISTINCT ?g`` enumerates the named graphs that
       currently have at least one triple.
    2. Filter to graphs prefixed by ``graph_base``.
    3. For each candidate, SPARQL ``SELECT COUNT(*)`` measures the
       triple count.
    4. If count > ``max_triples_per_graph`` and ``force=False``,
       skip the graph and record it under ``skipped_oversized`` —
       defensive against a misconfigured ``graph_base`` that accidentally
       prefix-matches a vocabulary URI.
    5. Otherwise, ``DELETE`` the graph via GSP (unless ``dry_run=True``,
       in which case the URI is added to ``dropped_graphs`` for the
       operator preview but no HTTP delete fires).

    Fuseki unreachable (``httpx.ConnectError`` on the initial SELECT)
    is recorded as ``fuseki_reachable=False`` on the result; the
    caller (pipeline init or ``runs clear-fuseki`` CLI) decides
    whether to abort or continue.

    ``client`` is injectable so tests use ``httpx.MockTransport``.
    """
    should_close_client = False
    if client is None:
        client = httpx.Client(timeout=30.0)
        should_close_client = True

    result = ClearResult(dry_run=dry_run)
    try:
        try:
            candidates = _enumerate_graphs_under_prefix(client, fuseki_url, graph_base)
        except httpx.ConnectError as exc:
            logger.warning(
                "[bffi-pipeline] Fuseki at %s unreachable (%s); skipping pre-run clear.",
                fuseki_url,
                exc,
            )
            result.fuseki_reachable = False
            return result

        for graph_uri in candidates:
            triples = _count_triples_in_graph(client, fuseki_url, graph_uri)
            result.total_triples_before += triples
            if triples > max_triples_per_graph and not force:
                logger.warning(
                    "[bffi-pipeline] Skipping %s: %d triples exceeds safety threshold "
                    "(%d). Pass --force-clear to drop anyway, or check that "
                    "BFFI_GRAPH_BASE isn't accidentally prefix-matching a vocab URI.",
                    graph_uri,
                    triples,
                    max_triples_per_graph,
                )
                result.skipped_oversized.append((graph_uri, triples))
                continue
            if dry_run:
                logger.info(
                    "[bffi-pipeline] (dry-run) would drop %s (%d triples).",
                    graph_uri,
                    triples,
                )
                result.dropped_graphs.append(graph_uri)
                continue
            logger.warning("[bffi-pipeline] Dropping %s (%d triples).", graph_uri, triples)
            if delete_graph(client, fuseki_url=fuseki_url, graph_uri=graph_uri):
                result.dropped_graphs.append(graph_uri)
            else:
                logger.warning(
                    "[bffi-pipeline] Failed to delete %s; leaving it. Operator "
                    "should investigate Fuseki state manually.",
                    graph_uri,
                )
    finally:
        if should_close_client:
            client.close()
    return result


def _enumerate_graphs_under_prefix(
    client: httpx.Client,
    fuseki_url: str,
    graph_base: str,
) -> list[str]:
    """Return the named-graph URIs in Fuseki whose URI starts with ``graph_base``."""
    query = "SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }"
    bindings = run_select(client, fuseki_url=fuseki_url, query=query)
    out: list[str] = []
    for row in bindings:
        graph_node = row.get("g") or {}
        uri = str(graph_node.get("value", ""))
        if uri.startswith(graph_base):
            out.append(uri)
    return sorted(out)


def _count_triples_in_graph(
    client: httpx.Client,
    fuseki_url: str,
    graph_uri: str,
) -> int:
    """Return the triple count for a single named graph."""
    # Use a parameterised query string with an inline IRI — the URI
    # comes from a SELECT result we just ran, so it's already a
    # vetted Fuseki-reported graph URI; injection isn't a concern.
    query = f"SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <{graph_uri}> {{ ?s ?p ?o }} }}"
    bindings = run_select(client, fuseki_url=fuseki_url, query=query)
    if not bindings:
        return 0
    n_node = bindings[0].get("n") or {}
    try:
        return int(str(n_node.get("value", "0")))
    except ValueError:
        return 0


__all__ = ["ClearResult", "clear_run_output_graphs"]
