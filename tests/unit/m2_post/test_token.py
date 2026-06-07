"""P-50 Phase A — token format + MARCXML index tests."""

from __future__ import annotations

import textwrap

import pytest

from bffi_pipeline.stages.m2_post.token import (
    MarcFieldIndex,
    compute_token,
    parse_token,
)


def test_compute_token_canonical_format() -> None:
    assert compute_token("b10068004", "650", 3) == "b10068004:650:3"


def test_compute_token_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError, match="bib_id"):
        compute_token("", "650", 1)
    with pytest.raises(ValueError, match="tag"):
        compute_token("b1", "65", 1)
    with pytest.raises(ValueError, match="ordinal"):
        compute_token("b1", "650", 0)


def test_parse_token_round_trips() -> None:
    assert parse_token("b10068004:650:3") == ("b10068004", "650", 3)


def test_parse_token_tolerates_colons_in_bib_id() -> None:
    """URN-style bib_ids carry colons. ``rsplit`` from the right keeps
    the bib_id intact while the last two segments are tag + ordinal."""
    parsed = parse_token("urn:nbn:fi:b1:650:3")
    assert parsed == ("urn:nbn:fi:b1", "650", 3)


def test_parse_token_returns_none_for_malformed() -> None:
    assert parse_token("foo") is None
    assert parse_token("b1:65:3") is None  # tag too short
    assert parse_token("b1:650:x") is None  # ordinal not int
    assert parse_token("b1:650:0") is None  # ordinal must be >= 1


def _record(body: str, bib_id: str = "b1") -> str:
    return textwrap.dedent(
        f"""\
        <record xmlns="http://www.loc.gov/MARC21/slim">
          <controlfield tag="001">{bib_id}</controlfield>
          {body}
        </record>
        """
    )


def test_marc_field_index_counts_within_tag_buckets_in_document_order() -> None:
    """Three 650s + one 651 interleaved should rank as 650:1/2/3 and
    651:1 — not by global position. The P-50 redesign is built on
    this invariant."""
    xml = _record(
        """
        <datafield tag="650" ind1=" " ind2="7">
          <subfield code="a">sodat</subfield>
        </datafield>
        <datafield tag="651" ind1=" " ind2="7">
          <subfield code="a">Kreikka</subfield>
        </datafield>
        <datafield tag="650" ind1=" " ind2="7">
          <subfield code="a">musiikki</subfield>
        </datafield>
        <datafield tag="650" ind1=" " ind2="7">
          <subfield code="a">historia</subfield>
        </datafield>
        """
    )
    idx = MarcFieldIndex.from_marcxml_string(xml)
    tokens = [f.token for f in idx.fields if not f.is_control]
    assert tokens == ["b1:650:1", "b1:651:1", "b1:650:2", "b1:650:3"]


def test_marc_field_index_carries_subfields_in_source_order() -> None:
    xml = _record(
        """
        <datafield tag="700" ind1="1" ind2=" ">
          <subfield code="a">Andersson, Benny,</subfield>
          <subfield code="e">säveltäjä</subfield>
        </datafield>
        """
    )
    idx = MarcFieldIndex.from_marcxml_string(xml)
    field_700 = idx.get("700", 1)
    assert field_700 is not None
    assert field_700.subfields == (
        ("a", "Andersson, Benny,"),
        ("e", "säveltäjä"),
    )
    assert field_700.subfield_value("e") == "säveltäjä"


def test_marc_field_index_reads_bib_id_from_001() -> None:
    xml = _record("", bib_id="b10068004")
    idx = MarcFieldIndex.from_marcxml_string(xml)
    assert idx.bib_id == "b10068004"
