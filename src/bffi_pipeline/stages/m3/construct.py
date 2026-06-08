"""M3 SPARQL CONSTRUCT loader + per-record execution.

Three CONSTRUCTs live under ``sparql/``: ``bf_to_bffi_work.rq``,
``bf_to_bffi_expression.rq``, and ``bf_to_bffi_manifestation.rq``.
All three run against each source BIBFRAME graph; the union of their
results is the per-record BFFI graph ``post_process`` then mutates
further.

P-38 Phase D: extracted from m3/runner.py to keep the runner focused
on the conversion orchestration.

P-45 commit 2: added the third pass (Manifestation). Before this, the
pipeline collapsed Manifestation-level metadata into Work-side
properties; the third class restores BFFI 1.0.0's 4-class ontology
(Work / Expression / Manifestation / Item). The Helmet bib_id moved
from Work / Expression to Manifestation.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Final, cast

from rdflib import Graph
from rdflib.term import Node

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.uris import register_sparql_functions

_BFFI_PIPELINE_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[4]
_SPARQL_DIR: Final[Path] = _BFFI_PIPELINE_REPO_ROOT / "sparql"


@lru_cache(maxsize=1)
def _work_query() -> str:
    return (_SPARQL_DIR / "bf_to_bffi_work.rq").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _expression_query() -> str:
    return (_SPARQL_DIR / "bf_to_bffi_expression.rq").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _manifestation_query() -> str:
    return (_SPARQL_DIR / "bf_to_bffi_manifestation.rq").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _aggregation_query() -> str:
    return (_SPARQL_DIR / "bf_to_bffi_aggregation.rq").read_text(encoding="utf-8")


def construct_bffi(source: Graph) -> Graph:
    """Run all four CONSTRUCT passes against ``source`` and merge into one graph.

    The fourth pass (``bf_to_bffi_aggregation.rq``) is split off from
    the Expression CONSTRUCT to keep its multi-Hub walk from
    cross-producting with the parent Expression's non-primary
    contribution / variant-title OPTIONALs. See the rq file's
    header for the verification numbers behind the split.
    """
    register_sparql_functions()
    out = Graph()
    queries = (
        _work_query(),
        _expression_query(),
        _manifestation_query(),
        _aggregation_query(),
    )
    for query in queries:
        result = source.query(query)
        for triple in cast("Iterable[tuple[Node, Node, Node]]", result):
            out.add(triple)
    # P-50 fromMarcField passthrough. M2-post attaches
    # ``bffi-prov:fromMarcField`` to raw-bib URIs of marc2bibframe2-minted
    # entities (``#Agent700-N`` / ``#Hub730-N`` / ``#Topic650-N`` /
    # ``#Place651-N`` etc.) and to source blank nodes for flat-field
    # entities (``bf:Isbn`` / ``bf:Note`` / ``bf:Title`` / ``bf:Extent`` /
    # ``bf:ProvisionActivity`` from MARC 020 / 500 / 245 / 300 / 264 …).
    # The M3 CONSTRUCTs reference these source nodes verbatim as targets
    # of ``bffi:agent`` / ``bffi:subject`` / ``bf:identifiedBy`` / nested
    # Manifestation links, so a flat identity copy at the M3 boundary
    # lands the token on the BFFI-side counterpart with zero SPARQL
    # touch. URI-keyed subjects survive trivially; bnode-keyed subjects
    # also survive because rdflib preserves bnode identity within a
    # single ``Graph`` and the per-record Turtle serialiser emits a
    # consistent bnode label for each ``BNode`` instance, so the M8
    # corpus concat round-trips the identity within the per-record
    # scope (sufficient for the per-record round-trip diff consumer).
    # Phase C's Statement-subject token is already emitted by the work
    # CONSTRUCT — set semantics dedupes the duplicate.
    for s, _p, o in source.triples((None, V.fromMarcField, None)):
        out.add((s, V.fromMarcField, o))
    return out
