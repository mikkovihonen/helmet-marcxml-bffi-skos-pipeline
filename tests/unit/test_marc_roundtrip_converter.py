"""Unit tests for the BFFI → MARCXML round-trip converter.

These pin the field-level reconstruction surface so cataloguers
diffing original vs reconstructed MARC see a stable, documented
shape. The integration test against the live canonical-skosified.ttl
runs in the cataloguer-bundle stage; this file exercises the converter
in isolation against synthetic graphs.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, RDF, SKOS

from bffi_pipeline.marc_roundtrip import (
    MARC_NAMESPACE,
    reconstruct_marc,
    serialize_marc,
)
from bffi_pipeline.marc_roundtrip.converter import ROUNDTRIP_MARKER
from bffi_pipeline.provenance import vocab as V

NS = {"m": MARC_NAMESPACE}

WORK = URIRef("urn:work/A")
EXPR = URIRef("urn:expr/A")
MANIF = URIRef("urn:manif/A")
AGENT = URIRef("urn:agent/krag")


def _build_minimal_graph() -> Graph:
    """A Work + Expression + Manifestation triplet covering the
    converter's main field-emit paths."""
    g = Graph()
    g.add((WORK, RDF.type, V.BFFI.Work))
    g.add((WORK, SKOS.prefLabel, Literal("AADA WILDE", lang="fi")))
    g.add((WORK, V.BFFI.hasExpression, EXPR))
    # PrimaryContribution chain
    contrib_pri = URIRef("urn:contrib/pri")
    g.add((WORK, V.BFFI.contribution, contrib_pri))
    g.add((contrib_pri, RDF.type, V.BFFI.PrimaryContribution))
    g.add((contrib_pri, V.BFFI.agent, AGENT))
    g.add((AGENT, V.RDFS.label, Literal("KRAG, THOMAS PETER")))

    # Expression
    g.add((EXPR, RDF.type, V.BFFI.Expression))
    g.add((EXPR, V.BFFI.expressionOf, WORK))
    g.add((EXPR, V.BFFI.language, URIRef("http://id.loc.gov/vocabulary/languages/fin")))

    # Non-primary contribution (translator)
    contrib_trl = URIRef("urn:contrib/trl")
    translator = URIRef("urn:agent/translator")
    g.add((EXPR, V.BFFI.contribution, contrib_trl))
    g.add((contrib_trl, V.BFFI.agent, translator))
    g.add((contrib_trl, V.BF.role, URIRef("http://id.loc.gov/vocabulary/relators/trl")))
    g.add((translator, V.RDFS.label, Literal("Adrian, Esa")))

    # Manifestation
    g.add((MANIF, RDF.type, V.BFFI.Manifestation))
    g.add((MANIF, V.BFFI.expressionManifested, EXPR))
    g.add((MANIF, SKOS.prefLabel, Literal("AADA WILDE (Helsinki : Otava, 1912)", lang="fi")))
    g.add((MANIF, DCTERMS.identifier, Literal("b13511105")))
    g.add((MANIF, V.BFFI.media, URIRef("http://id.loc.gov/vocabulary/mediaTypes/n")))
    g.add((MANIF, V.BFFI.carrier, URIRef("http://id.loc.gov/vocabulary/carriers/nc")))

    # ISBN
    isbn = URIRef("urn:isbn/n0")
    g.add((MANIF, V.BF.identifiedBy, isbn))
    g.add((isbn, RDF.type, V.BF.Isbn))
    g.add((isbn, RDF.value, Literal("9789511440932")))

    # MARC 007-derived format details
    g.add(
        (
            MANIF,
            V.BFFI.digitalCharacteristic,
            URIRef("http://id.loc.gov/vocabulary/mencformat/dvdv"),
        )
    )
    g.add(
        (
            MANIF,
            V.BFFI.soundCharacteristic,
            URIRef("http://id.loc.gov/vocabulary/mrecmedium/opt"),
        )
    )
    g.add(
        (
            MANIF,
            V.BFFI.colorContent,
            URIRef("http://id.loc.gov/vocabulary/mcolor/mul"),
        )
    )

    # Labels for the format URIs so the converter can resolve them
    for uri, label in [
        ("http://id.loc.gov/vocabulary/mediaTypes/n", "ilman välinettä"),
        ("http://id.loc.gov/vocabulary/carriers/nc", "nidos"),
        ("http://id.loc.gov/vocabulary/mencformat/dvdv", "DVD video"),
        ("http://id.loc.gov/vocabulary/mrecmedium/opt", "optical"),
        ("http://id.loc.gov/vocabulary/mcolor/mul", "multicolored"),
    ]:
        lang = "fi" if label in {"ilman välinettä", "nidos"} else "en"
        g.add((URIRef(uri), SKOS.prefLabel, Literal(label, lang=lang)))

    # Subjects + genre/form
    subject_yso = URIRef("http://www.yso.fi/onto/yso/p1234")
    g.add((WORK, V.BFFI.subject, subject_yso))
    g.add((subject_yso, V.RDFS.label, Literal("sodat")))
    g.add((subject_yso, V.BF.source, Literal("yso/fin")))

    genre_slm = URIRef("http://urn.fi/URN:NBN:fi:au:slm:s1044")
    g.add((WORK, V.BFFI.genreForm, genre_slm))
    g.add((genre_slm, V.RDFS.label, Literal("käännökset")))
    g.add((genre_slm, V.BF.source, Literal("slm/fin")))

    return g


