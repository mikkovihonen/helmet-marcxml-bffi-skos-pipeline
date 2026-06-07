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
    g.add((subject_yso, V.BF.source, Literal("yso")))

    genre_slm = URIRef("http://urn.fi/URN:NBN:fi:au:slm:s1044")
    g.add((WORK, V.BFFI.genreForm, genre_slm))
    g.add((genre_slm, V.RDFS.label, Literal("käännökset")))
    g.add((genre_slm, V.BF.source, Literal("slm")))

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
    # Default: language material (LDR/06 'a'), monograph ('m'),
    # full-level encoding (LDR/17 blank). 24 chars total.
    assert leader.text == "00000nam  2200000   4500"
    assert len(leader.text) == 24


def test_leader_pos06_reflects_work_type_music_audio() -> None:
    """LDR/06 = 'j' when the canonical Work carries ``bf:MusicAudio``
    rdf:type — the music-recording case (b1411382x source pattern)."""
    g = _build_minimal_graph()
    g.add((WORK, RDF.type, V.BF.MusicAudio))
    rec = reconstruct_marc(g, WORK)  # WORK works too since manif resolves
    rec = reconstruct_marc(g, MANIF)
    leader = rec.element.find("m:leader", NS)
    assert leader is not None and leader.text is not None
    assert leader.text[6] == "j"
    assert len(leader.text) == 24


def test_leader_pos06_picks_most_specific_type() -> None:
    """When a Work has both ``bf:MusicAudio`` and ``bf:Audio``, the
    more-specific ``MusicAudio`` ('j' musical sound recording) wins
    over generic ``Audio`` ('i' nonmusical)."""
    g = _build_minimal_graph()
    g.add((WORK, RDF.type, V.BF.Audio))
    g.add((WORK, RDF.type, V.BF.MusicAudio))
    rec = reconstruct_marc(g, MANIF)
    leader = rec.element.find("m:leader", NS)
    assert leader is not None and leader.text is not None
    assert leader.text[6] == "j"


def test_leader_pos17_reflects_encoding_level_from_admin_metadata() -> None:
    """LDR/17 = source encoding-level character, from
    ``bffi:encodingLevel`` URI on the AdminMetadata block pointing at
    ``id.loc.gov/vocabulary/menclvl/<n>``."""
    g = _build_minimal_graph()
    admin = BNode()
    g.add((MANIF, V.BFFI.adminMetadata, admin))
    g.add(
        (
            admin,
            V.BFFI.encodingLevel,
            URIRef("http://id.loc.gov/vocabulary/menclvl/7"),
        )
    )
    rec = reconstruct_marc(g, MANIF)
    leader = rec.element.find("m:leader", NS)
    assert leader is not None and leader.text is not None
    assert leader.text[17] == "7"
    assert len(leader.text) == 24


def test_leader_pos17_ignores_pipeline_internal_enc_level_marker() -> None:
    """Our pipeline's own ``bib:enc-level/auto`` marker (a
    ``bffi:EncodingLevel`` URI outside the LoC menclvl vocab) must
    NOT land in LDR/17 — only the LoC ``menclvl/<n>`` URI is a valid
    MARC encoding-level source. Falls back to blank."""
    g = _build_minimal_graph()
    admin = BNode()
    g.add((MANIF, V.BFFI.adminMetadata, admin))
    g.add(
        (
            admin,
            V.BFFI.encodingLevel,
            URIRef("http://urn.fi/URN:NBN:fi:bib:enc-level/auto"),
        )
    )
    rec = reconstruct_marc(g, MANIF)
    leader = rec.element.find("m:leader", NS)
    assert leader is not None and leader.text is not None
    assert leader.text[17] == " "


def test_controlfields_001_003_carry_bib_id_and_helmet(minimal_record: ET.Element) -> None:
    cf001 = _find(minimal_record, "m:controlfield[@tag='001']")
    cf003 = _find(minimal_record, "m:controlfield[@tag='003']")
    assert cf001 is not None and cf001.text == "b13511105"
    assert cf003 is not None and cf003.text == "FI-HELME"


def test_008_carries_primary_language_at_pos_35_37(minimal_record: ET.Element) -> None:
    cf008 = _find(minimal_record, "m:controlfield[@tag='008']")
    assert cf008 is not None and cf008.text is not None
    assert cf008.text[35:38] == "fin"


def test_008_carries_transaction_date_at_pos_00_05() -> None:
    """Source MARC 005 transaction date (mirrored by M3 onto the
    AdminMetadata block as ``bffi:changeDate``, the canonical lkd.rdf
    name) lands at MARC 008 positions 00-05 in YYMMDD form. ISO
    datetime is parsed character-by-character — no timezone
    gymnastics."""
    g = _build_minimal_graph()
    admin = BNode()
    g.add((MANIF, V.BFFI.adminMetadata, admin))
    g.add((admin, V.BFFI.changeDate, Literal("2026-05-12T12:25:26")))
    rec = reconstruct_marc(g, MANIF)
    cf = rec.element.find("m:controlfield[@tag='008']", NS)
    assert cf is not None and cf.text is not None
    assert cf.text[0:6] == "260512"


def test_008_carries_publication_year_at_pos_07_10() -> None:
    """Date1 (008 pos 07-10) reads from
    ``bffi:provisionActivity → bf:date`` (typed) preferentially,
    fallback to digits in ``bflc:simpleDate`` (\"c1997\" → \"1997\")."""
    g = _build_minimal_graph()
    prov = BNode()
    g.add((MANIF, V.BFFI.provisionActivity, prov))
    g.add((prov, V.BFLC.simpleDate, Literal("c1997")))
    rec = reconstruct_marc(g, MANIF)
    cf = rec.element.find("m:controlfield[@tag='008']", NS)
    assert cf is not None and cf.text is not None
    assert cf.text[7:11] == "1997"


def test_008_carries_country_code_at_pos_15_17() -> None:
    """008 pos 15-17 = 3-char MARC country code, derived from the
    ``bffi:provisionActivity → bf:place`` URI tail
    (``…/countries/xxk`` → ``"xxk"``). Shorter codes are
    space-padded right (``"fi"`` → ``"fi "``)."""
    g = _build_minimal_graph()
    prov = BNode()
    g.add((MANIF, V.BFFI.provisionActivity, prov))
    g.add(
        (
            prov,
            V.BF.place,
            URIRef("http://id.loc.gov/vocabulary/countries/xxk"),
        )
    )
    rec = reconstruct_marc(g, MANIF)
    cf = rec.element.find("m:controlfield[@tag='008']", NS)
    assert cf is not None and cf.text is not None
    assert cf.text[15:18] == "xxk"


def test_008_pads_short_country_code_with_spaces() -> None:
    """``…/countries/fi`` → ``"fi "`` (right-pad to 3 chars)."""
    g = _build_minimal_graph()
    prov = BNode()
    g.add((MANIF, V.BFFI.provisionActivity, prov))
    g.add((prov, V.BF.place, URIRef("http://id.loc.gov/vocabulary/countries/fi")))
    rec = reconstruct_marc(g, MANIF)
    cf = rec.element.find("m:controlfield[@tag='008']", NS)
    assert cf is not None and cf.text is not None
    assert cf.text[15:18] == "fi "


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


def _all_subfield_values(df: ET.Element, code: str) -> list[str]:
    return [sf.text or "" for sf in df.findall("m:subfield", NS) if sf.attrib.get("code") == code]


def test_100_does_not_carry_marckey_bypass_sentinel_for_label_only_emission() -> None:
    """P-49 Phase A: when the converter builds 100 $a from
    ``rdfs:label`` (no ``bflc:marcKey`` present on the agent), the
    recon row should NOT carry the ``$9 marckey-bypass`` sentinel —
    the row is honestly built from structured BFFI data."""
    df = _datafield(_minimal_record_re_render(), "100")
    assert df is not None
    assert "marckey-bypass" not in _all_subfield_values(df, "9")


def test_100_emits_marckey_bypass_sentinel_when_built_from_marc_key() -> None:
    """P-49 Phase A: when the agent carries ``bflc:marcKey`` (so the
    converter pulls $a / $c / $d from it instead of from
    ``rdfs:label``), the recon row gets the ``$9 marckey-bypass``
    sentinel. The cataloguer-review diff classifies the row as
    ``marckey-bypass`` regardless of byte-match."""
    g = _build_minimal_graph()
    g.add(
        (
            AGENT,
            V.BFLC.marcKey,
            Literal("1001 $aKRAG, THOMAS PETER,$d1868-1913,$ekirjoittaja"),
        )
    )
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='100']", NS)
    assert df is not None
    # The marcKey carried $d 1868-1913 — recon should now emit $d
    # (with the ISBD trailing comma preserved verbatim from marcKey).
    assert _subfield(df, "d") == "1868-1913,"
    # Sentinel present.
    assert "marckey-bypass" in _all_subfield_values(df, "9")


def _minimal_record_re_render() -> ET.Element:
    """Per-test minimal record (rebuilt to avoid fixture sharing)."""
    g = _build_minimal_graph()
    return reconstruct_marc(g, MANIF).element


