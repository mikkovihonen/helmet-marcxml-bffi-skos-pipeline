"""M5 graph-walk → per-Work embedding-input extraction.

Walks a merged BFFI + BIBFRAME graph and yields one
:class:`WorkEmbeddingInput` per ``bffi:Work``: creator from the first
primary contribution, title from ``skos:prefLabel``, language /
content-type from the Work's expressions, year from
``bffi:originDate``. ``embedding_input_string`` then renders that tuple
into the fixed-format string the embedder sees, and
``to_blocking_key`` reproduces M4's Stage-1 blocking key for it.

P-38 Phase D: extracted from m5/runner.py. No logic change.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Final

from rdflib import Graph, URIRef
from rdflib import Literal as RdfLiteral
from rdflib.namespace import RDF, RDFS

from bffi_pipeline.blocking import compute_blocking_key
from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m5.schemas import WorkEmbeddingInput

_LANG_URI_PREFIX: Final[str] = "http://id.loc.gov/vocabulary/languages/"
_CONTENT_URI_PREFIX: Final[str] = "http://id.loc.gov/vocabulary/contentTypes/"
_YEAR_RE: Final[re.Pattern[str]] = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def _short_segment(uri_or_value: str | None, prefix: str) -> str | None:
    """Strip a known LoC prefix to a short code; passthrough otherwise."""
    if not uri_or_value:
        return None
    s = uri_or_value.strip()
    if not s:
        return None
    if s.startswith(prefix):
        return s[len(prefix) :] or None
    return s.rsplit("/", 1)[-1] if "/" in s else s


def _normalise_year(value: str | None) -> str | None:
    """Pull the first 4-digit year from ``value`` if present."""
    if not value:
        return None
    m = _YEAR_RE.search(value)
    return m.group(1) if m else None


def embedding_input_string(work: WorkEmbeddingInput) -> str:
    """Build the fixed-order input string the embedder sees.

    The format matches spec § 6 Stage 2: pipe-separated, fixed field
    order ``creator | title | language | year | type``. Empty fields
    are kept as ``"<field>:"`` so re-embedding the same Work always
    produces an identical vector regardless of which fields were
    populated.
    """

    def part(label: str, value: str | None) -> str:
        """Render one ``label: value`` segment, leaving empty values as ``label:``."""
        return f"{label}: {(value or '').strip()}"

    return " | ".join(
        (
            part("creator", work.creator),
            part("title", work.title),
            part("language", work.language),
            part("year", work.year),
            part("type", work.content_type),
        )
    )


def _first_pref_label(graph: Graph, subject: URIRef) -> str | None:
    for o in graph.objects(subject, V.SKOS.prefLabel):
        if isinstance(o, RdfLiteral):
            return str(o)
    return None


def _primary_agent_uris(graph: Graph, work: URIRef) -> list[URIRef]:
    agents: list[URIRef] = []
    for contrib in graph.objects(work, V.BFFI.contribution):
        types = set(graph.objects(contrib, RDF.type))
        if V.BFFI.PrimaryContribution not in types:
            continue
        for agent in graph.objects(contrib, V.BFFI.agent):
            if isinstance(agent, URIRef):
                agents.append(agent)
    return agents


def _agent_label(graph: Graph, agent: URIRef) -> str | None:
    for label in graph.objects(agent, RDFS.label):
        if isinstance(label, RdfLiteral):
            return str(label)
    return None


def _expression_objects(graph: Graph, work: URIRef, predicate: URIRef) -> Iterator[str | URIRef]:
    """Yield ``work``'s expressions' values for a given Expression-side predicate."""
    for expr in graph.objects(work, V.BFFI.hasExpression):
        if not isinstance(expr, URIRef):
            continue
        for obj in graph.objects(expr, predicate):
            yield obj if isinstance(obj, URIRef) else str(obj)


def _first_short_segment(graph: Graph, work: URIRef, predicate: URIRef, prefix: str) -> str | None:
    for value in _expression_objects(graph, work, predicate):
        short = _short_segment(str(value), prefix)
        if short:
            return short
    return None


def _origin_year(graph: Graph, work: URIRef) -> str | None:
    for date in graph.objects(work, V.BFFI.originDate):
        year = _normalise_year(str(date))
        if year:
            return year
    return None


def extract_embedding_inputs(graph: Graph) -> Iterator[WorkEmbeddingInput]:
    """Walk a combined BFFI + BIBFRAME graph and yield per-Work embedding inputs."""
    for work in graph.subjects(RDF.type, V.BFFI.Work):
        if not isinstance(work, URIRef):
            continue
        title = _first_pref_label(graph, work)
        creator: str | None = None
        for agent in _primary_agent_uris(graph, work):
            label = _agent_label(graph, agent)
            if label:
                creator = label
                break
        language = _first_short_segment(graph, work, V.BFFI.language, _LANG_URI_PREFIX)
        content = _first_short_segment(graph, work, V.BFFI.content, _CONTENT_URI_PREFIX)
        yield WorkEmbeddingInput(
            work_uri=str(work),
            creator=creator,
            title=title,
            language=language,
            year=_origin_year(graph, work),
            content_type=content,
        )


def to_blocking_key(work: WorkEmbeddingInput) -> str:
    """Compose the same Stage-1 blocking key M4 produces for ``work``."""
    return compute_blocking_key(
        {
            "creator": work.creator,
            "title": work.title,
            "content_type": work.content_type,
        }
    )
