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

from rdflib import Graph, URIRef
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


def construct_bffi(source: Graph) -> Graph:
    """Run all three CONSTRUCT passes against ``source`` and merge into one graph."""
    register_sparql_functions()
    out = Graph()
    for query in (_work_query(), _expression_query(), _manifestation_query()):
        result = source.query(query)
        for triple in cast("Iterable[tuple[Node, Node, Node]]", result):
            out.add(triple)
    # P-50 fromMarcField passthrough for URI-keyed source entities.
    # M2-post attaches ``bffi-prov:fromMarcField`` to raw-bib URIs of
    # entities marc2bibframe2 mints from each source field (#Agent700-N,
    # #Hub730-N, #Topic650-N, #Place651-N, etc.). Those URIs survive
    # verbatim through the M3 CONSTRUCTs (the BFFI Contribution / Subject
    # links target the same raw URI), so a flat identity copy at the M3
    # boundary lands the token on the BFFI-side counterpart with zero
    # SPARQL touch. Phase C's Statement-subject token is already emitted
    # by the work CONSTRUCT and would be duplicated here — set semantics
    # means it's a no-op. Blank-node-keyed entities (bf:Isbn / bf:Note /
    # bf:Title / bf:Extent / bf:ProvisionActivity that M2-post matched
    # via flat-literal-* paths) are excluded: their bnode identity isn't
    # preserved across rdflib serialise/parse cycles, so a flat copy
    # wouldn't survive the M8 corpus concat. Coverage for those needs a
    # separate URI-minting redesign (tracked separately).
    for s, _p, o in source.triples((None, V.fromMarcField, None)):
        if isinstance(s, URIRef):
            out.add((s, V.fromMarcField, o))
    return out
