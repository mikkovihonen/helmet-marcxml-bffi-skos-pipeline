"""Unit tests for ``stages.local_concept_resolver`` (M9 tier-0).

All HTTP traffic goes through ``httpx.MockTransport``; no live Fuseki.
The reconcile-orchestrator integration tests verify that a tier-0 hit
short-circuits the tier-1 ``client.query`` call, so the corpus-scale
run actually saves the Finto round-trip.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m9.local_concept_resolver import (
    _KIND_TO_GRAPHS,
    LEGACY_BRIDGE_VOCABS,
    VOCAB_VIA_ALLARS,
    VOCAB_VIA_KAUNO,
    VOCAB_VIA_MUSA,
    VOCAB_VIA_YSA,
    FusekiConceptResolver,
    LocalConceptHit,
    StubLocalConceptResolver,
    _build_allars_redirect_query,
    _build_kauno_redirect_query,
    _build_legacy_mapping_query,
    _build_query,
    _quote_sparql_literal,
)
from bffi_pipeline.stages.m9.runner import (
    STAGE_LEXICAL,
    STAGE_LOCAL,
    AuthorityCandidate,
    EntityRequest,
    StubAuthorityClient,
    StubPicker,
    apply_reconciliation,
    reconcile_one,
)

# --- SPARQL string building ----------------------------------------------


def test_quote_sparql_literal_escapes_quotes() -> None:
    assert _quote_sparql_literal('Tampere "vanha"') == '"Tampere \\"vanha\\""'


def test_quote_sparql_literal_keeps_unicode_intact() -> None:
    """Finnish diacritics must round-trip; otherwise the FILTER never matches."""
    assert _quote_sparql_literal("Venäjä") == '"Venäjä"'


def test_build_query_includes_all_graph_uris() -> None:
    q = _build_query("Venäjä", ("http://www.yso.fi/onto/yso/",))
    assert "http://www.yso.fi/onto/yso/" in q
    # Multi-valued VALUES: each row pairs a graph URI with a numeric
    # priority used by the secondary ORDER BY tiebreaker.
    assert "VALUES (?graph ?graphRank)" in q
    assert '"Venäjä"' in q


def test_build_query_graph_priority_descends_with_declaration_order() -> None:
    """The first graph in the tuple gets the highest ?graphRank; ties
    on language priority break in favour of the earlier-declared graph."""
    q = _build_query(
        "x",
        (
            "http://www.yso.fi/onto/yso/",
            "http://www.yso.fi/onto/allars/",
            "http://id.loc.gov/authorities/subjects/",
        ),
    )
    # First graph → rank 3; second → 2; third → 1.
    assert "(<http://www.yso.fi/onto/yso/> 3)" in q
    assert "(<http://www.yso.fi/onto/allars/> 2)" in q
    assert "(<http://id.loc.gov/authorities/subjects/> 1)" in q
    # And the ORDER BY uses ?graphRank as the secondary key.
    assert "DESC(?graphRank)" in q


def test_build_query_unions_multiple_graphs_for_genre_form() -> None:
    """KAUNO + SLM must both appear in the VALUES clause for genre_form."""
    q = _build_query(
        "historialliset romaanit",
        ("http://www.yso.fi/onto/kauno/", "http://urn.fi/URN:NBN:fi:au:slm:"),
    )
    assert "http://www.yso.fi/onto/kauno/" in q
    assert "http://urn.fi/URN:NBN:fi:au:slm:" in q


def test_genre_form_routing_includes_yso_fallback_for_kaunokki_misroutes() -> None:
    """Cataloguers tag heterogeneous content (places, time periods,
    fiction-specific topics) with ``$2 kaunokki`` in MARC 6XX. The
    M9 walker routes those to ``genre_form``, but the underlying
    concepts often live in YSO-Aika / YSO-Paikat (loaded into the
    YSO named graph). Without the YSO fallback in tier-0, a kaunokki-
    tagged "1800-luku" misses tier-0 and falls through to tier-1
    ``vocab=kauno`` which doesn't carry temporal concepts either."""
    graphs = _KIND_TO_GRAPHS["genre_form"]
    graph_uris = [uri for _, uri in graphs]
    assert "http://www.yso.fi/onto/kauno/" in graph_uris
    assert "http://urn.fi/URN:NBN:fi:au:slm:" in graph_uris
    assert "http://www.yso.fi/onto/yso/" in graph_uris
    # KAUNO must precede YSO so genuine fiction genre/form literals
    # (e.g. "historialliset romaanit") still bind to KAUNO when an
    # equivalent label happens to exist in YSO.
    assert graph_uris.index("http://www.yso.fi/onto/kauno/") < graph_uris.index(
        "http://www.yso.fi/onto/yso/"
    )