def test_245_emits_work_pref_label_as_title(minimal_record: ET.Element) -> None:
    df = _datafield(minimal_record, "245")
    assert df is not None
    assert df.attrib["ind1"] == "1"
    assert df.attrib["ind2"] == "0"
    assert _subfield(df, "a") == "AADA WILDE"


def test_264_parses_publication_statement_from_manifestation_label(
    minimal_record: ET.Element,
) -> None:
    """MARC 264 (RDA replacement for 260): when no structured
    ``bffi:provisionActivity`` bnode is present, fall back to
    parsing the Manifestation's prefLabel suffix for the publication
    statement. Emits a single 264 $c with ind2=1 (Publication)."""
    df = _datafield(minimal_record, "264")
    assert df is not None
    assert df.attrib["ind2"] == "1"
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
    assert _subfield(df, "2") == "yso"
    assert _subfield(df, "0") == "http://www.yso.fi/onto/yso/p1234"


def test_650_emits_kauno_source_from_subject_uri_namespace() -> None:
    """``bf:source`` URI is lossy (``<…/subjectSchemes/kauno>``); the
    converter recovers the cataloguer-typed ``$2 kauno`` from the
    subject URI's own namespace (``http://www.yso.fi/onto/kauno/…``).
    Language suffix dropped per modern Finto / RDA convention."""
    g = _build_minimal_graph()
    kauno_uri = URIRef("http://www.yso.fi/onto/kauno/p4013")
    g.add((WORK, V.BFFI.subject, kauno_uri))
    g.add((kauno_uri, V.RDFS.label, Literal("avaruus")))
    # bf:source is the URI marc2bibframe2 emits (without /fin).
    g.add(
        (
            kauno_uri,
            V.BF.source,
            URIRef("http://id.loc.gov/vocabulary/subjectSchemes/kauno"),
        )
    )
    rec = reconstruct_marc(g, MANIF)
    df = next(
        df
        for df in rec.element.findall("m:datafield[@tag='650']", NS)
        if _subfield(df, "0") == str(kauno_uri)
    )
    assert _subfield(df, "2") == "kauno"


def test_650_emits_yso_paikat_source_for_geographic_subject() -> None:
    """yso-paikat namespace → ``yso`` source code (cataloguer
    convention: $2 yso even for geographic subjects in YSO-paikat,
    language suffix dropped)."""
    g = _build_minimal_graph()
    paikat = URIRef("http://www.yso.fi/onto/yso-paikat/p105076")
    g.add((WORK, V.BFFI.subject, paikat))
    g.add((paikat, V.RDFS.label, Literal("Tampere")))
    rec = reconstruct_marc(g, MANIF)
    df = next(
        df
        for df in rec.element.findall("m:datafield[@tag='651']", NS)
        if _subfield(df, "0") == str(paikat)
    )
    assert _subfield(df, "2") == "yso"


def test_650_emits_bella_source_for_swedish_fiction_namespace() -> None:
    """bella (Swedish fiction-subject vocab) → bella (no /swe
    suffix; namespace already encodes the language)."""
    g = _build_minimal_graph()
    bella = URIRef("http://www.yso.fi/onto/bella/p1234")
    g.add((WORK, V.BFFI.subject, bella))
    g.add((bella, V.RDFS.label, Literal("rymden")))
    rec = reconstruct_marc(g, MANIF)
    df = next(
        df
        for df in rec.element.findall("m:datafield[@tag='650']", NS)
        if _subfield(df, "0") == str(bella)
    )
    assert _subfield(df, "2") == "bella"


def test_655_emits_slm_source_for_slm_genre_form() -> None:
    """SLM (Suomalainen lajityyppi- ja muotosanasto) URI →
    ``slm`` (modern Finto / RDA form; language suffix dropped)."""
    g = _build_minimal_graph()
    slm = URIRef("http://urn.fi/URN:NBN:fi:au:slm:s9999")
    g.add((WORK, V.BFFI.genreForm, slm))
    g.add((slm, V.RDFS.label, Literal("uusi laji")))
    rec = reconstruct_marc(g, MANIF)
    df = next(
        df
        for df in rec.element.findall("m:datafield[@tag='655']", NS)
        if _subfield(df, "0") == str(slm)
    )
    assert _subfield(df, "2") == "slm"


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
    assert _subfield(df, "2") == "slm"
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


def test_700_dedups_e_when_both_uri_and_bnode_label_present() -> None:
    """The post-M3 relator-term enrichment pass adds a sibling
    ``bf:role <relators/aut>`` next to the original blank-node-with-
    label. The converter must emit ``$e`` once, preferring the
    cataloguer's original term (the BNode label) over the LoC URI
    label."""
    g = Graph()
    work = URIRef("urn:work/dedup")
    expr = URIRef("urn:expr/dedup")
    manif = URIRef("urn:manif/dedup")
    g.add((work, RDF.type, V.BFFI.Work))
    g.add((work, SKOS.prefLabel, Literal("X", lang="fi")))
    g.add((work, V.BFFI.hasExpression, expr))
    g.add((expr, RDF.type, V.BFFI.Expression))
    g.add((expr, V.BFFI.expressionOf, work))
    g.add((manif, RDF.type, V.BFFI.Manifestation))
    g.add((manif, V.BFFI.expressionManifested, expr))
    g.add((manif, DCTERMS.identifier, Literal("bD")))

    contrib = URIRef("urn:contrib/dedup")
    agent = URIRef("urn:agent/dedup")
    role_bnode = BNode()
    role_uri = URIRef("http://id.loc.gov/vocabulary/relators/aut")
    g.add((expr, V.BFFI.contribution, contrib))
    g.add((contrib, V.BFFI.agent, agent))
    g.add((agent, V.RDFS.label, Literal("Adrian, Esa")))
    g.add((contrib, V.BF.role, role_bnode))
    g.add((role_bnode, V.RDFS.label, Literal("kirjoittaja", lang="fi")))
    g.add((contrib, V.BF.role, role_uri))
    # LoC label that the converter would otherwise pick as fallback
    g.add((role_uri, SKOS.prefLabel, Literal("tekijä", lang="fi")))

    rec = reconstruct_marc(g, manif)
    df = rec.element.find("m:datafield[@tag='700']", NS)
    assert df is not None
    # $4 comes from the URI tail.
    assert _subfield(df, "4") == "aut"
    # $e is the BNode label (cataloguer's original word) — NOT the
    # LoC vocab's @fi prefLabel "tekijä".
    e_values = [sf.text for sf in df.findall("m:subfield", NS) if sf.attrib.get("code") == "e"]
    assert e_values == ["kirjoittaja"]


def test_100_emits_e_and_4_from_primary_contribution_role() -> None:
    """MARC 100 ``$e`` (primary creator relator term) must be
    reconstructed on the round-trip when the BFFI graph carries a
    ``bf:role`` on the primary contribution — same shape as 700."""
    g = Graph()
    work = URIRef("urn:work/P")
    expr = URIRef("urn:expr/P")
    manif = URIRef("urn:manif/P")
    g.add((work, RDF.type, V.BFFI.Work))
    g.add((work, SKOS.prefLabel, Literal("X", lang="fi")))
    g.add((work, V.BFFI.hasExpression, expr))
    g.add((expr, RDF.type, V.BFFI.Expression))
    g.add((expr, V.BFFI.expressionOf, work))
    g.add((manif, RDF.type, V.BFFI.Manifestation))
    g.add((manif, V.BFFI.expressionManifested, expr))
    g.add((manif, DCTERMS.identifier, Literal("bP")))

    contrib = URIRef("urn:contrib/P")
    agent = URIRef("urn:agent/P")
    role_bnode = BNode()
    g.add((work, V.BFFI.contribution, contrib))
    g.add((contrib, RDF.type, V.BFFI.PrimaryContribution))
    g.add((contrib, V.BFFI.agent, agent))
    g.add((agent, V.RDFS.label, Literal("Krohn, Aino")))
    g.add((contrib, V.BF.role, role_bnode))
    g.add((role_bnode, V.RDFS.label, Literal("kirjoittaja", lang="fi")))
    g.add(
        (
            contrib,
            V.BF.role,
            URIRef("http://id.loc.gov/vocabulary/relators/aut"),
        )
    )

    rec = reconstruct_marc(g, manif)
    df = rec.element.find("m:datafield[@tag='100']", NS)
    assert df is not None
    assert _subfield(df, "a") == "Krohn, Aino"
    assert _subfield(df, "e") == "kirjoittaja"
    assert _subfield(df, "4") == "aut"


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


