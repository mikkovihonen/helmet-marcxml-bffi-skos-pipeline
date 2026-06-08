"""Hand-crafted SHACL pass/fail cases for the Boundary 3 (BFFI) shape.

P-45 commit 2: ``bf:identifiedBy`` moved from Work + Expression to
Manifestation. Test fixtures updated accordingly.
"""

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

# A perfectly-shaped Work + Expression + Manifestation triplet that
# should conform. ``bf:identifiedBy`` lives on the Manifestation only.
VALID_TTL = (
    PREAMBLE
    + textwrap.dedent(
        """

    <urn:work/A> a bffi:Work ;
        bffi:hasExpression <urn:expr/A> ;
        skos:prefLabel "Sota ja rauha"@fi .

    <urn:expr/A> a bffi:Expression ;
        bffi:expressionOf <urn:work/A> ;
        skos:prefLabel "Sota ja rauha"@fi .

    <urn:manif/A> a bffi:Manifestation ;
        bffi:expressionManifested <urn:expr/A> ;
        bffi:identifiedBy <urn:manif/A/id> .

    <urn:manif/A/id> a bffi:Local ;
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
            <urn:expr/C> a bffi:Expression .
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
                skos:prefLabel "untagged" .

            <urn:expr/D> a bffi:Expression ;
                bffi:expressionOf <urn:work/D> .
            """
        )
    )
    assert not report.conforms
    assert "fi/sv/en" in report.text


def test_manifestation_without_helmet_identifier_fails() -> None:
    """P-45 commit 2: the Helmet identifier requirement now lives on
    Manifestation, not Work / Expression."""
    report = validate_graph(
        _graph(
            PREAMBLE
            + """
            <urn:work/E> a bffi:Work ;
                bffi:hasExpression <urn:expr/E> ;
                skos:prefLabel "x"@fi .

            <urn:expr/E> a bffi:Expression ;
                bffi:expressionOf <urn:work/E> .

            <urn:manif/E> a bffi:Manifestation ;
                bffi:expressionManifested <urn:expr/E> .
            """
        )
    )
    assert not report.conforms
    assert "Helmet" in report.text


def test_work_with_bf_identifiedby_fails() -> None:
    """P-45 commit 2: ``bf:identifiedBy`` is now Manifestation-only.
    Putting it on a Work violates the shape's max-cardinality 0 rule."""
    report = validate_graph(
        _graph(
            PREAMBLE
            + """
            <urn:work/E2> a bffi:Work ;
                bffi:hasExpression <urn:expr/E2> ;
                bffi:identifiedBy [ a bffi:Local ;
                                    rdf:value "x" ;
                                    bf:source <http://urn.fi/URN:NBN:fi:bib:source:helmet> ] ;
                skos:prefLabel "x"@fi .

            <urn:expr/E2> a bffi:Expression ;
                bffi:expressionOf <urn:work/E2> .

            <urn:manif/E2> a bffi:Manifestation ;
                bffi:expressionManifested <urn:expr/E2> ;
                bffi:identifiedBy [ a bffi:Local ;
                                    rdf:value "x" ;
                                    bf:source <http://urn.fi/URN:NBN:fi:bib:source:helmet> ] .
            """
        )
    )
    assert not report.conforms
    assert "Manifestation-only" in report.text


def test_work_with_expression_only_property_fails() -> None:
    report = validate_graph(
        _graph(
            PREAMBLE
            + """
            <urn:work/F> a bffi:Work ;
                bffi:hasExpression <urn:expr/F> ;
                bffi:language <urn:lang/fi> ;
                skos:prefLabel "x"@fi .

            <urn:expr/F> a bffi:Expression ;
                bffi:expressionOf <urn:work/F> .

            <urn:manif/F> a bffi:Manifestation ;
                bffi:expressionManifested <urn:expr/F> ;
                bffi:identifiedBy [ a bffi:Local ;
                                    rdf:value "x" ;
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
                skos:prefLabel "x"@fi .

            <urn:expr/G> a bffi:Expression ;
                bffi:expressionOf <urn:work/G> ;
                bffi:originDate "2023" .

            <urn:manif/G> a bffi:Manifestation ;
                bffi:expressionManifested <urn:expr/G> ;
                bffi:identifiedBy [ a bffi:Local ;
                                    rdf:value "x" ;
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
                skos:prefLabel "x"@fi .
            """
        )
    )
    assert not report.conforms