# --- FusekiConceptResolver ----------------------------------------------


def _bindings(uri: str, label: str, lang: str, graph: str) -> dict[str, Any]:
    """Build the SPARQL JSON-results envelope for one row."""
    return {
        "results": {
            "bindings": [
                {
                    "uri": {"type": "uri", "value": uri},
                    "label": {"type": "literal", "value": label, "xml:lang": lang},
                    "graph": {"type": "uri", "value": graph},
                }
            ]
        }
    }


def test_resolver_yso_subject_match() -> None:
    yso_uri = "http://www.yso.fi/onto/yso/p105076"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sparql")
        return httpx.Response(
            200, json=_bindings(yso_uri, "Tampere", "fi", "http://www.yso.fi/onto/yso/")
        )

    transport = httpx.MockTransport(handler)
    resolver = FusekiConceptResolver(
        http_client=httpx.Client(transport=transport),
        fuseki_url="http://localhost:3030/bffi",
    )
    hit = resolver.resolve(literal="Tampere", kind="subject")
    assert hit is not None
    assert hit.uri == yso_uri
    assert hit.pref_label == "Tampere"
    assert hit.source_vocabulary == "yso"


def test_resolver_genre_form_picks_correct_vocab_tag_for_slm_hit() -> None:
    """When the matched graph is SLM, source_vocabulary tag must be 'slm', not 'kauno'."""
    slm_uri = "http://urn.fi/URN:NBN:fi:au:slm:s123"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_bindings(slm_uri, "muistelmat", "fi", "http://urn.fi/URN:NBN:fi:au:slm:"),
        )

    resolver = FusekiConceptResolver(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        fuseki_url="http://localhost:3030/bffi",
    )
    hit = resolver.resolve(literal="muistelmat", kind="genre_form")
    assert hit is not None
    assert hit.source_vocabulary == "slm"


def test_resolver_returns_none_on_empty_bindings() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"results": {"bindings": []}})
    )
    resolver = FusekiConceptResolver(
        http_client=httpx.Client(transport=transport),
        fuseki_url="http://localhost:3030/bffi",
    )
    assert resolver.resolve(literal="not-a-real-concept", kind="subject") is None


def test_resolver_returns_none_on_http_error() -> None:
    """Tier-0 must fall through silently to tier-1 on Fuseki failure;
    a 500 is not a reason to abort the entire reconcile run."""
    transport = httpx.MockTransport(lambda _: httpx.Response(503, json={"error": "boom"}))
    resolver = FusekiConceptResolver(
        http_client=httpx.Client(transport=transport),
        fuseki_url="http://localhost:3030/bffi",
    )
    assert resolver.resolve(literal="Tampere", kind="subject") is None


def test_resolver_returns_none_for_kinds_without_local_graph_mapping() -> None:
    """Persons / corporate bodies are routed through tier-1 KANTO; tier-0 short-circuits."""
    transport = httpx.MockTransport(lambda _: pytest.fail("Fuseki must NOT be hit for persons"))
    resolver = FusekiConceptResolver(
        http_client=httpx.Client(transport=transport),
        fuseki_url="http://localhost:3030/bffi",
    )
    assert resolver.resolve(literal="Tolstoy, Leo,", kind="person") is None
    assert resolver.resolve(literal="ACME, Inc.", kind="corporate_body") is None


def test_resolver_caches_per_kind_and_literal() -> None:
    """Repeated lookups for the same (kind, literal) hit the in-memory cache."""
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json=_bindings(
                "http://www.yso.fi/onto/yso/p1", "Tampere", "fi", "http://www.yso.fi/onto/yso/"
            ),
        )

    resolver = FusekiConceptResolver(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        fuseki_url="http://localhost:3030/bffi",
    )
    resolver.resolve(literal="Tampere", kind="subject")
    resolver.resolve(literal="Tampere", kind="subject")
    assert calls["n"] == 1


def test_resolver_caches_misses_too() -> None:
    """A second lookup for a known-miss literal must not re-query Fuseki.

    A first ``subject`` miss fires two SPARQL queries — the lexical
    tier-0 plus the YSA / MUSA / Allärs legacy-mapping tier — so the
    floor is two calls, not one. The cache invariant is that a SECOND
    lookup for the same ``(kind, literal)`` doesn't increase the count
    further.
    """
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"results": {"bindings": []}})

    resolver = FusekiConceptResolver(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        fuseki_url="http://localhost:3030/bffi",
    )
    assert resolver.resolve(literal="nope", kind="subject") is None
    calls_after_first_lookup = calls["n"]
    assert resolver.resolve(literal="nope", kind="subject") is None
    assert calls["n"] == calls_after_first_lookup


