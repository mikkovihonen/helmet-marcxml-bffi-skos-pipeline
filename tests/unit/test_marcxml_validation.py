"""Unit tests for Boundary 1 (validation/marcxml.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bffi_pipeline.validation.marcxml import (
    MARC_NS,
    MarcXmlValidationError,
    helmet_bib_id_from_filename,
    validate,
    validate_filename,
)

FIXTURES = Path(__file__).resolve().parents[1] / "data" / "sample-marcxml"


@pytest.mark.parametrize(
    "name",
    [
        "12345678.xml",  # legacy bare-digits form
        "b12345678x.xml",  # Sierra bib-id form, check 'x'
        "b11007849.xml",  # Sierra bib-id form, check '0-9'
    ],
)
def test_filename_pattern_accepts_both_forms(tmp_path: Path, name: str) -> None:
    """Boundary-1 accepts both the legacy bare-digits filename
    (``<num>.xml``) and the canonical Sierra-bib-id form
    (``b<num><check>.xml``) — see
    ``src/marcxml_export_pipeline/sierra/marcxml.py``'s
    ``sierra_bib_id`` for the format the exporter writes after
    2026-05-12."""
    valid = tmp_path / name
    valid.write_text("<x/>")
    validate_filename(valid)


@pytest.mark.parametrize(
    "name",
    [
        "bad-name.xml",
        "abc.xml",
        "12345678.txt",
        "12345678.XML",
        "12345678",
        "b.xml",  # b with no digits/check
        "b123y.xml",  # b with digits + invalid check (not 0-9 or x)
        "B12345678.xml",  # uppercase B not accepted
    ],
)
def test_filename_pattern_rejects_malformed(tmp_path: Path, name: str) -> None:
    bad = tmp_path / name
    bad.write_text("<x/>")
    with pytest.raises(MarcXmlValidationError) as exc:
        validate_filename(bad)
    assert exc.value.error_type == "marcxml-filename"


def test_helmet_bib_id_from_filename_legacy_form() -> None:
    assert helmet_bib_id_from_filename(Path("/x/12345678.xml")) == "12345678"


def test_helmet_bib_id_from_filename_sierra_bib_id_form() -> None:
    """``b<num><check>.xml`` → ``b<num><check>`` returned verbatim.
    Downstream stages carry the canonical Sierra bib ID as the
    pipeline-internal ``helmet_bib_id``."""
    assert helmet_bib_id_from_filename(Path("/x/b11007849.xml")) == "b11007849"


def test_validate_accepts_synthetic_valid_records() -> None:
    for name in ("10000001.xml", "10000002.xml", "10000004.xml", "10000006.xml"):
        result = validate(FIXTURES / name)
        assert result.helmet_bib_id == name.split(".")[0]
        assert result.tree.getroot().tag.endswith(
            "collection"
        ) or result.tree.getroot().tag.endswith("record")


def test_validate_rejects_bad_encoding() -> None:
    with pytest.raises(MarcXmlValidationError) as exc:
        validate(FIXTURES / "99999900.xml")
    assert exc.value.error_type == "marcxml-encoding"


def test_validate_rejects_xsd_failure() -> None:
    with pytest.raises(MarcXmlValidationError) as exc:
        validate(FIXTURES / "99999901.xml")
    assert exc.value.error_type == "marcxml-xsd-validation"


def test_validate_rejects_missing_minimum_content() -> None:
    with pytest.raises(MarcXmlValidationError) as exc:
        validate(FIXTURES / "99999902.xml")
    assert exc.value.error_type == "marcxml-content-minimum"
    assert "245" in exc.value.message


def test_marc_namespace_constant_matches_spec() -> None:
    # Pin the namespace literal so a typo elsewhere is caught here.
    assert MARC_NS == "http://www.loc.gov/MARC21/slim"


def _write_minimal_record(
    path: Path,
    *,
    leader: str,
    include_33x: bool,
    include_1xx: bool = True,
) -> None:
    """Write a minimal-but-valid MARCXML record with a configurable leader."""
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<record xmlns="http://www.loc.gov/MARC21/slim">',
        f"<leader>{leader}</leader>",
        '<controlfield tag="008">240101s2024    fi              fin  </controlfield>',
    ]
    if include_1xx:
        parts.append(
            '<datafield tag="100" ind1="1" ind2=" ">'
            '<subfield code="a">Sibelius, Jean</subfield></datafield>'
        )
    parts.append(
        '<datafield tag="245" ind1="1" ind2="0">'
        '<subfield code="a">Some title</subfield></datafield>'
    )
    if include_33x:
        parts.append(
            '<datafield tag="336" ind1=" " ind2=" ">'
            '<subfield code="a">notated music</subfield>'
            '<subfield code="b">ntm</subfield>'
            '<subfield code="2">rdacontent</subfield></datafield>'
        )
    parts.append("</record>")
    path.write_text("".join(parts), encoding="utf-8")


@pytest.mark.parametrize("record_type", ["c", "d", "i", "j"])
def test_validate_exempts_music_records_from_33x_requirement(
    tmp_path: Path, record_type: str
) -> None:
    """Music records (leader pos 6 in {c, d, i, j}) commonly lack 33X in
    Helmet's Sierra export — that's a known cataloguing pattern, not a
    validation failure. Boundary-1 must accept these without 33X."""
    record_path = tmp_path / "12345678.xml"
    leader = f"00000n{record_type}m  22000007a 4500"
    _write_minimal_record(record_path, leader=leader, include_33x=False)
    result = validate(record_path)
    assert result.helmet_bib_id == "12345678"


def test_validate_still_requires_33x_for_textual_records(tmp_path: Path) -> None:
    """Textual works (record-type-code = 'a' / 'm' / 't' / etc.) keep
    the 33X requirement. The relaxation is targeted at music only."""
    record_path = tmp_path / "12345678.xml"
    leader = "00000nam  22000007a 4500"  # 'a' = language material (textual)
    _write_minimal_record(record_path, leader=leader, include_33x=False)
    with pytest.raises(MarcXmlValidationError) as exc:
        validate(record_path)
    assert exc.value.error_type == "marcxml-content-minimum"
    assert "336/337/338" in exc.value.message


def test_validate_accepts_music_records_with_33x_too(tmp_path: Path) -> None:
    """The exemption is permissive — music records WITH 33X still pass."""
    record_path = tmp_path / "12345678.xml"
    _write_minimal_record(record_path, leader="00000ncm  22000007a 4500", include_33x=True)
    validate(record_path)  # should not raise
