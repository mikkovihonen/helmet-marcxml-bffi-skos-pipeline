"""M3 SPARQL CONSTRUCT loader + per-record execution.

Two CONSTRUCTs live under ``sparql/``: ``bf_to_bffi_work.rq`` and
``bf_to_bffi_expression.rq``. Both run against each source BIBFRAME
graph; the union of their results is the per-record BFFI graph
``post_process`` then mutates further.

P-38 Phase D: extracted from m3/runner.py to keep the runner focused
on the conversion orchestration. No logic change — moves only.
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


def construct_bffi(source: Graph) -> Graph:
    """Run both CONSTRUCT passes against ``source`` and merge into one graph."""
    register_sparql_functions()
    out = Graph()
    for query in (_work_query(), _expression_query()):
        result = source.query(query)
        for triple in cast("Iterable[tuple[Node, Node, Node]]", result):
            out.add(triple)
    return out
