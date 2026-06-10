"""Unit tests for the MARCXML diff classifier."""

from __future__ import annotations

from pathlib import Path

import pytest

from bffi_pipeline.stages.roundtrip_eval.diff import (
    FieldRow,
    MarcxmlParseError,
    diff_fields,
    diff_records,
    parse_record,
)

_MIN_RECORD = b"""<?xml version='1.0' encoding='utf-8'?>
<record xmlns="http://www.loc.gov/MARC21/slim">
  <leader>00000nam a2200000 a 4500</leader>
  <controlfield tag="001">b1</controlfield>
  <datafield tag="245" ind1="0" ind2="0">
    <subfield code="a">Same Title</subfield>
  </datafield>
</record>
"""


def _write(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    return path


# --- parse_record -------------------------------------------------------


def test_parse_record_extracts_bib_id_and_fields(tmp_path: Path) -> None:
    path = _write(tmp_path / "rec.xml", _MIN_RECORD)
    bib_id, fields = parse_record(path)
    assert bib_id == "b1"
    tags = sorted(f.tag for f in fields)
    assert tags == ["001", "245"]


def test_parse_record_handles_collection_wrapper(tmp_path: Path) -> None:
    """The MARC21 slim schema allows ``<collection><record>...</record></collection>``."""
    payload = (
        b"<?xml version='1.0' encoding='utf-8'?>"
        b"<collection xmlns='http://www.loc.gov/MARC21/slim'>"
        + _MIN_RECORD.split(b"?>\n", 1)[1]
        + b"</collection>"
    )
    path = _write(tmp_path / "rec.xml", payload)
    bib_id, _ = parse_record(path)
    assert bib_id == "b1"


def test_parse_record_raises_on_missing_001(tmp_path: Path) -> None:
    payload = b"""<?xml version='1.0' encoding='utf-8'?>
<record xmlns="http://www.loc.gov/MARC21/slim">
  <leader>00000nam a2200000 a 4500</leader>
</record>"""
    path = _write(tmp_path / "rec.xml", payload)
    with pytest.raises(MarcxmlParseError, match="no controlfield 001"):
        parse_record(path)


# --- diff_fields --------------------------------------------------------


def _df(tag: str, code: str, value: str) -> FieldRow:
    return FieldRow(tag=tag, ind1="0", ind2="0", subfields=((code, value),), text=None)


def _cf(tag: str, value: str) -> FieldRow:
    return FieldRow(tag=tag, ind1=None, ind2=None, subfields=(), text=value)


def test_diff_fields_identical_when_source_matches_reconstructed_exactly() -> None:
    source = (_cf("001", "b1"), _df("245", "a", "Title"))
    diffs = diff_fields(source=source, reconstructed=source)
    statuses = [d.status for d in diffs]
    assert statuses == ["identical", "identical"]


def test_diff_fields_classifies_changed_when_same_tag_different_content() -> None:
    source = (_df("245", "a", "Source Title"),)
    recon = (_df("245", "a", "Different Title"),)
    diffs = diff_fields(source=source, reconstructed=recon)
    assert len(diffs) == 1
    assert diffs[0].status == "changed"
    assert diffs[0].source.subfields[0][1] == "Source Title"
    assert diffs[0].reconstructed.subfields[0][1] == "Different Title"


def test_diff_fields_classifies_lost_when_only_in_source() -> None:
    source = (_df("700", "a", "Andersen, H. C."),)
    recon: tuple[FieldRow, ...] = ()
    diffs = diff_fields(source=source, reconstructed=recon)
    assert [d.status for d in diffs] == ["lost"]
    assert diffs[0].source.subfields[0][1] == "Andersen, H. C."
    assert diffs[0].reconstructed is None


def test_diff_fields_classifies_added_when_only_in_reconstructed() -> None:
    source: tuple[FieldRow, ...] = ()
    recon = (_df("999", "a", "Synthesised"),)
    diffs = diff_fields(source=source, reconstructed=recon)
    assert [d.status for d in diffs] == ["added"]
    assert diffs[0].source is None
    assert diffs[0].reconstructed.subfields[0][1] == "Synthesised"


def test_diff_fields_pairs_identical_first_then_zips_remainder() -> None:
    """Repeated tags pair identical instances first, then zip leftover
    positionally as `changed`, then overhang as lost / added."""
    source = (
        _df("700", "a", "A"),
        _df("700", "a", "B"),  # matches recon[0]
        _df("700", "a", "C"),
    )
    recon = (
        _df("700", "a", "B"),  # identical to source[1]
        _df("700", "a", "X"),  # paired as changed with leftover source[0]
    )
    diffs = diff_fields(source=source, reconstructed=recon)
    statuses = sorted(d.status for d in diffs)
    # 1 identical (B↔B), 1 changed (A↔X), 1 lost (C).
    assert statuses == ["changed", "identical", "lost"]


# --- diff_records (integration) -----------------------------------------


def test_diff_records_round_trip_on_same_input(tmp_path: Path) -> None:
    """A record diffed against itself yields all-identical."""
    path = _write(tmp_path / "rec.xml", _MIN_RECORD)
    result = diff_records(source_path=path, reconstructed_path=path)
    assert result.bib_id == "b1"
    assert all(d.status == "identical" for d in result.fields)
    counts = result.status_counts
    assert counts["identical"] == len(result.fields)
