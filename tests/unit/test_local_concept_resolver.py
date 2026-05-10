"""Unit tests for ``stages.local_concept_resolver`` (M9 tier-0).

All HTTP traffic goes through ``httpx.MockTransport``; no live Fuseki.
The reconcile-orchestrator integration tests verify that a tier-0 hit
short-circuits the tier-1 ``client.query`` call, so the corpus-scale
run actually saves the Finto round-trip.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.local_concept_resolver import (
    FusekiConceptResolver,
    LocalConceptHit,
    StubLocalConceptResolver,
    _build_query,
    _quote_sparql_literal,
)
from bffi_pipeline.stages.reconcile import (
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
    assert "VALUES ?graph" in q
    assert '"Venäjä"' in q


def test_build_query_unions_multiple_graphs_for_genre_form() -> None:
    """KAUNO + SLM must both appear in the VALUES clause for genre_form."""
    q = _build_query(
        "historialliset romaanit",
        ("http://www.yso.fi/onto/kauno/", "http://urn.fi/URN:NBN:fi:au:slm:"),
    )
    assert "http://www.yso.fi/onto/kauno/" in q
    assert "http://urn.fi/URN:NBN:fi:au:slm:" in q


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
    """A second lookup for a known-miss literal must not re-query Fuseki."""
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"results": {"bindings": []}})

    resolver = FusekiConceptResolver(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        fuseki_url="http://localhost:3030/bffi",
    )
    assert resolver.resolve(literal="nope", kind="subject") is None
    assert resolver.resolve(literal="nope", kind="subject") is None
    assert calls["n"] == 1


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
