"""Unit tests for the BFFI -> MARCXML reverse converter (v0)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from lxml import etree
from rdflib import RDF, Graph, Literal, URIRef

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
