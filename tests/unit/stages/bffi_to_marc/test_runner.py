"""Unit tests for the BFFI -> MARCXML reverse converter (v0)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from lxml import etree
from rdflib import RDF, Graph, Literal, URIRef
from rdflib.namespace import RDFS

from bffi_pipeline.observability.events import StageEventEmitter, set_active_emitter
from bffi_pipeline.provenance.vocab import BFFI, bind_canonical_prefixes
from bffi_pipeline.stages.bffi_to_marc.runner import (
    MARC21_NS,
    BffiToMarcError,
    ConversionOptions,
    convert_corpus,
    convert_one,
    emit_marcxml,
)
from bffi_pipeline.stages.bibframe_to_bffi.mappings import load_rules
from bffi_pipeline.stages.bibframe_to_bffi.runner import (
    ConversionOptions as Bf2BffiOptions,
)
from bffi_pipeline.stages.bibframe_to_bffi.runner import (
    convert_one as bf2bffi_one,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SAMPLE_MARC = _REPO_ROOT / "third_party" / "marc2bibframe2" / "test" / "data" / "marc.xml"
_M2BF_XSL = _REPO_ROOT / "third_party" / "marc2bibframe2" / "xsl" / "marc2bibframe2.xsl"


# --- fixtures ------------------------------------------------------------


def _produce_bffi_fixture(out_dir: Path, *, stem: str = "test") -> Path:
    """Run MARC -> BIBFRAME -> BFFI to produce a real BFFI Turtle fixture."""
    out_dir.mkdir(parents=True, exist_ok=True)
    bibframe_path = out_dir / f"{stem}.bibframe.xml"
    bffi_path = out_dir / f"{stem}.bffi.ttl"

    result = subprocess.run(
        ["xsltproc", str(_M2BF_XSL), str(_SAMPLE_MARC)],
        capture_output=True,
        text=True,
        check=True,
    )
    bibframe_path.write_text(result.stdout, encoding="utf-8")

    # Run the BFFI conversion via the runner so we exercise the same code
    # path the pipeline uses, not a hand-rolled BFFI graph.
    bf2bffi_one(
        bibframe_path,
        options=Bf2BffiOptions(input_dir=out_dir, output_dir=out_dir),
        rules=load_rules(),
    )
    return bffi_path


def _build_minimal_bffi_graph(*, manifestation_uri: str, bib_id: str, title: str) -> Graph:
    """Build a hand-rolled BFFI graph for tightly-scoped unit tests.

    Avoids the cost of running the upstream stages; the resulting graph
    has exactly the shape v0 needs to consume.
    """
    g = Graph()
    bind_canonical_prefixes(g)
    m = URIRef(manifestation_uri)
    g.add((m, RDF.type, BFFI.Manifestation))

    local_block = URIRef(manifestation_uri + "/local-id")
    g.add((local_block, RDF.type, BFFI.Local))
    g.add((local_block, RDF.value, Literal(bib_id)))
    g.add((m, BFFI.identifiedBy, local_block))

    title_block = URIRef(manifestation_uri + "/title")
    g.add((title_block, RDF.type, BFFI.Title))
    g.add((title_block, BFFI.mainTitle, Literal(title)))
    g.add((m, BFFI.title, title_block))

    return g


# --- emit_marcxml --------------------------------------------------------


def test_emit_marcxml_minimal_record_round_trips_bib_id_and_title() -> None:
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b123#Instance",
        bib_id="b123",
        title="Test Title",
    )
    manifestation = next(g.subjects(RDF.type, BFFI.Manifestation))

    marcxml = emit_marcxml(g, manifestation=manifestation)
    root = etree.fromstring(marcxml)

    assert root.tag == f"{{{MARC21_NS}}}record"

    controlfields = root.findall(f"{{{MARC21_NS}}}controlfield")
    assert len(controlfields) == 1
    assert controlfields[0].get("tag") == "001"
    assert controlfields[0].text == "b123"

    df245 = root.find(f"{{{MARC21_NS}}}datafield[@tag='245']")
    assert df245 is not None
    sf_a = df245.find(f"{{{MARC21_NS}}}subfield[@code='a']")
    assert sf_a is not None
    assert sf_a.text == "Test Title"


def test_emit_marcxml_leader_defaults_for_unsignalled_record() -> None:
    """A minimal Manifestation (no issuance, content, status, encoding
    level) yields a leader with the documented defaults: position 05
    'n' (new), 06 'a' (language material), 07 'm' (monograph), 17 ' '
    (full level). Positions 10-11 = '22' (indicator + subfield code
    count), 20-23 = '4500' per MARC 21 convention."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    leader = root.find(f"{{{MARC21_NS}}}leader")
    assert leader is not None
    text = leader.text or ""
    assert len(text) == 24
    assert text[5] == "n"  # status
    assert text[6] == "a"  # record type
    assert text[7] == "m"  # bibliographic level
    assert text[10:12] == "22"
    assert text[17] == " "  # encoding level
    assert text[20:24] == "4500"


def test_emit_marcxml_leader_derives_from_bffi_signals() -> None:
    """A DVD-like record (issuance=mono, content=tdi two-dim moving
    image, status=corrected, menclvl/7 minimal) yields a leader of
    the shape ``"00000cgm  22000007  4500"`` — position 05 'c'
    (corrected), 06 'g' (projected medium), 07 'm' (monograph),
    17 '7' (minimal level)."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))
    work = URIRef("http://example.org/b1#Work")
    g.add((work, RDF.type, BFFI.BibframeWork))
    g.add((m, BFFI.workManifested, work))
    g.add(
        (
            work,
            BFFI.content,
            URIRef("http://id.loc.gov/vocabulary/contentTypes/tdi"),
        )
    )
    g.add((m, BFFI.issuance, URIRef("http://id.loc.gov/vocabulary/issuance/mono")))
    admin = URIRef("http://example.org/b1#admin")
    g.add((admin, RDF.type, BFFI.AdminMetadata))
    g.add((admin, BFFI.status, URIRef("http://id.loc.gov/vocabulary/mstatus/c")))
    g.add(
        (
            admin,
            URIRef("http://id.loc.gov/ontologies/bflc/encodingLevel"),
            URIRef("http://id.loc.gov/vocabulary/menclvl/7"),
        )
    )
    g.add((m, BFFI.adminMetadata, admin))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    leader = root.find(f"{{{MARC21_NS}}}leader")
    assert leader is not None
    text = leader.text or ""
    assert text[5] == "c"
    assert text[6] == "g"
    assert text[7] == "m"
    assert text[17] == "7"


def test_emit_marcxml_leader_full_level_menclvl_f_emits_blank() -> None:
    """``bffi:adminMetadata / bflc:encodingLevel <…/menclvl/f>`` (full
    level) emits a literal blank at leader position 17 — the MARC
    convention for full bibliographic encoding."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))
    admin = URIRef("http://example.org/b1#admin")
    g.add((admin, RDF.type, BFFI.AdminMetadata))
    g.add(
        (
            admin,
            URIRef("http://id.loc.gov/ontologies/bflc/encodingLevel"),
            URIRef("http://id.loc.gov/vocabulary/menclvl/f"),
        )
    )
    g.add((m, BFFI.adminMetadata, admin))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    leader = root.find(f"{{{MARC21_NS}}}leader")
    assert leader is not None
    assert (leader.text or "")[17] == " "