@pytest.fixture
def minimal_record() -> ET.Element:
    g = _build_minimal_graph()
    rec = reconstruct_marc(g, MANIF)
    return rec.element


# --- Top-level shape ----------------------------------------------------


def test_reconstruct_emits_marcxml_record_element() -> None:
    g = _build_minimal_graph()
    rec = reconstruct_marc(g, MANIF)
    assert rec.element.tag == f"{{{MARC_NAMESPACE}}}record"
    assert rec.bib_id == "b13511105"


def test_serialize_emits_utf8_marcxml_bytes() -> None:
    g = _build_minimal_graph()
    rec = reconstruct_marc(g, MANIF)
    blob = serialize_marc(rec)
    assert blob.startswith(b"<?xml")
    assert b"AADA WILDE" in blob
    assert "ilman välinettä".encode() in blob


# --- Field-level pins ---------------------------------------------------


def _find(elem: ET.Element, xpath: str) -> ET.Element | None:
    return elem.find(xpath, NS)


def _findall(elem: ET.Element, xpath: str) -> list[ET.Element]:
    return elem.findall(xpath, NS)


def _datafield(rec: ET.Element, tag: str) -> ET.Element | None:
    return rec.find(f"m:datafield[@tag='{tag}']", NS)


def _datafields(rec: ET.Element, tag: str) -> list[ET.Element]:
    return rec.findall(f"m:datafield[@tag='{tag}']", NS)


def _subfield(df: ET.Element, code: str) -> str | None:
    sf = df.find(f"m:subfield[@code='{code}']", NS)
    return sf.text if sf is not None else None


def test_leader_is_present(minimal_record: ET.Element) -> None:
    leader = _find(minimal_record, "m:leader")
    assert leader is not None
    assert leader.text == "00000nam  2200000   4500"


def test_controlfields_001_003_carry_bib_id_and_helmet(minimal_record: ET.Element) -> None:
    cf001 = _find(minimal_record, "m:controlfield[@tag='001']")
    cf003 = _find(minimal_record, "m:controlfield[@tag='003']")
    assert cf001 is not None and cf001.text == "b13511105"
    assert cf003 is not None and cf003.text == "FI-HELME"


def test_008_carries_primary_language_at_pos_35_37(minimal_record: ET.Element) -> None:
    cf008 = _find(minimal_record, "m:controlfield[@tag='008']")
    assert cf008 is not None and cf008.text is not None
    assert cf008.text[35:38] == "fin"


def test_020_emits_isbn(minimal_record: ET.Element) -> None:
    df = _datafield(minimal_record, "020")
    assert df is not None
    assert _subfield(df, "a") == "9789511440932"


def test_041_carries_primary_language_code(minimal_record: ET.Element) -> None:
    df = _datafield(minimal_record, "041")
    assert df is not None
    assert _subfield(df, "a") == "fin"


def test_100_emits_primary_creator_label(minimal_record: ET.Element) -> None:
    df = _datafield(minimal_record, "100")
    assert df is not None
    assert df.attrib["ind1"] == "1"
    assert _subfield(df, "a") == "KRAG, THOMAS PETER"


