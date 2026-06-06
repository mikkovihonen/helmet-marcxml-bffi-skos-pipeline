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
    return out