def test_emit_marcxml_emits_245_b_when_subtitle_is_present() -> None:
    """A bffi:Title block carrying both bffi:mainTitle and bffi:subtitle
    produces a 245 datafield with $a and $b subfields. Maps to MARC 245
    where $b is the parallel/subtitle portion."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b123#Instance",
        bib_id="b123",
        title="Main Title",
    )
    manifestation = next(g.subjects(RDF.type, BFFI.Manifestation))
    title_block = next(g.objects(manifestation, BFFI.title))
    g.add((title_block, BFFI.subtitle, Literal("an explanatory subtitle")))

    marcxml = emit_marcxml(g, manifestation=manifestation)
    root = etree.fromstring(marcxml)
    df245 = root.find(f"{{{MARC21_NS}}}datafield[@tag='245']")
    assert df245 is not None
    sf_a = df245.find(f"{{{MARC21_NS}}}subfield[@code='a']")
    sf_b = df245.find(f"{{{MARC21_NS}}}subfield[@code='b']")
    assert sf_a is not None and sf_a.text == "Main Title"
    assert sf_b is not None and sf_b.text == "an explanatory subtitle"


def test_emit_marcxml_omits_245_b_when_subtitle_absent() -> None:
    """No bffi:subtitle → no $b subfield (record stays in v0 shape)."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b123#Instance",
        bib_id="b123",
        title="Bare Title",
    )
    manifestation = next(g.subjects(RDF.type, BFFI.Manifestation))

    marcxml = emit_marcxml(g, manifestation=manifestation)
    root = etree.fromstring(marcxml)
    df245 = root.find(f"{{{MARC21_NS}}}datafield[@tag='245']")
    assert df245 is not None
    assert df245.find(f"{{{MARC21_NS}}}subfield[@code='b']") is None


def test_emit_marcxml_emits_245_n_p_when_part_number_and_name_present() -> None:
    """``bffi:partNumber`` and ``bffi:partName`` on the Title block emit
    as MARC 245 ``$n`` and ``$p`` respectively, between the main title
    ``$a`` and the subtitle ``$b`` per MARC 21 subfield order."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="15 ikivihreätä tangoa",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))
    title_block = next(g.objects(m, BFFI.title))
    g.add((title_block, BFFI.partNumber, Literal("4")))
    g.add((title_block, BFFI.partName, Literal("Iltarusko")))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    df245 = root.find(f"{{{MARC21_NS}}}datafield[@tag='245']")
    assert df245 is not None
    sf_codes = [sf.get("code") for sf in df245.findall(f"{{{MARC21_NS}}}subfield")]
    sf_values = [sf.text for sf in df245.findall(f"{{{MARC21_NS}}}subfield")]
    assert sf_codes == ["a", "n", "p"]
    assert sf_values == ["15 ikivihreätä tangoa", "4", "Iltarusko"]


def test_emit_marcxml_emits_245_c_when_responsibility_statement_present() -> None:
    """bffi:responsibilityStatement on the Manifestation maps to MARC 245
    $c (statement of responsibility — directors, screenwriters, etc.)."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b123#Instance",
        bib_id="b123",
        title="A Film",
    )
    manifestation = next(g.subjects(RDF.type, BFFI.Manifestation))
    g.add(
        (
            manifestation,
            BFFI.responsibilityStatement,
            Literal("directed by Guy Hamilton ; screenplay by Richard Maibaum"),
        )
    )

    marcxml = emit_marcxml(g, manifestation=manifestation)
    root = etree.fromstring(marcxml)
    df245 = root.find(f"{{{MARC21_NS}}}datafield[@tag='245']")
    sf_c = df245.find(f"{{{MARC21_NS}}}subfield[@code='c']") if df245 is not None else None
    assert sf_c is not None
    assert sf_c.text == "directed by Guy Hamilton ; screenplay by Richard Maibaum"


def test_emit_marcxml_emits_020_isbn_datafield() -> None:
    """An ISBN identifier block on the Manifestation produces a MARC
    020 datafield with the value in $a. The dispatch reads the
    bffi:source URI to pick the right MARC tag."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b123#Instance",
        bib_id="b123",
        title="A book",
    )
    manifestation = next(g.subjects(RDF.type, BFFI.Manifestation))
    isbn_block = URIRef("http://example.org/isbn-1")
    g.add((isbn_block, RDF.type, BFFI.Identifier))
    g.add(
        (
            isbn_block,
            BFFI.source,
            URIRef("http://id.loc.gov/vocabulary/identifiers/isbn"),
        )
    )
    g.add((isbn_block, RDF.value, Literal("9780123456789")))
    g.add((manifestation, BFFI.identifiedBy, isbn_block))

    marcxml = emit_marcxml(g, manifestation=manifestation)
    root = etree.fromstring(marcxml)
    df020 = root.find(f"{{{MARC21_NS}}}datafield[@tag='020']")
    assert df020 is not None
    sf_a = df020.find(f"{{{MARC21_NS}}}subfield[@code='a']")
    assert sf_a is not None
    assert sf_a.text == "9780123456789"


def test_emit_marcxml_emits_022_issn_datafield() -> None:
    """An ISSN identifier block produces a 022 datafield. Same dispatch
    pattern as ISBN — different bffi:source URI maps to a different
    MARC tag."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b123#Instance",
        bib_id="b123",
        title="A serial",
    )
    manifestation = next(g.subjects(RDF.type, BFFI.Manifestation))
    issn_block = URIRef("http://example.org/issn-1")
    g.add((issn_block, RDF.type, BFFI.Identifier))
    g.add(
        (
            issn_block,
            BFFI.source,
            URIRef("http://id.loc.gov/vocabulary/identifiers/issn"),
        )
    )
    g.add((issn_block, RDF.value, Literal("0028-0836")))
    g.add((manifestation, BFFI.identifiedBy, issn_block))

    marcxml = emit_marcxml(g, manifestation=manifestation)
    root = etree.fromstring(marcxml)
    df022 = root.find(f"{{{MARC21_NS}}}datafield[@tag='022']")
    assert df022 is not None
    sf_a = df022.find(f"{{{MARC21_NS}}}subfield[@code='a']")
    assert sf_a is not None
    assert sf_a.text == "0028-0836"


def test_emit_marcxml_emits_300_physical_description() -> None:
    """bffi:extent → bffi:Extent → rdfs:label produces MARC 300 \\$a;
    bffi:dimensions on the Manifestation produces \\$c. ISBD trailing
    punctuation (\" ;\") is added on $a when $c follows."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b123#Instance",
        bib_id="b123",
        title="t",
    )
    manifestation = next(g.subjects(RDF.type, BFFI.Manifestation))

    extent_block = URIRef("http://example.org/extent-1")
    g.add((extent_block, RDF.type, BFFI.Extent))
    g.add((extent_block, RDFS.label, Literal("136 pages")))
    g.add((manifestation, BFFI.extent, extent_block))
    g.add((manifestation, BFFI.dimensions, Literal("24 cm")))

    marcxml = emit_marcxml(g, manifestation=manifestation)
    root = etree.fromstring(marcxml)
    df300 = root.find(f"{{{MARC21_NS}}}datafield[@tag='300']")
    assert df300 is not None
    sf_a = df300.find(f"{{{MARC21_NS}}}subfield[@code='a']")
    sf_c = df300.find(f"{{{MARC21_NS}}}subfield[@code='c']")
    assert sf_a is not None and sf_a.text == "136 pages ;"
    assert sf_c is not None and sf_c.text == "24 cm"


def test_emit_marcxml_emits_300_b_from_extent_physical_note() -> None:
    """The Extent bnode's inner ``bffi:note`` typed
    ``<…/mnotetype/physical>`` emits as MARC 300 \\$b (other physical
    details — illustrations, colour, etc.). $a gets the ISBD trailing
    \" :\" when $b follows."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b123#Instance",
        bib_id="b123",
        title="t",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))
    extent = URIRef("http://example.org/extent-1")
    g.add((extent, RDF.type, BFFI.Extent))
    g.add((extent, RDFS.label, Literal("[16 sivua]")))
    physical_note = URIRef("http://example.org/extent-1-pnote")
    g.add((physical_note, RDF.type, BFFI.Note))
    g.add(
        (
            physical_note,
            RDF.type,
            URIRef("http://id.loc.gov/vocabulary/mnotetype/physical"),
        )
    )
    g.add((physical_note, RDFS.label, Literal("nid.")))
    g.add((extent, BFFI.note, physical_note))
    g.add((m, BFFI.extent, extent))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    df300 = root.find(f"{{{MARC21_NS}}}datafield[@tag='300']")
    assert df300 is not None
    sf_codes = [sf.get("code") for sf in df300.findall(f"{{{MARC21_NS}}}subfield")]
    sf_values = [sf.text for sf in df300.findall(f"{{{MARC21_NS}}}subfield")]
    assert sf_codes == ["a", "b"]
    assert sf_values == ["[16 sivua] :", "nid."]


