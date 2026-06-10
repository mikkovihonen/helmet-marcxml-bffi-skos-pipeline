"""Clean-rename rules extracted from ``vocab/lkd.rdf``.

Implements p-56 Phase 1: every ``bf:*`` term that has a ``bffi:*``
counterpart via ``owl:equivalentClass`` / ``owl:equivalentProperty`` /
``rdfs:subPropertyOf`` becomes a clean rename. This module parses the
BFFI ontology with rdflib (per CLAUDE.md's "never grep lkd.rdf" rule)
and returns the substitution table the BIBFRAME → BFFI runner applies.

The ``rdfs:subPropertyOf`` cases are BFFI's "tightened range" idiom:
``bffi:X rdfs:subPropertyOf bf:Y`` with ``rdfs:range bffi:Z`` where
``bffi:Z owl:equivalentClass bf:Z`` — the BFFI predicate strictly
narrows the BIBFRAME range. Most are unambiguous (one ``bffi:X`` per
``bf:Y``); a few have a representative-vs-not split
(``bffi:date`` / ``bffi:dateOfRepresentativeExpression``). For the
ambiguous cases we pick the lexicographically-first target, which
happens to always be the non-``OfRepresentativeExpression`` variant
— the right default for Helmet's main use case.

Out of scope (revisited in step 7):

- ``bffi-meta:broadMatch`` / ``closeMatch`` rows (genuine semantic
  shifts; need per-case review).
- Discriminator-routed terms (`bf:Hub`, `bf:VariantTitle`, the
  Identifier subclasses, `bf:Audio`, `bf:KeyMode`,
  `bf:mediumOfPerformance`). Step 6 handles these via
  :mod:`bffi_pipeline.stages.bibframe_to_bffi.routings`.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDFS

from bffi_pipeline.config import get_settings

BFFI_NAMESPACE: Final[str] = "http://urn.fi/URN:NBN:fi:schema:bffi:"
BF_NAMESPACE: Final[str] = "http://id.loc.gov/ontologies/bibframe/"
BFLC_NAMESPACE: Final[str] = "http://id.loc.gov/ontologies/bflc/"

#: Source namespaces whose terms ``lkd.rdf`` may declare BFFI aliases for.
#: BIBFRAME is the obvious one; BIBFRAME-LC (``bflc:``) carries
#: marc2bibframe2-specific extensions (``marcKey``, ``simplePlace`` /
#: ``simpleAgent`` / ``simpleDate``, ``nonSortNum``, etc.) for which the
#: BFFI ontology declares parallel ``bffi:*`` properties via
#: ``owl:equivalentProperty``. The rule extractor must consider both
#: namespaces so the rename table is complete; otherwise BFLC terms
#: leak unrenamed into the BFFI emit graph.
_SOURCE_NAMESPACES: Final[tuple[str, ...]] = (BF_NAMESPACE, BFLC_NAMESPACE)


@dataclass(frozen=True)
class CleanRenameRules:
    """Substitution table: BIBFRAME URI -> BFFI URI.

    ``classes`` maps the ``bf:*`` class URI that appears as an
    ``rdf:type`` object to the chosen ``bffi:*`` counterpart.
    ``predicates`` does the same for ``bf:*`` predicate URIs that
    appear in the predicate slot of a triple.
    """

    classes: dict[URIRef, URIRef]
    predicates: dict[URIRef, URIRef]

    def rename(self, uri: URIRef) -> URIRef:
        """Return the BFFI URI for ``uri`` if a rename applies, else ``uri``.

        Looks up both the class table and the predicate table; in
        Phase 1 there's no URI that's both a class and a predicate, so
        the order doesn't matter.
        """
        renamed = self.classes.get(uri)
        if renamed is not None:
            return renamed
        renamed = self.predicates.get(uri)
        if renamed is not None:
            return renamed
        return uri


def _is_bf(uri: URIRef) -> bool:
    """True for any source-namespace URI (BIBFRAME or BIBFRAME-LC)."""
    return any(str(uri).startswith(ns) for ns in _SOURCE_NAMESPACES)


def _is_bffi(uri: URIRef) -> bool:
    return str(uri).startswith(BFFI_NAMESPACE)


def _collect_directional_equivalences(g: Graph, predicate: URIRef) -> dict[URIRef, URIRef]:
    """Walk every ``bffi:* <predicate> bf:*`` (and reverse) triple in ``g``.

    Returns a ``bf -> bffi`` dict. If multiple BFFI terms point at the
    same ``bf:`` URI (rare for ``equivalentClass`` / ``equivalentProperty``;
    none in the current `lkd.rdf`), the lexicographically-first wins
    deterministically.
    """
    out: dict[URIRef, URIRef] = {}
    candidates: dict[URIRef, list[URIRef]] = {}
    for s, _, o in g.triples((None, predicate, None)):
        if not isinstance(s, URIRef) or not isinstance(o, URIRef):
            continue
        if _is_bffi(s) and _is_bf(o):
            candidates.setdefault(o, []).append(s)
        elif _is_bf(s) and _is_bffi(o):
            candidates.setdefault(s, []).append(o)
    for bf_uri, bffi_uris in candidates.items():
        out[bf_uri] = sorted(bffi_uris, key=str)[0]
    return out


def _collect_subproperty_of_renames(g: Graph) -> dict[URIRef, URIRef]:
    """Walk ``bffi:* rdfs:subPropertyOf bf:*`` triples.

    Unlike ``owl:equivalentProperty``, this relation is directional —
    only ``bffi:X subPropertyOf bf:Y`` counts (the reverse direction
    would mean BIBFRAME predicates are narrower than BFFI, which isn't
    BFFI's design). Returns the ``bf -> bffi`` dict; lexicographic
    tie-break for the (rare) multi-target cases consistently picks the
    non-``OfRepresentativeExpression`` variant.
    """
    out: dict[URIRef, URIRef] = {}
    candidates: dict[URIRef, list[URIRef]] = {}
    for s, _, o in g.triples((None, RDFS.subPropertyOf, None)):
        if not isinstance(s, URIRef) or not isinstance(o, URIRef):
            continue
        if _is_bffi(s) and _is_bf(o):
            candidates.setdefault(o, []).append(s)
    for bf_uri, bffi_uris in candidates.items():
        out[bf_uri] = sorted(bffi_uris, key=str)[0]
    return out


@lru_cache(maxsize=1)
def load_rules(lkd_rdf_path: Path | None = None) -> CleanRenameRules:
    """Parse ``vocab/lkd.rdf`` and return the substitution table.

    Cached: first call pays the ~1s rdflib parse cost; subsequent calls
    return the same instance. Pass an explicit path to bypass the cache
    (e.g. in tests using a fixture ontology snippet).

    The predicate table merges ``owl:equivalentProperty`` rules (the
    bidirectional direct-equivalence cases) with
    ``rdfs:subPropertyOf`` rules (BFFI's tightened-range idiom).
    Equivalent-property mappings win on key collision since they're
    the more general substitution.
    """
    if lkd_rdf_path is None:
        lkd_rdf_path = get_settings().vocab_dir / "lkd.rdf"
    g = Graph()
    g.parse(lkd_rdf_path, format="xml")

    predicates = _collect_subproperty_of_renames(g)
    # owl:equivalentProperty entries override any subPropertyOf entry on
    # the same bf:X (the direct equivalence is the stronger relation).
    predicates.update(_collect_directional_equivalences(g, OWL.equivalentProperty))

    return CleanRenameRules(
        classes=_collect_directional_equivalences(g, OWL.equivalentClass),
        predicates=predicates,
    )
