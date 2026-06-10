"""BIBFRAME ontology scan from ``vocab/bibframe.rdf``.

Parses the vendored LoC BIBFRAME ontology with rdflib and indexes the
class / property hierarchy + the closed term sets. Mirrors the approach
in :mod:`bffi_pipeline.stages.bibframe_to_bffi.mappings` (which does the
same for BFFI's ``lkd.rdf``).

Use this when you need to ask:

  - "Does this ``bf:*`` URI actually exist in BIBFRAME?" — useful for
    catching marc2bibframe2 artifacts (e.g. ``bf:Statement`` which the
    XSLT emits but the ontology doesn't declare).
  - "What are the ancestors of ``bf:Isbn``?" — drives discriminator-by-
    parent-class routings (an Identifier subclass route can be derived
    from the ontology rather than hard-coded).
  - "Which ``bf:*`` terms does BIBFRAME declare that BFFI's lkd.rdf
    doesn't acknowledge?" — surfaces corpus-derived gaps that the
    auto-generated ``docs/bf_to_bffi_mapping.md`` can't catch (since
    that generator only walks lkd.rdf-declared relations).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Final

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bffi_pipeline.config import get_settings

#: BIBFRAME namespace URI prefix.
BF_NAMESPACE: Final[str] = "http://id.loc.gov/ontologies/bibframe/"


def _is_bf(uri: URIRef) -> bool:
    return str(uri).startswith(BF_NAMESPACE)


@dataclass(frozen=True)
class BibframeOntology:
    """Indexed view of the BIBFRAME ontology surface.

    All sets and mappings are restricted to URIs in the BIBFRAME
    namespace — external references (OWL / RDFS / SKOS / etc.) are
    filtered out at construction time.
    """

    #: All ``bf:*`` URIs declared as ``owl:Class``.
    classes: frozenset[URIRef]
    #: All ``bf:*`` URIs declared as ``owl:ObjectProperty``.
    object_properties: frozenset[URIRef]
    #: All ``bf:*`` URIs declared as ``owl:DatatypeProperty``.
    datatype_properties: frozenset[URIRef]
    #: ``child -> {parent, …}`` for every ``rdfs:subClassOf`` edge where
    #: both endpoints are in the ``bf:`` namespace.
    class_parents: Mapping[URIRef, frozenset[URIRef]] = field(repr=False)
    #: ``child -> {parent, …}`` for every ``rdfs:subPropertyOf`` edge
    #: where both endpoints are in the ``bf:`` namespace.
    property_parents: Mapping[URIRef, frozenset[URIRef]] = field(repr=False)

    # --- membership predicates -------------------------------------------

    def is_known_class(self, uri: URIRef) -> bool:
        return uri in self.classes

    def is_known_property(self, uri: URIRef) -> bool:
        return uri in self.object_properties or uri in self.datatype_properties

    # --- hierarchy walks --------------------------------------------------

    def class_ancestors(self, cls: URIRef) -> frozenset[URIRef]:
        """Transitive closure of ``rdfs:subClassOf`` upward from ``cls``.

        Returns the set of ancestor classes; does NOT include ``cls``
        itself. For ``bf:Person`` returns ``{bf:Agent}``; for an
        identifier subclass like ``bf:Isbn`` returns ``{bf:Identifier}``.
        """
        return frozenset(_walk(self.class_parents, cls))

    def class_descendants(self, cls: URIRef) -> frozenset[URIRef]:
        """Transitive closure of ``rdfs:subClassOf`` downward from ``cls``.

        For ``bf:Identifier`` returns the entire set of identifier
        subclasses (Isbn / Issn / Ean / AudioIssueNumber / Lccn / Upc /
        Ismn / Isrc / Strn / Nbn / MusicPlate / MatrixNumber /
        PublisherNumber / VideoRecordingNumber / OtherIdentifier / …).
        """
        children_index = _invert(self.class_parents)
        return frozenset(_walk(children_index, cls))

    def property_ancestors(self, prop: URIRef) -> frozenset[URIRef]:
        """Transitive closure of ``rdfs:subPropertyOf`` upward from ``prop``."""
        return frozenset(_walk(self.property_parents, prop))

    def is_subclass_of(self, child: URIRef, parent: URIRef) -> bool:
        return parent in self.class_ancestors(child)

    def is_subproperty_of(self, child: URIRef, parent: URIRef) -> bool:
        return parent in self.property_ancestors(child)

    # --- diagnostics ------------------------------------------------------

    def root_classes(self) -> frozenset[URIRef]:
        """Classes with no ``bf:*`` parent — the top of each subtree."""
        return frozenset(c for c in self.classes if not self.class_parents.get(c))

    def root_properties(self) -> frozenset[URIRef]:
        """Properties with no ``bf:*`` parent."""
        return frozenset(
            p
            for p in self.object_properties | self.datatype_properties
            if not self.property_parents.get(p)
        )


# --- helpers -----------------------------------------------------------------


def _walk(edges: Mapping[URIRef, frozenset[URIRef]], start: URIRef) -> Iterator[URIRef]:
    """Breadth-first transitive walk over ``edges`` starting from ``start``.

    Yields each node reachable from ``start`` (excluding ``start`` itself).
    Handles cycles defensively via a visited set.
    """
    visited: set[URIRef] = {start}
    frontier: list[URIRef] = list(edges.get(start, ()))
    while frontier:
        node = frontier.pop()
        if node in visited:
            continue
        visited.add(node)
        yield node
        frontier.extend(edges.get(node, ()))


def _invert(
    edges: Mapping[URIRef, frozenset[URIRef]],
) -> Mapping[URIRef, frozenset[URIRef]]:
    """Invert a parent→? mapping into a child←? (i.e. children-of-X) map."""
    inverted: dict[URIRef, set[URIRef]] = {}
    for child, parents in edges.items():
        for parent in parents:
            inverted.setdefault(parent, set()).add(child)
    return {k: frozenset(v) for k, v in inverted.items()}


# --- loader -----------------------------------------------------------------


@lru_cache(maxsize=1)
def load_ontology(path: Path | None = None) -> BibframeOntology:
    """Parse ``vocab/bibframe.rdf`` and return the indexed ontology.

    Cached — first call pays the ~0.5s rdflib parse + indexing cost;
    subsequent calls return the same instance. Pass an explicit path
    to bypass the cache (e.g. in tests using a fixture snippet).
    """
    if path is None:
        path = get_settings().vocab_dir / "bibframe.rdf"
    g = Graph()
    g.parse(path, format="xml")

    classes: set[URIRef] = set()
    object_properties: set[URIRef] = set()
    datatype_properties: set[URIRef] = set()

    for s, _, _ in g.triples((None, RDF.type, OWL.Class)):
        if isinstance(s, URIRef) and _is_bf(s):
            classes.add(s)
    for s, _, _ in g.triples((None, RDF.type, OWL.ObjectProperty)):
        if isinstance(s, URIRef) and _is_bf(s):
            object_properties.add(s)
    for s, _, _ in g.triples((None, RDF.type, OWL.DatatypeProperty)):
        if isinstance(s, URIRef) and _is_bf(s):
            datatype_properties.add(s)

    class_parents: dict[URIRef, set[URIRef]] = {}
    for s, _, o in g.triples((None, RDFS.subClassOf, None)):
        if isinstance(s, URIRef) and isinstance(o, URIRef) and _is_bf(s) and _is_bf(o):
            class_parents.setdefault(s, set()).add(o)

    property_parents: dict[URIRef, set[URIRef]] = {}
    for s, _, o in g.triples((None, RDFS.subPropertyOf, None)):
        if isinstance(s, URIRef) and isinstance(o, URIRef) and _is_bf(s) and _is_bf(o):
            property_parents.setdefault(s, set()).add(o)

    return BibframeOntology(
        classes=frozenset(classes),
        object_properties=frozenset(object_properties),
        datatype_properties=frozenset(datatype_properties),
        class_parents={k: frozenset(v) for k, v in class_parents.items()},
        property_parents={k: frozenset(v) for k, v in property_parents.items()},
    )