def test_emit_marcxml_emits_300_e_from_manifestation_accmat_note() -> None:
    """A Manifestation ``bffi:note`` typed ``<…/mnotetype/accmat>``
    emits as MARC 300 \\$e (accompanying material) — NOT as a generic
    500 (the generic note walk skips accmat-typed notes so they don't
    double-emit)."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b123#Instance",
        bib_id="b123",
        title="t",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))
    extent = URIRef("http://example.org/extent-1")
    g.add((extent, RDF.type, BFFI.Extent))
    g.add((extent, RDFS.label, Literal("1 CD-levy")))
    g.add((m, BFFI.extent, extent))
    accmat = URIRef("http://example.org/accmat-1")
    g.add((accmat, RDF.type, BFFI.Note))
    g.add(
        (
            accmat,
            RDF.type,
            URIRef("http://id.loc.gov/vocabulary/mnotetype/accmat"),
        )
    )
    g.add((accmat, RDFS.label, Literal("esiteliite")))
    g.add((m, BFFI.note, accmat))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    df300 = root.find(f"{{{MARC21_NS}}}datafield[@tag='300']")
    assert df300 is not None
    sf_a = df300.find(f"{{{MARC21_NS}}}subfield[@code='a']")
    sf_e = df300.find(f"{{{MARC21_NS}}}subfield[@code='e']")
    assert sf_a is not None and sf_a.text == "1 CD-levy +"
    assert sf_e is not None and sf_e.text == "esiteliite"
    # No 500 for the accmat — it routes to 300 $e instead.
    assert root.find(f"{{{MARC21_NS}}}datafield[@tag='500']") is None


def test_emit_marcxml_omits_300_when_no_physical_data() -> None:
    """No extent or dimensions → no 300 datafield."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b123#Instance",
        bib_id="b123",
        title="t",
    )
    manifestation = next(g.subjects(RDF.type, BFFI.Manifestation))

    marcxml = emit_marcxml(g, manifestation=manifestation)
    root = etree.fromstring(marcxml)
    assert root.find(f"{{{MARC21_NS}}}datafield[@tag='300']") is None