def test_resolver_strips_trailing_slash_on_fuseki_url() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"results": {"bindings": []}})

    resolver = FusekiConceptResolver(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        fuseki_url="http://localhost:3030/bffi/",
    )
    resolver.resolve(literal="x", kind="subject")
    assert captured["url"].endswith("/sparql")
    assert "/bffi/sparql" in captured["url"]


# --- Legacy-mapping tier (YSA / MUSA / Allärs → YSO) --------------------


def _legacy_bindings(uri: str, label: str, lang: str, via: str) -> dict[str, Any]:
    """Build the SPARQL JSON-results envelope for one legacy-mapping row."""
    return {
        "results": {
            "bindings": [
                {
                    "uri": {"type": "uri", "value": uri},
                    "label": {"type": "literal", "value": label, "xml:lang": lang},
                    "via": {"type": "literal", "value": via},
                }
            ]
        }
    }


def test_build_legacy_mapping_query_includes_all_three_legacy_graphs() -> None:
    q = _build_legacy_mapping_query("lapset")
    # All three legacy graphs covered
    assert "http://www.yso.fi/onto/ysa/" in q
    assert "http://www.yso.fi/onto/musa/" in q
    assert "http://www.yso.fi/onto/allars/" in q
    # YSO destination filter present
    assert 'STRSTARTS(STR(?uri), "http://www.yso.fi/onto/yso/")' in q
    # MUSA chain follows dct:isReplacedBy
    assert "dct:isReplacedBy" in q
    # exactMatch / closeMatch alternation present
    assert "skos:exactMatch | skos:closeMatch" in q
    assert '"lapset"' in q


def _decoded_body(request: httpx.Request) -> str:
    """URL-decode a Fuseki form-encoded POST body so test substring
    checks can look at the raw SPARQL the resolver sent."""
    body = request.content.decode("utf-8")
    parsed = parse_qs(body)
    queries = parsed.get("query", [])
    return queries[0] if queries else ""


def test_resolve_falls_back_to_legacy_mapping_when_lexical_misses() -> None:
    """The flagship YSA-bridge case: ``lapset`` doesn't survive as a YSO
    altLabel after the 2014-2018 merge (split into disambiguated forms),
    but the YSA graph still carries the bare prefLabel + exactMatch
    triple to YSO. Tier-0 lexical misses; legacy-mapping tier hits."""
    yso_uri = "http://www.yso.fi/onto/yso/p4354"
    bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sparql = _decoded_body(request)
        bodies.append(sparql)
        if "dct:isReplacedBy" in sparql:
            return httpx.Response(200, json=_legacy_bindings(yso_uri, "lapset", "fi", "via-ysa"))
        # Lexical query — empty.
        return httpx.Response(200, json={"results": {"bindings": []}})

    resolver = FusekiConceptResolver(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        fuseki_url="http://localhost:3030/bffi",
    )
    hit = resolver.resolve(literal="lapset", kind="subject")
    assert hit is not None
    assert hit.uri == yso_uri
    assert hit.source_vocabulary == VOCAB_VIA_YSA
    assert hit.source_vocabulary in LEGACY_BRIDGE_VOCABS
    assert len(bodies) == 2  # Lexical THEN legacy mapping
    assert "VALUES (?graph ?graphRank)" in bodies[0]
    assert "dct:isReplacedBy" in bodies[1]


def test_resolve_picks_musa_via_tag_when_mapping_chain_passes_through_musa() -> None:
    """MUSA → YSA → YSO is the two-hop chain; the SPARQL UNION returns
    ``via-musa`` as the bridge tag on that branch."""

    def handler(request: httpx.Request) -> httpx.Response:
        sparql = _decoded_body(request)
        if "dct:isReplacedBy" in sparql:
            return httpx.Response(
                200,
                json=_legacy_bindings(
                    "http://www.yso.fi/onto/yso/p27182",
                    "transkriptiot (musiikki)",
                    "fi",
                    "via-musa",
                ),
            )
        return httpx.Response(200, json={"results": {"bindings": []}})

    resolver = FusekiConceptResolver(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        fuseki_url="http://localhost:3030/bffi",
    )
    hit = resolver.resolve(literal="transkriptiot (musiikki)", kind="subject")
    assert hit is not None
    assert hit.source_vocabulary == VOCAB_VIA_MUSA