def test_245_splits_a_and_b_from_manifestation_structured_title() -> None:
    """marc2bibframe2 emits two parallel titles per record: a
    transcribed structured form (``bf:mainTitle`` + ``bf:subtitle``)
    on bf:Instance, and a flat concatenated form on bf:Work.
    M3 routes the Instance side onto the Manifestation as
    ``bffi:title → bffi:Title → bffi:mainTitle`` / ``bffi:subtitle``.
    The converter must emit 245 $a from ``bffi:mainTitle`` and 245 $b
    from ``bffi:subtitle`` — without this, the Work's flat label gets
    concatenated into a single $a (the b1095840x regression)."""
    g = _build_minimal_graph()
    title_node = URIRef("http://urn.fi/URN:NBN:fi:bib:manifestation:abc#title")
    g.add((MANIF, V.BFFI.title, title_node))
    g.add((title_node, RDF.type, V.BFFI.Title))
    g.add((title_node, V.BFFI.mainTitle, Literal("Svenskt konstnärslexikon")))
    g.add(
        (
            title_node,
            V.BFFI.subtitle,
            Literal("tiotusen svenska konstnärers liv och verk. IV : Lundgren-Sallberg"),
        )
    )
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='245']", NS)
    assert df is not None
    assert _subfield(df, "a") == "Svenskt konstnärslexikon"
    assert _subfield(df, "b") == "tiotusen svenska konstnärers liv och verk. IV : Lundgren-Sallberg"


def test_245_omits_b_when_manifestation_title_has_no_subtitle() -> None:
    """Manifestation-side structured title with only ``bffi:mainTitle``
    (no ``bffi:subtitle``) emits 245 $a alone; no synthesised $b."""
    g = _build_minimal_graph()
    title_node = URIRef("http://urn.fi/URN:NBN:fi:bib:manifestation:def#title")
    g.add((MANIF, V.BFFI.title, title_node))
    g.add((title_node, RDF.type, V.BFFI.Title))
    g.add((title_node, V.BFFI.mainTitle, Literal("AADA WILDE")))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='245']", NS)
    assert df is not None
    assert _subfield(df, "a") == "AADA WILDE"
    assert _subfield(df, "b") is None


def test_245_falls_back_to_work_pref_label_when_no_manifestation_title() -> None:
    """Backwards compat: when M3 did not route a Manifestation-side
    ``bffi:title`` (older canonical graphs), the converter falls back
    to the Work's prefLabel for 245 $a — preserving the previous
    behaviour rather than emitting an empty 245."""
    g = _build_minimal_graph()
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='245']", NS)
    assert df is not None
    # _build_minimal_graph sets the Work prefLabel to "AADA WILDE";
    # see the canonical fixture in this file.
    assert _subfield(df, "a") == "AADA WILDE"


def test_240_emits_uniform_title_with_a_n_p_l_from_hub_marcKey() -> None:
    """MARC 240 uniform title: bf:Hub on the Expression via
    ``bffi:uniformTitleHub`` carries a ``bflc:marcKey`` with the
    combined 1XX + 240 subfields. Converter parses ``$t``/``$n``/
    ``$p``/``$l`` and maps to MARC 240 ``$a``/``$n``/``$p``/``$l``.
    The b26164413 Russian-translation case."""
    g = _build_minimal_graph()
    hub = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/b26164413#Hub240-14")
    g.add((EXPR, V.BFFI.title, hub))
    g.add((hub, RDF.type, V.BF.Hub))
    g.add(
        (
            hub,
            V.BFLC.marcKey,
            Literal(
                "1001 $aMorosinotto, Davide,$ekirjoittaja."
                "$tGrandissimi.$n2,$pLeonardo da Vincei, genio senza tempo.$lVenäjä"
            ),
        )
    )
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='240']", NS)
    assert df is not None
    assert df.attrib["ind1"] == "1"
    assert df.attrib["ind2"] == "0"
    assert _subfield(df, "a") == "Grandissimi."
    assert _subfield(df, "n") == "2,"
    assert _subfield(df, "p") == "Leonardo da Vincei, genio senza tempo."
    assert _subfield(df, "l") == "Venäjä"


def test_240_prefers_structured_part_number_and_part_name() -> None:
    """P-49 Layer 1: when the Hub's bf:Title carries structured
    ``bf:partNumber`` / ``bf:partName``, the converter sources $n
    and $p from them — NOT from marcKey. Only $a (from $t) and $l
    still come from marcKey, so the row remains ``marckey_bypass``
    but the structured side is honoured for the part subfields."""
    g = _build_minimal_graph()
    hub = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/b26164413#Hub240-14")
    g.add((EXPR, V.BFFI.title, hub))
    g.add((hub, RDF.type, V.BF.Hub))
    g.add(
        (
            hub,
            V.BFLC.marcKey,
            Literal(
                "1001 $aMorosinotto, Davide,$ekirjoittaja."
                "$tGrandissimi.$nDISCARDED,$pDISCARDED.$lVenäjä"
            ),
        )
    )
    # Structured: bf:Title with explicit bf:partNumber + bf:partName
    # (the marc2bibframe2 path that emits these alongside marcKey).
    title_node = BNode()
    g.add((hub, V.BF.title, title_node))
    g.add((title_node, RDF.type, V.BF.Title))
    g.add((title_node, V.BF.mainTitle, Literal("Grandissimi. 2, Leonardo da Vincei")))
    g.add((title_node, V.BF.partNumber, Literal("2,")))
    g.add((title_node, V.BF.partName, Literal("Leonardo da Vincei, genio senza tempo.")))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='240']", NS)
    assert df is not None
    # $n / $p sourced from structured BFFI predicates, NOT marcKey.
    assert _subfield(df, "n") == "2,"
    assert _subfield(df, "p") == "Leonardo da Vincei, genio senza tempo."
    # $a / $l still from marcKey (Layer 3 gap).
    assert _subfield(df, "a") == "Grandissimi."
    assert _subfield(df, "l") == "Venäjä"


def test_240_skipped_when_no_uniform_title_hub() -> None:
    """No 240 emitted when the Expression has no
    ``bffi:uniformTitleHub`` — silent skip, not a synth row."""
    g = _build_minimal_graph()
    rec = reconstruct_marc(g, MANIF)
    assert rec.element.find("m:datafield[@tag='240']", NS) is None


def test_246_emits_variant_title_from_bf_VariantTitle() -> None:
    """MARC 246 varying-form title: ``bffi:variantTitle ->
    bf:VariantTitle -> bf:mainTitle``. Single ``$a`` with
    ind1=3 (no note, added entry)."""
    g = _build_minimal_graph()
    variant = BNode()
    g.add((EXPR, V.BFFI.title, variant))
    g.add((variant, RDF.type, V.BF.VariantTitle))
    g.add(
        (
            variant,
            V.BF.mainTitle,
            Literal("Leonardo da Vino : genij na vse vremena"),
        )
    )
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='246']", NS)
    assert df is not None
    assert df.attrib["ind1"] == "3"
    assert _subfield(df, "a") == "Leonardo da Vino : genij na vse vremena"


def test_246_skipped_when_no_variant_title() -> None:
    g = _build_minimal_graph()
    rec = reconstruct_marc(g, MANIF)
    assert rec.element.find("m:datafield[@tag='246']", NS) is None


def test_264_emits_structured_publication_with_a_b_c_subfields() -> None:
    """Structured ``bffi:provisionActivity`` bnode → one MARC 264 row
    with $a (place), $b (agent), $c (date). ind2 derived from the
    bnode's ``rdf:type`` (Publication=1, the dominant case)."""
    g = _build_minimal_graph()
    prov = BNode()
    g.add((MANIF, V.BFFI.provisionActivity, prov))
    g.add((prov, RDF.type, V.BF.Publication))
    g.add((prov, V.BFLC.simplePlace, Literal("Moskva")))
    g.add((prov, V.BFLC.simpleAgent, Literal("Izdatelstvo AST")))
    g.add((prov, V.BFLC.simpleDate, Literal("2025")))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='264']", NS)
    assert df is not None
    assert df.attrib["ind2"] == "1"
    assert _subfield(df, "a") == "Moskva"
    assert _subfield(df, "b") == "Izdatelstvo AST"
    assert _subfield(df, "c") == "2025"


def test_264_ind2_for_manufacture_provision_activity() -> None:
    """bf:Manufacture → MARC 264 ind2=3 (manufacture)."""
    g = _build_minimal_graph()
    prov = BNode()
    g.add((MANIF, V.BFFI.provisionActivity, prov))
    g.add((prov, RDF.type, V.BF.Manufacture))
    g.add((prov, V.BFLC.simplePlace, Literal("Helsinki")))
    g.add((prov, V.BFLC.simpleAgent, Literal("Otavan Kirjapaino")))
    g.add((prov, V.BFLC.simpleDate, Literal("2024")))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='264']", NS)
    assert df is not None
    assert df.attrib["ind2"] == "3"


def test_648_routing_from_bf_temporal_rdf_type_on_subject() -> None:
    """A cataloguer-typed ``$0 http://www.yso.fi/onto/yso/p…`` URI
    on source MARC 648 lands as ``<bf:Temporal rdf:about="…">``
    in BIBFRAME. M3 routes ``a bf:Temporal`` to canonical; the
    converter reads the type and emits 648 (not the default 650)."""
    g = _build_minimal_graph()
    temporal = URIRef("http://www.yso.fi/onto/yso/p6140061499")
    g.add((WORK, V.BFFI.subject, temporal))
    g.add((temporal, RDF.type, V.BF.Temporal))
    g.add((temporal, V.RDFS.label, Literal("1400-luku")))
    rec = reconstruct_marc(g, MANIF)
    rows = [
        df
        for df in rec.element.findall("m:datafield[@tag='648']", NS)
        if _subfield(df, "0") == str(temporal)
    ]
    assert len(rows) == 1


