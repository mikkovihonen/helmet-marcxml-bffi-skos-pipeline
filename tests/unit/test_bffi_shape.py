"""Hand-crafted SHACL pass/fail cases for the Boundary 3 (BFFI) shape."""

from __future__ import annotations

import textwrap

from rdflib import Graph

from bffi_pipeline.validation.bffi import validate_graph

PREAMBLE = textwrap.dedent(
    """
    @prefix bf:   <http://id.loc.gov/ontologies/bibframe/> .
    @prefix bffi: <http://urn.fi/URN:NBN:fi:schema:bffi:> .
    @prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
    @prefix skos: <http://www.w3.org/2004/02/skos/core#> .
    @prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .

    <http://urn.fi/URN:NBN:fi:bib:source:helmet> a bf:Source .
    """
).strip()

# A perfectly-shaped Work + Expression pair that should conform.
VALID_TTL = (
    PREAMBLE
    + textwrap.dedent(
        """

    <urn:work/A> a bffi:Work ;
        bffi:hasExpression <urn:expr/A> ;
        bf:identifiedBy <urn:work/A/id> ;
        skos:prefLabel "Sota ja rauha"@fi .

    <urn:work/A/id> a bf:Local ;
        rdf:value "12345" ;
        bf:source <http://urn.fi/URN:NBN:fi:bib:source:helmet> .

    <urn:expr/A> a bffi:Expression ;
        bffi:expressionOf <urn:work/A> ;
        bf:identifiedBy <urn:expr/A/id> ;
        skos:prefLabel "Sota ja rauha"@fi .

    <urn:expr/A/id> a bf:Local ;
        rdf:value "12345" ;
        bf:source <http://urn.fi/URN:NBN:fi:bib:source:helmet> .
    """
    ).strip()
)


def _graph(turtle: str) -> Graph:
    g = Graph()
    g.parse(data=turtle, format="turtle")
    return g


def test_valid_pair_conforms() -> None:
    report = validate_graph(_graph(VALID_TTL))
    assert report.conforms, report.text


def test_work_without_expression_fails() -> None:
    report = validate_graph(
        _graph(
            PREAMBLE
            + """
            <urn:work/B> a bffi:Work ;
                bf:identifiedBy [ a bf:Local ;
                                  rdf:value "1" ;
                                  bf:source <http://urn.fi/URN:NBN:fi:bib:source:helmet> ] ;
                skos:prefLabel "x"@fi .
            """
        )
    )
    assert not report.conforms
    assert "hasExpression" in report.text


def test_expression_without_work_fails() -> None:
    report = validate_graph(
        _graph(
            PREAMBLE
            + """
            <urn:expr/C> a bffi:Expression ;
                bf:identifiedBy [ a bf:Local ;
                                  rdf:value "1" ;
                                  bf:source <http://urn.fi/URN:NBN:fi:bib:source:helmet> ] .
            """
        )
    )
    assert not report.conforms
    assert "expressionOf" in report.text


def test_work_with_untagged_pref_label_fails() -> None:
    report = validate_graph(
        _graph(
            PREAMBLE
            + """
            <urn:work/D> a bffi:Work ;
                bffi:hasExpression <urn:expr/D> ;
                bf:identifiedBy [ a bf:Local ;
                                  rdf:value "1" ;
                                  bf:source <http://urn.fi/URN:NBN:fi:bib:source:helmet> ] ;
                skos:prefLabel "untagged" .

            <urn:expr/D> a bffi:Expression ;
                bffi:expressionOf <urn:work/D> ;
                bf:identifiedBy [ a bf:Local ;
                                  rdf:value "1" ;
                                  bf:source <http://urn.fi/URN:NBN:fi:bib:source:helmet> ] .
            """
        )
    )
    assert not report.conforms
    assert "fi/sv/en" in report.text


def test_work_without_helmet_identifier_fails() -> None:
    report = validate_graph(
        _graph(
            PREAMBLE
            + """
            <urn:work/E> a bffi:Work ;
                bffi:hasExpression <urn:expr/E> ;
                skos:prefLabel "x"@fi .

            <urn:expr/E> a bffi:Expression ;
                bffi:expressionOf <urn:work/E> ;
                bf:identifiedBy [ a bf:Local ;
                                  rdf:value "1" ;
                                  bf:source <http://urn.fi/URN:NBN:fi:bib:source:helmet> ] .
            """
        )
    )
    assert not report.conforms
    assert "Helmet" in report.text


def test_work_with_expression_only_property_fails() -> None:
    report = validate_graph(
        _graph(
            PREAMBLE
            + """
            <urn:work/F> a bffi:Work ;
                bffi:hasExpression <urn:expr/F> ;
                bffi:language <urn:lang/fi> ;
                bf:identifiedBy [ a bf:Local ;
                                  rdf:value "1" ;
                                  bf:source <http://urn.fi/URN:NBN:fi:bib:source:helmet> ] ;
                skos:prefLabel "x"@fi .

            <urn:expr/F> a bffi:Expression ;
                bffi:expressionOf <urn:work/F> ;
                bf:identifiedBy [ a bf:Local ;
                                  rdf:value "1" ;
                                  bf:source <http://urn.fi/URN:NBN:fi:bib:source:helmet> ] .
            """
        )
    )
    assert not report.conforms
    assert "Expression-only" in report.text


def test_expression_with_work_only_property_fails() -> None:
    report = validate_graph(
        _graph(
            PREAMBLE
            + """
            <urn:work/G> a bffi:Work ;
                bffi:hasExpression <urn:expr/G> ;
                bf:identifiedBy [ a bf:Local ;
                                  rdf:value "1" ;
                                  bf:source <http://urn.fi/URN:NBN:fi:bib:source:helmet> ] ;
                skos:prefLabel "x"@fi .

            <urn:expr/G> a bffi:Expression ;
                bffi:expressionOf <urn:work/G> ;
                bffi:originDate "2023" ;
                bf:identifiedBy [ a bf:Local ;
                                  rdf:value "1" ;
                                  bf:source <http://urn.fi/URN:NBN:fi:bib:source:helmet> ] .
            """
        )
    )
    assert not report.conforms
    assert "Work-only" in report.text


def test_dual_typed_node_fails_disjointness() -> None:
    report = validate_graph(
        _graph(
            PREAMBLE
            + """
            <urn:hybrid/H> a bffi:Work, bffi:Expression ;
                bffi:hasExpression <urn:expr/H> ;
                bffi:expressionOf <urn:work/H> ;
                bf:identifiedBy [ a bf:Local ;
                                  rdf:value "1" ;
                                  bf:source <http://urn.fi/URN:NBN:fi:bib:source:helmet> ] ;
                skos:prefLabel "x"@fi .
            """
        )
    )
    assert not report.conforms
