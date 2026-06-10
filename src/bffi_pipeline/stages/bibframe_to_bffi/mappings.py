"""Clean-rename rules extracted from ``vocab/lkd.rdf``.

Implements p-56 Phase 1: every ``bf:*`` term that has a ``bffi:*``
counterpart via ``owl:equivalentClass`` or ``owl:equivalentProperty``
becomes a clean rename. This module parses the BFFI ontology with
rdflib (per CLAUDE.md's "never grep lkd.rdf" rule) and returns the
substitution table the BIBFRAME → BFFI runner applies.

Out of scope for Phase 1 (revisited in step 6):

- ``rdfs:subPropertyOf bf:X`` rows (BFFI's "tightened range" idiom).
  Some have a single BFFI subproperty per ``bf:X``; others have
  multiple, which needs a per-instance discriminator. Step 6 picks
  these up via the property-discriminator routings.
- ``bffi-meta:broadMatch`` / ``closeMatch`` rows (semantic shifts).
- Discriminator-routed terms (`bf:Hub`, `bf:VariantTitle`, the
  Identifier subclasses, `bf:Audio`, `bf:KeyMode`,
  `bf:mediumOfPerformance`). Step 6 / step 7.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

from rdflib import Graph, URIRef
from rdflib.namespace import OWL

from bffi_pipeline.config import get_settings

BFFI_NAMESPACE: Final[str] = "http://urn.fi/URN:NBN:fi:schema:bffi:"
BF_NAMESPACE: Final[str] = "http://id.loc.gov/ontologies/bibframe/"


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
    return str(uri).startswith(BF_NAMESPACE)


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


@lru_cache(maxsize=1)
def load_rules(lkd_rdf_path: Path | None = None) -> CleanRenameRules:
    """Parse ``vocab/lkd.rdf`` and return the substitution table.

    Cached: first call pays the ~1s rdflib parse cost; subsequent calls
    return the same instance. Pass an explicit path to bypass the cache
    (e.g. in tests using a fixture ontology snippet).
    """
    if lkd_rdf_path is None:
        lkd_rdf_path = get_settings().vocab_dir / "lkd.rdf"
    g = Graph()
    g.parse(lkd_rdf_path, format="xml")

    return CleanRenameRules(
        classes=_collect_directional_equivalences(g, OWL.equivalentClass),
        predicates=_collect_directional_equivalences(g, OWL.equivalentProperty),
    )