def test_651_routing_from_bf_place_rdf_type_on_subject() -> None:
    """The Iso-Britannia / b26164413 case: plain ``yso/p104990``
    URI on a source 651 lands as ``<bf:Place rdf:about="…">``.
    The converter routes to 651 via the ``a bf:Place`` typing
    (not via URI namespace — plain yso/ has no geographic hint)."""
    g = _build_minimal_graph()
    place = URIRef("http://www.yso.fi/onto/yso/p104990")
    g.add((WORK, V.BFFI.subject, place))
    g.add((place, RDF.type, V.BF.Place))
    g.add((place, V.RDFS.label, Literal("Iso-Britannia")))
    rec = reconstruct_marc(g, MANIF)
    rows = [
        df
        for df in rec.element.findall("m:datafield[@tag='651']", NS)
        if _subfield(df, "0") == str(place)
    ]
    assert len(rows) == 1


def test_264_prefers_publication_statement_literal_over_label_parse() -> None:
    """When ``bffi:publicationStatement`` is present, 264 $c uses it
    directly instead of parsing the Manifestation prefLabel suffix."""
    g = _build_minimal_graph()
    g.add((MANIF, V.BFFI.publicationStatement, Literal("Helsinki : Otava, 1912")))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='264']", NS)
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


def test_300_emits_a_and_b_from_extent_with_nested_physical_note() -> None:
    """MARC 300 $a (extent) + $b (other physical details) round-trip
    from the BIBFRAME nested structure: ``bf:Extent`` carries
    ``rdfs:label "201 sivua"`` (→ $a) plus a nested ``bf:note → bf:Note
    (a mnotetype/physical) rdfs:label "kuvitettu"`` (→ $b)."""
    g = _build_minimal_graph()
    extent = BNode()
    phys_note = BNode()
    g.add((MANIF, V.BFFI.extent, extent))
    g.add((extent, V.RDFS.label, Literal("201 sivua")))
    g.add((extent, V.BF.note, phys_note))
    g.add(
        (
            phys_note,
            RDF.type,
            URIRef("http://id.loc.gov/vocabulary/mnotetype/physical"),
        )
    )
    g.add((phys_note, V.RDFS.label, Literal("kuvitettu")))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='300']", NS)
    assert df is not None
    assert _subfield(df, "a") == "201 sivua"
    assert _subfield(df, "b") == "kuvitettu"


def test_300_emits_e_from_instance_accmat_note() -> None:
    """MARC 300 $e (accompanying material) — ``bffi:note`` on the
    Manifestation typed ``rdf:type <mnotetype/accmat>``. The
    converter routes the typed note to 300 $e, NOT to 500."""
    g = _build_minimal_graph()
    acc_note = BNode()
    g.add((MANIF, V.BFFI.note, acc_note))
    g.add(
        (
            acc_note,
            RDF.type,
            URIRef("http://id.loc.gov/vocabulary/mnotetype/accmat"),
        )
    )
    g.add((acc_note, V.RDFS.label, Literal("1 CD-äänilevy")))
    rec = reconstruct_marc(g, MANIF)
    df_300 = rec.element.find("m:datafield[@tag='300']", NS)
    assert df_300 is not None
    assert _subfield(df_300, "e") == "1 CD-äänilevy"
    # Must NOT also appear as a 500.
    notes_500 = rec.element.findall("m:datafield[@tag='500']", NS)
    assert all(_subfield(df, "a") != "1 CD-äänilevy" for df in notes_500)


def test_300_emits_all_four_subfields_when_present() -> None:
    """All four common 300 subfields together: $a extent, $b physical,
    $c dimensions, $e accompanying material."""
    g = _build_minimal_graph()
    extent = BNode()
    phys_note = BNode()
    acc_note = BNode()
    g.add((MANIF, V.BFFI.extent, extent))
    g.add((extent, V.RDFS.label, Literal("1 säveImäkokoelma (48 s.)")))
    g.add((extent, V.BF.note, phys_note))
    g.add((phys_note, RDF.type, URIRef("http://id.loc.gov/vocabulary/mnotetype/physical")))
    g.add((phys_note, V.RDFS.label, Literal("AAD")))
    g.add((MANIF, V.BFFI.dimensions, Literal("30 cm")))
    g.add((MANIF, V.BFFI.note, acc_note))
    g.add((acc_note, RDF.type, URIRef("http://id.loc.gov/vocabulary/mnotetype/accmat")))
    g.add((acc_note, V.RDFS.label, Literal("1 CD-äänilevy")))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='300']", NS)
    assert df is not None
    assert _subfield(df, "a") == "1 säveImäkokoelma (48 s.)"
    assert _subfield(df, "b") == "AAD"
    assert _subfield(df, "c") == "30 cm"
    assert _subfield(df, "e") == "1 CD-äänilevy"


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


def test_505_emits_table_of_contents_from_bffi_manifestation_blank_node() -> None:
    """M3 hoists bf:tableOfContents from source bf:Work onto the BFFI
    Manifestation as a bffi:TableOfContents blank node with rdfs:label.
    The converter walks that and emits MARC 505 ind1=0 $a with the
    full track-listing / chapter-list blob."""
    g = _build_minimal_graph()
    toc_node = BNode()
    toc_text = (
        "12 soitinsävelmää: Horos tou sakena / Stavros Ksarhakos. "
        "Fildisenio karavaki / Manos Hadjidakis."
    )
    g.add((MANIF, V.BFFI.tableOfContents, toc_node))
    g.add((toc_node, RDF.type, V.BFFI.TableOfContents))
    g.add((toc_node, V.RDFS.label, Literal(toc_text)))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='505']", NS)
    assert df is not None
    assert df.attrib["ind1"] == "0"
    assert _subfield(df, "a") == toc_text


def test_505_skipped_when_no_table_of_contents_present(
    minimal_record: ET.Element,
) -> None:
    """No 505 datafield when the BFFI graph carries no
    bffi:tableOfContents — silent skip, not a synth row."""
    assert _datafield(minimal_record, "505") is None


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


def test_028_emits_audio_issue_number() -> None:
    """MARC 028 publisher / catalog number: bf:identifiedBy on the
    Manifestation pointing to a bf:AudioIssueNumber → 028 $a, ind1=0."""
    g = _build_minimal_graph()
    issue = BNode()
    g.add((MANIF, V.BF.identifiedBy, issue))
    g.add((issue, RDF.type, V.BF.AudioIssueNumber))
    g.add((issue, RDF.value, Literal("AM950224")))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='028']", NS)
    assert df is not None
    assert df.attrib["ind1"] == "0"
    assert _subfield(df, "a") == "AM950224"


def test_250_emits_edition_statement_literal() -> None:
    """MARC 250 edition: bffi:editionStatement literal on the
    Manifestation → 250 $a."""
    g = _build_minimal_graph()
    g.add((MANIF, V.BFFI.editionStatement, Literal("94. vsk.")))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='250']", NS)
    assert df is not None
    assert _subfield(df, "a") == "94. vsk."


def test_490_emits_series_statement_from_manifestation_node() -> None:
    """MARC 490 series: M3 routes the bf:relation → bf:Series →
    bf:title → bf:mainTitle chain down to a flat bffi:hasSeries link
    from the Manifestation to a bffi:Series node carrying
    ``rdfs:label``. Converter emits 490 $a with ind1=0."""
    g = _build_minimal_graph()
    series = BNode()
    g.add((MANIF, V.BF.hasSeries, series))
    g.add((series, RDF.type, V.BF.Series))
    g.add((series, V.RDFS.label, Literal("Usborne lots of things to know")))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='490']", NS)
    assert df is not None
    assert df.attrib["ind1"] == "0"
    assert _subfield(df, "a") == "Usborne lots of things to know"


def test_500_emits_notes_from_both_expression_and_manifestation() -> None:
    """Instance-side notes (physical-format / accompanying-material,
    e.g. ``Cd-levyllä laulujen taustat``) are routed onto the
    Manifestation by M3. The converter reads notes from BOTH sides
    and emits one 500 row per distinct text — dedup on the text
    so a note attached to both doesn't double-emit."""
    g = _build_minimal_graph()
    expr_note = BNode()
    manif_note = BNode()
    g.add((EXPR, V.BFFI.note, expr_note))
    g.add((expr_note, V.RDF.value, Literal("Käännös englanniksi.")))
    g.add((MANIF, V.BFFI.note, manif_note))
    g.add((manif_note, V.RDFS.label, Literal("1 CD-äänilevy")))
    rec = reconstruct_marc(g, MANIF)
    notes = rec.element.findall("m:datafield[@tag='500']", NS)
    texts = {_subfield(df, "a") for df in notes}
    assert texts == {"Käännös englanniksi.", "1 CD-äänilevy"}


def test_500_dedupes_when_same_note_on_expression_and_manifestation() -> None:
    g = _build_minimal_graph()
    note_text = "Same note attached to both."
    e_node = BNode()
    m_node = BNode()
    g.add((EXPR, V.BFFI.note, e_node))
    g.add((e_node, V.RDF.value, Literal(note_text)))
    g.add((MANIF, V.BFFI.note, m_node))
    g.add((m_node, V.RDF.value, Literal(note_text)))
    rec = reconstruct_marc(g, MANIF)
    notes = rec.element.findall("m:datafield[@tag='500']", NS)
    assert len(notes) == 1


