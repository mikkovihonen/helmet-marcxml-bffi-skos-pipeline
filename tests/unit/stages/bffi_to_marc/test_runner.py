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
    bffi:dimensions on the Manifestation produces \\$c."""
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
    assert sf_a is not None and sf_a.text == "136 pages"
    assert sf_c is not None and sf_c.text == "24 cm"


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


def test_emit_marcxml_emits_260_publication_statement() -> None:
    """``bffi:publicationStatement`` literal → MARC 260 \\$a (full statement)."""
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
    sf_a = df260.find(f"{{{MARC21_NS}}}subfield[@code='a']")
    assert sf_a is not None
    assert sf_a.text == "Helsinki : WSOY, 2010"


def test_emit_marcxml_emits_336_337_338_rda_descriptors() -> None:
    """bffi:content on the Work + bffi:media / bffi:carrier on the
    Manifestation each render as MARC 336 / 337 / 338 with the
    3-letter LoC code in \\$a."""
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
            URIRef("http://id.loc.gov/vocabulary/contentTypes/txt"),
        )
    )
    g.add((m, BFFI.media, URIRef("http://id.loc.gov/vocabulary/mediaTypes/n")))
    g.add((m, BFFI.carrier, URIRef("http://id.loc.gov/vocabulary/carriers/nc")))

    marcxml = emit_marcxml(g, manifestation=m)
    root = etree.fromstring(marcxml)
    df336 = root.find(f"{{{MARC21_NS}}}datafield[@tag='336']")
    df337 = root.find(f"{{{MARC21_NS}}}datafield[@tag='337']")
    df338 = root.find(f"{{{MARC21_NS}}}datafield[@tag='338']")
    assert df336 is not None and df336.find(f"{{{MARC21_NS}}}subfield[@code='a']").text == "txt"  # type: ignore[union-attr]
    assert df337 is not None and df337.find(f"{{{MARC21_NS}}}subfield[@code='a']").text == "n"  # type: ignore[union-attr]
    assert df338 is not None and df338.find(f"{{{MARC21_NS}}}subfield[@code='a']").text == "nc"  # type: ignore[union-attr]


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


def test_emit_marcxml_skips_unsupported_identifier_schemes() -> None:
    """Identifier blocks with a bffi:source URI not in the dispatch
    table are skipped — those land in their own follow-on commits.
    The Local block (which carries the bib ID for 001) is also skipped
    here; it has no bffi:source URI in the dispatch table."""
    g = _build_minimal_bffi_graph(
        manifestation_uri="http://example.org/b123#Instance",
        bib_id="b123",  # produces a bffi:Local identifier block
        title="t",
    )
    manifestation = next(g.subjects(RDF.type, BFFI.Manifestation))
    # Add a UPC identifier (not yet in the dispatch table).
    upc_block = URIRef("http://example.org/upc-1")
    g.add((upc_block, RDF.type, BFFI.Identifier))
    g.add(
        (
            upc_block,
            BFFI.source,
            URIRef("http://id.loc.gov/vocabulary/identifiers/upc"),
        )
    )
    g.add((upc_block, RDF.value, Literal("123456")))
    g.add((manifestation, BFFI.identifiedBy, upc_block))

    marcxml = emit_marcxml(g, manifestation=manifestation)
    root = etree.fromstring(marcxml)
    # No 020 / 022 / 024 — UPC is not yet dispatched.
    assert root.find(f"{{{MARC21_NS}}}datafield[@tag='020']") is None
    assert root.find(f"{{{MARC21_NS}}}datafield[@tag='022']") is None
    assert root.find(f"{{{MARC21_NS}}}datafield[@tag='024']") is None


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