def test_emit_marcxml_emits_041_language_codes() -> None:
    """bffi:language URIs (LoC language vocab) map to MARC 041 \\$a using
    the URI's 3-letter local name. Multiple languages → multiple \\$a
    subfields, sorted for determinism."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b123#Instance",
        bib_id="b123",
        title="t",
    )
    manifestation = next(g.subjects(RDF.type, BFFI.Manifestation))
    for code in ("eng", "fin", "swe"):
        g.add(
            (
                manifestation,
                BFFI.language,
                URIRef(f"http://id.loc.gov/vocabulary/languages/{code}"),
            )
        )

    marcxml = emit_marcxml(g, manifestation=manifestation)
    root = etree.fromstring(marcxml)
    df041 = root.find(f"{{{MARC21_NS}}}datafield[@tag='041']")
    assert df041 is not None
    sf_a_values = [sf.text for sf in df041.findall(f"{{{MARC21_NS}}}subfield[@code='a']")]
    assert sf_a_values == ["eng", "fin", "swe"]  # sorted


def test_emit_marcxml_emits_6xx_subject_datafields() -> None:
    """bffi:subject from a Work (reached via bffi:workManifested) emits
    MARC 6XX datafields. The subject node's URI fragment carries the
    source MARC tag (#Topic650-N → 650, #Place651-N → 651, etc.).
    rdfs:label on the subject becomes \\$a."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    manifestation = next(g.subjects(RDF.type, BFFI.Manifestation))

    work = URIRef("http://example.org/b1#Work")
    g.add((work, RDF.type, BFFI.BibframeWork))
    g.add((manifestation, BFFI.workManifested, work))

    topic = URIRef("http://example.org/b1#Topic650-1")
    g.add((topic, RDF.type, BFFI.Topic))
    g.add((topic, RDFS.label, Literal("Programming")))
    g.add((work, BFFI.subject, topic))

    place = URIRef("http://example.org/b1#Place651-2")
    g.add((place, RDF.type, BFFI.Place))
    g.add((place, RDFS.label, Literal("Finland")))
    g.add((work, BFFI.subject, place))

    marcxml = emit_marcxml(g, manifestation=manifestation)
    root = etree.fromstring(marcxml)
    df650 = root.find(f"{{{MARC21_NS}}}datafield[@tag='650']")
    df651 = root.find(f"{{{MARC21_NS}}}datafield[@tag='651']")
    assert df650 is not None
    assert df651 is not None
    assert df650.find(f"{{{MARC21_NS}}}subfield[@code='a']").text == "Programming"  # type: ignore[union-attr]
    assert df651.find(f"{{{MARC21_NS}}}subfield[@code='a']").text == "Finland"  # type: ignore[union-attr]


def test_emit_marcxml_emits_subject_2_from_bffi_source_and_sets_ind2_7() -> None:
    """A subject with ``bffi:source <…/subjectSchemes/yso>`` emits MARC
    ``$2 yso`` and ind2=7 (the MARC convention for "source specified in
    $2"). ind2 stays blank when no source signal is present."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    manifestation = next(g.subjects(RDF.type, BFFI.Manifestation))
    work = URIRef("http://example.org/b1#Work")
    g.add((work, RDF.type, BFFI.BibframeWork))
    g.add((manifestation, BFFI.workManifested, work))

    topic = URIRef("http://example.org/b1#Topic650-1")
    g.add((topic, RDF.type, BFFI.Topic))
    g.add((topic, RDFS.label, Literal("suomen kieli")))
    g.add((topic, BFFI.source, URIRef("http://id.loc.gov/vocabulary/subjectSchemes/ysa")))
    g.add((work, BFFI.subject, topic))

    marcxml = emit_marcxml(g, manifestation=manifestation)
    root = etree.fromstring(marcxml)
    df650 = root.find(f"{{{MARC21_NS}}}datafield[@tag='650']")
    assert df650 is not None
    assert df650.get("ind2") == "7"
    sf_a = df650.find(f"{{{MARC21_NS}}}subfield[@code='a']")
    sf_2 = df650.find(f"{{{MARC21_NS}}}subfield[@code='2']")
    assert sf_a is not None
    assert sf_2 is not None
    assert sf_a.text == "suomen kieli"
    assert sf_2.text == "ysa"
    # No $0 — the subject URI is a bib-internal mint, not an authority URI.
    assert df650.find(f"{{{MARC21_NS}}}subfield[@code='0']") is None


def test_emit_marcxml_emits_subject_0_from_external_authority_uri() -> None:
    """When the subject is an external authority URI (e.g. YSO concept),
    that URI emits as MARC ``$0``. The tag is derived from the
    ``rdf:type`` (``bffi:Topic`` → 650) since there's no
    bib-internal-fragment regex match to drive the tag."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    manifestation = next(g.subjects(RDF.type, BFFI.Manifestation))
    work = URIRef("http://example.org/b1#Work")
    g.add((work, RDF.type, BFFI.BibframeWork))
    g.add((manifestation, BFFI.workManifested, work))

    yso = URIRef("http://www.yso.fi/onto/yso/p12148")
    g.add((yso, RDF.type, BFFI.Topic))
    g.add((yso, RDFS.label, Literal("saksan kieli")))
    g.add((yso, BFFI.source, URIRef("http://id.loc.gov/vocabulary/subjectSchemes/yso")))
    g.add((work, BFFI.subject, yso))

    marcxml = emit_marcxml(g, manifestation=manifestation)
    root = etree.fromstring(marcxml)
    df650 = root.find(f"{{{MARC21_NS}}}datafield[@tag='650']")
    assert df650 is not None
    assert df650.get("ind2") == "7"
    sf_codes = [sf.get("code") for sf in df650.findall(f"{{{MARC21_NS}}}subfield")]
    sf_values = [sf.text for sf in df650.findall(f"{{{MARC21_NS}}}subfield")]
    assert sf_codes == ["a", "0", "2"]
    assert sf_values == [
        "saksan kieli",
        "http://www.yso.fi/onto/yso/p12148",
        "yso",
    ]


def test_emit_marcxml_subject_without_source_uses_blank_ind2() -> None:
    """A subject with no ``bffi:source`` keeps ind2=" " (the existing
    behaviour) — ind2=7 only kicks in when there's actually a $2 to
    point at."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    manifestation = next(g.subjects(RDF.type, BFFI.Manifestation))
    work = URIRef("http://example.org/b1#Work")
    g.add((work, RDF.type, BFFI.BibframeWork))
    g.add((manifestation, BFFI.workManifested, work))

    topic = URIRef("http://example.org/b1#Topic650-1")
    g.add((topic, RDF.type, BFFI.Topic))
    g.add((topic, RDFS.label, Literal("Programming")))
    g.add((work, BFFI.subject, topic))

    marcxml = emit_marcxml(g, manifestation=manifestation)
    root = etree.fromstring(marcxml)
    df650 = root.find(f"{{{MARC21_NS}}}datafield[@tag='650']")
    assert df650 is not None
    assert df650.get("ind2") == " "
    assert df650.find(f"{{{MARC21_NS}}}subfield[@code='2']") is None
    assert df650.find(f"{{{MARC21_NS}}}subfield[@code='0']") is None


def test_emit_marcxml_skips_subject_nodes_with_unrecognised_tags() -> None:
    """A subject node with a URI fragment outside the 6XX tag set
    (e.g. #Work730 for uniform-title added entry) is skipped by the
    subject routing — those land in their own follow-on commit."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    manifestation = next(g.subjects(RDF.type, BFFI.Manifestation))
    work = URIRef("http://example.org/b1#Work")
    g.add((work, RDF.type, BFFI.BibframeWork))
    g.add((manifestation, BFFI.workManifested, work))

    # An off-band node with a 7XX-style URI fragment.
    other = URIRef("http://example.org/b1#Work730-1")
    g.add((other, RDF.type, BFFI.Work))
    g.add((other, RDFS.label, Literal("Other work")))
    g.add((work, BFFI.subject, other))

    marcxml = emit_marcxml(g, manifestation=manifestation)
    root = etree.fromstring(marcxml)
    # No 730 emitted via the subject routing.
    assert root.find(f"{{{MARC21_NS}}}datafield[@tag='730']") is None


def test_emit_marcxml_emits_005_change_date() -> None:
    """``bffi:adminMetadata / bffi:changeDate`` → MARC 005 controlfield."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))
    admin = URIRef("http://example.org/b1#admin")
    g.add((admin, RDF.type, BFFI.AdminMetadata))
    g.add((admin, BFFI.changeDate, Literal("20260610154300.0")))
    g.add((m, BFFI.adminMetadata, admin))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    cf005 = root.find(f"{{{MARC21_NS}}}controlfield[@tag='005']")
    assert cf005 is not None
    assert cf005.text == "20260610154300.0"


def _add_publication_activity(
    g: Graph,
    manifestation: URIRef,
    *,
    place: str | None = None,
    agent: str | None = None,
    date: str | None = None,
) -> None:
    """Helper: attach a Publication-typed bffi:provisionActivity with
    the requested simple* parts to the Manifestation."""
    pa = URIRef(f"{manifestation}-pa")
    g.add((pa, RDF.type, BFFI.ProvisionActivity))
    g.add((pa, RDF.type, BFFI.Publication))
    if place is not None:
        g.add((pa, BFFI.simplePlace, Literal(place)))
    if agent is not None:
        g.add((pa, BFFI.simpleAgent, Literal(agent)))
    if date is not None:
        g.add((pa, BFFI.simpleDate, Literal(date)))
    g.add((manifestation, BFFI.provisionActivity, pa))


def test_emit_marcxml_emits_260_split_with_isbd_punctuation() -> None:
    """``bffi:provisionActivity`` (Publication-typed) carrying
    ``bffi:simplePlace`` / ``bffi:simpleAgent`` / ``bffi:simpleDate``
    drives MARC 260 ``$a`` / ``$b`` / ``$c``. ISBD trailing punctuation
    (\" :\" before $b, \",\" before $c) is added on emit so the result
    matches source-MARC cataloguer convention byte-for-byte."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))
    _add_publication_activity(g, m, place="Helsinki", agent="WSOY", date="2010")

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    df260 = root.find(f"{{{MARC21_NS}}}datafield[@tag='260']")
    assert df260 is not None
    sf_codes = [sf.get("code") for sf in df260.findall(f"{{{MARC21_NS}}}subfield")]
    sf_values = [sf.text for sf in df260.findall(f"{{{MARC21_NS}}}subfield")]
    assert sf_codes == ["a", "b", "c"]
    assert sf_values == ["Helsinki :", "WSOY,", "2010"]


def test_emit_marcxml_emits_260_falls_back_to_publication_statement_when_unstructured() -> None:
    """When no Publication-typed provisionActivity carries structured
    parts, the flat ``bffi:publicationStatement`` literal is the
    fallback — whole transcribed string emits in $a."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))
    g.add((m, BFFI.publicationStatement, Literal("Helsinki : WSOY, 2010")))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    df260 = root.find(f"{{{MARC21_NS}}}datafield[@tag='260']")
    assert df260 is not None
    sf_codes = [sf.get("code") for sf in df260.findall(f"{{{MARC21_NS}}}subfield")]
    sf_values = [sf.text for sf in df260.findall(f"{{{MARC21_NS}}}subfield")]
    assert sf_codes == ["a"]
    assert sf_values == ["Helsinki : WSOY, 2010"]


def test_emit_marcxml_emits_260_with_only_place_and_date() -> None:
    """When $b agent is absent but $c date is present, $a takes a
    trailing comma (the ISBD separator before $c)."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))
    _add_publication_activity(g, m, place="London", date="1999")

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    df260 = root.find(f"{{{MARC21_NS}}}datafield[@tag='260']")
    assert df260 is not None
    sf_codes = [sf.get("code") for sf in df260.findall(f"{{{MARC21_NS}}}subfield")]
    sf_values = [sf.text for sf in df260.findall(f"{{{MARC21_NS}}}subfield")]
    assert sf_codes == ["a", "c"]
    assert sf_values == ["London,", "1999"]


def test_emit_marcxml_emits_336_337_338_rda_descriptors() -> None:
    """bffi:content on the Work + bffi:media / bffi:carrier on the
    Manifestation each render as MARC 336 / 337 / 338. Each datafield
    carries the URI's ``rdfs:label`` in ``$a``, the 3-letter LoC code
    in ``$b``, and the scheme name in ``$2`` (rdacontent / rdamedia /
    rdacarrier — derived from the URI namespace)."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))
    work = URIRef("http://example.org/b1#Work")
    g.add((work, RDF.type, BFFI.BibframeWork))
    g.add((m, BFFI.workManifested, work))

    content_uri = URIRef("http://id.loc.gov/vocabulary/contentTypes/txt")
    g.add((work, BFFI.content, content_uri))
    g.add((content_uri, RDFS.label, Literal("text")))
    media_uri = URIRef("http://id.loc.gov/vocabulary/mediaTypes/n")
    g.add((m, BFFI.media, media_uri))
    g.add((media_uri, RDFS.label, Literal("unmediated")))
    carrier_uri = URIRef("http://id.loc.gov/vocabulary/carriers/nc")
    g.add((m, BFFI.carrier, carrier_uri))
    g.add((carrier_uri, RDFS.label, Literal("volume")))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    df336 = root.find(f"{{{MARC21_NS}}}datafield[@tag='336']")
    df337 = root.find(f"{{{MARC21_NS}}}datafield[@tag='337']")
    df338 = root.find(f"{{{MARC21_NS}}}datafield[@tag='338']")
    assert df336 is not None
    assert df337 is not None
    assert df338 is not None
    for df, scheme, label, code in [
        (df336, "rdacontent", "text", "txt"),
        (df337, "rdamedia", "unmediated", "n"),
        (df338, "rdacarrier", "volume", "nc"),
    ]:
        sf_a = df.find(f"{{{MARC21_NS}}}subfield[@code='a']")
        sf_b = df.find(f"{{{MARC21_NS}}}subfield[@code='b']")
        sf_2 = df.find(f"{{{MARC21_NS}}}subfield[@code='2']")
        assert sf_a is not None and sf_a.text == label
        assert sf_b is not None and sf_b.text == code
        assert sf_2 is not None and sf_2.text == scheme


def test_emit_marcxml_emits_336_without_subfield_a_when_uri_has_no_label() -> None:
    """When the RDA URI lacks an ``rdfs:label`` (rare — happens when the
    URI dictionary wasn't loaded), the emit drops ``$a`` but still
    writes ``$b`` (code) and ``$2`` (scheme) so the structural
    information survives."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))
    work = URIRef("http://example.org/b1#Work")
    g.add((work, RDF.type, BFFI.BibframeWork))
    g.add((m, BFFI.workManifested, work))
    g.add((work, BFFI.content, URIRef("http://id.loc.gov/vocabulary/contentTypes/txt")))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    df336 = root.find(f"{{{MARC21_NS}}}datafield[@tag='336']")
    assert df336 is not None
    assert df336.find(f"{{{MARC21_NS}}}subfield[@code='a']") is None
    assert df336.find(f"{{{MARC21_NS}}}subfield[@code='b']").text == "txt"  # type: ignore[union-attr]
    assert df336.find(f"{{{MARC21_NS}}}subfield[@code='2']").text == "rdacontent"  # type: ignore[union-attr]


def test_emit_marcxml_emits_500_general_notes() -> None:
    """Each ``bffi:note ?n . ?n rdfs:label ?text`` becomes a MARC 500 \\$a.
    Multiple notes produce repeated 500 datafields, sorted for
    determinism."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))
    note1 = URIRef("http://example.org/b1#note-1")
    g.add((note1, RDF.type, BFFI.Note))
    g.add((note1, RDFS.label, Literal("Includes index.")))
    g.add((m, BFFI.note, note1))
    note2 = URIRef("http://example.org/b1#note-2")
    g.add((note2, RDF.type, BFFI.Note))
    g.add((note2, RDFS.label, Literal("Bibliography: pp. 200-220.")))
    g.add((m, BFFI.note, note2))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    df500s = root.findall(f"{{{MARC21_NS}}}datafield[@tag='500']")
    assert len(df500s) == 2
    texts = sorted(
        df.find(f"{{{MARC21_NS}}}subfield[@code='a']").text  # type: ignore[union-attr]
        for df in df500s
    )
    assert texts == ["Bibliography: pp. 200-220.", "Includes index."]


def test_emit_marcxml_dispatches_mnotetype_lang_to_546() -> None:
    """A ``bffi:Note`` co-typed ``<…/mnotetype/lang>`` emits as MARC 546
    (language note), not as a generic 500. The discriminator is the
    note's mnotetype rdf:type — preserved by marc2bibframe2 from
    source 546 records."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))
    note = URIRef("http://example.org/b1#note-1")
    g.add((note, RDF.type, BFFI.Note))
    g.add((note, RDF.type, URIRef("http://id.loc.gov/vocabulary/mnotetype/lang")))
    g.add((note, RDFS.label, Literal("Tekstitys: suomi, svenska, englanti")))
    g.add((m, BFFI.note, note))
    # Plus a generic note that should still route to 500.
    note_general = URIRef("http://example.org/b1#note-2")
    g.add((note_general, RDF.type, BFFI.Note))
    g.add((note_general, RDFS.label, Literal("Includes index.")))
    g.add((m, BFFI.note, note_general))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    df546 = root.find(f"{{{MARC21_NS}}}datafield[@tag='546']")
    df500 = root.find(f"{{{MARC21_NS}}}datafield[@tag='500']")
    assert df546 is not None
    assert df500 is not None
    assert df546.find(f"{{{MARC21_NS}}}subfield[@code='a']").text == (  # type: ignore[union-attr]
        "Tekstitys: suomi, svenska, englanti"
    )
    assert df500.find(f"{{{MARC21_NS}}}subfield[@code='a']").text == "Includes index."  # type: ignore[union-attr]


def test_emit_marcxml_emits_505_from_table_of_contents() -> None:
    """``bffi:tableOfContents [a bffi:TableOfContents ; rdfs:label ?text]``
    emits as MARC 505 ind1=0 \\$a — the formatted contents note."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))
    toc = URIRef("http://example.org/b1#toc-1")
    g.add((toc, RDF.type, BFFI.TableOfContents))
    g.add((toc, RDFS.label, Literal("Chapter 1. — Chapter 2. — Chapter 3.")))
    g.add((m, BFFI.tableOfContents, toc))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    df505 = root.find(f"{{{MARC21_NS}}}datafield[@tag='505']")
    assert df505 is not None
    assert df505.get("ind1") == "0"
    sf_a = df505.find(f"{{{MARC21_NS}}}subfield[@code='a']")
    assert sf_a is not None
    assert sf_a.text == "Chapter 1. — Chapter 2. — Chapter 3."


def test_emit_marcxml_emits_084_classification() -> None:
    """``?work bffi:classification [bffi:classificationPortion ?num]``
    produces MARC 084 \\$a with the classification number."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))
    work = URIRef("http://example.org/b1#Work")
    g.add((work, RDF.type, BFFI.BibframeWork))
    g.add((m, BFFI.workManifested, work))
    cls_block = URIRef("http://example.org/b1#cls-1")
    g.add((cls_block, RDF.type, BFFI.Classification))
    g.add((cls_block, BFFI.classificationPortion, Literal("82.3")))
    g.add((work, BFFI.classification, cls_block))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    df084 = root.find(f"{{{MARC21_NS}}}datafield[@tag='084']")
    assert df084 is not None
    sf_a = df084.find(f"{{{MARC21_NS}}}subfield[@code='a']")
    assert sf_a is not None
    assert sf_a.text == "82.3"
    # Without bffi:source, no $2 subfield emits.
    assert df084.find(f"{{{MARC21_NS}}}subfield[@code='2']") is None


def test_emit_marcxml_emits_084_scheme_code_in_subfield_2() -> None:
    """``bffi:Classification`` with ``bffi:source [a bffi:Source ; bffi:code "ykl"]``
    produces MARC 084 ``$a 82.3 $2 ykl``. This is the Helmet-canonical
    shape — every 084 in the corpus carries a scheme code; without ``$2``
    the reconstructed record loses the scheme attribution."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))
    work = URIRef("http://example.org/b1#Work")
    g.add((work, RDF.type, BFFI.BibframeWork))
    g.add((m, BFFI.workManifested, work))
    cls_block = URIRef("http://example.org/b1#cls-1")
    g.add((cls_block, RDF.type, BFFI.Classification))
    g.add((cls_block, BFFI.classificationPortion, Literal("82.3")))
    src = URIRef("http://example.org/b1#cls-1-src")
    g.add((src, RDF.type, BFFI.Source))
    g.add((src, BFFI.code, Literal("ykl")))
    g.add((cls_block, BFFI.source, src))
    g.add((work, BFFI.classification, cls_block))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    df084 = root.find(f"{{{MARC21_NS}}}datafield[@tag='084']")
    assert df084 is not None
    sf_a = df084.find(f"{{{MARC21_NS}}}subfield[@code='a']")
    sf_2 = df084.find(f"{{{MARC21_NS}}}subfield[@code='2']")
    assert sf_a is not None
    assert sf_2 is not None
    assert sf_a.text == "82.3"
    assert sf_2.text == "ykl"


def test_emit_marcxml_emits_100_for_primary_personal_contributor() -> None:
    """``bffi:PrimaryContribution`` with a ``bffi:Person`` agent emits
    MARC 100 \\$a (with the role's LoC relator code in \\$4)."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))
    work = URIRef("http://example.org/b1#Work")
    g.add((work, RDF.type, BFFI.BibframeWork))
    g.add((m, BFFI.workManifested, work))

    contrib = URIRef("http://example.org/b1#contrib-1")
    g.add((contrib, RDF.type, BFFI.Contribution))
    g.add((contrib, RDF.type, BFFI.PrimaryContribution))
    g.add((work, BFFI.contribution, contrib))
    agent = URIRef("http://example.org/b1#agent-1")
    g.add((agent, RDF.type, BFFI.Person))
    g.add((agent, RDFS.label, Literal("Auster, Paul")))
    g.add((contrib, BFFI.agent, agent))
    g.add((contrib, BFFI.role, URIRef("http://id.loc.gov/vocabulary/relators/aut")))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    df100 = root.find(f"{{{MARC21_NS}}}datafield[@tag='100']")
    assert df100 is not None
    assert df100.find(f"{{{MARC21_NS}}}subfield[@code='a']").text == "Auster, Paul"  # type: ignore[union-attr]
    assert df100.find(f"{{{MARC21_NS}}}subfield[@code='4']").text == "aut"  # type: ignore[union-attr]


def test_emit_marcxml_emits_700_with_relator_term_in_subfield_e() -> None:
    """When ``bffi:role`` is a bnode with ``rdfs:label``, the cataloguer's
    free-text relator term (e.g. Finnish ``"näyttelijä"``) emits as MARC
    ``$e``. This is the high-volume Helmet shape: most 700 contributors
    carry ``$e`` from the source but no ``$4`` URI."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))
    work = URIRef("http://example.org/b1#Work")
    g.add((work, RDF.type, BFFI.BibframeWork))
    g.add((m, BFFI.workManifested, work))

    contrib = URIRef("http://example.org/b1#contrib-1")
    g.add((contrib, RDF.type, BFFI.Contribution))
    g.add((work, BFFI.contribution, contrib))
    agent = URIRef("http://example.org/b1#agent-1")
    g.add((agent, RDF.type, BFFI.Person))
    g.add((agent, RDFS.label, Literal("Connery, Sean,")))
    g.add((contrib, BFFI.agent, agent))
    role = URIRef("http://example.org/b1#role-1")
    g.add((role, RDF.type, BFFI.Role))
    g.add((role, RDFS.label, Literal("näyttelijä")))
    g.add((contrib, BFFI.role, role))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    df700 = root.find(f"{{{MARC21_NS}}}datafield[@tag='700']")
    assert df700 is not None
    sf_a = df700.find(f"{{{MARC21_NS}}}subfield[@code='a']")
    sf_e = df700.find(f"{{{MARC21_NS}}}subfield[@code='e']")
    assert sf_a is not None
    assert sf_e is not None
    assert sf_a.text == "Connery, Sean,"
    assert sf_e.text == "näyttelijä"
    # No $4 (no LoC relator URI in this shape).
    assert df700.find(f"{{{MARC21_NS}}}subfield[@code='4']") is None
    # Order: $a then $e (MARC X00 subfield convention).
    sf_codes = [sf.get("code") for sf in df700.findall(f"{{{MARC21_NS}}}subfield")]
    assert sf_codes == ["a", "e"]


def test_emit_marcxml_emits_700_with_both_e_and_4_when_both_signals_present() -> None:
    """A contribution carrying both a bnode-with-label role AND a LoC
    relator URI on ``bffi:role`` emits ``$a $e $4`` in MARC order — the
    free-text term and the relator code coexist when source MARC had
    both ``$e`` and ``$4``."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))
    work = URIRef("http://example.org/b1#Work")
    g.add((work, RDF.type, BFFI.BibframeWork))
    g.add((m, BFFI.workManifested, work))

    contrib = URIRef("http://example.org/b1#contrib-1")
    g.add((contrib, RDF.type, BFFI.Contribution))
    g.add((work, BFFI.contribution, contrib))
    agent = URIRef("http://example.org/b1#agent-1")
    g.add((agent, RDF.type, BFFI.Person))
    g.add((agent, RDFS.label, Literal("Hamilton, Guy,")))
    g.add((contrib, BFFI.agent, agent))
    role_bnode = URIRef("http://example.org/b1#role-1")
    g.add((role_bnode, RDF.type, BFFI.Role))
    g.add((role_bnode, RDFS.label, Literal("ohjaaja")))
    g.add((contrib, BFFI.role, role_bnode))
    g.add((contrib, BFFI.role, URIRef("http://id.loc.gov/vocabulary/relators/drt")))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    df700 = root.find(f"{{{MARC21_NS}}}datafield[@tag='700']")
    assert df700 is not None
    sf_codes = [sf.get("code") for sf in df700.findall(f"{{{MARC21_NS}}}subfield")]
    sf_values = [sf.text for sf in df700.findall(f"{{{MARC21_NS}}}subfield")]
    assert sf_codes == ["a", "e", "4"]
    assert sf_values == ["Hamilton, Guy,", "ohjaaja", "drt"]


def test_emit_marcxml_emits_700_with_analytical_title_from_marckey() -> None:
    """For analytical 700 entries (source ind2=2) the agent's
    ``bffi:marcKey`` preserves the full source row verbatim — including
    ``$t`` (title within work). The reverse converter parses marcKey
    and emits any subfield code beyond ``$a`` / ``$e`` / ``$4``
    (typically ``$t``, sometimes ``$c`` / ``$d``) plus the source
    indicators."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))
    work = URIRef("http://example.org/b1#Work")
    g.add((work, RDF.type, BFFI.BibframeWork))
    g.add((m, BFFI.workManifested, work))

    contrib = URIRef("http://example.org/b1#contrib-1")
    g.add((contrib, RDF.type, BFFI.Contribution))
    g.add((work, BFFI.contribution, contrib))
    agent = URIRef("http://example.org/b1#agent-1")
    g.add((agent, RDF.type, BFFI.Person))
    g.add((agent, RDFS.label, Literal("Tikka, Eeva")))
    g.add((agent, BFFI.marcKey, Literal("70012$aTikka, Eeva.$tVarjolaiva")))
    g.add((contrib, BFFI.agent, agent))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    df700 = root.find(f"{{{MARC21_NS}}}datafield[@tag='700']")
    assert df700 is not None
    # Indicators come from marcKey: ind1=1 (surname), ind2=2 (analytical).
    assert df700.get("ind1") == "1"
    assert df700.get("ind2") == "2"
    sf_codes = [sf.get("code") for sf in df700.findall(f"{{{MARC21_NS}}}subfield")]
    sf_values = [sf.text for sf in df700.findall(f"{{{MARC21_NS}}}subfield")]
    assert sf_codes == ["a", "t"]
    # $a still comes from the structured rdfs:label (without source's
    # trailing punctuation); $t comes from marcKey verbatim.
    assert sf_values == ["Tikka, Eeva", "Varjolaiva"]


def test_emit_marcxml_emits_710_for_added_corporate_contributor() -> None:
    """A non-primary ``bffi:Contribution`` with a ``bffi:Organization``
    agent emits MARC 710 \\$a — the added corporate entry."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))
    work = URIRef("http://example.org/b1#Work")
    g.add((work, RDF.type, BFFI.BibframeWork))
    g.add((m, BFFI.workManifested, work))

    contrib = URIRef("http://example.org/b1#contrib-1")
    g.add((contrib, RDF.type, BFFI.Contribution))  # not Primary
    g.add((work, BFFI.contribution, contrib))
    agent = URIRef("http://example.org/b1#agent-1")
    g.add((agent, RDF.type, BFFI.Organization))
    g.add((agent, RDFS.label, Literal("Helsingin yliopisto")))
    g.add((contrib, BFFI.agent, agent))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    df710 = root.find(f"{{{MARC21_NS}}}datafield[@tag='710']")
    assert df710 is not None
    assert df710.find(f"{{{MARC21_NS}}}subfield[@code='a']").text == "Helsingin yliopisto"  # type: ignore[union-attr]
    # No role → no $4
    assert df710.find(f"{{{MARC21_NS}}}subfield[@code='4']") is None


def test_emit_marcxml_emits_730_from_relation_chain() -> None:
    """A bffi:relation chain pointing at a Hub-routed Work whose
    bffi:marcKey begins with '730' produces a MARC 730 datafield with
    every subfield carried by the marcKey, in order. The discriminator
    is the marcKey tag prefix."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))

    rel = URIRef("http://example.org/b1#rel-1")
    g.add((rel, RDF.type, BFFI.Relation))
    g.add((m, BFFI.relation, rel))

    hub = URIRef("http://example.org/b1#Hub730-1")
    g.add((hub, RDF.type, BFFI.Work))
    g.add((hub, BFFI.marcKey, Literal("7300 $aAngel /$gChild, Desmond")))
    g.add((rel, BFFI.associatedResource, hub))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    df730 = root.find(f"{{{MARC21_NS}}}datafield[@tag='730']")
    assert df730 is not None
    assert df730.get("ind1") == "0"
    assert df730.get("ind2") == " "
    sf_a = df730.find(f"{{{MARC21_NS}}}subfield[@code='a']")
    sf_g = df730.find(f"{{{MARC21_NS}}}subfield[@code='g']")
    assert sf_a is not None
    assert sf_g is not None
    assert sf_a.text == "Angel /"
    assert sf_g.text == "Child, Desmond"


def test_emit_marcxml_preserves_730_indicators_and_extra_subfields() -> None:
    """Indicators (especially the nonfiling-character count in ind1) and
    every subfield in marcKey survive verbatim into the emitted 730 —
    not just $a. This is the marcKey-driven recovery path: BFFI has no
    structured predicate for $g/$l/$n/$o/$p, so we parse them from the
    preserved marcKey literal."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))

    rel = URIRef("http://example.org/b1#rel-1")
    g.add((rel, RDF.type, BFFI.Relation))
    g.add((m, BFFI.relation, rel))

    hub = URIRef("http://example.org/b1#Hub730-1")
    g.add((hub, RDF.type, BFFI.Work))
    g.add(
        (
            hub,
            BFFI.marcKey,
            Literal("7304 $aThe symphony,$nno. 5,$lEnglish$osel.$gop. 67"),
        )
    )
    g.add((rel, BFFI.associatedResource, hub))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    df730 = root.find(f"{{{MARC21_NS}}}datafield[@tag='730']")
    assert df730 is not None
    # ind1=4 → "The " is 4 nonfiling characters in source MARC.
    assert df730.get("ind1") == "4"
    assert df730.get("ind2") == " "

    sf_codes = [sf.get("code") for sf in df730.findall(f"{{{MARC21_NS}}}subfield")]
    sf_values = [sf.text for sf in df730.findall(f"{{{MARC21_NS}}}subfield")]
    assert sf_codes == ["a", "n", "l", "o", "g"]
    assert sf_values == [
        "The symphony,",
        "no. 5,",
        "English",
        "sel.",
        "op. 67",
    ]


def test_emit_marcxml_preserves_740_nonfiling_indicator() -> None:
    """MARC 740 records with leading articles ("The making of Goldfinger")
    have ind1 set to the nonfiling-character count. The marcKey-driven
    parser preserves it; hard-coding ind1="0" would corrupt round-trip."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))

    rel = URIRef("http://example.org/b1#rel-1")
    g.add((rel, RDF.type, BFFI.Relation))
    g.add((m, BFFI.relation, rel))

    work = URIRef("http://example.org/b1#Hub740-1")
    g.add((work, RDF.type, BFFI.BibframeWork))
    g.add((work, BFFI.marcKey, Literal("7404 $aThe making of Goldfinger")))
    g.add((rel, BFFI.associatedResource, work))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    df740 = root.find(f"{{{MARC21_NS}}}datafield[@tag='740']")
    assert df740 is not None
    assert df740.get("ind1") == "4"
    sf_a = df740.find(f"{{{MARC21_NS}}}subfield[@code='a']")
    assert sf_a is not None
    assert sf_a.text == "The making of Goldfinger"


def test_emit_marcxml_ignores_relation_targets_without_730_or_740_marckey() -> None:
    """Relation chains pointing at series/accompanied-by/etc. targets
    (marcKey beginning with 4XX/5XX/etc.) are NOT emitted as 730/740 —
    those routings own those tags separately."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b1#Instance",
        bib_id="b1",
        title="t",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))

    rel = URIRef("http://example.org/b1#rel-1")
    g.add((rel, RDF.type, BFFI.Relation))
    g.add((m, BFFI.relation, rel))
    series_work = URIRef("http://example.org/b1#Hub830-1")
    g.add((series_work, RDF.type, BFFI.SeriesWork))
    g.add((series_work, BFFI.marcKey, Literal("8300 $aSome series")))
    g.add((rel, BFFI.associatedResource, series_work))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    assert root.find(f"{{{MARC21_NS}}}datafield[@tag='730']") is None
    assert root.find(f"{{{MARC21_NS}}}datafield[@tag='740']") is None


def test_emit_marcxml_emits_024_with_indicator_per_scheme() -> None:
    """UPC / ISMN / EAN identifier blocks all emit as MARC 024 with the
    correct ind1: 1=UPC, 2=ISMN, 3=EAN. This is the LoC convention."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b123#Instance",
        bib_id="b123",
        title="t",
    )
    manifestation = next(g.subjects(RDF.type, BFFI.Manifestation))
    for scheme, value in [
        ("upc", "123456"),
        ("ismn", "M-2306-7118-7"),
        ("ean", "6420614617359"),
    ]:
        block = URIRef(f"http://example.org/{scheme}-1")
        g.add((block, RDF.type, BFFI.Identifier))
        g.add(
            (
                block,
                BFFI.source,
                URIRef(f"http://id.loc.gov/vocabulary/identifiers/{scheme}"),
            )
        )
        g.add((block, RDF.value, Literal(value)))
        g.add((manifestation, BFFI.identifiedBy, block))

    marcxml = emit_marcxml(g, manifestation=manifestation)
    root = etree.fromstring(marcxml)
    df024s = root.findall(f"{{{MARC21_NS}}}datafield[@tag='024']")
    assert len(df024s) == 3
    by_ind1 = {df.get("ind1"): df for df in df024s}
    assert set(by_ind1.keys()) == {"1", "2", "3"}
    assert by_ind1["1"].find(f"{{{MARC21_NS}}}subfield[@code='a']").text == "123456"  # type: ignore[union-attr]
    assert by_ind1["2"].find(f"{{{MARC21_NS}}}subfield[@code='a']").text == "M-2306-7118-7"  # type: ignore[union-attr]
    assert by_ind1["3"].find(f"{{{MARC21_NS}}}subfield[@code='a']").text == "6420614617359"  # type: ignore[union-attr]


def test_emit_marcxml_emits_028_with_assigner_in_subfield_b() -> None:
    """Audio-issue-number identifier → MARC 028 ind1=0 ind2=1 \\$a value
    \\$b assigner-name (e.g. ``$b MGM DVD $a 16197-58``). The dispatch
    reads ``bffi:assigner`` for the publisher / issuing-body label."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b123#Instance",
        bib_id="b123",
        title="t",
    )
    m = next(g.subjects(RDF.type, BFFI.Manifestation))
    ident = URIRef("http://example.org/audio-1")
    g.add((ident, RDF.type, BFFI.Identifier))
    g.add(
        (
            ident,
            BFFI.source,
            URIRef("http://id.loc.gov/vocabulary/identifiers/audio-issue-number"),
        )
    )
    g.add((ident, RDF.value, Literal("16197-58")))
    assigner = URIRef("http://example.org/assigner-1")
    g.add((assigner, RDF.type, BFFI.Organization))
    g.add((assigner, RDFS.label, Literal("MGM DVD")))
    g.add((ident, BFFI.assigner, assigner))
    g.add((m, BFFI.identifiedBy, ident))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    df028 = root.find(f"{{{MARC21_NS}}}datafield[@tag='028']")
    assert df028 is not None
    assert df028.get("ind1") == "0"
    assert df028.get("ind2") == "1"
    sf_a = df028.find(f"{{{MARC21_NS}}}subfield[@code='a']")
    sf_b = df028.find(f"{{{MARC21_NS}}}subfield[@code='b']")
    assert sf_a is not None and sf_a.text == "16197-58"
    assert sf_b is not None and sf_b.text == "MGM DVD"


def test_emit_marcxml_skips_identifiers_with_unmapped_source() -> None:
    """Identifier blocks with a bffi:source URI not in the dispatch
    table are skipped — those land in their own follow-on commits.
    The Local block (which carries the bib ID for 001) is also skipped
    here; it has no bffi:source URI in the dispatch table."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b123#Instance",
        bib_id="b123",
        title="t",
    )
    manifestation = next(g.subjects(RDF.type, BFFI.Manifestation))
    other_block = URIRef("http://example.org/other-1")
    g.add((other_block, RDF.type, BFFI.Identifier))
    g.add(
        (
            other_block,
            BFFI.source,
            URIRef("http://id.loc.gov/vocabulary/identifiers/some-future-scheme"),
        )
    )
    g.add((other_block, RDF.value, Literal("xyz-123")))
    g.add((manifestation, BFFI.identifiedBy, other_block))

    marcxml = emit_marcxml(g, manifestation=manifestation)
    root = etree.fromstring(marcxml)
    assert root.find(f"{{{MARC21_NS}}}datafield[@tag='020']") is None
    assert root.find(f"{{{MARC21_NS}}}datafield[@tag='022']") is None
    assert root.find(f"{{{MARC21_NS}}}datafield[@tag='024']") is None
    assert root.find(f"{{{MARC21_NS}}}datafield[@tag='028']") is None


def test_emit_marcxml_falls_back_to_uri_fragment_when_no_local_block() -> None:
    """The BIBFRAME emit shape from marc2bibframe2 puts the bib ID in the
    URI path component. If no Local identifier exists in the graph, the
    converter still recovers the bib ID from the URI."""
    g = Graph()
    m = URIRef("http://urn.fi/URN:NBN:fi:bib:b10068004#Instance")
    g.add((m, RDF.type, BFFI.Manifestation))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    cf001 = root.find(f"{{{MARC21_NS}}}controlfield[@tag='001']")
    assert cf001 is not None
    assert cf001.text == "b10068004"


def test_emit_marcxml_prefers_local_block_over_uri_fragment() -> None:
    """When both signals exist, the Local block wins — it's the canonical
    Helmet bib-ID carrier. The URI fragment is a fallback."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/other-uri-id#Instance",
        bib_id="b-canonical",
        title="Doesn't matter",
    )
    manifestation = next(g.subjects(RDF.type, BFFI.Manifestation))
    marcxml = emit_marcxml(g, manifestation=manifestation)
    root = etree.fromstring(marcxml)
    cf001 = root.find(f"{{{MARC21_NS}}}controlfield[@tag='001']")
    assert cf001 is not None
    assert cf001.text == "b-canonical"


def test_emit_marcxml_omits_245_when_no_title_present() -> None:
    """A Manifestation without a title still emits a valid record — the
    245 datafield is dropped rather than emitted empty."""
    g = Graph()
    m = URIRef("http://example.org/b777#Instance")
    g.add((m, RDF.type, BFFI.Manifestation))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    assert root.find(f"{{{MARC21_NS}}}datafield[@tag='245']") is None


# --- convert_one + convert_corpus ----------------------------------------


def test_convert_one_round_trips_marc_to_bffi_to_marc(tmp_path: Path) -> None:
    """End-to-end round-trip on the vendored marc.xml: the bib ID survives
    MARC -> BIBFRAME -> BFFI -> MARC (every stage so far)."""
    bffi_path = _produce_bffi_fixture(tmp_path, stem="test")

    out_dir = tmp_path / "marc-out"
    output_path = convert_one(
        bffi_path,
        options=ConversionOptions(input_dir=tmp_path, output_dir=out_dir),
    )
    assert output_path == out_dir / "test.marcxml"
    assert output_path.exists()

    root = etree.fromstring(output_path.read_bytes())
    cf001 = root.find(f"{{{MARC21_NS}}}controlfield[@tag='001']")
    assert cf001 is not None
    # The vendored marc.xml carries 001 = 13600108.
    assert cf001.text == "13600108"


def test_convert_one_round_trips_main_title(tmp_path: Path) -> None:
    """Same fixture: the 245$a text survives the full pipeline."""
    bffi_path = _produce_bffi_fixture(tmp_path, stem="test")
    out_dir = tmp_path / "marc-out"
    output_path = convert_one(
        bffi_path,
        options=ConversionOptions(input_dir=tmp_path, output_dir=out_dir),
    )
    root = etree.fromstring(output_path.read_bytes())
    sf_a = root.find(f"{{{MARC21_NS}}}datafield[@tag='245']/{{{MARC21_NS}}}subfield[@code='a']")
    assert sf_a is not None
    # The vendored marc.xml's main title (transmitted via bf:mainTitle).
    assert sf_a.text == "Ole Lukøie"


def test_convert_one_raises_when_no_manifestation(tmp_path: Path) -> None:
    bffi_path = tmp_path / "empty.bffi.ttl"
    bffi_path.write_text(
        "@prefix bffi: <http://urn.fi/URN:NBN:fi:schema:bffi:> .\n", encoding="utf-8"
    )
    with pytest.raises(BffiToMarcError, match="no bffi:Manifestation"):
        convert_one(
            bffi_path,
            options=ConversionOptions(input_dir=tmp_path, output_dir=tmp_path),
        )


def test_convert_corpus_summary_and_sidecar_events(tmp_path: Path) -> None:
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    src = _produce_bffi_fixture(in_dir, stem="test")
    for stem in ("a", "b"):
        shutil.copy(src, in_dir / f"{stem}.bffi.ttl")
    src.unlink()
    # Clean up the bibframe artifact from the fixture helper so it doesn't
    # confuse the corpus walk.
    (in_dir / "test.bibframe.xml").unlink(missing_ok=True)

    out_dir = tmp_path / "out"
    sidecar = tmp_path / "stage-events.jsonl"
    emitter = StageEventEmitter(sidecar_path=sidecar, run_uuid="test-run")
    set_active_emitter(emitter)
    try:
        summary = convert_corpus(options=ConversionOptions(input_dir=in_dir, output_dir=out_dir))
    finally:
        set_active_emitter(None)

    assert summary.total == 2
    assert summary.converted == 2
    assert summary.failed == 0
    assert (out_dir / "a.marcxml").exists()
    assert (out_dir / "b.marcxml").exists()

    events = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines()]
    assert all(e["stage"] == "bffi2marc" for e in events)
    start = next(e for e in events if e["event"] == "start")
    assert start["counters"]["entities_total"] == 2
    end = next(e for e in events if e["event"] == "end")
    assert end["counters"]["success"] == 2
    assert end["counters"]["failed"] == 0


@pytest.fixture(autouse=True)
def _clear_active_emitter() -> None:
    yield
    set_active_emitter(None)