def test_651_routes_via_raw_place_fragment_when_authority_lacks_paikat() -> None:
    """The b10303327 bug — Greece (yso/p105037) was being emitted as
    650 because the plain ``yso/`` URI has no geographic signal. The
    raw ``#Place651-N`` URI carries ``skos:exactMatch <yso/p105037>``;
    the converter walks the back-link and routes to 651."""
    g = _build_minimal_graph()
    raw_place = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/b10303327#Place651-21")
    auth_yso = URIRef("http://www.yso.fi/onto/yso/p105037")
    g.add((WORK, V.BFFI.subject, raw_place))
    g.add((WORK, V.BFFI.subject, auth_yso))
    g.add((raw_place, V.SKOS.exactMatch, auth_yso))
    g.add((auth_yso, V.RDFS.label, Literal("Kreikka")))
    rec = reconstruct_marc(g, MANIF)
    # The authority URI emits the row (raw is suppressed by the
    # dedup logic in _subject_row); the routing is 651, not 650.
    df_650 = [
        df
        for df in rec.element.findall("m:datafield[@tag='650']", NS)
        if _subfield(df, "0") == str(auth_yso)
    ]
    df_651 = [
        df
        for df in rec.element.findall("m:datafield[@tag='651']", NS)
        if _subfield(df, "0") == str(auth_yso)
    ]
    assert not df_650, "Greece URI must not emit as 650"
    assert df_651, "Greece URI must emit as 651 via raw #Place651 back-link"


def test_648_routes_via_raw_topic648_fragment_back_link() -> None:
    """Same pattern for MARC 648 chronological subject: the raw
    ``#Topic648-N`` URI's ``skos:exactMatch`` lets the converter
    route the bare YSO authority URI to 648."""
    g = _build_minimal_graph()
    raw_chr = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/b15177154#Topic648-7")
    auth_yso = URIRef("http://www.yso.fi/onto/yso/p10000")
    g.add((WORK, V.BFFI.subject, raw_chr))
    g.add((WORK, V.BFFI.subject, auth_yso))
    g.add((raw_chr, V.SKOS.exactMatch, auth_yso))
    g.add((auth_yso, V.RDFS.label, Literal("1990-luku")))
    rec = reconstruct_marc(g, MANIF)
    df_648 = [
        df
        for df in rec.element.findall("m:datafield[@tag='648']", NS)
        if _subfield(df, "0") == str(auth_yso)
    ]
    assert df_648, "yso/p10000 must emit as 648 via raw #Topic648 back-link"


def test_lineage_stamped_on_subject_with_within_tag_rank() -> None:
    """P-48 Phase A: a subject URI minted as ``#Topic650-N`` produces
    a recon 650 row carrying ``$9 src=650-<rank>`` where rank is
    1-indexed within the source's 650 bucket. M3's per-record entity
    counter (the ``-N`` suffix) is normalised to a within-tag rank
    by sorting all #Topic650 fragments and renumbering 1, 2, … N.
    Single subject → rank 1."""
    g = _build_minimal_graph()
    raw = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/b13511105#Topic650-7")
    g.add((WORK, V.BFFI.subject, raw))
    g.add((raw, V.RDFS.label, Literal("dinosaurukset")))
    rec = reconstruct_marc(g, MANIF)
    rows = [
        df
        for df in rec.element.findall("m:datafield[@tag='650']", NS)
        if _subfield(df, "a") == "dinosaurukset"
    ]
    assert len(rows) == 1
    assert _subfield(rows[0], "9") == "src=650-1"


def test_lineage_stamped_on_added_entry_with_within_tag_rank() -> None:
    """7XX added entries carry within-tag rank, not raw M3 ordinal."""
    g = _build_minimal_graph()
    contrib = URIRef("urn:contrib/x")
    agent = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/b13511105#Agent700-14")
    g.add((EXPR, V.BFFI.contribution, contrib))
    g.add((contrib, V.BFFI.agent, agent))
    g.add((agent, V.RDFS.label, Literal("Vamvakaris, Markos")))
    rec = reconstruct_marc(g, MANIF)
    rows = [
        df
        for df in rec.element.findall("m:datafield[@tag='700']", NS)
        if _subfield(df, "a") == "Vamvakaris, Markos"
    ]
    assert len(rows) == 1
    assert _subfield(rows[0], "9") == "src=700-1"


def test_lineage_multi_700_assigns_sequential_ranks_in_m3_order() -> None:
    """When multiple ``#Agent700-N`` URIs exist (e.g. 9 source 700s with
    M3 ordinals 27-35 on b10068004), the converter ranks them by their
    M3 ordinal (= source MARC encounter order) and emits ``src=700-1``
    … ``src=700-9``. The diff comparator pairs these against the source
    side's 1-indexed-within-tag counter — closing the cross-graph-
    iteration-order pairing bug."""
    g = _build_minimal_graph()
    # 5 agents with M3 ordinals 27, 28, 29, 30, 31 - mimics b10068004.
    # Add them to the graph in REVERSE order to confirm rank assignment
    # ignores graph-iteration order and only looks at the M3 ordinal.
    expected_label_for_rank = {}
    for m3_ord, name in (
        (31, "Andersson, Benny"),
        (30, "Coleman, Cy"),
        (29, "Jacobs, Jim"),
        (28, "Lind, Jon"),
        (27, "Gore, Michael"),
    ):
        contrib = URIRef(f"urn:contrib/{m3_ord}")
        agent = URIRef(f"http://urn.fi/URN:NBN:fi:bib:raw/bX#Agent700-{m3_ord}")
        g.add((EXPR, V.BFFI.contribution, contrib))
        g.add((contrib, V.BFFI.agent, agent))
        g.add((agent, V.RDFS.label, Literal(name)))
        # Rank = position in sorted M3-ordinal list.
        # Sorted ordinals: 27, 28, 29, 30, 31 → ranks 1..5
        # 27=Gore=rank1, 28=Lind=rank2, 29=Jacobs=rank3,
        # 30=Coleman=rank4, 31=Andersson=rank5
    expected_label_for_rank = {
        1: "Gore, Michael",
        2: "Lind, Jon",
        3: "Jacobs, Jim",
        4: "Coleman, Cy",
        5: "Andersson, Benny",
    }
    rec = reconstruct_marc(g, MANIF)
    for df in rec.element.findall("m:datafield[@tag='700']", NS):
        a = _subfield(df, "a")
        lin = _subfield(df, "9")
        if lin and lin.startswith("src=700-"):
            rank = int(lin[len("src=700-") :])
            assert expected_label_for_rank[rank] == a, (
                f"rank {rank} should be {expected_label_for_rank[rank]!r}, got {a!r}"
            )


def test_lineage_recovered_via_skos_exactmatch_for_authority_subject() -> None:
    """When M9 reconciles ``#Place651-21`` to an authority URI, the
    raw URI carries ``skos:exactMatch <auth>``; the converter walks
    the back-link so the authority's emitted row carries the raw's
    within-tag rank in $9."""
    g = _build_minimal_graph()
    raw = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/b13511105#Place651-21")
    auth = URIRef("http://www.yso.fi/onto/yso/p105037")
    g.add((WORK, V.BFFI.subject, raw))
    g.add((WORK, V.BFFI.subject, auth))
    g.add((raw, V.SKOS.exactMatch, auth))
    g.add((auth, V.RDFS.label, Literal("Kreikka")))
    rec = reconstruct_marc(g, MANIF)
    rows = [
        df
        for df in rec.element.findall("m:datafield[@tag='651']", NS)
        if _subfield(df, "0") == str(auth)
    ]
    assert len(rows) == 1
    # Single #Place651 in the graph → rank 1
    assert _subfield(rows[0], "9") == "src=651-1"


def test_lineage_absent_for_flat_instance_fields_pending_phase_b() -> None:
    """Phase A intentionally doesn't stamp 020 / 028 / 250 / 490 /
    500 / 505 — those need ``bffi-prov:fromSourceField`` triples in
    Phase B. Pin so adding lineage to them later updates this test
    deliberately."""
    g = _build_minimal_graph()
    rec = reconstruct_marc(g, MANIF)
    # The minimal graph carries an ISBN (`bf:identifiedBy → bf:Isbn`).
    df_020 = rec.element.find("m:datafield[@tag='020']", NS)
    assert df_020 is not None
    assert _subfield(df_020, "9") is None


def test_authority_subject_resolves_a_from_finnish_preflabel() -> None:
    """When the YSO/KANTO authority URI has a ``skos:prefLabel`` in
    the graph (Finto dumps are loaded into the round-trip graph),
    the recon row emits ``$a`` with the Finnish prefLabel."""
    g = _build_minimal_graph()
    auth = URIRef("http://www.yso.fi/onto/yso/p29977")
    g.add((WORK, V.BFFI.subject, auth))
    g.add((auth, SKOS.prefLabel, Literal("buzukit", lang="fi")))
    g.add((auth, SKOS.prefLabel, Literal("bouzoukis", lang="en")))
    rec = reconstruct_marc(g, MANIF)
    rows = [
        df
        for df in rec.element.findall("m:datafield[@tag='650']", NS)
        if _subfield(df, "0") == str(auth)
    ]
    assert len(rows) == 1
    assert _subfield(rows[0], "a") == "buzukit"


