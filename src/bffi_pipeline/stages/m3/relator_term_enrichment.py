"""Post-M3 enrichment that lifts Finnish / Swedish MARC ``$e`` relator
terms into LoC relator URIs.

The M3 SPARQL CONSTRUCTs route ``bf:role`` as a blank node carrying
``rdfs:label "kirjoittaja"`` (or whatever the cataloguer typed in
MARC 100/700/710/711 ``$e``). Helmet MARC essentially never has the
``$4`` relator code present in the source (only 10 occurrences in
473k records per the 2026-06-07 corpus inventory), so the BIBFRAME
``bf:role`` URI form is unavailable.

This pass walks every ``bf:role`` blank node in the M3 output and
— when its ``rdfs:label`` resolves through the curated
:data:`~bffi_pipeline.marc_relator_terms.RELATOR_TERM_TO_URI`
table — adds a sibling ``bf:role <relators/CODE>`` triple on the
parent contribution node. The original blank node and its label
are left intact so the round-trip MARC converter can emit the
cataloguer's original ``$e`` term while ``$4`` comes from the URI.

Coverage from the inventory: top 100 mapped terms cover > 99% of
all ``$e`` occurrences across MARC tags 100 / 110 / 111 / 700 /
710 / 711. Terms not in the table fall through untouched — the
blank-node label still produces a free-text ``$e`` in the
round-trip, just without ``$4``. That matches the spec's
"errors over silent fallbacks" rule by NOT guessing a code for
ambiguous terms like ``johtaja`` (director / conductor /
manager).
"""

from __future__ import annotations

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDFS

from bffi_pipeline.marc_relator_terms import lookup_relator_uri
from bffi_pipeline.provenance import vocab as V


def enrich_role_uris(graph: Graph) -> int:
    """Add ``bf:role <LoC-URI>`` triples for every blank-node role
    whose ``rdfs:label`` matches a curated MARC-FI relator term.

    Returns the number of contributions enriched (one URI added
    per matched contribution; multiple roles on the same
    contribution may add multiple URIs).

    Idempotent: re-running on an already-enriched graph adds
    nothing because rdflib triple-set semantics drop duplicates
    and the lookup is deterministic.
    """
    added = 0
    # Materialise the iteration so we can mutate ``graph`` inside
    # the loop without disturbing the underlying triple store
    # iterator.
    for contrib, _p, role in list(graph.triples((None, V.BF.role, None))):
        if isinstance(role, URIRef):
            continue  # already carries an authority URI
        for label in graph.objects(role, RDFS.label):
            if not isinstance(label, Literal):
                continue
            uri = lookup_relator_uri(str(label))
            if uri is None:
                continue
            new_triple = (contrib, V.BF.role, URIRef(uri))
            if new_triple in graph:
                continue
            graph.add(new_triple)
            added += 1
            # Don't break — a contribution may carry several roles
            # via separate blank nodes; each gets evaluated
            # independently. Inside the inner loop, a single role
            # node typically has one rdfs:label, but multiple
            # language-tagged labels on the same node would each
            # try the lookup; the duplicate-URI guard above keeps
            # the result idempotent.
    return added


__all__ = ["enrich_role_uris"]
