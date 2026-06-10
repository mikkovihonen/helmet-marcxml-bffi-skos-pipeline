"""Unit tests for the mapping-tables generator.

The generator produces the Classes + Predicates tables in
`docs/bf_to_bffi_mapping.md` from the union of `vocab/bibframe.rdf`,
`vocab/lkd.rdf`, and the routing registry in
`src/bffi_pipeline/stages/bibframe_to_bffi/routings.py`. These tests
lock the per-row classification logic and the doc-drift guard.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from rdflib import URIRef

from bffi_pipeline.bibframe import load_ontology
from bffi_pipeline.diagnostic.mapping_tables import (
    CLASSES_BEGIN_MARKER,
    CLASSES_END_MARKER,
    DEFAULT_DOC_PATH,
    PREDICATES_BEGIN_MARKER,
    PREDICATES_END_MARKER,
    build_blocks,
    regenerate_mapping_tables,
)
from bffi_pipeline.rdf_utils import local_name

# --- coverage: every bf:* term in BIBFRAME 3.0.1 lands in exactly one table ---


def _bf_terms_in_block(block: str) -> set[str]:
    """Local names of every ``bf:*`` term referenced in the first column.

    Local names can contain digits (e.g. ``bf:Gtin14Number``), so the
    regex matches alphanumerics — not just letters.
    """
    return set(re.findall(r"\| `bf:([A-Za-z][A-Za-z0-9]*)`", block))


def test_classes_block_covers_every_bibframe_class() -> None:
    """Every ``owl:Class`` BIBFRAME declares appears once in the Classes
    table — no under- or over-coverage of the 224-term universe."""
    blocks = build_blocks()
    ontology = load_ontology()
    expected = {local_name(URIRef(c)) for c in ontology.classes}
    actual = _bf_terms_in_block(blocks.classes_block)
    assert expected == actual, (
        f"missing from classes table: {sorted(expected - actual)}; "
        f"unexpected in classes table: {sorted(actual - expected)}"
    )


def test_predicates_block_covers_every_bibframe_property() -> None:
    """Every ``owl:ObjectProperty`` + ``owl:DatatypeProperty`` BIBFRAME
    declares appears once in the Predicates table."""
    blocks = build_blocks()
    ontology = load_ontology()
    expected = {
        local_name(URIRef(p)) for p in (ontology.object_properties | ontology.datatype_properties)
    }
    actual = _bf_terms_in_block(blocks.predicates_block)
    assert expected == actual


def test_class_and_predicate_tables_are_disjoint() -> None:
    """A ``bf:*`` term is either a class or a property — never both. The
    generator must not double-list a term across the two tables."""
    blocks = build_blocks()
    class_terms = _bf_terms_in_block(blocks.classes_block)
    pred_terms = _bf_terms_in_block(blocks.predicates_block)
    overlap = class_terms & pred_terms
    assert not overlap, f"terms appearing in both tables: {sorted(overlap)}"


# --- status classification: precedence rules ----------------------------


def _row_with_term(block: str, bf_local_name: str) -> str:
    """Return the table row whose first column is ``bf:<local_name>``."""
    for line in block.splitlines():
        if line.startswith(f"| `bf:{bf_local_name}` |"):
            return line
    msg = f"no row found for bf:{bf_local_name}"
    raise AssertionError(msg)


def test_clean_status_beats_routing_when_direct_equivalence_exists() -> None:
    """``bf:Local`` is both a ``bf:Identifier`` descendant (would be
    routed) *and* has a direct ``owl:equivalentClass bffi:Local`` link.
    Clean wins — the row reports `clean`, not `routed`."""
    blocks = build_blocks()
    row = _row_with_term(blocks.classes_block, "Local")
    assert "**clean**" in row
    assert "bffi:Local" in row
    assert "route_identifier_schemes" not in row


def test_routed_status_when_no_direct_equivalence() -> None:
    """``bf:Isbn`` has no direct ``lkd.rdf`` link but IS handled by
    `route_identifier_schemes`. Row should be tagged `routed` with the
    handler named."""
    blocks = build_blocks()
    row = _row_with_term(blocks.classes_block, "Isbn")
    assert "**routed**" in row
    assert "route_identifier_schemes" in row
    assert "identifiers/isbn" in row


def test_inherited_status_when_subclass_chain_reaches_bffi() -> None:
    """``bf:AbbreviatedTitle`` reaches ``bffi:Title`` via two
    ``bf:subClassOf`` hops plus the ``bf:Title ≡ bffi:Title``
    equivalence. The chain has no semantic-shift link — status should
    be `inherited`."""
    blocks = build_blocks()
    row = _row_with_term(blocks.classes_block, "AbbreviatedTitle")
    assert "*inherited*" in row
    assert "bffi:Title" in row


def test_drop_status_when_routing_is_marked_is_drop() -> None:
    """``bf:variantType`` (redundant with marcKey) and ``bf:noteType`` (no BFFI
    carrier) are dropped at emit. The auto-table must tag them with the
    distinct ``drop`` status — not the generic ``routed`` status — so the
    semantic is visible at a glance."""
    blocks = build_blocks()
    variant_row = _row_with_term(blocks.predicates_block, "variantType")
    note_type_row = _row_with_term(blocks.predicates_block, "noteType")
    for row in (variant_row, note_type_row):
        assert "**drop**" in row
        assert "**routed**" not in row


def test_zero_gap_terms_milestone() -> None:
    """Every BIBFRAME 3.0.1 term either has a clean rename, a discriminator
    routing, an inheritance chain, a semantic-shift link, or a documented
    drop. The ``GAP`` status indicates a term with no path of any kind —
    a true ontology gap. The current milestone: zero GAPs across all
    450 declared terms (224 classes + 226 properties).

    If this test starts failing: a future ontology refresh added a term
    that doesn't fit any existing pattern. Either add a routing for it,
    register it as a defensive drop with a documented limitation, or
    propose a BFFI extension via NLF — don't ignore. The GAP status
    rendering is still tested indirectly via the routing-status fixtures."""
    blocks = build_blocks()
    classes_gaps = [line for line in blocks.classes_block.splitlines() if "**GAP**" in line]
    predicates_gaps = [line for line in blocks.predicates_block.splitlines() if "**GAP**" in line]
    assert classes_gaps == [], f"unexpected class GAPs: {classes_gaps}"
    assert predicates_gaps == [], f"unexpected predicate GAPs: {predicates_gaps}"


def test_hub_is_routed_with_marckey_discriminator() -> None:
    """``bf:Hub`` is in the diagnostic's `unreachable` bucket (no
    `lkd.rdf` relation) but `route_hubs` covers it. Row must surface
    the routing, not list it as a gap."""
    blocks = build_blocks()
    row = _row_with_term(blocks.classes_block, "Hub")
    assert "**routed**" in row
    assert "route_hubs" in row
    assert "marcKey" in row


def test_provision_activity_statement_routed_via_uri_fragment() -> None:
    """The corpus-derived gap surfaced by the 20 k bench. The routing
    splits to ``bffi:date`` vs ``bffi:Note`` based on the URI
    fragment — both targets must appear in the replacement column."""
    blocks = build_blocks()
    row = _row_with_term(blocks.predicates_block, "provisionActivityStatement")
    assert "**routed**" in row
    assert "route_provision_activity_statement" in row
    assert "bffi:date" in row
    assert "bffi:Note" in row


# --- tally line ---------------------------------------------------------


def test_classes_tally_line_includes_expected_buckets() -> None:
    """The summary line below the Classes table mentions the active
    bucket statuses (the line only shows non-zero counts). At the
    current "zero GAPs" milestone, ``GAP`` legitimately drops from
    the line; ``clean`` and ``routed`` (the two largest buckets)
    remain non-zero and stay listed."""
    blocks = build_blocks()
    tally_line = blocks.classes_block.strip().splitlines()[-1]
    assert tally_line.startswith("_")
    assert "terms total" in tally_line
    assert "clean" in tally_line
    assert "routed" in tally_line


# --- doc-drift guard ----------------------------------------------------


def test_doc_on_disk_matches_generator_output() -> None:
    """The committed `docs/bf_to_bffi_mapping.md` must equal what the
    generator would emit. If this fails: run
    ``bffi-pipeline regenerate-mapping-tables`` and commit the diff."""
    new_text, changed = regenerate_mapping_tables(check=True)
    assert not changed, (
        "docs/bf_to_bffi_mapping.md is out of sync with the generator — "
        "run `bffi-pipeline regenerate-mapping-tables` to refresh."
    )
    # Belt-and-braces: also confirm the markers are present (the
    # check above relies on them existing — explicit assert makes the
    # failure mode obvious if a future edit removes them).
    on_disk = DEFAULT_DOC_PATH.read_text(encoding="utf-8")
    assert CLASSES_BEGIN_MARKER in on_disk
    assert CLASSES_END_MARKER in on_disk
    assert PREDICATES_BEGIN_MARKER in on_disk
    assert PREDICATES_END_MARKER in on_disk
    assert on_disk == new_text


# --- idempotency --------------------------------------------------------


def test_generator_is_idempotent(tmp_path: Path) -> None:
    """Running the generator twice in a row produces the same output —
    no diff on the second run."""
    tmp_doc = tmp_path / "mapping.md"
    shutil.copy(DEFAULT_DOC_PATH, tmp_doc)

    text_after_first, _ = regenerate_mapping_tables(doc_path=tmp_doc)
    text_after_second, changed_second = regenerate_mapping_tables(doc_path=tmp_doc)

    assert text_after_first == text_after_second
    assert changed_second is False