def test_authority_subject_label_prefers_swedish_over_english() -> None:
    """Finnish > Swedish > English. With no Finnish, picks Swedish."""
    g = _build_minimal_graph()
    auth = URIRef("http://www.yso.fi/onto/yso/p11111")
    g.add((WORK, V.BFFI.subject, auth))
    g.add((auth, SKOS.prefLabel, Literal("Some concept", lang="en")))
    g.add((auth, SKOS.prefLabel, Literal("Något", lang="sv")))
    rec = reconstruct_marc(g, MANIF)
    rows = [
        df
        for df in rec.element.findall("m:datafield[@tag='650']", NS)
        if _subfield(df, "0") == str(auth)
    ]
    assert len(rows) == 1
    assert _subfield(rows[0], "a") == "Något"


def test_authority_subject_falls_back_to_raw_uri_label_when_finto_absent() -> None:
    """Cataloguer's typed text on the raw URI is the last-resort
    label when the Finto dump for the authority's namespace isn't
    loaded. The raw URI's ``skos:exactMatch`` points to the
    authority; the converter walks the inverse link to find the
    cataloguer's original typed term."""
    g = _build_minimal_graph()
    raw = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/bX#Topic650-9")
    auth = URIRef("http://www.yso.fi/onto/yso/pXXXXX")  # no labels attached
    g.add((WORK, V.BFFI.subject, raw))
    g.add((WORK, V.BFFI.subject, auth))
    g.add((raw, V.RDFS.label, Literal("buzuki", lang="fi")))
    g.add((raw, V.SKOS.exactMatch, auth))
    rec = reconstruct_marc(g, MANIF)
    rows = [
        df
        for df in rec.element.findall("m:datafield[@tag='650']", NS)
        if _subfield(df, "0") == str(auth)
    ]
    assert len(rows) == 1
    # Raw's label "buzuki" surfaces as $a since the authority has none.
    assert _subfield(rows[0], "a") == "buzuki"


def test_730_emits_from_hub_marcKey_subfields_preferentially() -> None:
    """When the bf:Hub carries a ``bflc:marcKey`` (faithful source
    subfield string), the converter parses ``$a`` / ``$g`` directly
    from it — preserves the cataloguer's original subfield structure
    exactly, including trailing punctuation on $a."""
    g = _build_minimal_graph()
    hub = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/bX#Hub730-7")
    rel = BNode()
    g.add((MANIF, V.BFFI.relation, rel))
    g.add((rel, RDF.type, V.BFFI.Relation))
    g.add(
        (
            rel,
            V.BFFI.relationship,
            URIRef("http://id.loc.gov/vocabulary/relationship/relatedwork"),
        )
    )
    g.add((rel, V.BFFI.associatedResource, hub))
    g.add((hub, RDF.type, V.BF.Hub))
    g.add(
        (
            hub,
            V.BFLC.marcKey,
            Literal("73000 $aAnother suitcase in another hall /$gLloyd Webber, Andrew"),
        )
    )
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='730']", NS)
    assert df is not None
    assert df.attrib["ind1"] == "0"
    assert _subfield(df, "a") == "Another suitcase in another hall /"
    assert _subfield(df, "g") == "Lloyd Webber, Andrew"
    # Lineage stamped from #Hub730-7 (single Hub → rank 1).
    assert _subfield(df, "9") == "src=730-1"


def test_730_falls_back_to_mainTitle_split_when_no_marcKey() -> None:
    """Without bflc:marcKey, the converter splits bf:Title.bf:mainTitle
    on the cataloguer-conventional ` / ` separator."""
    g = _build_minimal_graph()
    hub = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/bX#Hub730-5")
    title = BNode()
    rel = BNode()
    g.add((MANIF, V.BFFI.relation, rel))
    g.add((rel, RDF.type, V.BFFI.Relation))
    g.add((rel, V.BFFI.associatedResource, hub))
    g.add((hub, RDF.type, V.BF.Hub))
    g.add((hub, V.BF.title, title))
    g.add((title, V.BF.mainTitle, Literal("Fame / Gore, Michael")))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='730']", NS)
    assert df is not None
    assert _subfield(df, "a") == "Fame /"
    assert _subfield(df, "g") == "Gore, Michael"


def test_730_prefers_structured_part_number_and_part_name() -> None:
    """P-49 Layer 1: when the Hub's bf:Title carries structured
    ``bf:partNumber`` / ``bf:partName`` (the Beethoven Op. 18 case),
    the converter sources $n and $p from them. $a and $g still
    come from marcKey (P-49 Layer 3 gaps), so the row is still
    flagged ``marckey_bypass`` — but the part subfields are
    structured-sourced. The structured values override any $n/$p
    that marcKey happens to also carry."""
    g = _build_minimal_graph()
    hub = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/b11567594#Hub730-33")
    rel = BNode()
    g.add((MANIF, V.BFFI.relation, rel))
    g.add((rel, V.BFFI.associatedResource, hub))
    g.add((hub, RDF.type, V.BF.Hub))
    g.add(
        (
            hub,
            V.BFLC.marcKey,
            Literal(
                "7300 $aKvartetot, jouset, op18.$nDISCARDED-N,"
                "$pDISCARDED-P /$gBeethoven, Ludwig van"
            ),
        )
    )
    title = BNode()
    g.add((hub, V.BF.title, title))
    g.add((title, RDF.type, V.BF.Title))
    g.add((title, V.BF.partNumber, Literal("Nro 3")))
    g.add((title, V.BF.partName, Literal("D-duuri")))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='730']", NS)
    assert df is not None
    # $n / $p sourced from structured bf:partNumber/bf:partName.
    assert _subfield(df, "n") == "Nro 3"
    assert _subfield(df, "p") == "D-duuri"
    # $a / $g still from marcKey (Layer 3 gap).
    assert _subfield(df, "a") == "Kvartetot, jouset, op18."
    assert _subfield(df, "g") == "Beethoven, Ludwig van"


def test_730_emits_a_only_when_mainTitle_has_no_responsibility() -> None:
    """A bf:mainTitle without ` / ` separator → just ``$a`` (no $g)."""
    g = _build_minimal_graph()
    hub = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/bX#Hub730-1")
    title = BNode()
    rel = BNode()
    g.add((MANIF, V.BFFI.relation, rel))
    g.add((rel, V.BFFI.associatedResource, hub))
    g.add((hub, RDF.type, V.BF.Hub))
    g.add((hub, V.BF.title, title))
    g.add((title, V.BF.mainTitle, Literal("Standalone Title")))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='730']", NS)
    assert df is not None
    assert _subfield(df, "a") == "Standalone Title"
    assert _subfield(df, "g") is None


def test_730_skips_non_hub_associatedResource() -> None:
    """A ``bffi:relation → bffi:Relation → bffi:associatedResource →
    bf:Series`` chain (the 490 series shape) must NOT emit a 730 —
    the type filter is on the associated resource's class."""
    g = _build_minimal_graph()
    series = BNode()
    rel = BNode()
    g.add((MANIF, V.BFFI.relation, rel))
    g.add((rel, V.BFFI.associatedResource, series))
    g.add((series, RDF.type, V.BF.Series))
    g.add((series, V.RDFS.label, Literal("Some series")))
    rec = reconstruct_marc(g, MANIF)
    assert rec.element.find("m:datafield[@tag='730']", NS) is None


def test_730_emits_multiple_hubs_with_ranked_lineage() -> None:
    """Nine 730s on a single record (the b10068004 music-collection
    case) emit nine 730 rows with lineage ``src=730-1`` … ``src=730-9``
    in source-MARC encounter order. Ranks are derived from the
    ``#Hub730-N`` ordinals; the converter's rank-map normalises
    across-kind M3 offsets to within-tag positions."""
    g = _build_minimal_graph()
    # M3 ordinals 36..38 mimic the b10068004 spacing (Hubs start
    # after Topic + Agent entities). Add in reverse order to
    # confirm graph-iteration order doesn't leak into ranking.
    for m3_ord, song in (
        (38, "Third song"),
        (36, "First song"),
        (37, "Second song"),
    ):
        hub = URIRef(f"http://urn.fi/URN:NBN:fi:bib:raw/bX#Hub730-{m3_ord}")
        title = BNode()
        rel = BNode()
        g.add((MANIF, V.BFFI.relation, rel))
        g.add((rel, V.BFFI.associatedResource, hub))
        g.add((hub, RDF.type, V.BF.Hub))
        g.add((hub, V.BF.title, title))
        g.add((title, V.BF.mainTitle, Literal(song)))
    rec = reconstruct_marc(g, MANIF)
    rows = rec.element.findall("m:datafield[@tag='730']", NS)
    assert len(rows) == 3
    by_lineage = {_subfield(df, "9"): _subfield(df, "a") for df in rows}
    assert by_lineage == {
        "src=730-1": "First song",  # M3 ord 36 = within-tag rank 1
        "src=730-2": "Second song",  # M3 ord 37 = rank 2
        "src=730-3": "Third song",  # M3 ord 38 = rank 3
    }


