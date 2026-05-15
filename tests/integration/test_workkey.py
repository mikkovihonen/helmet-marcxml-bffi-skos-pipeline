"""End-to-end test for stage M4 against the synthetic corpus.

Runs M2 + M3, then loads the combined BFFI + BIBFRAME graph and verifies
that ``compute_blocks`` produces the right per-record keys and that the
extracted creator / title / content-type per Work matches expectations.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rdflib import RDF, RDFS, BNode

from bffi_pipeline.blocking import compute_blocking_key
from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m2 import run as run_m2
from bffi_pipeline.stages.m3 import run as run_m3
from bffi_pipeline.stages.workkey import (
    compute_blocks,
    extract_blocking_inputs,
    load_corpus,
)
from bffi_pipeline.uris import mint_raw_work_uri

FIXTURES = Path(__file__).resolve().parents[1] / "data" / "sample-marcxml"

_EXPECTED_KEYS = {
    "10000001": "tolstoy|sota|txt",
    "10000002": "linna|tuntematon|txt",
    "10000003": "lindgren|pippi|txt",
    "10000004": "sibelius|finlandia|ntm",
    "10000005": "oksanen|puhdistus|txt",
    "10000006": "helsingin|tieteessä|txt",
}


@pytest.fixture(scope="module")
def corpus_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("m4-out")
    run_m2(FIXTURES, output_dir=out)
    run_m3(output_dir=out)
    return out


def test_extracts_creator_title_and_content_for_each_work(corpus_path: Path) -> None:
    g = load_corpus(corpus_path)
    by_work = {entry.work_uri: entry for entry in extract_blocking_inputs(g)}
    for bib_id, expected_key in _EXPECTED_KEYS.items():
        work_uri = mint_raw_work_uri(f"http://urn.fi/URN:NBN:fi:bib:raw/{bib_id}#Work")
        entry = by_work[work_uri]
        assert entry.creator, f"missing creator for {bib_id}"
        assert entry.title, f"missing title for {bib_id}"
        assert entry.content_type, f"missing content type for {bib_id}"
        actual_key = compute_blocking_key(
            {
                "creator": entry.creator,
                "title": entry.title,
                "content_type": entry.content_type,
            }
        )
        assert actual_key == expected_key, f"{bib_id}: got {actual_key}"


def test_compute_blocks_groups_synthetic_corpus_into_singletons(corpus_path: Path) -> None:
    """Each synthetic record is a distinct Work; expect six singleton blocks."""
    stats = compute_blocks(load_corpus(corpus_path))
    assert stats.total_works == len(_EXPECTED_KEYS)
    assert stats.block_count == len(_EXPECTED_KEYS)
    assert stats.size_distribution == {1: len(_EXPECTED_KEYS)}


def test_load_corpus_accepts_single_ttl_file(corpus_path: Path) -> None:
    """Standalone .ttl path: M4 loads creator + title even without the
    bibframe sibling directory.

    Before the M3 SPARQL CONSTRUCT routed ``?primaryAgent rdfs:label
    ?primaryAgentLabel``, the agent URI was a dangling reference in
    the BFFI .ttl and the label only existed in the bibframe RDF/XML.
    Loading a single .ttl in isolation therefore returned ``creator =
    None``. The fix wires the label into the BFFI itself (so M9 has
    something to walk on the canonical graph), and as a side benefit
    standalone-.ttl loaders also see the creator.
    """
    one_ttl = corpus_path / "bffi" / "10000001.ttl"
    g = load_corpus(one_ttl)
    entries = list(extract_blocking_inputs(g))
    assert len(entries) == 1
    assert entries[0].title == "Sota ja rauha"
    assert entries[0].creator == "Tolstoy, Leo, 1828-1910"


def test_translator_e_role_round_trips_through_m2_m3(corpus_path: Path) -> None:
    """P-36 Phase A: a MARC 700 with ``$e kääntäjä.`` (Finnish translator
    role) round-trips through M2 → M3 to a ``bffi:Contribution`` carrying
    ``bf:role [a bf:Role ; rdfs:label "..."]``.

    Fixture ``10000001.xml`` ("Sota ja rauha", Tolstoy) has
    ``700 1   $a Adrian, Esa, $e kääntäjä.`` — the translator who rendered
    War and Peace into Finnish. marc2bibframe2 emits this as a blank-node
    ``bf:Role`` with the cataloguer's text on ``rdfs:label``; the M3
    SPARQL CONSTRUCT (post-Phase-A) routes both the role node and its
    label onto the bffi:Contribution.

    Pre-Phase-A this propagation happened in
    ``bf_to_bffi._propagate_non_primary_roles`` (now deleted). The
    assertion shape is unchanged.
    """
    g = load_corpus(corpus_path / "bffi" / "10000001.ttl")
    contribs = list(g.subjects(RDF.type, V.BFFI.Contribution))
    assert contribs, "expected at least one bffi:Contribution for 10000001"
    role_labels: set[str] = set()
    for contrib in contribs:
        for role in g.objects(contrib, V.BF.role):
            if not isinstance(role, BNode):
                continue
            assert (role, RDF.type, V.BF.Role) in g, (
                f"role blank node {role} missing a bf:Role typing"
            )
            for lab in g.objects(role, RDFS.label):
                role_labels.add(str(lab))
    # marc2bibframe2 carries the trailing period through; that's the
    # cataloguer's text as recorded.
    assert "kääntäjä." in role_labels or "kääntäjä" in role_labels, (
        f"expected a Finnish translator role label; got {role_labels}"
    )
