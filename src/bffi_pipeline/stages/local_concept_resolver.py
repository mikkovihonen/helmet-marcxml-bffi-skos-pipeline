"""Tier-0 reconciliation: exact-prefLabel lookup against the locally-loaded
Finto authority graphs in our own Fuseki.

Stage M9 reconciles cataloguer literals against KANTO / YSO / KAUNO /
MUSO / SLM. The default tier-1 path hits Finto's public REST API once
per literal; on the 800k-record corpus that's a Finto-API call per
unresolved subject (tens of thousands of round-trips). Option 3b
already loaded each Finto vocabulary into a named graph in our local
Fuseki for Skosmos browsing — tier-0 reuses those graphs as the first
lookup. If a YSO concept's ``skos:prefLabel`` exactly matches the
cataloguer literal, we bind that URI deterministically without any
HTTP round-trip to api.finto.fi.

This is also the YSA-via-YSO path: the 2014-2018 vocabulary merge
brought YSA's prefLabels into YSO unchanged, so a MARC ``$2 ysa``
literal like ``"Venäjä"`` resolves to the same YSO concept tier-1
would have returned.

Tier-0 currently scopes to subject / genre_form / music_form. Person
names route through KANTO at tier-1 instead — KANTO prefLabels carry
birth-death dates the cataloguer literal doesn't, so exact match is
near-zero hit rate for persons.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Final, Protocol

import httpx

from bffi_pipeline.stages.reconcile import (
    VOCAB_KAUNO,
    VOCAB_MUSO,
    VOCAB_YSO,
    AuthorityKind,
)

#: SLM, LCGFT, and LCSH are loaded into Fuseki by ``load-finto`` but
#: aren't ``VOCAB_*`` constants in :mod:`reconcile` (those mirror
#: Finto's ``vocab=`` query parameter, and tier-0 doesn't use that
#: endpoint). Tier-0 hits each directly via its named graph URI, so we
#: just need a string source-vocabulary tag for provenance.
VOCAB_SLM: Final[str] = "slm"
VOCAB_LCGFT: Final[str] = "lcgft"
VOCAB_LCSH: Final[str] = "lcsh"
VOCAB_ALLARS: Final[str] = "allars"
VOCAB_KAUNOKKI: Final[str] = "kaunokki"

#: Authority kind → (source-vocabulary tag, named-graph URI) tuples.
#: Multiple entries per kind get tried in declaration order in a single
#: SPARQL query via ``VALUES``. Declaration order disambiguates when the
#: same literal happens to be a prefLabel in two graphs.
#:
#: ``genre_form`` lists KAUNO first (CLAUDE.md authority priority for
#: fiction genre/form), then SLM (Finnish genre/form list, separate from
#: KAUNO), then YSO. The YSO fallback exists because cataloguers tag
#: heterogeneous content with ``$2 kaunokki`` (the legacy KAUNO name)
#: in MARC 6XX fields — temporal periods like "1800-luku" and place
#: names like "Lontoo" route through the M9 walker as ``genre_form`` on
#: that signal, but those concepts live in YSO-Aika / YSO-Paikat (loaded
#: into the same Fuseki named graph as YSO general topics). Without the
#: fallback they'd miss tier-0 and fall through to tier-1 ``vocab=kauno``
#: which doesn't carry them either. KAUNO+SLM stay first so genuine
#: fiction genre/form literals like "historialliset romaanit" still bind
#: to their KAUNO/SLM URI even when an equivalent label exists in YSO.
_KIND_TO_GRAPHS: Final[dict[AuthorityKind, tuple[tuple[str, str], ...]]] = {
    "subject": (
        (VOCAB_YSO, "http://www.yso.fi/onto/yso/"),
        # Allars (Swedish general thesaurus) covers $2 allars-tagged
        # Swedish topical literals (ekonomi / gymnasiet etc) — the
        # 200-record corpus smoke surfaced Swedish-language records
        # whose subject literals don't have Finnish prefLabels in YSO.
        # Between YSO and LCSH so the language priority reads
        # Finnish → Swedish → English.
        (VOCAB_ALLARS, "http://www.yso.fi/onto/allars/"),
        # LCSH covers the cataloguer-tagged-$2-lcsh case where an
        # English topical literal lands without $0; YSO comes first
        # because Finnish-source records dominate Helmet.
        (VOCAB_LCSH, "http://id.loc.gov/authorities/subjects/"),
    ),
    "genre_form": (
        (VOCAB_KAUNO, "http://www.yso.fi/onto/kauno/"),
        (VOCAB_SLM, "http://urn.fi/URN:NBN:fi:au:slm:"),
        # Kaunokki/Bella — legacy KAUNO with Swedish parallel labels
        # under the Bella sub-vocab. Cataloguers tag $2 kaunokki
        # (Finnish) or $2 bella (Swedish) on fiction MARC 6XX. After
        # KAUNO+SLM in the lookup order so genuine modern KAUNO
        # bindings still win when both carry the same literal.
        (VOCAB_KAUNOKKI, "http://urn.fi/URN:NBN:fi:au:kaunokki:"),
        # LCGFT covers English-cataloguer-supplied genre/form labels
        # (Novels, Short stories, Video recordings, etc.) when the
        # cataloguer tagged $2 lcgft without $0. Last in the order so
        # KAUNO+SLM Finnish-language preferences still win when both
        # carry the literal.
        (VOCAB_LCGFT, "http://id.loc.gov/authorities/genreForms/"),
        (VOCAB_YSO, "http://www.yso.fi/onto/yso/"),
    ),
    "music_form": ((VOCAB_MUSO, "http://www.yso.fi/onto/muso/"),),
}


@dataclass(frozen=True)
class LocalConceptHit:
    """One concept matched in a locally-loaded authority graph."""

    uri: str
    pref_label: str
    source_vocabulary: str


class LocalConceptResolver(Protocol):
    """Protocol implementations satisfy to act as the tier-0 resolver."""

    def resolve(self, *, literal: str, kind: AuthorityKind) -> LocalConceptHit | None:
        """Return a concept hit for ``literal`` in the kind's vocab, or ``None``."""
        ...


