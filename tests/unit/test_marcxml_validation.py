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


def test_filename_pattern_accepts_numeric_xml(tmp_path: Path) -> None:
    valid = tmp_path / "12345678.xml"
    valid.write_text("<x/>")
    validate_filename(valid)


@pytest.mark.parametrize(
    "name",
    ["bad-name.xml", "abc.xml", "12345678.txt", "12345678.XML", "12345678"],
)
def test_filename_pattern_rejects_non_numeric(tmp_path: Path, name: str) -> None:
    bad = tmp_path / name
    bad.write_text("<x/>")
    with pytest.raises(MarcXmlValidationError) as exc:
        validate_filename(bad)
    assert exc.value.error_type == "marcxml-filename"


def test_helmet_bib_id_from_filename() -> None:
    assert helmet_bib_id_from_filename(Path("/x/12345678.xml")) == "12345678"


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
