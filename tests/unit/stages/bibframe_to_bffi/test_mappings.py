"""Unit tests for the clean-rename rule extractor."""

from __future__ import annotations

from rdflib import URIRef

from bffi_pipeline.stages.bibframe_to_bffi.mappings import (
    BF_NAMESPACE,
    BFFI_NAMESPACE,
    load_rules,
)


def _bf(local: str) -> URIRef:
    return URIRef(BF_NAMESPACE + local)


def _bffi(local: str) -> URIRef:
    return URIRef(BFFI_NAMESPACE + local)


def test_load_rules_pulls_bf_work_to_bffi_bibframework() -> None:
    """The canonical case from the memory note: ``bf:Work`` is the anchor for
    ``bffi:BibframeWork``, **not** ``bffi:Work`` (which is a separate
    RDA-aligned concept)."""
    rules = load_rules()
    assert rules.classes[_bf("Work")] == _bffi("BibframeWork")


def test_load_rules_pulls_bf_person_to_bffi_person() -> None:
    rules = load_rules()
    assert rules.classes[_bf("Person")] == _bffi("Person")


def test_load_rules_pulls_bf_title_to_bffi_title() -> None:
    rules = load_rules()
    assert rules.classes[_bf("Title")] == _bffi("Title")


def test_load_rules_pulls_bf_identifiedby_to_bffi_identifiedby() -> None:
    rules = load_rules()
    assert rules.predicates[_bf("identifiedBy")] == _bffi("identifiedBy")


def test_load_rules_pulls_bf_maintitle_to_bffi_maintitle() -> None:
    rules = load_rules()
    assert rules.predicates[_bf("mainTitle")] == _bffi("mainTitle")


def test_rules_rename_method_handles_unmapped_uri() -> None:
    """URIs without a rename rule pass through unchanged."""
    rules = load_rules()
    unmapped = URIRef("http://example.org/something")
    assert rules.rename(unmapped) == unmapped


def test_rules_rename_method_substitutes_class_uri() -> None:
    rules = load_rules()
    assert rules.rename(_bf("Work")) == _bffi("BibframeWork")


def test_rules_rename_method_substitutes_predicate_uri() -> None:
    rules = load_rules()
    assert rules.rename(_bf("mainTitle")) == _bffi("mainTitle")


def test_load_rules_yields_dozens_of_class_renames() -> None:
    """Sanity check the rdflib parse — `lkd.rdf` declares roughly a
    hundred bf:<->bffi equivalent-class triples; a regression that
    silently empties the rule set would be hard to catch from a single
    spot-check above."""
    rules = load_rules()
    assert len(rules.classes) > 50
    assert len(rules.predicates) > 50