def test_resolve_picks_allars_via_tag_for_swedish_legacy_literal() -> None:
    """Swedish ``$2 allars`` literals can hit the legacy tier too — Allärs
    carries the same exactMatch/closeMatch mapping pattern."""

    def handler(request: httpx.Request) -> httpx.Response:
        sparql = _decoded_body(request)
        if "dct:isReplacedBy" in sparql:
            return httpx.Response(
                200,
                json=_legacy_bindings(
                    "http://www.yso.fi/onto/yso/p4354", "barn", "sv", "via-allars"
                ),
            )
        return httpx.Response(200, json={"results": {"bindings": []}})

    resolver = FusekiConceptResolver(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        fuseki_url="http://localhost:3030/bffi",
    )
    hit = resolver.resolve(literal="barn", kind="subject")
    assert hit is not None
    assert hit.source_vocabulary == VOCAB_VIA_ALLARS


def test_resolve_skips_legacy_mapping_when_lexical_already_hit() -> None:
    """Lexical YSO hit short-circuits — no legacy-mapping SPARQL fired."""
    yso_uri = "http://www.yso.fi/onto/yso/p104958"

    def handler(request: httpx.Request) -> httpx.Response:
        sparql = _decoded_body(request)
        if "dct:isReplacedBy" in sparql:
            pytest.fail("Legacy-mapping tier must NOT fire on a lexical hit")
        return httpx.Response(
            200,
            json=_bindings(yso_uri, "Päijänne", "fi", "http://www.yso.fi/onto/yso/"),
        )

    resolver = FusekiConceptResolver(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        fuseki_url="http://localhost:3030/bffi",
    )
    hit = resolver.resolve(literal="Päijänne", kind="subject")
    assert hit is not None
    assert hit.uri == yso_uri
    assert hit.source_vocabulary == "yso"


def test_resolve_skips_legacy_mapping_for_non_subject_kinds() -> None:
    """Legacy mapping is subject-only: YSA / MUSA / Allärs are subject
    vocabularies. Genre/form + music_form must not fire the bridge."""

    def handler(request: httpx.Request) -> httpx.Response:
        sparql = _decoded_body(request)
        if "dct:isReplacedBy" in sparql:
            pytest.fail("Legacy-mapping must not fire for non-subject kinds")
        return httpx.Response(200, json={"results": {"bindings": []}})

    resolver = FusekiConceptResolver(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        fuseki_url="http://localhost:3030/bffi",
    )
    assert resolver.resolve(literal="muistelmat", kind="genre_form") is None
    assert resolver.resolve(literal="sinfoniat", kind="music_form") is None


def test_resolve_returns_none_when_both_tiers_miss() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": {"bindings": []}})

    resolver = FusekiConceptResolver(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        fuseki_url="http://localhost:3030/bffi",
    )
    assert resolver.resolve(literal="nonsense", kind="subject") is None


# --- Allars → YSO redirect (subject-kind post-tier-0) ------------------


def _allars_redirect_bindings(yso_uri: str, label: str, lang: str) -> dict[str, Any]:
    """SPARQL JSON-results envelope for an Allars→YSO redirect row."""
    return {
        "results": {
            "bindings": [
                {
                    "yso": {"type": "uri", "value": yso_uri},
                    "label": {"type": "literal", "value": label, "xml:lang": lang},
                }
            ]
        }
    }


def test_build_allars_redirect_query_has_bridge_predicates() -> None:
    q = _build_allars_redirect_query("http://www.yso.fi/onto/allars/Y22080")
    assert "http://www.yso.fi/onto/allars/" in q
    assert "http://www.yso.fi/onto/allars/Y22080" in q
    assert 'STRSTARTS(STR(?yso), "http://www.yso.fi/onto/yso/")' in q
    # Allars uses skos:exactMatch / skos:closeMatch only; no dct:isReplacedBy
    # path to YSO is published for Allars (cf. KAUNO which uses both).
    assert "skos:exactMatch" in q
    assert "skos:closeMatch" in q


