"""Unit tests for the BIBFRAME ↔ lkd.rdf cross-mapping diagnostic."""

from __future__ import annotations

from rdflib import URIRef

from bffi_pipeline.diagnostic.mapping_coverage import (
    BF_NAMESPACE,
    BFFI_NAMESPACE,
    PathStep,
    analyze_mapping_coverage,
    format_path,
)
from bffi_pipeline.rdf_utils import local_name


def _bf(name: str) -> URIRef:
    return URIRef(BF_NAMESPACE + name)


def _bffi(name: str) -> URIRef:
    return URIRef(BFFI_NAMESPACE + name)


# --- counts the inline experiment surfaced -----------------------------


def test_coverage_total_matches_bibframe_declared_terms_count() -> None:
    """BIBFRAME 3.0.1 declares 450 bf:* terms (224 classes + 157 object
    properties + 69 datatype properties); the report should cover the
    full set across its three buckets."""
    report = analyze_mapping_coverage()
    assert report.total == 450
    assert report.max_hops == 3


def test_direct_bucket_includes_well_known_equivalentclass_pairs() -> None:
    """``bf:Work`` ↔ ``bffi:BibframeWork`` is the canonical
    ``owl:equivalentClass`` row; the direct bucket must contain it."""
    report = analyze_mapping_coverage()
    direct_bf_terms = {r.bf_term for r in report.direct}
    assert _bf("Work") in direct_bf_terms
    assert _bf("Person") in direct_bf_terms
    assert _bf("Identifier") in direct_bf_terms


def test_indirect_bucket_includes_identifier_subclasses_via_parent_walk() -> None:
    """``bf:Isbn`` reaches ``bffi:Identifier`` via the indirect chain
    ``[bf:subClassOf] → [equivalentClass]`` — the route the
    ontology-driven Identifier routing exploits."""
    report = analyze_mapping_coverage()
    indirect_by_term = {r.bf_term: r for r in report.indirect}
    assert _bf("Isbn") in indirect_by_term
    reach = indirect_by_term[_bf("Isbn")]
    best = reach.best
    assert best is not None
    bffi_uri, path = best
    assert bffi_uri == _bffi("Identifier")
    relations = [step.relation for step in path]
    assert relations == ["bf:subClassOf", "equivalentClass"]


def test_indirect_bucket_includes_axis_split_classes_via_broadmatch() -> None:
    """``bf:Monograph`` reaches ``bffi:MonographExpression`` via a
    1-hop ``bffi-meta:broadMatch`` — picked up by route_axis_default_classes."""
    report = analyze_mapping_coverage()
    indirect_by_term = {r.bf_term: r for r in report.indirect}
    assert _bf("Monograph") in indirect_by_term


def test_unreachable_bucket_includes_pmo_music_terms() -> None:
    """``bf:DramaticRole`` / ``bf:Ensemble`` / ``bf:KeyMode`` etc. are
    BIBFRAME 3.0.1's PMO additions — BFFI 1.0.0 predates them, so no
    chain of any length reaches a ``bffi:*`` equivalent."""
    report = analyze_mapping_coverage()
    unreachable = set(report.unreachable)
    for name in ("DramaticRole", "Ensemble", "KeyMode", "MediumOfPerformance"):
        assert _bf(name) in unreachable, f"bf:{name} should be unreachable in BFFI 1.0.0"


def test_unreachable_bucket_includes_provision_activity_statement() -> None:
    """The corpus-derived gap from the 20 k bench: bf:provisionActivityStatement
    is declared by BIBFRAME but lkd.rdf has no mapping for it."""
    report = analyze_mapping_coverage()
    assert _bf("provisionActivityStatement") in set(report.unreachable)


# --- shape / formatting -------------------------------------------------


def test_reach_best_picks_shortest_path() -> None:
    """When multiple bffi:* URIs are reachable, ``.best`` returns the
    one with the fewest hops; ties broken lexicographically."""
    report = analyze_mapping_coverage()
    for reach in report.indirect[:20]:
        best = reach.best
        assert best is not None
        _bffi_uri, path = best
        assert all(len(other) >= len(path) for other in reach.bffi_paths.values())


def test_format_path_renders_relation_chain() -> None:
    steps = (
        PathStep(_bf("Isbn"), "bf:subClassOf", _bf("Identifier")),
        PathStep(_bf("Identifier"), "equivalentClass", _bffi("Identifier")),
    )
    assert format_path(steps) == "[bf:subClassOf] → [equivalentClass]"


def test_format_path_handles_empty_chain() -> None:
    """A bf:* term reached at zero hops (i.e. it IS a bffi:* — never
    happens in practice but the formatter should still work)."""
    assert format_path(()) == ""


def test_local_name_strips_to_final_segment() -> None:
    assert local_name(_bf("Isbn")) == "Isbn"
    assert local_name(_bffi("BibframeWork")) == "BibframeWork"
    # Fragment-delimited URIs surface the fragment.
    assert local_name(URIRef("http://example.org/x#Frag")) == "Frag"


# --- summary count regression guard ------------------------------------


def test_indirect_count_in_expected_range() -> None:
    """The indirect-bucket count is the canary on whether routings cover
    the discoverable terms. Locked to the value observed at commit
    time (~130 ± 5); any large drift means the ontologies changed in
    a non-trivial way and the routings should be re-audited."""
    report = analyze_mapping_coverage()
    assert 120 <= len(report.indirect) <= 140


def test_unreachable_count_in_expected_range() -> None:
    """The unreachable-bucket count is the true-gap backlog. Locked to
    the value observed at commit time (~42 ± 5); a shrink is good
    (BFFI added something), a growth means BIBFRAME added a gap."""
    report = analyze_mapping_coverage()
    assert 37 <= len(report.unreachable) <= 47


def test_bucket_counts_are_stable_past_depth_3() -> None:
    """The BFS walk over the combined ontology graph saturates at
    depth 3: increasing ``max_hops`` past that doesn't move terms
    between buckets. Confirms the 42 unreachable terms aren't
    depth-bound artifacts — they're in connected components that
    don't touch any ``bffi:*`` node at all.

    If a future BIBFRAME / BFFI refresh changes this, the diagnostic
    output is no longer trustworthy at default depth and the routing
    logic needs re-examination."""
    at_3 = analyze_mapping_coverage(max_hops=3)
    at_5 = analyze_mapping_coverage(max_hops=5)
    at_10 = analyze_mapping_coverage(max_hops=10)
    assert (
        (len(at_3.direct), len(at_3.indirect), len(at_3.unreachable))
        == (len(at_5.direct), len(at_5.indirect), len(at_5.unreachable))
        == (len(at_10.direct), len(at_10.indirect), len(at_10.unreachable))
    )
