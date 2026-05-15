"""M6 graph-walk → per-Work :class:`WorkRecord` extraction.

The judge's view of a Work is richer than the embedder's: it splits
*original* and *expression* language, captures variant titles, and
keeps ``date_of_origin``. Stage-isolation rules forbid importing the
M4 / M5 extractors, so this is a parallel implementation rather than
a delegation.

P-38 Phase D: extracted from m6/runner.py. No logic change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from rdflib import Graph, URIRef
from rdflib import Literal as RdfLiteral
from rdflib.namespace import RDF, RDFS

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m6.validation import WorkRecord

#: LoC URI prefixes used to short-code language and content-type values
#: (matches the bffi:language / bffi:content URIs M3 emits).
_LANG_URI_PREFIX: Final[str] = "http://id.loc.gov/vocabulary/languages/"
_CONTENT_URI_PREFIX: Final[str] = "http://id.loc.gov/vocabulary/contentTypes/"


def _first_pref_label(graph: Graph, subject: URIRef) -> str | None:
    for o in graph.objects(subject, V.SKOS.prefLabel):
        if isinstance(o, RdfLiteral):
            return str(o)
    return None


def _strip_loc_prefix(uri: str, prefix: str) -> str | None:
    if uri.startswith(prefix):
        tail = uri[len(prefix) :]
        return tail or None
    return uri.rsplit("/", 1)[-1] if "/" in uri else uri


def _primary_creator(graph: Graph, work: URIRef) -> tuple[str | None, str | None]:
    """Return ``(creator_label, creator_uri)`` for ``work``'s primary contribution."""
    for contrib in graph.objects(work, V.BFFI.contribution):
        if V.BFFI.PrimaryContribution not in set(graph.objects(contrib, RDF.type)):
            continue
        for agent in graph.objects(contrib, V.BFFI.agent):
            if not isinstance(agent, URIRef):
                continue
            for label in graph.objects(agent, RDFS.label):
                if isinstance(label, RdfLiteral):
                    return str(label), str(agent)
            return None, str(agent)
    return None, None


def _expression_summary(graph: Graph, work: URIRef) -> tuple[str | None, str | None, list[str]]:
    """Return (language, content_type, variant_titles) for ``work``'s expressions."""
    expression_language: str | None = None
    content_type: str | None = None
    variant_titles: list[str] = []
    for expr in graph.objects(work, V.BFFI.hasExpression):
        if not isinstance(expr, URIRef):
            continue
        if expression_language is None:
            for lang in graph.objects(expr, V.BFFI.language):
                if isinstance(lang, URIRef):
                    expression_language = _strip_loc_prefix(str(lang), _LANG_URI_PREFIX)
                    break
        if content_type is None:
            for ct in graph.objects(expr, V.BFFI.content):
                if isinstance(ct, URIRef):
                    content_type = _strip_loc_prefix(str(ct), _CONTENT_URI_PREFIX)
                    break
        for var in graph.objects(expr, V.SKOS.altLabel):
            if isinstance(var, RdfLiteral):
                variant_titles.append(str(var))
    return expression_language, content_type, variant_titles


def _origin_date(graph: Graph, work: URIRef) -> str | None:
    for date in graph.objects(work, V.BFFI.originDate):
        return str(date)
    return None


def extract_work_records(graph: Graph) -> dict[str, WorkRecord]:
    """Walk the combined BFFI + BIBFRAME graph and return ``Work URI → WorkRecord``.

    The judge's view of a Work is richer than the embedder's: it splits
    *original* and *expression* language, captures variant titles, and
    keeps ``date_of_origin`` (from ``bffi:originDate``). Stage-isolation
    rules forbid importing the M4 / M5 extractors, so this is a
    parallel implementation rather than a delegation.
    """
    records: dict[str, WorkRecord] = {}
    for work in graph.subjects(RDF.type, V.BFFI.Work):
        if not isinstance(work, URIRef):
            continue
        creator, creator_uri = _primary_creator(graph, work)
        expression_language, content_type, variant_titles = _expression_summary(graph, work)
        records[str(work)] = WorkRecord(
            record_id=str(work),
            creator=creator,
            creator_uri=creator_uri,
            preferred_title=_first_pref_label(graph, work),
            variant_titles=variant_titles,
            original_language=expression_language,  # default: assume mono until M9 splits
            expression_language=expression_language,
            content_type=content_type,
            date_of_origin=_origin_date(graph, work),
            publication_year=None,
        )
    return records


def _load_work_records_from_corpus(corpus_dir: Path) -> dict[str, WorkRecord]:
    """Read all BFFI Turtle + BIBFRAME RDF/XML under ``corpus_dir`` and extract."""
    g = Graph()
    bffi_dir = corpus_dir / "bffi"
    bibframe_dir = corpus_dir / "bibframe"
    if bffi_dir.is_dir():
        for path in sorted(bffi_dir.glob("*.ttl")):
            g.parse(str(path), format="turtle")
    if bibframe_dir.is_dir():
        for path in sorted(bibframe_dir.glob("*.rdf")):
            if not path.name.startswith("_"):
                g.parse(str(path), format="xml")
    return extract_work_records(g)