def _quote_sparql_literal(value: str) -> str:
    """Wrap ``value`` as a properly-escaped SPARQL string literal.

    JSON's string escaping is conservative enough to be a valid SPARQL
    string literal — same trick used in
    :mod:`bffi_pipeline.stages.load`.
    """
    return json.dumps(value, ensure_ascii=False)


def _build_query(literal: str, graph_uris: tuple[str, ...]) -> str:
    """Build the tier-0 SPARQL SELECT.

    Exact match on ``str(?label)`` so casing matters (YSO sometimes has
    near-duplicate concepts that differ only in capitalisation; a
    case-insensitive match would mis-bind those). Language preference
    via ``ORDER BY`` so a Finnish prefLabel match sorts above a Swedish
    one for the same literal — unlikely in practice (cataloguer text is
    almost always Finnish), but cheap insurance.
    """
    values_clause = " ".join(f"<{uri}>" for uri in graph_uris)
    quoted = _quote_sparql_literal(literal)
    return (
        "PREFIX skos: <http://www.w3.org/2004/02/skos/core#>\n"
        "SELECT ?uri ?label ?graph WHERE {\n"
        f"  VALUES ?graph {{ {values_clause} }}\n"
        "  GRAPH ?graph {\n"
        "    ?uri skos:prefLabel ?label .\n"
        f"    FILTER (str(?label) = {quoted})\n"
        "  }\n"
        "}\n"
        'ORDER BY DESC(IF(LANG(?label) = "fi", 3, '
        'IF(LANG(?label) = "sv", 2, '
        'IF(LANG(?label) = "en", 1, 0))))\n'
        "LIMIT 1\n"
    )


@dataclass
class FusekiConceptResolver:
    """Tier-0 resolver backed by a SPARQL endpoint.

    Production callers pass the same ``httpx.Client`` used elsewhere in
    the reconcile CLI; tests inject one wrapping ``httpx.MockTransport``.
    Caches on ``(kind, literal)`` because a corpus-scale walk asks for
    ``"Tampere"`` once per Helmet record that mentions Tampere — we'd
    otherwise do thousands of identical SPARQL round-trips.
    """

    http_client: httpx.Client
    fuseki_url: str
    timeout_seconds: float = 5.0
    _cache: dict[tuple[AuthorityKind, str], LocalConceptHit | None] = field(default_factory=dict)

    def resolve(self, *, literal: str, kind: AuthorityKind) -> LocalConceptHit | None:
        """SPARQL the local authority graphs for an exact prefLabel match."""
        graphs = _KIND_TO_GRAPHS.get(kind)
        if not graphs:
            return None
        cache_key = (kind, literal)
        if cache_key in self._cache:
            return self._cache[cache_key]

        graph_uris = tuple(g for _, g in graphs)
        query = _build_query(literal, graph_uris)
        try:
            response = self.http_client.post(
                f"{self.fuseki_url.rstrip('/')}/sparql",
                data={"query": query},
                headers={"Accept": "application/sparql-results+json"},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            self._cache[cache_key] = None
            return None

        bindings = payload.get("results", {}).get("bindings", [])
        if not bindings:
            self._cache[cache_key] = None
            return None
        row = bindings[0]
        uri = row.get("uri", {}).get("value")
        label = row.get("label", {}).get("value", "")
        graph = row.get("graph", {}).get("value")
        if not uri or not graph:
            self._cache[cache_key] = None
            return None
        vocab_tag = next((tag for tag, g_uri in graphs if g_uri == graph), graphs[0][0])
        hit = LocalConceptHit(uri=str(uri), pref_label=str(label), source_vocabulary=vocab_tag)
        self._cache[cache_key] = hit
        return hit


@dataclass
class StubLocalConceptResolver:
    """Test stub: returns wired hits per ``(kind, literal)``."""

    fixtures: dict[tuple[AuthorityKind, str], LocalConceptHit] = field(default_factory=dict)

    def resolve(self, *, literal: str, kind: AuthorityKind) -> LocalConceptHit | None:
        """Look up a wired hit for ``(kind, literal)``; default to ``None``."""
        return self.fixtures.get((kind, literal))


__all__ = [
    "VOCAB_ALLARS",
    "VOCAB_KAUNOKKI",
    "VOCAB_LCGFT",
    "VOCAB_LCSH",
    "VOCAB_SLM",
    "FusekiConceptResolver",
    "LocalConceptHit",
    "LocalConceptResolver",
    "StubLocalConceptResolver",
]