def test_subject_allars_hit_redirects_to_yso_when_bridge_exists() -> None:
    """Headline case: cataloguer literal `$2 allars` matches an Allars
    prefLabel; the matched Allars concept has ``skos:exactMatch yso:…``;
    the resolver swaps to the YSO URI with ``source_vocabulary=via-allars``."""
    allars_uri = "http://www.yso.fi/onto/allars/Y22080"
    yso_uri = "http://www.yso.fi/onto/yso/p1780"

    def handler(request: httpx.Request) -> httpx.Response:
        sparql = _decoded_body(request)
        if allars_uri in sparql:
            return httpx.Response(200, json=_allars_redirect_bindings(yso_uri, "historia", "fi"))
        return httpx.Response(
            200,
            json=_bindings(allars_uri, "lokalhistoria", "sv", "http://www.yso.fi/onto/allars/"),
        )

    resolver = FusekiConceptResolver(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        fuseki_url="http://localhost:3030/bffi",
    )
    hit = resolver.resolve(literal="lokalhistoria", kind="subject")
    assert hit is not None
    assert hit.uri == yso_uri
    assert hit.source_vocabulary == VOCAB_VIA_ALLARS
    assert hit.source_vocabulary in LEGACY_BRIDGE_VOCABS


def test_subject_allars_hit_without_yso_bridge_keeps_allars_uri() -> None:
    """When the matched Allars concept has no YSO bridge (Swedish-only
    place names like Ålandsfrågan), the original Allars URI stays."""
    allars_uri = "http://www.yso.fi/onto/allars/Y23647"

    def handler(request: httpx.Request) -> httpx.Response:
        sparql = _decoded_body(request)
        if allars_uri in sparql:
            return httpx.Response(200, json={"results": {"bindings": []}})
        return httpx.Response(
            200,
            json=_bindings(allars_uri, "Ålandsfrågan", "sv", "http://www.yso.fi/onto/allars/"),
        )

    resolver = FusekiConceptResolver(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        fuseki_url="http://localhost:3030/bffi",
    )
    hit = resolver.resolve(literal="Ålandsfrågan", kind="subject")
    assert hit is not None
    assert hit.uri == allars_uri
    assert hit.source_vocabulary == "allars"  # original tag, not redirected


def test_subject_yso_hit_does_not_trigger_allars_redirect() -> None:
    """When tier-0 returns a YSO hit directly, the Allars redirect must
    not fire (the redirect is keyed on the Allars namespace)."""
    yso_uri = "http://www.yso.fi/onto/yso/p1780"

    def handler(request: httpx.Request) -> httpx.Response:
        sparql = _decoded_body(request)
        # Redirect-only marker: the redirect SPARQL has the
        # ``STRSTARTS(STR(?yso)`` filter; the tier-0 lexical query
        # doesn't. (Tier-0 references the Allars graph URI in its
        # ``VALUES`` clause, so a naive substring check would fire.)
        if "STRSTARTS(STR(?yso)" in sparql:
            pytest.fail("Allars redirect must NOT fire when tier-0 hit YSO directly")
        return httpx.Response(
            200, json=_bindings(yso_uri, "historia", "fi", "http://www.yso.fi/onto/yso/")
        )

    resolver = FusekiConceptResolver(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        fuseki_url="http://localhost:3030/bffi",
    )
    hit = resolver.resolve(literal="historia", kind="subject")
    assert hit is not None
    assert hit.uri == yso_uri
    assert hit.source_vocabulary == "yso"


def test_genre_form_allars_hit_does_not_trigger_allars_redirect() -> None:
    """The Allars redirect is subject-kind-only (Allars is a topical
    subject vocabulary). A genre_form tier-0 hit that lands in Allars
    (defensive: shouldn't happen given `_KIND_TO_GRAPHS["genre_form"]`)
    doesn't trigger the redirect."""
    allars_uri = "http://www.yso.fi/onto/allars/Y99"

    def handler(request: httpx.Request) -> httpx.Response:
        sparql = _decoded_body(request)
        # Same redirect-only discriminator as the YSO-hit test above.
        if "STRSTARTS(STR(?yso)" in sparql:
            pytest.fail("Allars redirect must NOT fire for kind=genre_form")
        return httpx.Response(
            200,
            json=_bindings(allars_uri, "fake-genre", "sv", "http://www.yso.fi/onto/allars/"),
        )

    resolver = FusekiConceptResolver(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        fuseki_url="http://localhost:3030/bffi",
    )
    # Stub the tier-0 result; whether kind=genre_form actually returns
    # this hit is _KIND_TO_GRAPHS-dependent — the assertion lives in the
    # handler's pytest.fail above.
    resolver.resolve(literal="fake-genre", kind="genre_form")


# --- KAUNO → YSO redirect (genre_form-kind post-tier-0) ----------------


def _kauno_redirect_bindings(yso_uri: str, label: str, lang: str) -> dict[str, Any]:
    """Build the SPARQL JSON-results envelope for a KAUNO→YSO redirect row."""
    return {
        "results": {
            "bindings": [
                {
                    "yso": {"type": "uri", "value": yso_uri},
                    "label": {"type": "literal", "value": label, "xml:lang": lang},
                }
            ]
        }
    }