def test_245_emits_work_pref_label_as_title(minimal_record: ET.Element) -> None:
    df = _datafield(minimal_record, "245")
    assert df is not None
    assert df.attrib["ind1"] == "1"
    assert df.attrib["ind2"] == "0"
    assert _subfield(df, "a") == "AADA WILDE"


def test_260_parses_publication_statement_from_manifestation_label(
    minimal_record: ET.Element,
) -> None:
    df = _datafield(minimal_record, "260")
    assert df is not None
    assert _subfield(df, "c") == "Helsinki : Otava, 1912"


def test_337_338_emit_media_and_carrier_with_finnish_labels(
    minimal_record: ET.Element,
) -> None:
    df_337 = _datafield(minimal_record, "337")
    df_338 = _datafield(minimal_record, "338")
    assert df_337 is not None
    assert _subfield(df_337, "a") == "ilman välinettä"
    assert _subfield(df_337, "b") == "n"
    assert _subfield(df_337, "2") == "rdamedia"
    assert df_338 is not None
    assert _subfield(df_338, "a") == "nidos"
    assert _subfield(df_338, "b") == "nc"
    assert _subfield(df_338, "2") == "rdacarrier"


def test_344_345_346_347_emit_marc007_format_facets(minimal_record: ET.Element) -> None:
    df_344 = _datafield(minimal_record, "344")
    df_346 = _datafield(minimal_record, "346")
    df_347 = _datafield(minimal_record, "347")
    # mrecmedium → 344 $h
    assert df_344 is not None
    assert _subfield(df_344, "h") == "optical"
    # mcolor → 346 $a
    assert df_346 is not None
    assert _subfield(df_346, "a") == "multicolored"
    # mencformat → 347 $b
    assert df_347 is not None
    assert _subfield(df_347, "b") == "DVD video"


def test_650_emits_yso_subject_with_authority_uri(minimal_record: ET.Element) -> None:
    df = _datafield(minimal_record, "650")
    assert df is not None
    assert df.attrib["ind2"] == "7"
    assert _subfield(df, "a") == "sodat"
    assert _subfield(df, "2") == "yso/fin"
    assert _subfield(df, "0") == "http://www.yso.fi/onto/yso/p1234"


def test_raw_bib_subject_with_skos_exactmatch_yields_only_authority_row() -> None:
    """When M9 binds a raw M3-minted subject URI to an authority
    (emitting ``<raw> skos:exactMatch <auth>`` per P-47), the
    converter follows the link: the raw URI's row is suppressed,
    the authority URI emits its own row with ``$0`` = the authority,
    and no pipeline-internal ``urn.fi/.../bib:raw/.../#Topic650-N`` URI
    appears in MARC output."""
    g = Graph()
    work = URIRef("urn:work/X")
    expr = URIRef("urn:expr/X")
    manif = URIRef("urn:manif/X")
    raw_sub = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/bX#Topic650-22")
    yso_sub = URIRef("http://www.yso.fi/onto/yso/p11180")
    g.add((work, RDF.type, V.BFFI.Work))
    g.add((work, SKOS.prefLabel, Literal("X", lang="fi")))
    g.add((work, V.BFFI.hasExpression, expr))
    g.add((expr, RDF.type, V.BFFI.Expression))
    g.add((expr, V.BFFI.expressionOf, work))
    g.add((manif, RDF.type, V.BFFI.Manifestation))
    g.add((manif, V.BFFI.expressionManifested, expr))
    g.add((manif, DCTERMS.identifier, Literal("bX")))
    # M9-bound: both raw + authority on the Work, with skos:exactMatch
    g.add((work, V.BFFI.subject, raw_sub))
    g.add((work, V.BFFI.subject, yso_sub))
    g.add((raw_sub, V.RDFS.label, Literal("norjankielinen kirjallisuus", lang="fi")))
    g.add((raw_sub, V.SKOS.exactMatch, yso_sub))
    g.add((yso_sub, SKOS.prefLabel, Literal("norjankielinen kirjallisuus", lang="fi")))

    rec = reconstruct_marc(g, manif)
    rows = [df for df in rec.element.findall("m:datafield[@tag='650']", NS)]
    assert len(rows) == 1
    [df] = rows
    assert _subfield(df, "0") == str(yso_sub)
    # No raw bib URI appears anywhere on the row.
    for sf in df.findall("m:subfield", NS):
        assert "bib:raw/" not in (sf.text or "")


