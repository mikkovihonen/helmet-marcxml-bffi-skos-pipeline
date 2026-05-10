"""Unit tests for the heuristic + orchestration in contrib_extract.

Covers token coverage, the multilingual stop-word filter, and the
heuristic gate that decides whether to call the LLM cascade. The LLM
side is exercised in test_contrib_extract_llm.py — never loaded here."""

from __future__ import annotations

from textwrap import dedent

import pytest
from rdflib import Graph, URIRef

from bffi_pipeline.contrib_extract import (
    ExtractionInputs,
    compute_uncovered_tokens,
    extract_contributions,
    gather_inputs,
    heuristic_fires,
    read_existing_agent_labels,
    read_responsibility_statement,
)
from bffi_pipeline.contrib_extract_llm import (
    ContribCandidate,
    ContribExtractDecision,
    StubContribExtractor,
)

# --- compute_uncovered_tokens ---------------------------------------------


def test_compute_uncovered_tokens_returns_empty_when_245c_fully_covered() -> None:
    """The Tšarka pattern: every 245$c name is also in 100/700, so the
    heuristic should NOT fire."""
    c = (
        "K. Helenius ; [photography = valokuvat: Katja Hagelstam] ; "
        "[translated by ... Anastassia Tsernosova ... Pauliina Tervo ... Sonja Hemberg]"
    )
    agents = (
        "Helenius, Kari",
        "Hagelstam, Katja",
        "Tsernosova, Anastassia",
        "Tervo, Pauliina",
        "Hemberg, Sonja",
    )
    assert compute_uncovered_tokens(c, agents) == set()


def test_compute_uncovered_tokens_finds_genuinely_missing_agent() -> None:
    """Christopher Hogwood appears in 245$c but not in 100/700 — the
    cascade should fire on this record."""
    c = "Vivaldi ; Simon Standage, The Academy of Ancient Music & Christopher Hogwood"
    agents = ("Vivaldi, Antonio", "Standage, Simon", "Academy of Ancient Music")
    uncovered = compute_uncovered_tokens(c, agents)
    assert "christopher" in uncovered
    assert "hogwood" in uncovered


def test_compute_uncovered_tokens_filters_articles() -> None:
    """Capitalised English / Spanish / German articles must not fire."""
    assert compute_uncovered_tokens("The Rolling Stones", ("Rolling Stones",)) == set()
    assert compute_uncovered_tokens("Los Pirañas", ("Pirañas",)) == set()
    assert compute_uncovered_tokens("Der Spiegel", ("Spiegel",)) == set()


def test_compute_uncovered_tokens_filters_role_markers() -> None:
    """Role-marker verbs at sentence start ('Edited', 'Compiled',
    'Translated') were major false-positive sources in the v1
    measurement and must be filtered."""
    c = "Edited by Stanley Sadie"
    assert compute_uncovered_tokens(c, ("Sadie, Stanley",)) == set()
    assert compute_uncovered_tokens("Compiled by Lucy Holiday", ()) == {"holiday", "lucy"}


def test_compute_uncovered_tokens_filters_language_names() -> None:
    """Capitalised language adjectives must not fire — they're never
    agent names. Triggered on real Helmet records like
    'translated into Kannada by B. G. Ramesh'."""
    c = "Nitin Agarwal ; [translated into Kannada by B. G. Ramesh]"
    agents = ("Agarwal, Nitin", "Ramesh, B. G.")
    assert compute_uncovered_tokens(c, agents) == set()


def test_compute_uncovered_tokens_filters_finnish_role_markers() -> None:
    """Finnish role markers ('toimittanut', 'kustantaja', 'käännös')
    capitalise at sentence start in real records — must be filtered.
    With the stop-word list applied, every name in this 245$c is
    covered by the matching 100/710 entries."""
    c = "toimittanut Martti Vuori ; kustantaja Metsäteknikkojen keskusliitto r.y."
    agents = ("Vuori, Martti", "Metsäteknikkojen keskusliitto")
    assert compute_uncovered_tokens(c, agents) == set()


def test_compute_uncovered_tokens_surfaces_finnish_declension_mismatch() -> None:
    """When the cataloguer gave the 245$c form in genitive but the 710
    entry uses nominative, the heuristic correctly flags the declined
    form as uncovered. Lemmatisation would dedup these; for now the
    LLM cascade gets to make the call via the existing-agents context."""
    c = "kustantaja Metsäteknikkojen keskusliitto r.y."
    agents = ("Metsäteknikkö keskusliitto",)  # nominative form
    assert compute_uncovered_tokens(c, agents) == {"metsäteknikkojen"}


def test_compute_uncovered_tokens_short_tokens_are_ignored() -> None:
    """The regex requires capital + 3+ chars; honorifics like 'Sir'
    are stop-listed; 'Mr' / 'Dr' (2 chars) never match."""
    c = "Mr Smith and Dr Jones"
    assert compute_uncovered_tokens(c, ()) == {"smith", "jones"}


def test_compute_uncovered_tokens_handles_finnish_diacritics() -> None:
    """The token regex must accept Å/Ä/Ö as the leading capital so
    Finnish-only records still tokenise correctly."""
    c = "Ääripäät ; Ässä Mörkö"
    assert compute_uncovered_tokens(c, ()) == {"ääripäät", "ässä", "mörkö"}


# --- BIBFRAME readers ----------------------------------------------------


