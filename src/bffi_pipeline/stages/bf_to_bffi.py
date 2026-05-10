"""Stage M3: BIBFRAME to BFFI Work + Expression.

Runs the two CONSTRUCTs in ``sparql/`` against each ``<output_dir>/bibframe/<id>.rdf``,
combines them, post-processes ``skos:prefLabel`` with language tags derived
from ``bf:language``, validates against ``config/shapes/bffi.shape.ttl``
(Boundary 3 — *non-blocking*), and writes a Turtle file per record.

Per ``docs/BUILD_PLAN.md`` M3 the SHACL failures do not halt the pipeline.
Counts and per-record validation reports go to
``<output_dir>/bffi/_validation.jsonl``; the CLI prints a summary warning.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Final, cast

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS
from rdflib.term import Node

from bffi_pipeline.config import get_settings
from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.uris import register_sparql_functions
from bffi_pipeline.validation.bffi import validate_graph

_BFFI_PIPELINE_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
_SPARQL_DIR: Final[Path] = _BFFI_PIPELINE_REPO_ROOT / "sparql"

_LANG_URI_PREFIX: Final[str] = "http://id.loc.gov/vocabulary/languages/"
# 3-letter MARC language code -> BCP-47 2-letter for the languages this
# pipeline displays (fi/sv/en); other codes leave prefLabel untagged.
_LANG_3_TO_2: Final[dict[str, str]] = {
    "fin": "fi",
    "swe": "sv",
    "eng": "en",
}

SKOS_prefLabel: Final[URIRef] = URIRef("http://www.w3.org/2004/02/skos/core#prefLabel")


# --- Public dataclasses ---------------------------------------------------


@dataclass(frozen=True)
class ValidationRow:
    """One row of ``_validation.jsonl`` per (Boundary-3-failing) record."""

    helmet_bib_id: str
    output_file: str
    conforms: bool
    report_text: str


@dataclass
class BffiSummary:
    """Aggregate counts for an end-of-run report."""

    converted: list[str] = field(default_factory=list)
    skipped_idempotent: list[str] = field(default_factory=list)
    failed_shape: list[str] = field(default_factory=list)
    errored: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        """Total number of input files seen, excluding shape-only flags."""
        return len(self.converted) + len(self.skipped_idempotent) + len(self.errored)

    def render(self) -> str:
        """Format this summary as paste-ready text for the bf-to-bffi CLI."""
        lines = [
            f"BIBFRAME to BFFI conversion summary ({self.total} input file(s))",
            f"  converted: {len(self.converted)}",
            f"  skipped (already converted): {len(self.skipped_idempotent)}",
            f"  shape-failing (kept; flagged): {len(self.failed_shape)}",
            f"  errored: {len(self.errored)}",
        ]
        if self.failed_shape:
            lines.append("Shape-failing records:")
            lines.extend(f"  - {bib}" for bib in self.failed_shape)
        if self.errored:
            lines.append("Hard errors (record skipped):")
            lines.extend(f"  - {bib}: {msg}" for bib, msg in self.errored)
        return "\n".join(lines)


# --- Caching --------------------------------------------------------------


@lru_cache(maxsize=1)
def _work_query() -> str:
    return (_SPARQL_DIR / "bf_to_bffi_work.rq").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _expression_query() -> str:
    return (_SPARQL_DIR / "bf_to_bffi_expression.rq").read_text(encoding="utf-8")


# --- CONSTRUCT runner -----------------------------------------------------


def construct_bffi(source: Graph) -> Graph:
    """Run both CONSTRUCT passes against ``source`` and merge into one graph."""
    register_sparql_functions()
    out = Graph()
    for query in (_work_query(), _expression_query()):
        result = source.query(query)
        for triple in cast("Iterable[tuple[Node, Node, Node]]", result):
            out.add(triple)
    return out


# --- Post-processing ------------------------------------------------------


def _candidate_languages(source: Graph) -> frozenset[str]:
    """Return BCP-47 candidate codes from the main ``bf:Work``'s ``bf:language``.

    Only walks URIRef-typed ``bf:Work`` subjects that aren't referenced
    via ``bf:associatedResource`` — i.e. only the main Work counts.
    marc2bibframe2 emits a separate ``Note otx`` sub-node carrying
    ``bf:language`` for the *translated-from* language (MARC 041 $h);
    aggregate records emit ``bf:language`` on contained Works too.
    Both pollute downstream language detection if not filtered.
    """
    contained: set[URIRef] = {
        o
        for _, _, o in source.triples((None, V.BF.associatedResource, None))
        if isinstance(o, URIRef)
    }
    codes: set[str] = set()
    for work in source.subjects(RDF.type, V.BF.Work):
        if not isinstance(work, URIRef) or work in contained:
            continue
        for lang in source.objects(work, V.BF.language):
            if isinstance(lang, URIRef) and str(lang).startswith(_LANG_URI_PREFIX):
                code3 = str(lang)[len(_LANG_URI_PREFIX) :]
                if code3 in _LANG_3_TO_2:
                    codes.add(_LANG_3_TO_2[code3])
                elif code3 == "rus":
                    codes.add("ru")
    return frozenset(codes)


def _retag_pref_labels(graph: Graph, candidates: frozenset[str]) -> None:
    """Replace untagged ``skos:prefLabel`` literals with split + per-language ones.

    For each untagged ``skos:prefLabel`` literal, runs
    :func:`bffi_pipeline.title_lang.tag_title` against the cataloguer's
    declared language candidates. Emits one labeled prefLabel per
    confidently-detected segment (or one fallback label on the whole
    string when splitting / detection didn't help).
    """
    from bffi_pipeline.title_lang import tag_title

    to_remove: list[tuple[URIRef, URIRef, Literal]] = []
    to_add: list[tuple[URIRef, URIRef, Literal]] = []
    for s, _, o in graph.triples((None, SKOS_prefLabel, None)):
        if not isinstance(o, Literal) or o.language or not isinstance(s, URIRef):
            continue
        tagged = tag_title(str(o), candidates)
        if not tagged:
            continue
        to_remove.append((s, SKOS_prefLabel, o))
        for seg in tagged:
            literal = Literal(seg.text, lang=seg.lang) if seg.lang else Literal(seg.text)
            to_add.append((s, SKOS_prefLabel, literal))
    for triple in to_remove:
        graph.remove(triple)
    for triple in to_add:
        graph.add(triple)


def post_process(bffi_graph: Graph, source: Graph) -> Graph:
    """Mutate ``bffi_graph`` in place: tag prefLabels, bind namespaces."""
    candidates = _candidate_languages(source)
    if candidates:
        _retag_pref_labels(bffi_graph, candidates)
    bffi_graph.bind("bf", V.BF)
    bffi_graph.bind("bffi", V.BFFI)
    bffi_graph.bind("bib", V.BIB)
    bffi_graph.bind("rdf", RDF)
    bffi_graph.bind("rdfs", RDFS)
    bffi_graph.bind("skos", V.SKOS)
    return bffi_graph


# --- Driver ---------------------------------------------------------------


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _is_output_fresh(input_path: Path, output_path: Path) -> bool:
    return output_path.exists() and output_path.stat().st_mtime >= input_path.stat().st_mtime


def _iter_bibframe_files(bibframe_dir: Path) -> Iterator[Path]:
    yield from sorted(p for p in bibframe_dir.glob("*.rdf") if not p.name.startswith("_"))


def _append_jsonl(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _convert_one(input_path: Path, output_path: Path) -> Graph:
    source = Graph()
    source.parse(str(input_path), format="xml")
    bffi_graph = construct_bffi(source)
    post_process(bffi_graph, source)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(output_path, bffi_graph.serialize(format="turtle").encode("utf-8"))
    return bffi_graph


def run(
    bibframe_dir: Path | None = None,
    *,
    output_dir: Path | None = None,
    force: bool = False,
) -> BffiSummary:
    """Convert every ``<bibframe_dir>/<id>.rdf`` to a BFFI Turtle file."""
    base = output_dir or get_settings().data_dir
    bibframe_dir = bibframe_dir or (base / "bibframe")
    summary = BffiSummary()
    validation_path = base / "bffi" / "_validation.jsonl"

    for rdf_path in _iter_bibframe_files(bibframe_dir):
        bib_id = rdf_path.stem
        out_path = base / "bffi" / f"{bib_id}.ttl"
        if not force and _is_output_fresh(rdf_path, out_path):
            summary.skipped_idempotent.append(bib_id)
            continue

        try:
            graph = _convert_one(rdf_path, out_path)
        except Exception as exc:
            summary.errored.append((bib_id, str(exc)))
            continue

        report = validate_graph(graph)
        if not report.conforms:
            summary.failed_shape.append(bib_id)
            _append_jsonl(
                validation_path,
                asdict(
                    ValidationRow(
                        helmet_bib_id=bib_id,
                        output_file=str(out_path.name),
                        conforms=False,
                        report_text=report.text,
                    )
                ),
            )
        summary.converted.append(bib_id)

    return summary


__all__ = [
    "BffiSummary",
    "ValidationRow",
    "construct_bffi",
    "post_process",
    "run",
]