def test_build_kauno_redirect_query_has_all_bridge_predicates() -> None:
    q = _build_kauno_redirect_query("http://www.yso.fi/onto/kauno/p1248")
    assert "http://www.yso.fi/onto/kauno/" in q
    assert "http://www.yso.fi/onto/kauno/p1248" in q
    # Filter to YSO destinations
    assert 'STRSTARTS(STR(?yso), "http://www.yso.fi/onto/yso/")' in q
    # Follow all three bridge predicates (exactMatch, closeMatch, isReplacedBy)
    assert "skos:exactMatch" in q
    assert "skos:closeMatch" in q
    assert "dct:isReplacedBy" in q


def test_genre_form_kauno_hit_redirects_to_yso_when_bridge_exists() -> None:
    """The headline case: cataloguer literal matches a KAUNO prefLabel
    via tier-0; the matched KAUNO concept has ``skos:exactMatch yso:…``;
    the resolver swaps to the YSO URI with ``source_vocabulary=via-kauno``.
    """
    kauno_uri = "http://www.yso.fi/onto/kauno/p1248"
    yso_uri = "http://www.yso.fi/onto/yso/p19569"

    def handler(request: httpx.Request) -> httpx.Response:
        sparql = _decoded_body(request)
        # The redirect query references the kauno graph URI directly.
        if kauno_uri in sparql:
            return httpx.Response(
                200, json=_kauno_redirect_bindings(yso_uri, "kunnianloukkaus", "fi")
            )
        # Lexical tier-0 returns the KAUNO hit.
        return httpx.Response(
            200,
            json=_bindings(kauno_uri, "kunnianloukkaus", "fi", "http://www.yso.fi/onto/kauno/"),
        )

    resolver = FusekiConceptResolver(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        fuseki_url="http://localhost:3030/bffi",
    )
    hit = resolver.resolve(literal="kunnianloukkaus", kind="genre_form")
    assert hit is not None
    assert hit.uri == yso_uri
    assert hit.source_vocabulary == VOCAB_VIA_KAUNO
    assert hit.source_vocabulary in LEGACY_BRIDGE_VOCABS
    # The hit's prefLabel reflects the YSO label (so downstream
    # rendering surfaces the modern form).
    assert hit.pref_label == "kunnianloukkaus"


def test_genre_form_kauno_hit_without_yso_bridge_keeps_kauno_uri() -> None:
    """When the matched KAUNO concept has no YSO bridge (some KAUNO
    concepts are KAUNO-native and never modernised), the original
    KAUNO URI stays. No data loss; bridge is opportunistic."""
    kauno_uri = "http://www.yso.fi/onto/kauno/p999"

    def handler(request: httpx.Request) -> httpx.Response:
        sparql = _decoded_body(request)
        if kauno_uri in sparql:
            # No bridge — empty results.
            return httpx.Response(200, json={"results": {"bindings": []}})
        return httpx.Response(
            200,
            json=_bindings(kauno_uri, "kauno-only-concept", "fi", "http://www.yso.fi/onto/kauno/"),
        )

    resolver = FusekiConceptResolver(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        fuseki_url="http://localhost:3030/bffi",
    )
    hit = resolver.resolve(literal="kauno-only-concept", kind="genre_form")
    assert hit is not None
    assert hit.uri == kauno_uri  # original KAUNO URI, unmodified
    assert hit.source_vocabulary == "kauno"


def test_genre_form_slm_hit_does_not_trigger_kauno_redirect() -> None:
    """When tier-0 lands in SLM or another non-KAUNO graph, the redirect
    does NOT fire (the redirect is keyed on the KAUNO namespace)."""
    slm_uri = "http://urn.fi/URN:NBN:fi:au:slm:s123"

    def handler(request: httpx.Request) -> httpx.Response:
        sparql = _decoded_body(request)
        if "http://www.yso.fi/onto/kauno/" in sparql and slm_uri in sparql:
            pytest.fail("KAUNO redirect must NOT fire when tier-0 hit landed in SLM")
        return httpx.Response(
            200,
            json=_bindings(slm_uri, "muistelmat", "fi", "http://urn.fi/URN:NBN:fi:au:slm:"),
        )

    resolver = FusekiConceptResolver(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        fuseki_url="http://localhost:3030/bffi",
    )
    hit = resolver.resolve(literal="muistelmat", kind="genre_form")
    assert hit is not None
    assert hit.uri == slm_uri
    assert hit.source_vocabulary == "slm"