_BIBFRAME_FIXTURE = dedent(
    """
    @prefix bf: <http://id.loc.gov/ontologies/bibframe/> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

    <http://example.org/Work> a bf:Work ;
        bf:hasInstance <http://example.org/Instance> ;
        bf:contribution <http://example.org/contrib1> .

    <http://example.org/Instance> a bf:Instance ;
        bf:responsibilityStatement "Edited by Stanley Sadie" .

    <http://example.org/contrib1> a bf:Contribution ;
        bf:agent <http://example.org/agent/Sadie> .

    <http://example.org/agent/Sadie> a bf:Person ;
        rdfs:label "Sadie, Stanley" .
    """
).strip()


@pytest.fixture
def bibframe_graph() -> Graph:
    g = Graph()
    g.parse(data=_BIBFRAME_FIXTURE, format="turtle")
    return g


def test_read_responsibility_statement_walks_hasInstance(bibframe_graph: Graph) -> None:
    work = URIRef("http://example.org/Work")
    assert read_responsibility_statement(bibframe_graph, work) == "Edited by Stanley Sadie"


def test_read_responsibility_statement_returns_none_when_absent() -> None:
    g = Graph()
    g.parse(
        data="@prefix bf: <http://id.loc.gov/ontologies/bibframe/> . "
        "<http://example.org/W> a bf:Work .",
        format="turtle",
    )
    assert read_responsibility_statement(g, URIRef("http://example.org/W")) is None


def test_read_existing_agent_labels_walks_contribution_chain(bibframe_graph: Graph) -> None:
    work = URIRef("http://example.org/Work")
    assert read_existing_agent_labels(bibframe_graph, work) == ("Sadie, Stanley",)


def test_read_existing_agent_labels_deduplicates_and_sorts() -> None:
    """Two contributions that share the same agent label collapse to
    one entry; the result is sorted so cascade re-runs see identical
    inputs."""
    g = Graph()
    g.parse(
        data=dedent(
            """
            @prefix bf: <http://id.loc.gov/ontologies/bibframe/> .
            @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
            <http://example.org/W> a bf:Work ;
                bf:contribution <http://example.org/c1>, <http://example.org/c2> .
            <http://example.org/c1> bf:agent <http://example.org/a1> .
            <http://example.org/c2> bf:agent <http://example.org/a2> .
            <http://example.org/a1> rdfs:label "Zola, Émile" .
            <http://example.org/a2> rdfs:label "Adams, Ansel" .
            """
        ).strip(),
        format="turtle",
    )
    assert read_existing_agent_labels(g, URIRef("http://example.org/W")) == (
        "Adams, Ansel",
        "Zola, Émile",
    )


# --- gather_inputs / heuristic_fires / extract_contributions -------------


def test_gather_inputs_returns_none_for_records_without_245c() -> None:
    g = Graph()
    g.parse(
        data="@prefix bf: <http://id.loc.gov/ontologies/bibframe/> . "
        "<http://example.org/W> a bf:Work .",
        format="turtle",
    )
    assert gather_inputs(g, URIRef("http://example.org/W")) is None


def test_gather_inputs_packages_245c_and_agents(bibframe_graph: Graph) -> None:
    inputs = gather_inputs(bibframe_graph, URIRef("http://example.org/Work"))
    assert inputs is not None
    assert inputs.c_subfield == "Edited by Stanley Sadie"
    assert inputs.existing_agent_labels == ("Sadie, Stanley",)


def test_heuristic_does_not_fire_when_245c_fully_covered() -> None:
    inputs = ExtractionInputs(
        work_uri=URIRef("http://example.org/W"),
        c_subfield="Edited by Stanley Sadie",
        existing_agent_labels=("Sadie, Stanley",),
    )
    assert heuristic_fires(inputs) is False


def test_heuristic_fires_when_245c_has_uncovered_names() -> None:
    inputs = ExtractionInputs(
        work_uri=URIRef("http://example.org/W"),
        c_subfield="Vivaldi ; Christopher Hogwood",
        existing_agent_labels=("Vivaldi, Antonio",),
    )
    assert heuristic_fires(inputs) is True


def test_extract_contributions_skips_when_heuristic_does_not_fire() -> None:
    inputs = ExtractionInputs(
        work_uri=URIRef("http://example.org/W"),
        c_subfield="Edited by Stanley Sadie",
        existing_agent_labels=("Sadie, Stanley",),
    )
    extractor = StubContribExtractor(
        default=ContribExtractDecision(
            contributions=[
                ContribCandidate(name="Should not appear", relator_code="aut"),
            ],
            rationale="Stub default — should not be reached when heuristic is false.",
        )
    )
    assert extract_contributions(inputs, extractor=extractor) is None


def test_extract_contributions_returns_none_when_no_extractor_supplied() -> None:
    """Heuristic-only mode: caller wants the gate measurement without
    paying for any LLM calls."""
    inputs = ExtractionInputs(
        work_uri=URIRef("http://example.org/W"),
        c_subfield="Vivaldi ; Christopher Hogwood",
        existing_agent_labels=("Vivaldi, Antonio",),
    )
    assert heuristic_fires(inputs) is True
    assert extract_contributions(inputs, extractor=None) is None


def test_extract_contributions_calls_extractor_when_heuristic_fires() -> None:
    inputs = ExtractionInputs(
        work_uri=URIRef("http://example.org/W"),
        c_subfield="Vivaldi ; Christopher Hogwood",
        existing_agent_labels=("Vivaldi, Antonio",),
    )
    decision = ContribExtractDecision(
        contributions=[
            ContribCandidate(name="Christopher Hogwood", relator_code="cnd"),
        ],
        rationale="Christopher Hogwood is new; assigned cnd (conductor).",
    )
    stub = StubContribExtractor(decisions={inputs.c_subfield: decision})
    out = extract_contributions(inputs, extractor=stub)
    assert out is decision