def test_740_emits_from_bf_work_associatedResource() -> None:
    """MARC 740 (uncontrolled related/analytical title) lands in
    BIBFRAME as the same relation chain as 730, but the
    ``bf:associatedResource`` is a ``bf:Work`` (URI ``#Work740-N``)
    instead of a ``bf:Hub``. The converter branches on the type
    and emits MARC 740. ind1=0; lineage stamps via ``#Work740-N``
    rank."""
    g = _build_minimal_graph()
    related_work = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/bX#Work740-39")
    title = BNode()
    rel = BNode()
    g.add((MANIF, V.BFFI.relation, rel))
    g.add((rel, V.BFFI.associatedResource, related_work))
    g.add((related_work, RDF.type, V.BF.Work))
    g.add((related_work, V.BF.title, title))
    g.add((title, V.BF.mainTitle, Literal("Kartor och gatunamnen")))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='740']", NS)
    assert df is not None
    assert df.attrib["ind1"] == "0"
    assert _subfield(df, "a") == "Kartor och gatunamnen"
    assert _subfield(df, "9") == "src=740-1"


def test_740_and_730_both_emit_from_same_record() -> None:
    """A record carrying both 730 (Hub-typed associated resource) and
    740 (Work-typed) emits both tags. Confirms the type-branch in
    ``_emit_related_uniform_titles`` routes each row to its tag."""
    g = _build_minimal_graph()
    hub = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/bX#Hub730-7")
    related_work = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/bX#Work740-8")
    hub_title = BNode()
    work_title = BNode()
    g.add((MANIF, V.BFFI.relation, BNode()))  # filler — needs predicate path
    rel_hub = BNode()
    g.add((MANIF, V.BFFI.relation, rel_hub))
    g.add((rel_hub, V.BFFI.associatedResource, hub))
    g.add((hub, RDF.type, V.BF.Hub))
    g.add((hub, V.BF.title, hub_title))
    g.add((hub_title, V.BF.mainTitle, Literal("Fame / Gore, Michael")))
    rel_work = BNode()
    g.add((MANIF, V.BFFI.relation, rel_work))
    g.add((rel_work, V.BFFI.associatedResource, related_work))
    g.add((related_work, RDF.type, V.BF.Work))
    g.add((related_work, V.BF.title, work_title))
    g.add((work_title, V.BF.mainTitle, Literal("Pääkaupunkiseutu")))
    rec = reconstruct_marc(g, MANIF)
    df_730 = rec.element.find("m:datafield[@tag='730']", NS)
    df_740 = rec.element.find("m:datafield[@tag='740']", NS)
    assert df_730 is not None
    assert df_740 is not None
    assert _subfield(df_730, "a") == "Fame /"
    assert _subfield(df_730, "g") == "Gore, Michael"
    assert _subfield(df_740, "a") == "Pääkaupunkiseutu"


def test_710_emits_asteri_id_in_dollar0_with_source_code_prefix() -> None:
    """MARC 710 $0 (FI-ASTERI-N)NNNNNN must round-trip from BIBFRAME's
    ``agent → bf:identifiedBy → bf:Identifier → rdf:value + bf:source →
    bf:Source → bf:code "FI-ASTERI-N"`` shape into a ``$0 (CODE)VALUE``
    subfield matching the cataloguer's typed form. Trailing whitespace
    on the value is preserved verbatim — round-trip is round-trip."""
    g = _build_minimal_graph()
    contrib = URIRef("urn:contrib/asteri")
    agent = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/bX#Agent710-1")
    ident = BNode()
    source = BNode()
    g.add((EXPR, V.BFFI.contribution, contrib))
    g.add((contrib, V.BFFI.agent, agent))
    g.add((agent, V.RDFS.label, Literal("Otava, kustannusosakeyhtiö")))
    g.add((agent, V.BF.identifiedBy, ident))
    g.add((ident, RDF.type, V.BF.Identifier))
    g.add((ident, RDF.value, Literal("000039084 ")))  # trailing space
    g.add((ident, V.BF.source, source))
    g.add((source, RDF.type, V.BF.Source))
    g.add((source, V.BF.code, Literal("FI-ASTERI-N")))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='710']", NS)
    assert df is not None
    assert _subfield(df, "a") == "Otava, kustannusosakeyhtiö"
    assert _subfield(df, "0") == "(FI-ASTERI-N)000039084 "


def test_100_emits_asteri_id_on_primary_contribution() -> None:
    """ASTERI IDs on primary creators (MARC 100 $0) round-trip via the
    same agent-identifier chain — the Work-side CONSTRUCT routes it
    onto the BFFI primary contribution's agent."""
    g = _build_minimal_graph()
    # Override the minimal graph's primary agent with one that carries
    # an ASTERI identifier.
    ident = BNode()
    source = BNode()
    g.add((AGENT, V.BF.identifiedBy, ident))
    g.add((ident, RDF.type, V.BF.Identifier))
    g.add((ident, RDF.value, Literal("000123456")))
    g.add((ident, V.BF.source, source))
    g.add((source, V.BF.code, Literal("FI-ASTERI-N")))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='100']", NS)
    assert df is not None
    assert _subfield(df, "0") == "(FI-ASTERI-N)000123456"


def test_700_emits_dollar0_value_only_when_source_code_absent() -> None:
    """When an agent carries an identifier without a ``bf:source``
    code block (rare but possible in the corpus), emit ``$0 VALUE``
    without a prefix — better than dropping the data."""
    g = _build_minimal_graph()
    contrib = URIRef("urn:contrib/no-code")
    agent = URIRef("urn:agent/no-code")
    ident = BNode()
    g.add((EXPR, V.BFFI.contribution, contrib))
    g.add((contrib, V.BFFI.agent, agent))
    g.add((agent, V.RDFS.label, Literal("Some Agent")))
    g.add((agent, V.BF.identifiedBy, ident))
    g.add((ident, RDF.value, Literal("bare-id")))
    rec = reconstruct_marc(g, MANIF)
    df = next(
        (
            df
            for df in rec.element.findall("m:datafield[@tag='700']", NS)
            if _subfield(df, "a") == "Some Agent"
        ),
        None,
    )
    assert df is not None
    assert _subfield(df, "0") == "bare-id"


def test_020_emits_q_qualifier_when_isbn_carries_one() -> None:
    """``bf:Isbn → bf:qualifier "nidottu"`` round-trips as MARC 020
    $q (binding type — "nidottu" / "kovakantinen" / "pehmeäkantinen"
    in Helmet's Finnish cataloguing)."""
    g = _build_minimal_graph()
    isbn = URIRef("urn:isbn/withq")
    g.add((MANIF, V.BF.identifiedBy, isbn))
    g.add((isbn, RDF.type, V.BF.Isbn))
    g.add((isbn, RDF.value, Literal("9510066966")))
    g.add((isbn, V.BF.qualifier, Literal("pehmeäkantinen")))
    rec = reconstruct_marc(g, MANIF)
    df = next(
        df
        for df in rec.element.findall("m:datafield[@tag='020']", NS)
        if _subfield(df, "a") == "9510066966"
    )
    assert _subfield(df, "q") == "pehmeäkantinen"


def test_336_337_338_emit_a_from_rdfs_label_on_loc_uri() -> None:
    """marc2bibframe2 attaches the Finnish source-MARC ``$a`` label
    directly to the LoC URI as ``rdfs:label`` (e.g.
    ``<…/contentTypes/txt> rdfs:label "teksti"``). The converter
    walks ``rdfs:label`` in addition to ``skos:prefLabel`` so
    337/338 $a round-trip without depending on a LoC vocab dump."""
    g = _build_minimal_graph()
    content_uri = URIRef("http://id.loc.gov/vocabulary/contentTypes/txt")
    g.add((EXPR, V.BFFI.content, content_uri))
    g.add((content_uri, V.RDFS.label, Literal("teksti")))
    g.remove((URIRef("http://id.loc.gov/vocabulary/mediaTypes/n"), SKOS.prefLabel, None))
    g.add(
        (
            URIRef("http://id.loc.gov/vocabulary/mediaTypes/n"),
            V.RDFS.label,
            Literal("käytettävissä ilman laitetta"),
        )
    )
    g.remove((URIRef("http://id.loc.gov/vocabulary/carriers/nc"), SKOS.prefLabel, None))
    g.add(
        (
            URIRef("http://id.loc.gov/vocabulary/carriers/nc"),
            V.RDFS.label,
            Literal("nide"),
        )
    )
    rec = reconstruct_marc(g, MANIF)
    df_336 = rec.element.find("m:datafield[@tag='336']", NS)
    assert df_336 is not None and _subfield(df_336, "a") == "teksti"
    df_337 = rec.element.find("m:datafield[@tag='337']", NS)
    assert df_337 is not None
    assert _subfield(df_337, "a") == "käytettävissä ilman laitetta"
    df_338 = rec.element.find("m:datafield[@tag='338']", NS)
    assert df_338 is not None and _subfield(df_338, "a") == "nide"


