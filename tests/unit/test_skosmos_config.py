"""Unit tests for ``config/skosmos-config.ttl`` (M11).

Catches drift from spec § 4 — every committed predicate the Skosmos
container reads on startup. Pure parse-and-assert with rdflib; no
Docker, no live Skosmos.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS

SKOSMOS = Namespace("http://purl.org/net/skosmos#")
VOID = Namespace("http://rdfs.org/ns/void#")
DC = Namespace("http://purl.org/dc/terms/")
SKOS = Namespace("http://www.w3.org/2004/02/skos/core#")
BFFI = Namespace("http://urn.fi/URN:NBN:fi:schema:bffi:")
ISOTHES = Namespace("http://purl.org/iso25964/skos-thes#")

#: Repo path to the file under test.
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "skosmos-config.ttl"

# The fragment-only IRI Skosmos uses for the vocabulary entry. The file
# declares `:bffiWorks` with `@prefix : <#> .` so the resolved IRI is
# `<config-file-uri>#bffiWorks`. We assert by triples-on-subject rather
# than by URI equality because the resolved base depends on parse settings.


@pytest.fixture(scope="module")
def graph() -> Graph:
    g = Graph()
    g.parse(str(_CONFIG_PATH), format="turtle")
    return g


def _vocabulary_subject(g: Graph) -> URIRef:
    """Return the bffiWorks vocabulary node in the config — i.e. the
    ``skosmos:Vocabulary`` whose ``void:uriSpace`` is the canonical
    Helmet Work namespace. Other ``skosmos:Vocabulary`` declarations
    (KANTO/YSO/KAUNO/MUSO/SLM, added for option 3b cross-vocab linking)
    coexist with bffiWorks; matching by ``void:uriSpace`` lets us pin
    the assertions to the right one without depending on resolved URIs."""
    work_namespace = Literal("http://urn.fi/URN:NBN:fi:bib:work:")
    candidates = [s for s, _, _ in g.triples((None, VOID.uriSpace, work_namespace))]
    assert len(candidates) == 1, (
        "skosmos-config.ttl must declare exactly one Vocabulary with "
        "void:uriSpace = http://urn.fi/URN:NBN:fi:bib:work:"
    )
    subject = candidates[0]
    assert isinstance(subject, URIRef)
    return subject


# --- Type labels (spec § 4 "Skosmos requires rdfs:label on every custom type") ---


@pytest.mark.parametrize(
    ("type_uri", "fi_label", "sv_label", "en_label"),
    [
        (BFFI.Work, "Teos", "Verk", "Work"),
        (BFFI.Expression, "Ekspressio", "Uttryck", "Expression"),
        (BFFI.Manifestation, "Manifestaatio", "Manifestation", "Manifestation"),
    ],
)
def test_bffi_types_have_multilingual_labels(
    graph: Graph,
    type_uri: URIRef,
    fi_label: str,
    sv_label: str,
    en_label: str,
) -> None:
    labels = {
        (str(o), o.language)
        for _, _, o in graph.triples((type_uri, RDFS.label, None))
        if isinstance(o, Literal)
    }
    assert (fi_label, "fi") in labels
    assert (sv_label, "sv") in labels
    assert (en_label, "en") in labels


def test_bffi_types_are_subclass_of_skos_concept(graph: Graph) -> None:
    assert (BFFI.Work, RDFS.subClassOf, SKOS.Concept) in graph
    assert (BFFI.Expression, RDFS.subClassOf, SKOS.Concept) in graph
    assert (BFFI.Manifestation, RDFS.subClassOf, SKOS.Concept) in graph


# --- Vocabulary entry --------------------------------------------------


def test_vocabulary_entry_is_dual_typed_vocabulary_and_dataset(graph: Graph) -> None:
    vocab = _vocabulary_subject(graph)
    types = set(graph.objects(vocab, RDF.type))
    assert SKOSMOS.Vocabulary in types
    assert VOID.Dataset in types


def test_vocabulary_language_priority_is_fi_sv_en(graph: Graph) -> None:
    vocab = _vocabulary_subject(graph)
    languages = sorted(
        str(o) for o in graph.objects(vocab, SKOSMOS.language) if isinstance(o, Literal)
    )
    assert languages == ["en", "fi", "sv"]


def test_vocabulary_default_language_is_finnish(graph: Graph) -> None:
    vocab = _vocabulary_subject(graph)
    defaults = list(graph.objects(vocab, SKOSMOS.defaultLanguage))
    assert defaults == [Literal("fi")]


def test_vocabulary_uri_space_matches_committed_work_namespace(graph: Graph) -> None:
    """spec § 4: void:uriSpace MUST match minted Work URI prefix exactly."""
    vocab = _vocabulary_subject(graph)
    spaces = list(graph.objects(vocab, VOID.uriSpace))
    assert spaces == [Literal("http://urn.fi/URN:NBN:fi:bib:work:")]


def test_vocabulary_sparql_graph_points_at_bffi_works_named_graph(graph: Graph) -> None:
    vocab = _vocabulary_subject(graph)
    graphs = set(graph.objects(vocab, SKOSMOS.sparqlGraph))
    assert URIRef("http://urn.fi/URN:NBN:fi:bib:graph:bffi-works") in graphs


def test_vocabulary_sparql_endpoint_points_at_docker_fuseki(graph: Graph) -> None:
    """The config lives inside the Skosmos container; Fuseki is reachable
    via the docker-compose service hostname `fuseki`, not localhost."""
    vocab = _vocabulary_subject(graph)
    endpoints = set(graph.objects(vocab, SKOSMOS.sparqlEndpoint))
    assert URIRef("http://fuseki:3030/bffi/sparql") in endpoints


def test_vocabulary_sparql_dialect_is_jenatext(graph: Graph) -> None:
    """JenaText enables the text:query predicate Skosmos uses for fast label search."""
    vocab = _vocabulary_subject(graph)
    dialects = list(graph.objects(vocab, SKOSMOS.sparqlDialect))
    assert dialects == [Literal("JenaText")]


def test_bffi_works_vocab_lists_works_only(graph: Graph) -> None:
    """Each sibling vocab from the P-45 three-vocab split lists its
    own class only. ``:bffiWorks`` lists ``bffi:Work``;
    ``:bffiExpressions`` lists ``bffi:Expression``; ``:bffiManifestations``
    lists ``bffi:Manifestation``. Pre-P-45 this vocab was the combined
    Works + Expressions browse, but with Expressions promoted to their
    own sibling, listing them here would double-count the alphabetical
    index — the per-sibling assertions in
    :data:`_BFFI_SIBLING_VOCABS` already require Expression to live
    under its own vocab."""
    vocab = _vocabulary_subject(graph)
    classes = set(graph.objects(vocab, SKOSMOS.indexShowClass))
    assert classes == {BFFI.Work}


def test_vocabulary_sidebar_views_alphabetical_only(graph: Graph) -> None:
    """Bibliographic concepts (Works / Expressions / Manifestations) have
    no curated authority hierarchy, no concept groups, and the
    per-record ``dct:modified`` is just the pipeline run timestamp —
    none of Skosmos's Hierarchy / Groups / Changes ("New") tabs carry
    useful content for our corpus. ``skosmos:sidebarViews`` is set to
    the alphabetical browse only; the supporting ``groupClass`` and
    ``showTopConcepts`` knobs are intentionally absent.
    """
    vocab = _vocabulary_subject(graph)
    # The RDF Collection on ``sidebarViews`` resolves to a chain of
    # ``rdf:first`` / ``rdf:rest`` triples; walk the list once and
    # collect the literal members.
    sidebar_views_head = next(graph.objects(vocab, SKOSMOS.sidebarViews), None)
    assert sidebar_views_head is not None, "skosmos:sidebarViews must be declared"
    views: list[str] = []
    node = sidebar_views_head
    while node is not None and str(node) != str(RDF.nil):
        first = next(graph.objects(node, RDF.first), None)
        if isinstance(first, Literal):
            views.append(str(first))
        node = next(graph.objects(node, RDF.rest), None)
    assert views == ["alphabetical"], f"expected ['alphabetical'], got {views!r}"
    # The disabled-tab supporting config should be absent.
    assert not list(graph.objects(vocab, SKOSMOS.groupClass))
    assert not list(graph.objects(vocab, SKOSMOS.showTopConcepts))


def test_vocabulary_dc_title_carries_finnish_label(graph: Graph) -> None:
    vocab = _vocabulary_subject(graph)
    titles = {
        o.language for _, _, o in graph.triples((vocab, DC.title, None)) if isinstance(o, Literal)
    }
    assert "fi" in titles
    # And at least one of sv / en for cataloguers reading in those languages.
    assert "en" in titles or "sv" in titles


def test_vocabulary_short_name_is_bffi_works(graph: Graph) -> None:
    vocab = _vocabulary_subject(graph)
    names = list(graph.objects(vocab, SKOSMOS.shortName))
    assert any(isinstance(o, Literal) and str(o) == "bffi-works" for o in names)


def test_full_alphabetical_index_is_enabled(graph: Graph) -> None:
    """``fullAlphabeticalIndex true`` enables the unpaginated A-Z page
    behind the single ``alphabetical`` sidebar tab. ``showTopConcepts``
    only fed the disabled Hierarchy tab and is intentionally absent."""
    vocab = _vocabulary_subject(graph)
    assert (vocab, SKOSMOS.fullAlphabeticalIndex, Literal(True)) in graph


# --- Subject category --------------------------------------------------


def test_subject_category_has_multilingual_label(graph: Graph) -> None:
    """The dc:subject points at a skosmos:Category node that needs labels."""
    categories = list(graph.subjects(RDF.type, SKOSMOS.Category))
    assert len(categories) >= 1
    cat = categories[0]
    languages = {
        o.language for _, _, o in graph.triples((cat, RDFS.label, None)) if isinstance(o, Literal)
    }
    assert "fi" in languages
    assert "en" in languages
    assert "sv" in languages


# --- Finto cross-vocabulary entries (option 3b) ------------------------
#
# Pinning the URI spaces is critical: every Finto-namespaced URI on a
# bffi:Work concept page is routed through Skosmos's vocabulary
# registry by ``void:uriSpace`` prefix matching. A typo here = no
# label, no clickable link. The test pairs are the same strings the
# load_finto stage uses as named-graph URIs in Fuseki, so any drift
# between the two breaks the lookup silently.

_FINTO_VOCAB_ASSERTIONS: list[tuple[str, str]] = [
    ("yso", "http://www.yso.fi/onto/yso/"),
    ("kanto", "http://urn.fi/URN:NBN:fi:au:finaf:"),
    ("kauno", "http://www.yso.fi/onto/kauno/"),
    ("kaunokki", "http://urn.fi/URN:NBN:fi:au:kaunokki:"),
    ("muso", "http://www.yso.fi/onto/muso/"),
    ("slm", "http://urn.fi/URN:NBN:fi:au:slm:"),
    ("allars", "http://www.yso.fi/onto/allars/"),
    ("relators", "http://id.loc.gov/vocabulary/relators/"),
    ("rda-media", "http://id.loc.gov/vocabulary/mediaTypes/"),
    ("rda-carrier", "http://id.loc.gov/vocabulary/carriers/"),
    ("mencformat", "http://id.loc.gov/vocabulary/mencformat/"),
    ("mrecmedium", "http://id.loc.gov/vocabulary/mrecmedium/"),
    ("mplayspeed", "http://id.loc.gov/vocabulary/mplayspeed/"),
    ("mplayback", "http://id.loc.gov/vocabulary/mplayback/"),
    ("mcapturestorage", "http://id.loc.gov/vocabulary/mcapturestorage/"),
    ("mcolor", "http://id.loc.gov/vocabulary/mcolor/"),
    ("lcgft", "http://id.loc.gov/authorities/genreForms/"),
    ("lcsh", "http://id.loc.gov/authorities/subjects/"),
    ("childrensSubjects", "http://id.loc.gov/authorities/childrensSubjects/"),
]


@pytest.mark.parametrize(("short_name", "uri_space"), _FINTO_VOCAB_ASSERTIONS)
def test_finto_vocabulary_entries_carry_expected_uri_space_and_short_name(
    graph: Graph, short_name: str, uri_space: str
) -> None:
    """Each Finto vocab declaration must have the canonical URI-space
    and a matching short name. Skosmos uses the URI-space to route
    every encountered URI to the right vocab."""
    candidates = [s for s, _, _ in graph.triples((None, VOID.uriSpace, Literal(uri_space)))]
    assert len(candidates) == 1, f"missing vocab entry for {short_name} ({uri_space!r})"
    vocab = candidates[0]
    short_names = {str(o) for _, _, o in graph.triples((vocab, SKOSMOS.shortName, None))}
    assert short_name in short_names


@pytest.mark.parametrize(("_short", "uri_space"), _FINTO_VOCAB_ASSERTIONS)
def test_finto_vocabulary_entries_point_at_local_fuseki(
    graph: Graph, _short: str, uri_space: str
) -> None:
    """3b loads each vocab dump into our own Fuseki under the URI-space
    as the named graph. The vocab declaration must point at that exact
    graph URI on our SPARQL endpoint, not at api.finto.fi — that's the
    whole reason 3b exists."""
    [vocab] = [s for s, _, _ in graph.triples((None, VOID.uriSpace, Literal(uri_space)))]
    assert (vocab, SKOSMOS.sparqlEndpoint, URIRef("http://fuseki:3030/bffi/sparql")) in graph
    assert (vocab, SKOSMOS.sparqlGraph, URIRef(uri_space)) in graph


# --- BFFI 4-class siblings (P-45 commit 5) -----------------------------
#
# Works, Expressions and Manifestations each get their own Skosmos vocab
# entry so the URL routing (via ``void:uriSpace``) lands a user on the
# right entity-type page when they click a URI. All three share the
# same Fuseki named graph (``bffi-works``) because they're a single
# connected RDF graph in storage.

_BFFI_SIBLING_VOCABS: list[tuple[str, str, URIRef]] = [
    ("bffi-works", "http://urn.fi/URN:NBN:fi:bib:work:", BFFI.Work),
    ("bffi-expressions", "http://urn.fi/URN:NBN:fi:bib:expression:", BFFI.Expression),
    (
        "bffi-manifestations",
        "http://urn.fi/URN:NBN:fi:bib:manifestation:",
        BFFI.Manifestation,
    ),
]


@pytest.mark.parametrize(("short_name", "uri_space", "show_class"), _BFFI_SIBLING_VOCABS)
def test_bffi_sibling_vocabularies_share_bffi_works_named_graph(
    graph: Graph, short_name: str, uri_space: str, show_class: URIRef
) -> None:
    """All three BFFI vocab entries point at the same Fuseki named
    graph — separation is purely a UI navigation concern."""
    [vocab] = [s for s, _, _ in graph.triples((None, VOID.uriSpace, Literal(uri_space)))]
    short_names = {str(o) for _, _, o in graph.triples((vocab, SKOSMOS.shortName, None))}
    assert short_name in short_names
    assert (
        vocab,
        SKOSMOS.sparqlGraph,
        URIRef("http://urn.fi/URN:NBN:fi:bib:graph:bffi-works"),
    ) in graph
    assert (vocab, SKOSMOS.indexShowClass, show_class) in graph


def test_finto_vocabulary_kanto_is_finnish_only(graph: Graph) -> None:
    """KANTO carries Finnish labels only; the vocab entry must reflect
    that or Skosmos UI sessions in en/sv will request languages KANTO
    doesn't have and fall through silently."""
    [kanto] = [
        s
        for s, _, _ in graph.triples(
            (None, VOID.uriSpace, Literal("http://urn.fi/URN:NBN:fi:au:finaf:"))
        )
    ]
    languages = sorted(
        str(o) for o in graph.objects(kanto, SKOSMOS.language) if isinstance(o, Literal)
    )
    assert languages == ["fi"]