def test_raw_bib_subject_without_redirect_dedups_via_label_match() -> None:
    """For canonical graphs produced BEFORE the M9 skos:exactMatch
    emission landed: the raw URI lacks the redirect, but a sibling
    authority URI on the same Work shares the same label. The
    converter still de-dupes: emits the authority's row, suppresses
    the raw URI's row entirely."""
    g = Graph()
    work = URIRef("urn:work/Y")
    expr = URIRef("urn:expr/Y")
    manif = URIRef("urn:manif/Y")
    raw_sub = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/bY#Topic650-30")
    yso_sub = URIRef("http://www.yso.fi/onto/yso/p99999")
    g.add((work, RDF.type, V.BFFI.Work))
    g.add((work, SKOS.prefLabel, Literal("Y", lang="fi")))
    g.add((work, V.BFFI.hasExpression, expr))
    g.add((expr, RDF.type, V.BFFI.Expression))
    g.add((expr, V.BFFI.expressionOf, work))
    g.add((manif, RDF.type, V.BFFI.Manifestation))
    g.add((manif, V.BFFI.expressionManifested, expr))
    g.add((manif, DCTERMS.identifier, Literal("bY")))
    g.add((work, V.BFFI.subject, raw_sub))
    g.add((work, V.BFFI.subject, yso_sub))
    g.add((raw_sub, V.RDFS.label, Literal("sota", lang="fi")))
    # No skos:exactMatch — but the YSO label matches the raw's label.
    g.add((yso_sub, SKOS.prefLabel, Literal("sota", lang="fi")))

    rec = reconstruct_marc(g, manif)
    rows = [df for df in rec.element.findall("m:datafield[@tag='650']", NS)]
    assert len(rows) == 1
    [df] = rows
    assert _subfield(df, "0") == str(yso_sub)


def test_raw_bib_subject_without_authority_emits_with_no_dollar_zero() -> None:
    """When M9 found no authority for a raw URI, the round-trip emits
    the row with ``$a`` from rdfs:label but NO ``$0`` — the raw
    pipeline-internal URI is never the right thing to write into MARC
    ``$0``."""
    g = Graph()
    work = URIRef("urn:work/Z")
    expr = URIRef("urn:expr/Z")
    manif = URIRef("urn:manif/Z")
    raw_sub = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/bZ#Topic650-1")
    g.add((work, RDF.type, V.BFFI.Work))
    g.add((work, SKOS.prefLabel, Literal("Z", lang="fi")))
    g.add((work, V.BFFI.hasExpression, expr))
    g.add((expr, RDF.type, V.BFFI.Expression))
    g.add((expr, V.BFFI.expressionOf, work))
    g.add((manif, RDF.type, V.BFFI.Manifestation))
    g.add((manif, V.BFFI.expressionManifested, expr))
    g.add((manif, DCTERMS.identifier, Literal("bZ")))
    g.add((work, V.BFFI.subject, raw_sub))
    g.add((raw_sub, V.RDFS.label, Literal("oddball", lang="fi")))

    rec = reconstruct_marc(g, manif)
    [df] = rec.element.findall("m:datafield[@tag='650']", NS)
    assert _subfield(df, "a") == "oddball"
    assert _subfield(df, "0") is None


def test_655_emits_slm_genre_form(minimal_record: ET.Element) -> None:
    df = _datafield(minimal_record, "655")
    assert df is not None
    assert df.attrib["ind2"] == "7"
    assert _subfield(df, "a") == "käännökset"
    assert _subfield(df, "2") == "slm/fin"
    assert _subfield(df, "0") == "http://urn.fi/URN:NBN:fi:au:slm:s1044"


def test_700_emits_translator_added_entry(minimal_record: ET.Element) -> None:
    df = _datafield(minimal_record, "700")
    assert df is not None
    assert df.attrib["ind1"] == "1"
    assert _subfield(df, "a") == "Adrian, Esa"
    assert _subfield(df, "4") == "trl"