def test_040_emits_source_a_b_e_from_admin_metadata() -> None:
    """MARC 040 $a / $b / $e round-trip from the source's
    ``bffi:adminMetadata`` block: $a from ``bf:agent → bf:code``,
    $b from ``bffi:descriptionLanguage`` URI tail, $e from
    ``bffi:descriptionConventions`` URI tail. $d always carries the
    ``FI-HELME/bffi-roundtrip`` marker."""
    g = _build_minimal_graph()
    admin = BNode()
    agent = BNode()
    g.add((MANIF, V.BFFI.adminMetadata, admin))
    g.add((admin, RDF.type, V.BFFI.AdminMetadata))
    g.add((admin, V.BF.agent, agent))
    g.add((agent, V.BF.code, Literal("FI-BTJ")))
    g.add(
        (
            admin,
            V.BFFI.descriptionLanguage,
            URIRef("http://id.loc.gov/vocabulary/languages/fin"),
        )
    )
    g.add(
        (
            admin,
            V.BFFI.descriptionConventions,
            URIRef("http://id.loc.gov/vocabulary/descriptionConventions/rda"),
        )
    )
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='040']", NS)
    assert df is not None
    assert _subfield(df, "a") == "FI-BTJ"
    assert _subfield(df, "b") == "fin"
    assert _subfield(df, "e") == "rda"
    assert _subfield(df, "d") == "FI-HELME/bffi-roundtrip"


def test_040_falls_back_to_fi_helme_a_when_no_admin_metadata(
    minimal_record: ET.Element,
) -> None:
    """When no source AdminMetadata, emit synth $a 'FI-HELME' + $d
    marker so the round-trip stamp stays cataloguer-visible."""
    df = _datafield(minimal_record, "040")
    assert df is not None
    assert _subfield(df, "a") == "FI-HELME"
    assert _subfield(df, "b") is None
    assert _subfield(df, "e") is None
    assert _subfield(df, "d") == "FI-HELME/bffi-roundtrip"


def test_035_emits_from_loc_organizations_uri_assigner() -> None:
    """MARC 035 $a (FI-MELINDA)006644447 round-trips from
    ``bf:identifiedBy → bf:Local`` with ``bf:assigner`` pointing at
    the LoC organizations URI ``<…/organizations/fimelinda>``.
    Converter reverse-derives ``FI-MELINDA`` via the curated table."""
    g = _build_minimal_graph()
    ident = BNode()
    g.add((MANIF, V.BF.identifiedBy, ident))
    g.add((ident, RDF.type, V.BF.Local))
    g.add((ident, RDF.value, Literal("006644447")))
    g.add(
        (
            ident,
            V.BF.assigner,
            URIRef("http://id.loc.gov/vocabulary/organizations/fimelinda"),
        )
    )
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='035']", NS)
    assert df is not None
    assert _subfield(df, "a") == "(FI-MELINDA)006644447"


def test_035_emits_from_agent_bnode_assigner_with_bf_code() -> None:
    """When ``bf:assigner`` is a ``bf:Agent`` blank node with
    ``bf:code "FI-BTJ"``, the converter reads the code directly."""
    g = _build_minimal_graph()
    ident = BNode()
    agent = BNode()
    g.add((MANIF, V.BF.identifiedBy, ident))
    g.add((ident, RDF.type, V.BF.Local))
    g.add((ident, RDF.value, Literal("7247969")))
    g.add((ident, V.BF.assigner, agent))
    g.add((agent, RDF.type, V.BF.Agent))
    g.add((agent, V.BF.code, Literal("FI-BTJ")))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='035']", NS)
    assert df is not None
    assert _subfield(df, "a") == "(FI-BTJ)7247969"


def test_035_does_not_emit_for_helmet_bib_id_local() -> None:
    """The Helmet bib_id is also a ``bf:Local`` on the Manifestation
    but uses ``bf:source <…/source:helmet>`` (no ``bf:assigner``).
    Must not surface as 035 (that's 001 controlfield territory)."""
    g = _build_minimal_graph()
    ident = BNode()
    g.add((MANIF, V.BF.identifiedBy, ident))
    g.add((ident, RDF.type, V.BF.Local))
    g.add((ident, RDF.value, Literal("b13511105")))
    g.add((ident, V.BF.source, URIRef("http://urn.fi/URN:NBN:fi:bib:source:helmet")))
    rec = reconstruct_marc(g, MANIF)
    assert rec.element.find("m:datafield[@tag='035']", NS) is None


def test_035_falls_back_to_uppercased_uri_tail_for_unknown_org() -> None:
    """Unknown LoC organizations URI tails: fall back to upper-casing
    the tail rather than dropping the row entirely."""
    g = _build_minimal_graph()
    ident = BNode()
    g.add((MANIF, V.BF.identifiedBy, ident))
    g.add((ident, RDF.type, V.BF.Local))
    g.add((ident, RDF.value, Literal("99999")))
    g.add(
        (
            ident,
            V.BF.assigner,
            URIRef("http://id.loc.gov/vocabulary/organizations/zzunknown"),
        )
    )
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='035']", NS)
    assert df is not None
    assert _subfield(df, "a") == "(ZZUNKNOWN)99999"


def test_600_emits_separate_a_and_c_from_marcKey_on_subject() -> None:
    """The b12191139 / b22522396 "(fiktiivinen hahmo)" bug: source 600
    ``$a "Mikki Hiiri" $c "(fiktiivinen hahmo)"`` was collapsing to
    a single ``$a "Mikki Hiiri (fiktiivinen hahmo)"`` because
    marc2bibframe2 concatenates the subfields into ``rdfs:label`` and
    only ``bflc:marcKey`` preserves the boundary. The converter now
    parses marcKey and emits $a and $c as separate subfields."""
    g = _build_minimal_graph()
    subj = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/bX#Agent600-3")
    g.add((WORK, V.BFFI.subject, subj))
    g.add((subj, V.RDFS.label, Literal("Mikki Hiiri (fiktiivinen hahmo)")))
    g.add((subj, V.BFLC.marcKey, Literal("60004$aMikki Hiiri$c(fiktiivinen hahmo)")))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='600']", NS)
    assert df is not None
    assert _subfield(df, "a") == "Mikki Hiiri"
    assert _subfield(df, "c") == "(fiktiivinen hahmo)"


def test_700_emits_separate_a_and_d_from_marcKey_on_added_entry() -> None:
    """Same fix for 7XX added entries — source
    ``700 $aSibelius, Jean, $d1865-1957$ekirjoittaja`` round-trips
    with $a / $d split + $e from the role chain (no double-emit
    since marcKey-derived $e is filtered out)."""
    g = _build_minimal_graph()
    contrib = URIRef("urn:contrib/marckey")
    agent = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/bX#Agent700-9")
    g.add((EXPR, V.BFFI.contribution, contrib))
    g.add((contrib, V.BFFI.agent, agent))
    g.add((agent, V.RDFS.label, Literal("Sibelius, Jean, 1865-1957")))
    g.add(
        (
            agent,
            V.BFLC.marcKey,
            Literal("7001 $aSibelius, Jean,$d1865-1957$ekirjoittaja"),
        )
    )
    # Add a role separately (via bf:role blank node, like M3 emits).
    role = BNode()
    g.add((contrib, V.BF.role, role))
    g.add((role, V.RDFS.label, Literal("kirjoittaja")))
    rec = reconstruct_marc(g, MANIF)
    df = next(
        df
        for df in rec.element.findall("m:datafield[@tag='700']", NS)
        if _subfield(df, "a") and "Sibelius" in (_subfield(df, "a") or "")
    )
    assert _subfield(df, "a") == "Sibelius, Jean,"
    assert _subfield(df, "d") == "1865-1957"
    assert _subfield(df, "e") == "kirjoittaja"
    # $e must appear EXACTLY once even though it's in marcKey AND in
    # the role chain — the marcKey parser filters out $e since the
    # role helper owns it.
    e_subs = [sf.text for sf in df.findall("m:subfield", NS) if sf.attrib.get("code") == "e"]
    assert e_subs == ["kirjoittaja"]


def test_100_emits_separate_a_and_d_from_marcKey_on_primary() -> None:
    """Same fix for MARC 100 primary contribution."""
    g = _build_minimal_graph()
    g.add((AGENT, V.BFLC.marcKey, Literal("1001 $aKrag, Thomas Peter,$d1868-1913")))
    rec = reconstruct_marc(g, MANIF)
    df = rec.element.find("m:datafield[@tag='100']", NS)
    assert df is not None
    assert _subfield(df, "a") == "Krag, Thomas Peter,"
    assert _subfield(df, "d") == "1868-1913"


def test_skipped_tags_lists_852_and_336(minimal_record: ET.Element) -> None:
    # Smoke: we explicitly skip 852 (holdings) and 336 (content type
    # — not currently forwarded onto BFFI). Pin so adding either to
    # the converter later updates this test deliberately.
    g = _build_minimal_graph()
    rec = reconstruct_marc(g, MANIF)
    assert "852" in rec.skipped_tags
    assert "336" in rec.skipped_tags
