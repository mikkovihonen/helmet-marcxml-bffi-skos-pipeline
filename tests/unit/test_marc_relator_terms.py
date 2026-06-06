"""Unit tests for the Finnish / Swedish MARC ``$e`` → LoC relator URI
mapping table and the post-M3 enrichment pass.
"""

from __future__ import annotations

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDFS

from bffi_pipeline.marc_relator_terms import (
    RELATOR_TERM_TO_URI,
    lookup_relator_uri,
    normalise_relator_term,
)
from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m3.relator_term_enrichment import enrich_role_uris

LOC = "http://id.loc.gov/vocabulary/relators/"


def test_finnish_creator_terms_map_to_specific_codes() -> None:
    assert lookup_relator_uri("kirjoittaja") == LOC + "aut"
    assert lookup_relator_uri("säveltäjä") == LOC + "cmp"
    assert lookup_relator_uri("kääntäjä") == LOC + "trl"
    assert lookup_relator_uri("toimittaja") == LOC + "edt"
    assert lookup_relator_uri("kuvittaja") == LOC + "ill"
    assert lookup_relator_uri("näyttelijä") == LOC + "act"


def test_swedish_creator_terms_collapse_to_same_codes() -> None:
    assert lookup_relator_uri("författare") == LOC + "aut"
    assert lookup_relator_uri("översättare") == LOC + "trl"
    assert lookup_relator_uri("redaktör") == LOC + "edt"
    assert lookup_relator_uri("illustratör") == LOC + "ill"


def test_music_instrument_terms_all_map_to_performer() -> None:
    # Cataloguer's call: per-instrument distinctions live in the
    # free-text label; the URI says "this was a performer, not a
    # creator".
    for term in ("piano", "viulu", "sello", "kitara", "rummut", "saksofoni"):
        assert lookup_relator_uri(term) == LOC + "prf", term


def test_voice_terms_map_to_performer_or_singer() -> None:
    assert lookup_relator_uri("sopraano") == LOC + "prf"
    assert lookup_relator_uri("tenori") == LOC + "prf"
    assert lookup_relator_uri("laulaja") == LOC + "sng"
    assert lookup_relator_uri("laulu") == LOC + "sng"


def test_unknown_terms_return_none() -> None:
    # Ambiguous director / conductor / manager — intentionally
    # NOT in the map. The pipeline falls back to the existing
    # free-text role path on a miss.
    assert lookup_relator_uri("johtaja") is None
    assert lookup_relator_uri(None) is None
    assert lookup_relator_uri("") is None
    assert lookup_relator_uri("xx-no-such-role-xx") is None


def test_normalise_strips_trailing_punctuation_and_case() -> None:
    assert normalise_relator_term("Kirjoittaja,") == "kirjoittaja"
    assert normalise_relator_term("  Säveltäjä.  ") == "säveltäjä"


def test_normalised_form_matches_table() -> None:
    # MARC cataloguers frequently include a trailing comma /
    # period — the lookup must accept that form too.
    assert lookup_relator_uri("Kirjoittaja,") == LOC + "aut"
    assert lookup_relator_uri("SÄVELTÄJÄ.") == LOC + "cmp"


def test_table_covers_high_volume_terms_per_corpus_inventory() -> None:
    # Sanity check: a representative slice of the
    # 2026-06-07 corpus inventory's top entries must be present.
    # Catches accidental deletion / typo in the mapping table.
    high_volume = {
        "kirjoittaja",
        "säveltäjä",
        "kääntäjä",
        "esittäjä",
        "kuvittaja",
        "toimittaja",
        "näyttelijä",
        "laulaja",
        "kitara",
        "rummut",
        "piano",
        "viulu",
        "sello",
    }
    missing = high_volume - RELATOR_TERM_TO_URI.keys()
    assert not missing, f"missing high-volume terms: {missing}"


def test_enrichment_adds_uri_sibling_for_matched_label() -> None:
    g = Graph()
    work = URIRef("urn:w/1")
    contrib = URIRef("urn:c/1")
    role = BNode("r1")
    g.add((work, V.BFFI.contribution, contrib))
    g.add((contrib, V.BF.role, role))
    g.add((role, RDFS.label, Literal("kirjoittaja")))

    added = enrich_role_uris(g)

    assert added == 1
    # Original blank-node role still there with its label.
    assert (contrib, V.BF.role, role) in g
    assert (role, RDFS.label, Literal("kirjoittaja")) in g
    # New sibling triple bridges to the LoC URI.
    assert (contrib, V.BF.role, URIRef(LOC + "aut")) in g


def test_enrichment_skips_uri_roles_already_present() -> None:
    g = Graph()
    contrib = URIRef("urn:c/2")
    g.add((contrib, V.BF.role, URIRef(LOC + "trl")))

    added = enrich_role_uris(g)

    # URI roles are already authoritative — nothing to do.
    assert added == 0


def test_enrichment_skips_unknown_terms() -> None:
    g = Graph()
    contrib = URIRef("urn:c/3")
    role = BNode("r3")
    g.add((contrib, V.BF.role, role))
    # ``johtaja`` is intentionally not in the map (ambiguous).
    g.add((role, RDFS.label, Literal("johtaja")))

    added = enrich_role_uris(g)

    assert added == 0
    assert not any(o for o in g.objects(contrib, V.BF.role) if isinstance(o, URIRef))


def test_enrichment_is_idempotent_under_rerun() -> None:
    g = Graph()
    contrib = URIRef("urn:c/4")
    role = BNode("r4")
    g.add((contrib, V.BF.role, role))
    g.add((role, RDFS.label, Literal("piano")))

    first_pass = enrich_role_uris(g)
    second_pass = enrich_role_uris(g)

    assert first_pass == 1
    assert second_pass == 0  # nothing new to add
    # The URI sibling appears exactly once (rdflib set semantics).
    uri_roles = [o for o in g.objects(contrib, V.BF.role) if isinstance(o, URIRef)]
    assert uri_roles == [URIRef(LOC + "prf")]


def test_enrichment_handles_multiple_contribs_independently() -> None:
    g = Graph()
    c1, c2 = URIRef("urn:c/a"), URIRef("urn:c/b")
    r1, r2 = BNode("r-a"), BNode("r-b")
    g.add((c1, V.BF.role, r1))
    g.add((r1, RDFS.label, Literal("kirjoittaja")))
    g.add((c2, V.BF.role, r2))
    g.add((r2, RDFS.label, Literal("kääntäjä")))

    added = enrich_role_uris(g)

    assert added == 2
    assert (c1, V.BF.role, URIRef(LOC + "aut")) in g
    assert (c2, V.BF.role, URIRef(LOC + "trl")) in g
