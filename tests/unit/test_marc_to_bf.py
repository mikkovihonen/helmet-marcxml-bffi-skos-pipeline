"""Unit tests for stages/marc_to_bf — the in-tree sanitisers.

End-to-end MARCXML → BIBFRAME conversion is exercised by integration
tests under ``tests/integration/``; this module covers the small in-
process repair functions that run between the XSLT and rdflib's RDF/XML
parser.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from lxml import etree

from bffi_pipeline.stages.marc_to_bf import (
    ConversionErrorRow,
    _emit_errors_tsv,
    _sanitize_language_tags,
)

_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def _parse(xml: bytes) -> etree._ElementTree:
    return etree.parse(BytesIO(xml))


def test_sanitize_language_tags_strips_trailing_hyphen() -> None:
    """``ru-`` → ``ru``; valid tags untouched; element without xml:lang
    untouched. P-02 5k run found 7 / 5000 records emitting ``ru-`` /
    ``uk-`` from marc2bibframe2's 008-country → BCP-47 mapping."""
    src = b"""<?xml version="1.0"?>
<root xmlns:xml="http://www.w3.org/XML/1998/namespace">
  <a xml:lang="ru-">value</a>
  <b xml:lang="ru">value</b>
  <c xml:lang="ru-RU">value</c>
  <d xml:lang="uk-">value</d>
  <e>no attr</e>
  <f xml:lang="ru--">double dash trailing</f>
</root>"""
    tree = _parse(src)
    fixed = _sanitize_language_tags(tree)
    # ru-, uk-, ru-- are rewritten; ru and ru-RU stay; e has no attr.
    assert fixed == 3
    root = tree.getroot()
    langs = [el.get(_XML_LANG) for el in root]
    assert langs == ["ru", "ru", "ru-RU", "uk", None, "ru"]


def test_sanitize_language_tags_returns_zero_when_clean() -> None:
    src = b"""<?xml version="1.0"?>
<root xmlns:xml="http://www.w3.org/XML/1998/namespace">
  <a xml:lang="fi">x</a>
  <b xml:lang="en-US">x</b>
</root>"""
    tree = _parse(src)
    assert _sanitize_language_tags(tree) == 0


def test_sanitize_language_tags_walks_nested_elements() -> None:
    """Nested elements (e.g. literals inside a ``bf:Language``) get
    sanitised too — the sanitiser uses ``tree.iter()`` so depth doesn't
    matter."""
    src = b"""<?xml version="1.0"?>
<root xmlns:xml="http://www.w3.org/XML/1998/namespace">
  <outer xml:lang="ok">
    <inner xml:lang="ru-">deeply nested</inner>
  </outer>
</root>"""
    tree = _parse(src)
    fixed = _sanitize_language_tags(tree)
    assert fixed == 1
    nested = tree.getroot().find("outer/inner")
    assert nested is not None
    assert nested.get(_XML_LANG) == "ru"


# --- _emit_errors_tsv -------------------------------------------------------


def test_errors_tsv_surfaces_bib_id_error_type_and_message(tmp_path: Path) -> None:
    """One TSV row per failed record. ``helmet_bib_id`` is derived from
    the filename when the JSONL row's ``helmet_bib_id`` is None (the
    XSD-validation case parses too early to extract the 001)."""
    errors = [
        ConversionErrorRow(
            helmet_bib_id=None,
            filename="b2121847x.xml",
            error_type="marcxml-xsd-validation",
            message=(
                "XSD validation failed: <string>:1:0:ERROR:SCHEMASV:"
                "SCHEMAV_CVC_PATTERN_VALID: Element '{http://www.loc.gov/"
                "MARC21/slim}leader': [facet 'pattern'] The value "
                "'00000    a2200000 a 4500' is not accepted by the pattern."
            ),
            run_uuid="r1",
        ),
        ConversionErrorRow(
            helmet_bib_id=None,
            filename="b9999999.xml",
            error_type="marcxml-content-minimum",
            message="Missing required MARC fields",
            run_uuid="r1",
        ),
    ]
    path = tmp_path / "_errors.tsv"
    _emit_errors_tsv(path, errors)
    lines = path.read_text().splitlines()
    assert lines[0] == "helmet_bib_id\terror_type\tmessage"
    # Two data rows sorted by (bib_id, error_type) — b2121847x < b9999999.
    assert lines[1].startswith("b2121847x\tmarcxml-xsd-validation\t")
    assert lines[2].startswith("b9999999\tmarcxml-content-minimum\t")
    # Newline + tab characters in the message are sanitised; the row
    # stays a single TSV line.
    assert len(lines) == 3


def test_errors_tsv_truncates_long_message(tmp_path: Path) -> None:
    """XSD validation messages are 400+ chars; the TSV truncates to keep
    spreadsheet rendering readable. Full message stays in the JSONL."""
    long_msg = "x" * 1000
    errors = [
        ConversionErrorRow(
            helmet_bib_id=None,
            filename="b1.xml",
            error_type="marcxml-xsd-validation",
            message=long_msg,
            run_uuid="r",
        )
    ]
    path = tmp_path / "_errors.tsv"
    _emit_errors_tsv(path, errors)
    msg_col = path.read_text().splitlines()[1].split("\t")[2]
    assert len(msg_col) < len(long_msg)
    assert msg_col.endswith("…")


def test_errors_tsv_is_header_only_when_no_failures(tmp_path: Path) -> None:
    """Always-emit invariant: even when M2 completed without any record
    failing, the TSV is written with just the header. Cataloguer
    workflows wired to the artifact path never see a missing file."""
    path = tmp_path / "_errors.tsv"
    _emit_errors_tsv(path, [])
    assert path.read_text() == "helmet_bib_id\terror_type\tmessage\n"