def test_700_emits_relator_term_e_from_uri_via_label_lookup(tmp_path) -> None:
    """When the bf:role is a LoC relator URI AND the graph carries
    that URI's skos:prefLabel (the relators vocab dump merged in by
    the runner), the converter resolves $e from the label."""
    g = _build_minimal_graph()
    # Add a Finnish label for the trl URI as the relators vocab dump
    # would in production. The converter's _loc_label finds it.
    g.add(
        (
            URIRef("http://id.loc.gov/vocabulary/relators/trl"),
            SKOS.prefLabel,
            Literal("Translator", lang="en"),
        )
    )
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='700']", NS)
    assert df is not None
    assert _subfield(df, "e") == "Translator"


def test_700_emits_relator_term_e_from_freetext_role_label(tmp_path) -> None:
    """When the bf:role is a blank node carrying rdfs:label (M3's
    cataloguer-typed free-text $e), the converter emits $e directly
    from that label."""
    g = Graph()
    # Build a minimal graph with a free-text role only
    work = URIRef("urn:work/W")
    expr = URIRef("urn:expr/W")
    manif = URIRef("urn:manif/W")
    # Real M3 emission for free-text $e is a blank node — match that.
    role_node = BNode()
    g.add((work, RDF.type, V.BFFI.Work))
    g.add((work, SKOS.prefLabel, Literal("X", lang="fi")))
    g.add((work, V.BFFI.hasExpression, expr))
    g.add((expr, RDF.type, V.BFFI.Expression))
    g.add((expr, V.BFFI.expressionOf, work))
    g.add((manif, RDF.type, V.BFFI.Manifestation))
    g.add((manif, V.BFFI.expressionManifested, expr))
    g.add((manif, DCTERMS.identifier, Literal("bX")))
    # Non-primary contribution with a free-text Finnish role label
    contrib = URIRef("urn:contrib/c1")
    agent = URIRef("urn:agent/a1")
    g.add((expr, V.BFFI.contribution, contrib))
    g.add((contrib, V.BFFI.agent, agent))
    g.add((agent, V.RDFS.label, Literal("Adrian, Esa")))
    g.add((contrib, V.BF.role, role_node))
    g.add((role_node, V.RDFS.label, Literal("kääntäjä", lang="fi")))

    rec = reconstruct_marc(g, manif)
    df = rec.element.find("m:datafield[@tag='700']", NS)
    assert df is not None
    assert _subfield(df, "a") == "Adrian, Esa"
    assert _subfield(df, "e") == "kääntäjä"
    # No $4 — the free-text path doesn't have a relator code.
    assert _subfield(df, "4") is None


def test_907_emits_helmet_bib_id_in_sierra_display_form(minimal_record: ET.Element) -> None:
    df = _datafield(minimal_record, "907")
    assert df is not None
    assert _subfield(df, "a") == ".b13511105"


def test_every_datafield_carries_roundtrip_marker(minimal_record: ET.Element) -> None:
    """Every reconstructed datafield except the 040 synth-marker row
    itself carries ``$5 FI-HELME/bffi-roundtrip`` so cataloguers can
    spot which rows came from the round-trip vs the original MARC."""
    for df in minimal_record.findall("m:datafield", NS):
        if df.attrib["tag"] == "040":
            continue  # the 040 IS the marker row
        markers = [sf for sf in df.findall("m:subfield", NS) if sf.attrib.get("code") == "5"]
        assert any(sf.text == ROUNDTRIP_MARKER for sf in markers), df.attrib["tag"]


def test_245_emits_responsibility_statement_in_c_subfield() -> None:
    """P-47 commit 9: M3 lifts ``bf:responsibilityStatement`` from
    bf:Instance to Manifestation as ``bffi:responsibilityStatement``;
    the converter emits it as 245 $c."""
    g = _build_minimal_graph()
    g.add((MANIF, V.BFFI.responsibilityStatement, Literal("THOMAS PETER KRAG")))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='245']", NS)
    assert df is not None
    assert _subfield(df, "c") == "THOMAS PETER KRAG"


def test_260_prefers_publication_statement_literal_over_label_parse() -> None:
    """When ``bffi:publicationStatement`` is present, 260 $c uses it
    directly instead of parsing the Manifestation prefLabel suffix."""
    g = _build_minimal_graph()
    g.add((MANIF, V.BFFI.publicationStatement, Literal("Helsinki : Otava, 1912")))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='260']", NS)
    assert df is not None
    assert _subfield(df, "c") == "Helsinki : Otava, 1912"


