"""Unit tests for the BIBFRAME ontology scan."""

from __future__ import annotations

from rdflib import URIRef

from bffi_pipeline.bibframe import BF_NAMESPACE, load_ontology


def _bf(name: str) -> URIRef:
    return URIRef(BF_NAMESPACE + name)


# --- closed term sets ---------------------------------------------------


def test_load_ontology_indexes_well_known_classes() -> None:
    ont = load_ontology()
    assert _bf("Work") in ont.classes
    assert _bf("Instance") in ont.classes
    assert _bf("Item") in ont.classes
    assert _bf("Agent") in ont.classes
    assert _bf("Identifier") in ont.classes


def test_load_ontology_indexes_well_known_properties() -> None:
    ont = load_ontology()
    # mainTitle is a datatype property (carries a literal).
    assert _bf("mainTitle") in ont.datatype_properties
    # accompaniedBy is an object property (carries a URI).
    assert _bf("accompaniedBy") in ont.object_properties


def test_load_ontology_yields_hundreds_of_classes() -> None:
    """Sanity check the parse didn't silently produce an empty set."""
    ont = load_ontology()
    assert len(ont.classes) > 100
    assert len(ont.object_properties) > 100


def test_bf_statement_artifact_is_not_in_bibframe_ontology() -> None:
    """The ``bf:Statement`` URIs we saw in the 20 k stage-3 output are
    marc2bibframe2 artifacts — BIBFRAME itself doesn't declare a
    ``bf:Statement`` class. This test guards against accidentally
    treating it as a real BIBFRAME term."""
    ont = load_ontology()
    assert not ont.is_known_class(_bf("Statement"))


def test_provision_activity_statement_is_in_bibframe_ontology() -> None:
    """Contrast with bf:Statement above — bf:provisionActivityStatement
    IS a real BIBFRAME term (DatatypeProperty "Provider statement").
    BFFI just doesn't acknowledge it; the residue we saw is a true gap
    in lkd.rdf rather than an upstream-emit artifact."""
    ont = load_ontology()
    assert ont.is_known_property(_bf("provisionActivityStatement"))


# --- class hierarchy ----------------------------------------------------


def test_class_parents_includes_bf_person_subclass_of_bf_agent() -> None:
    ont = load_ontology()
    assert _bf("Agent") in ont.class_parents[_bf("Person")]


def test_class_ancestors_walks_transitively() -> None:
    """Multi-hop subclass walk: ``bf:Isbn`` is a subclass of
    ``bf:Identifier`` (one hop in BIBFRAME 3.0.1)."""
    ont = load_ontology()
    ancestors = ont.class_ancestors(_bf("Isbn"))
    assert _bf("Identifier") in ancestors


def test_class_descendants_enumerates_identifier_subclasses() -> None:
    """``bf:Identifier`` has many subclasses; the descendants set covers
    the standard ones routed in the Identifier-scheme routing."""
    ont = load_ontology()
    descendants = ont.class_descendants(_bf("Identifier"))
    for sub in ("Isbn", "Issn", "Ean", "Lccn", "AudioIssueNumber"):
        assert _bf(sub) in descendants, f"bf:Identifier should subsume bf:{sub}"


def test_is_subclass_of_returns_true_for_known_chain() -> None:
    ont = load_ontology()
    assert ont.is_subclass_of(_bf("Person"), _bf("Agent"))
    assert ont.is_subclass_of(_bf("Isbn"), _bf("Identifier"))


def test_is_subclass_of_returns_false_for_unrelated_classes() -> None:
    ont = load_ontology()
    assert not ont.is_subclass_of(_bf("Person"), _bf("Identifier"))


# --- property hierarchy -------------------------------------------------


def test_property_parents_includes_accompaniedby_subprop_of_relatedto() -> None:
    """``bf:accompaniedBy rdfs:subPropertyOf bf:relatedTo`` in BIBFRAME 3.0.1.
    Knowing this lets a routing dispatch on the broader ``bf:relatedTo``
    class of predicates instead of enumerating subclasses manually."""
    ont = load_ontology()
    assert _bf("relatedTo") in ont.property_parents[_bf("accompaniedBy")]


def test_property_ancestors_walks_transitively() -> None:
    ont = load_ontology()
    # bf:absorbed rdfs:subPropertyOf bf:precededBy in BIBFRAME 3.0.1
    ancestors = ont.property_ancestors(_bf("absorbed"))
    assert _bf("precededBy") in ancestors


# --- diagnostics --------------------------------------------------------


def test_root_classes_set_is_non_empty_and_includes_agent() -> None:
    """``bf:Agent`` has no ``bf:*`` parent — it's a top-level concept."""
    ont = load_ontology()
    roots = ont.root_classes()
    assert _bf("Agent") in roots
    assert len(roots) > 50
