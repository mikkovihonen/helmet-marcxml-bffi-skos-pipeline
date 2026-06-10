"""Cross-map every BIBFRAME term against `lkd.rdf` via bounded BFS.

For each ``bf:*`` URI BIBFRAME 3.0.1 declares, walk the combined
edge set across both vocabularies and report whether — and how — a
``bffi:*`` equivalent is reachable within a hop bound. The output
gives the operator four buckets:

  - **Direct**: a 1-hop ``owl:equivalentClass`` / ``owl:equivalentProperty``
    link to a ``bffi:*`` term. The conversion pipeline's clean-rename
    pass already covers these.
  - **Indirect**: 2 or more hops via taxonomy
    (``rdfs:subClassOf`` / ``rdfs:subPropertyOf`` in either ontology),
    ``bffi-meta:{broadMatch, closeMatch, exactMatch, narrowMatch}``,
    or ``owl:sameAs``. These are the rich "ancestor-mapped" patterns
    our ontology-driven routings exploit (e.g. ``bf:Isbn`` reaches
    ``bffi:Identifier`` via ``[bf:subClassOf] → [equivalentClass]``).
  - **Routed**: no ``bffi:*`` reach within the bound, but covered by
    a discriminator routing in the pipeline (``ROUTING_REGISTRY``).
    Includes both active rewrites (e.g. ``bf:Hub`` → ``bffi:Work`` via
    marcKey discriminator) and defensive drops. Without this bucket
    these would show up as ``unreachable`` even though the pipeline
    handles them.
  - **Unreachable**: no ``bffi:*`` term reached AND no routing handler.
    The genuine ontology gaps — pending NLF input, a new routing,
    or a future BFFI release.

Run via ``bffi-pipeline diagnose-mappings`` or programmatically via
:func:`analyze_mapping_coverage`.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bffi_pipeline.config import get_settings
from bffi_pipeline.stages.bibframe_to_bffi.routings import ROUTING_REGISTRY

BF_NAMESPACE: Final[str] = "http://id.loc.gov/ontologies/bibframe/"
BFFI_NAMESPACE: Final[str] = "http://urn.fi/URN:NBN:fi:schema:bffi:"
BFFI_META_NAMESPACE: Final[str] = "http://urn.fi/URN:NBN:fi:schema:bffi-meta:"

#: Default depth bound for the BFS walk. 3 hops covers the longest
#: known indirect chain we exploit in production (Title-variant
#: subclasses: ``bf:AbbreviatedTitle`` -> ``bf:VariantTitle`` ->
#: ``bf:Title`` -> ``bffi:Title``).
DEFAULT_MAX_HOPS: Final[int] = 3

#: Relation kinds tracked on each edge. The string labels appear in
#: the printed Path output so the operator can read the chain.
RelationKind = Literal[
    "equivalentClass",
    "equivalentProperty",
    "sameAs",
    "subClassOf",
    "subPropertyOf",
    "bf:subClassOf",
    "bf:subPropertyOf",
    "broadMatch",
    "closeMatch",
    "exactMatch",
    "narrowMatch",
]


@dataclass(frozen=True)
class PathStep:
    """One edge in a BFS walk."""

    source: URIRef
    relation: RelationKind
    target: URIRef


#: Per-relation quality cost used to tiebreak between paths of equal
#: hop count. Lower number = preferred. The intent: direct equivalences
#: outrank semantic-shifts; taxonomic walks outrank broadMatch.
_RELATION_COST: Final[dict[str, int]] = {
    "equivalentClass": 0,
    "equivalentProperty": 0,
    "sameAs": 1,
    "exactMatch": 2,
    "closeMatch": 3,
    "subPropertyOf": 4,
    "subClassOf": 5,
    "bf:subClassOf": 6,
    "bf:subPropertyOf": 6,
    "narrowMatch": 7,
    "broadMatch": 8,
}


def _path_cost(path: tuple[PathStep, ...]) -> tuple[int, int, str]:
    """Sort key for picking the highest-quality path to a ``bffi:*`` URI.

    Tuple ordering: (hop count, summed relation cost, target URI). BFS
    already finds the shortest path per target, so within a single
    Reach the hop count is fixed; but across DIFFERENT targets, we
    prefer (a) the shortest, then (b) the highest-quality relations on
    the path, then (c) lex-min URI for determinism.
    """
    hops = len(path)
    cost = sum(_RELATION_COST.get(step.relation, 99) for step in path)
    last = str(path[-1].target) if path else ""
    return (hops, cost, last)


@dataclass(frozen=True)
class Reach:
    """The set of ``bffi:*`` terms reachable from one ``bf:*`` start."""

    bf_term: URIRef
    #: One entry per reached ``bffi:*`` URI, keyed by the URI, value =
    #: the shortest path discovered to it (BFS guarantees shortest).
    bffi_paths: dict[URIRef, tuple[PathStep, ...]]

    @property
    def best(self) -> tuple[URIRef, tuple[PathStep, ...]] | None:
        """Pick the highest-quality reachable ``bffi:*`` URI.

        Ranks by (a) shortest hop count, (b) lowest summed relation
        cost (prefers equivalentClass/Property over semantic-shifts),
        (c) lex-min URI. Returns ``None`` if no ``bffi:*`` was reached.
        """
        if not self.bffi_paths:
            return None
        return min(self.bffi_paths.items(), key=lambda kv: _path_cost(kv[1]))

    @property
    def has_direct_equivalence(self) -> bool:
        """True iff any reached path is a 1-hop ``owl:equivalent*`` link.

        Defines the "direct" bucket: a term lands there when at least
        one path is a clean equivalence, regardless of whether other
        looser paths (broadMatch / etc.) also exist.
        """
        for path in self.bffi_paths.values():
            if len(path) == 1 and path[0].relation in (
                "equivalentClass",
                "equivalentProperty",
            ):
                return True
        return False


@dataclass(frozen=True)
class CoverageReport:
    """Outcome of analysing every ``bf:*`` term in BIBFRAME against ``lkd.rdf``.

    Four buckets:

      - **direct**: 1-hop ``owl:equivalent*`` to a ``bffi:*`` term.
      - **indirect**: 2+ hops via taxonomy / ``bffi-meta:*Match``.
      - **routed**: no ``lkd.rdf`` reach within the bound, but covered by
        a discriminator routing in the pipeline (``ROUTING_REGISTRY``).
        Includes drops as well as active rewrites.
      - **unreachable**: no ``lkd.rdf`` reach AND no routing handler — the
        true ontology gap, requires NLF input.
    """

    direct: tuple[Reach, ...] = field(default_factory=tuple)
    indirect: tuple[Reach, ...] = field(default_factory=tuple)
    routed: tuple[URIRef, ...] = field(default_factory=tuple)
    unreachable: tuple[URIRef, ...] = field(default_factory=tuple)
    max_hops: int = DEFAULT_MAX_HOPS

    @property
    def total(self) -> int:
        return len(self.direct) + len(self.indirect) + len(self.routed) + len(self.unreachable)

    def summary_text(self) -> str:
        return (
            f"BIBFRAME terms analysed: {self.total}\n"
            f"  direct (1-hop equivalentClass/Property): {len(self.direct)}\n"
            f"  indirect (2-{self.max_hops} hops via taxonomy / meta): "
            f"{len(self.indirect)}\n"
            f"  routed (no lkd.rdf reach, handled by routing code): "
            f"{len(self.routed)}\n"
            f"  unreachable (true GAPs — no path, no routing): "
            f"{len(self.unreachable)}"
        )


# --- internals ---------------------------------------------------------------


def _is_bf(uri: URIRef) -> bool:
    return str(uri).startswith(BF_NAMESPACE)


def _is_bffi(uri: URIRef) -> bool:
    return str(uri).startswith(BFFI_NAMESPACE)


_LKD_EDGE_PREDICATES: Final[tuple[tuple[URIRef, RelationKind], ...]] = (
    (OWL.equivalentClass, "equivalentClass"),
    (OWL.equivalentProperty, "equivalentProperty"),
    (OWL.sameAs, "sameAs"),
    (RDFS.subClassOf, "subClassOf"),
    (RDFS.subPropertyOf, "subPropertyOf"),
    (URIRef(BFFI_META_NAMESPACE + "broadMatch"), "broadMatch"),
    (URIRef(BFFI_META_NAMESPACE + "closeMatch"), "closeMatch"),
    (URIRef(BFFI_META_NAMESPACE + "exactMatch"), "exactMatch"),
    (URIRef(BFFI_META_NAMESPACE + "narrowMatch"), "narrowMatch"),
)


def _build_edges(
    bibframe_graph: Graph, lkd_graph: Graph
) -> dict[URIRef, list[tuple[URIRef, RelationKind]]]:
    """Build the symmetric adjacency list from both ontologies' relations.

    All edges are added bidirectionally — the walker doesn't care whose
    side a ``subClassOf`` originated from; it walks the union as an
    undirected graph for discovery purposes. The relation label is
    preserved on each half-edge so the printed chain is readable.
    """
    edges: dict[URIRef, list[tuple[URIRef, RelationKind]]] = {}

    def add(a: URIRef, b: URIRef, relation: RelationKind) -> None:
        edges.setdefault(a, []).append((b, relation))
        edges.setdefault(b, []).append((a, relation))

    for predicate, label in _LKD_EDGE_PREDICATES:
        for s, _, o in lkd_graph.triples((None, predicate, None)):
            if isinstance(s, URIRef) and isinstance(o, URIRef):
                add(s, o, label)

    for predicate, label in (
        (RDFS.subClassOf, "bf:subClassOf"),
        (RDFS.subPropertyOf, "bf:subPropertyOf"),
    ):
        for s, _, o in bibframe_graph.triples((None, predicate, None)):
            if isinstance(s, URIRef) and isinstance(o, URIRef):
                add(s, o, label)

    return edges


def _collect_bf_terms(bibframe_graph: Graph) -> set[URIRef]:
    """Every ``bf:*`` URI BIBFRAME declares as a class or property."""
    out: set[URIRef] = set()
    for rdf_type in (OWL.Class, OWL.ObjectProperty, OWL.DatatypeProperty):
        for s in bibframe_graph.subjects(RDF.type, rdf_type):
            if isinstance(s, URIRef) and _is_bf(s):
                out.add(s)
    return out


def _bfs(
    start: URIRef,
    edges: dict[URIRef, list[tuple[URIRef, RelationKind]]],
    max_hops: int,
) -> dict[URIRef, tuple[PathStep, ...]]:
    """BFS from ``start`` up to ``max_hops``; return reached ``bffi:*`` URIs."""
    visited: set[URIRef] = {start}
    queue: deque[tuple[URIRef, tuple[PathStep, ...]]] = deque([(start, ())])
    reached: dict[URIRef, tuple[PathStep, ...]] = {}
    while queue:
        node, path = queue.popleft()
        if len(path) >= max_hops:
            continue
        for neighbour, relation in edges.get(node, ()):
            if neighbour in visited:
                continue
            visited.add(neighbour)
            new_path = (*path, PathStep(source=node, relation=relation, target=neighbour))
            if _is_bffi(neighbour) and neighbour not in reached:
                reached[neighbour] = new_path
            queue.append((neighbour, new_path))
    return reached


def _categorise(reach: Reach) -> Literal["direct", "indirect", "unreachable"]:
    """Decide which bucket a reach falls into.

    A bf:* term is "direct" if *any* reached path is a 1-hop
    ``owl:equivalent*`` link — even if longer or semantic-shift paths
    also exist. Falls back to "indirect" when something is reached but
    no direct equivalence exists; "unreachable" when nothing reached.
    """
    if not reach.bffi_paths:
        return "unreachable"
    if reach.has_direct_equivalence:
        return "direct"
    return "indirect"


# --- public API --------------------------------------------------------------


def analyze_mapping_coverage(
    *,
    bibframe_path: Path | None = None,
    lkd_path: Path | None = None,
    max_hops: int = DEFAULT_MAX_HOPS,
) -> CoverageReport:
    """Cross-map every BIBFRAME-declared ``bf:*`` term against ``lkd.rdf``.

    Default paths come from :func:`bffi_pipeline.config.get_settings`'s
    ``vocab_dir``; passing explicit paths is useful for tests against
    fixture ontology snippets.
    """
    settings = get_settings()
    bibframe_p = bibframe_path or settings.vocab_dir / "bibframe.rdf"
    lkd_p = lkd_path or settings.vocab_dir / "lkd.rdf"

    bibframe_g = Graph()
    bibframe_g.parse(bibframe_p, format="xml")
    lkd_g = Graph()
    lkd_g.parse(lkd_p, format="xml")

    edges = _build_edges(bibframe_g, lkd_g)
    bf_terms = sorted(_collect_bf_terms(bibframe_g), key=str)

    routed_terms: set[URIRef] = set()
    for meta in ROUTING_REGISTRY:
        routed_terms.update(meta.resolve_terms())

    direct: list[Reach] = []
    indirect: list[Reach] = []
    routed: list[URIRef] = []
    unreachable: list[URIRef] = []

    for bf_term in bf_terms:
        reach = Reach(bf_term=bf_term, bffi_paths=_bfs(bf_term, edges, max_hops))
        bucket = _categorise(reach)
        if bucket == "direct":
            direct.append(reach)
        elif bucket == "indirect":
            indirect.append(reach)
        elif bf_term in routed_terms:
            routed.append(bf_term)
        else:
            unreachable.append(bf_term)

    return CoverageReport(
        direct=tuple(direct),
        indirect=tuple(indirect),
        routed=tuple(routed),
        unreachable=tuple(unreachable),
        max_hops=max_hops,
    )


def format_path(path: Iterable[PathStep]) -> str:
    """Pretty-print a chain like ``[bf:subClassOf] → [equivalentClass]``."""
    return " → ".join(f"[{step.relation}]" for step in path)