def test_300_emits_extent_and_dimensions_when_both_present() -> None:
    """P-47: 300 $a from ``bffi:extent`` literal, $c from
    ``bffi:dimensions`` literal — Manifestation-side."""
    g = _build_minimal_graph()
    g.add((MANIF, V.BFFI.extent, Literal("256 sivua")))
    g.add((MANIF, V.BFFI.dimensions, Literal("21 cm")))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='300']", NS)
    assert df is not None
    assert _subfield(df, "a") == "256 sivua"
    assert _subfield(df, "c") == "21 cm"


def test_500_emits_note_from_bffi_note_blank_node_with_rdf_value() -> None:
    """P-47: M3 emits ``bffi:note`` on Expression with the note text
    as ``rdf:value`` on a ``bf:Note`` blank node. The converter walks
    that and emits 500 $a."""
    g = _build_minimal_graph()
    note_node = BNode()
    g.add((EXPR, V.BFFI.note, note_node))
    g.add((note_node, V.RDF.value, Literal("Translated from Norwegian.")))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='500']", NS)
    assert df is not None
    assert _subfield(df, "a") == "Translated from Norwegian."


def test_336_emits_content_type_when_bffi_content_uri_present() -> None:
    """P-47: ``bffi:content`` on Expression → 336 $a label $b code
    $2 rdacontent. URI resolves to a label via the merged LoC
    contentTypes vocab when the runner loads it."""
    g = _build_minimal_graph()
    content_uri = URIRef("http://id.loc.gov/vocabulary/contentTypes/txt")
    g.add((EXPR, V.BFFI.content, content_uri))
    g.add((content_uri, SKOS.prefLabel, Literal("teksti", lang="fi")))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='336']", NS)
    assert df is not None
    assert _subfield(df, "a") == "teksti"
    assert _subfield(df, "b") == "txt"
    assert _subfield(df, "2") == "rdacontent"
    assert "336" not in rec.skipped_tags


def test_651_emits_geographic_subject_from_yso_paikat_uri() -> None:
    """6XX routing: yso-paikat URI → 651 geographic subject."""
    g = _build_minimal_graph()
    paikka = URIRef("http://www.yso.fi/onto/yso-paikat/p105076")
    g.add((WORK, V.BFFI.subject, paikka))
    g.add((paikka, V.RDFS.label, Literal("Tampere")))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='651']", NS)
    assert df is not None
    assert _subfield(df, "a") == "Tampere"


def test_600_emits_personal_subject_from_agent600_fragment() -> None:
    """6XX routing: marc2bibframe2 raw URI with ``#Agent600-N`` → 600."""
    g = _build_minimal_graph()
    agent_subj = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/bX#Agent600-7")
    g.add((WORK, V.BFFI.subject, agent_subj))
    g.add((agent_subj, V.RDFS.label, Literal("Sibelius, Jean")))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='600']", NS)
    assert df is not None
    assert _subfield(df, "a") == "Sibelius, Jean"


def test_710_emits_corporate_added_entry_from_agent710_fragment() -> None:
    """7XX routing: agent URI with ``#Agent710-N`` → 710 corporate
    added entry (not 700)."""
    g = _build_minimal_graph()
    contrib = URIRef("urn:contrib/corp")
    agent = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/bX#Agent710-3")
    g.add((EXPR, V.BFFI.contribution, contrib))
    g.add((contrib, V.BFFI.agent, agent))
    g.add((agent, V.RDFS.label, Literal("Suomen kirjailijaliitto")))
    rec = reconstruct_marc(g, MANIF)
    df_710 = rec.element.find("m:datafield[@tag='710']", NS)
    assert df_710 is not None
    assert _subfield(df_710, "a") == "Suomen kirjailijaliitto"


def test_skipped_tags_lists_852_and_336(minimal_record: ET.Element) -> None:
    # Smoke: we explicitly skip 852 (holdings) and 336 (content type
    # — not currently forwarded onto BFFI). Pin so adding either to
    # the converter later updates this test deliberately.
    g = _build_minimal_graph()
    rec = reconstruct_marc(g, MANIF)
    assert "852" in rec.skipped_tags
    assert "336" in rec.skipped_tags
