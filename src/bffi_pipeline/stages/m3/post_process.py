"""M3 graph post-processing — chains the per-stage helpers carved
out into ``language_detect`` / ``contributions``.

``post_process`` mutates the BFFI graph the CONSTRUCT pass produced
in three ways:

- Tags ``skos:prefLabel`` literals with BCP-47 language codes via
  the Lingua + optional local-LLM cascade
  (:mod:`bffi_pipeline.stages.m3.language_detect`).
- Optionally runs the MARC 245$c contributor-extraction cascade
  (:mod:`bffi_pipeline.stages.m3.contributions`).
- Binds the BFFI / Bibframe / Bib / DCT / RDF / SKOS prefixes on the
  output graph for human-readable Turtle.

P-38 Phase D: extracted from m3/runner.py to keep the runner focused
on the per-record driver loop. No logic change — moves only.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from rdflib import Graph
from rdflib.namespace import DCTERMS, RDF, RDFS

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m3.contributions import _emit_extracted_contributions
from bffi_pipeline.stages.m3.language_detect import _candidate_languages, _retag_pref_labels


def post_process(
    bffi_graph: Graph,
    source: Graph,
    *,
    llm_detector: object | None = None,
    contrib_extractor: object | None = None,
    variants_sidecar_path: Path | None = None,
    now: datetime | None = None,
) -> Graph:
    """Mutate ``bffi_graph`` in place: tag prefLabels, denormalise Helmet
    identifiers for Skosmos display, optionally extract 245$c
    contributors, bind namespaces.

    ``llm_detector`` enables the M3 title-language cascade;
    ``contrib_extractor`` enables the M3 245$c contributor-extraction
    cascade. Either / both can be ``None`` to keep that stage
    graph-only. ``variants_sidecar_path`` is where the cascade
    appends one row per detected transliteration variant; M8's
    binding pass reads the same file.
    """
    candidates = _candidate_languages(source)
    if candidates:
        _retag_pref_labels(bffi_graph, candidates, llm_detector=llm_detector)
    _emit_extracted_contributions(
        bffi_graph,
        source,
        contrib_extractor=contrib_extractor,
        variants_sidecar_path=variants_sidecar_path,
        now=now,
    )
    bffi_graph.bind("bf", V.BF)
    bffi_graph.bind("bffi", V.BFFI)
    bffi_graph.bind("bib", V.BIB)
    bffi_graph.bind("dct", DCTERMS)
    bffi_graph.bind("rdf", RDF)
    bffi_graph.bind("rdfs", RDFS)
    bffi_graph.bind("skos", V.SKOS)
    return bffi_graph