def test_subject_kind_kauno_hit_does_not_trigger_redirect() -> None:
    """The KAUNO redirect is genre_form-only. A subject-kind tier-0 hit
    that happens to land on a kauno URI (shouldn't happen given
    ``_KIND_TO_GRAPHS``, but defensive) doesn't fire the redirect."""
    yso_uri = "http://www.yso.fi/onto/yso/p1"

    def handler(request: httpx.Request) -> httpx.Response:
        sparql = _decoded_body(request)
        if "http://www.yso.fi/onto/kauno/" in sparql:
            pytest.fail("KAUNO redirect must NOT fire for kind=subject")
        return httpx.Response(
            200, json=_bindings(yso_uri, "Tampere", "fi", "http://www.yso.fi/onto/yso/")
        )

    resolver = FusekiConceptResolver(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        fuseki_url="http://localhost:3030/bffi",
    )
    hit = resolver.resolve(literal="Tampere", kind="subject")
    assert hit is not None
    assert hit.uri == yso_uri


def test_kauno_redirect_falls_back_to_original_label_when_yso_lacks_one() -> None:
    """If the YSO concept doesn't carry a prefLabel (edge case — every
    YSO concept SHOULD have one), the hit reuses the original KAUNO
    prefLabel so rendering doesn't silently lose the label."""
    kauno_uri = "http://www.yso.fi/onto/kauno/p2496"
    yso_uri = "http://www.yso.fi/onto/yso/p16156"

    def handler(request: httpx.Request) -> httpx.Response:
        sparql = _decoded_body(request)
        if kauno_uri in sparql:
            # Bridge exists but no YSO prefLabel returned.
            return httpx.Response(
                200,
                json={"results": {"bindings": [{"yso": {"type": "uri", "value": yso_uri}}]}},
            )
        return httpx.Response(
            200,
            json=_bindings(kauno_uri, "automatkailu", "fi", "http://www.yso.fi/onto/kauno/"),
        )

    resolver = FusekiConceptResolver(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        fuseki_url="http://localhost:3030/bffi",
    )
    hit = resolver.resolve(literal="automatkailu", kind="genre_form")
    assert hit is not None
    assert hit.uri == yso_uri
    assert hit.source_vocabulary == VOCAB_VIA_KAUNO
    # Fallback label from the original KAUNO hit kicks in.
    assert hit.pref_label == "automatkailu"


# --- StubLocalConceptResolver -------------------------------------------


def test_stub_resolver_returns_wired_hit() -> None:
    stub = StubLocalConceptResolver(
        fixtures={
            ("subject", "Tampere"): LocalConceptHit(
                uri="http://www.yso.fi/onto/yso/p1",
                pref_label="Tampere",
                source_vocabulary="yso",
            )
        }
    )
    hit = stub.resolve(literal="Tampere", kind="subject")
    assert hit is not None
    assert hit.uri.endswith("/p1")


def test_stub_resolver_returns_none_for_unwired() -> None:
    stub = StubLocalConceptResolver()
    assert stub.resolve(literal="anything", kind="subject") is None


# --- reconcile_one integration ------------------------------------------


def test_reconcile_one_short_circuits_tier1_when_local_resolver_hits() -> None:
    """The whole point: a tier-0 hit must NOT call client.query at all."""
    request = EntityRequest(
        work_uri="http://urn.fi/URN:NBN:fi:bib:work:abc",
        literal="Tampere",
        kind="subject",
        predicate_uri=str(V.BFFI.subject),
    )

    class _ExplodingClient:
        def query(self, *, request: EntityRequest, top_k: int = 10) -> list[AuthorityCandidate]:
            pytest.fail("tier-0 hit must not fall through to tier-1 client.query")

    resolver = StubLocalConceptResolver(
        fixtures={
            ("subject", "Tampere"): LocalConceptHit(
                uri="http://www.yso.fi/onto/yso/p105076",
                pref_label="Tampere",
                source_vocabulary="yso",
            )
        }
    )
    outcome = reconcile_one(
        request=request,
        client=_ExplodingClient(),
        fallback_client=None,
        picker=StubPicker(),
        local_resolver=resolver,
    )
    assert outcome.stage == STAGE_LOCAL
    assert outcome.chosen_uri == "http://www.yso.fi/onto/yso/p105076"
    assert outcome.confidence == pytest.approx(1.0)
    assert outcome.needs_review is False


def test_reconcile_one_falls_through_to_tier1_when_local_resolver_misses() -> None:
    """No tier-0 hit → existing four-tier logic runs as before."""
    request = EntityRequest(
        work_uri="http://urn.fi/URN:NBN:fi:bib:work:abc",
        literal="Some Obscure Subject",
        kind="subject",
        predicate_uri=str(V.BFFI.subject),
    )
    yso_uri = "http://www.yso.fi/onto/yso/p999"
    client = StubAuthorityClient(
        fixtures={
            ("subject", "Some Obscure Subject"): [
                AuthorityCandidate(
                    uri=yso_uri,
                    pref_label="Some Obscure Subject",
                    source_vocabulary="yso",
                    lexical_similarity=0.97,
                )
            ]
        }
    )
    outcome = reconcile_one(
        request=request,
        client=client,
        fallback_client=None,
        picker=StubPicker(),
        local_resolver=StubLocalConceptResolver(),  # always misses
    )
    assert outcome.stage == STAGE_LEXICAL
    assert outcome.chosen_uri == yso_uri


# --- apply_reconciliation integration -----------------------------------


def _build_subject_only_graph() -> Graph:
    g = Graph()
    work = URIRef("http://urn.fi/URN:NBN:fi:bib:work:abc")
    admin = URIRef("http://urn.fi/URN:NBN:fi:bib:adminmeta/1")
    g.add((work, RDF.type, V.BFFI.Work))
    subj = BNode()
    g.add((work, V.BFFI.subject, subj))
    g.add((subj, V.RDFS.label, Literal("Tampere")))
    g.add((subj, V.BF.source, Literal("yso/fin")))
    g.add((work, V.adminMetadata, admin))
    g.add((admin, RDF.type, V.AdminMetadata))
    g.add((admin, V.adminMetadataFor, work))
    g.add(
        (
            admin,
            V.descriptionChangeDate,
            Literal("2026-05-01T00:00:00+00:00", datatype=V.XSD.dateTime),
        )
    )
    g.add((admin, V.descriptionAuthentication, V.AUTH_AUTO_MERGED))
    return g


def test_apply_reconciliation_counts_tier0_hits_in_summary() -> None:
    g = _build_subject_only_graph()
    yso_uri = "http://www.yso.fi/onto/yso/p105076"
    resolver = StubLocalConceptResolver(
        fixtures={
            ("subject", "Tampere"): LocalConceptHit(
                uri=yso_uri,
                pref_label="Tampere",
                source_vocabulary="yso",
            )
        }
    )
    summary, outcomes = apply_reconciliation(
        client=StubAuthorityClient(),  # tier-1 must not be reached
        picker=StubPicker(),
        graph=g,
        local_resolver=resolver,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
    )
    assert summary.local == 1
    assert summary.lexical == 0
    assert summary.total == 1
    assert outcomes[0].stage == STAGE_LOCAL
    work = URIRef("http://urn.fi/URN:NBN:fi:bib:work:abc")
    assert (work, V.BFFI.subject, URIRef(yso_uri)) in g


def test_apply_reconciliation_renders_tier0_count_in_summary() -> None:
    g = _build_subject_only_graph()
    resolver = StubLocalConceptResolver(
        fixtures={
            ("subject", "Tampere"): LocalConceptHit(
                uri="http://www.yso.fi/onto/yso/p105076",
                pref_label="Tampere",
                source_vocabulary="yso",
            )
        }
    )
    summary, _ = apply_reconciliation(
        client=StubAuthorityClient(),
        picker=StubPicker(),
        graph=g,
        local_resolver=resolver,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
    )
    rendered = summary.render()
    assert "reconciliation-local:" in rendered
    assert "1" in rendered.split("reconciliation-local:")[1].split("\n")[0]


def test_apply_reconciliation_emits_tier0_provenance_with_local_stage() -> None:
    """Per spec: every reconciliation attempt logs one Activity, including tier-0."""
    g = _build_subject_only_graph()
    yso_uri = "http://www.yso.fi/onto/yso/p105076"
    resolver = StubLocalConceptResolver(
        fixtures={
            ("subject", "Tampere"): LocalConceptHit(
                uri=yso_uri,
                pref_label="Tampere",
                source_vocabulary="yso",
            )
        }
    )
    prov = Graph()
    apply_reconciliation(
        client=StubAuthorityClient(),
        picker=StubPicker(),
        graph=g,
        provenance_graph=prov,
        local_resolver=resolver,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
    )
    activities = list(prov.subjects(V.RDF.type, V.Reconciliation))
    assert len(activities) == 1
    activity = activities[0]
    stages = {str(s) for s in prov.objects(activity, V.stage)}
    assert STAGE_LOCAL in stages
    assert URIRef(yso_uri) in set(prov.objects(activity, V.chosenAuthorityUri))
