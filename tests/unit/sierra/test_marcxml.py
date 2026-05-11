"""Unit tests for ``marcxml_export_pipeline.sierra.marcxml``.

The deterministic record-building helpers (``build_record`` /
``build_marcxml_for_row`` / ``sierra_check_digit`` / etc.) are pure
— no DB, no I/O. The async ``export_async`` is exercised separately
in an integration suite (not committed here) that actually streams
from a Sierra Postgres replica.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import Any

import pytest
from pymarc import Record, marcxml

from marcxml_export_pipeline.sierra.dtos import Leaderfield, Subfield, Varfield
from marcxml_export_pipeline.sierra.marcxml import (
    AGENCY_CODE,
    SIERRA_LOCAL_TAG,
    _apply_limit,
    _flatten_inline_subfields,
    _format_005,
    _strip_subfield_prefix,
    _validate_keys,
    build_holdings_fields,
    build_leader,
    build_marcxml_for_row,
    build_record,
    sierra_check_digit,
)

# --- sierra_check_digit --------------------------------------------------


@pytest.mark.parametrize(
    ("record_num", "expected"),
    [
        # Innovative check-digit examples. The weighted sum walks
        # right-to-left starting from index 2.
        # 1389248: sum = 2*8 + 3*4 + 4*2 + 5*9 + 6*8 + 7*3 + 8*1 = 16+12+8+45+48+21+8 = 158
        #          158 % 11 = 4. (Reference: confirmed against
        #          .b13892484 in the actual Helmet ILS.)
        (1389248, "4"),
        # 2628274: 2*4 + 3*7 + 4*2 + 5*8 + 6*2 + 7*6 + 8*2 = 8+21+8+40+12+42+16 = 147
        #          147 % 11 = 4.
        (2628274, "4"),
    ],
)
def test_sierra_check_digit_known_values(record_num: int, expected: str) -> None:
    assert sierra_check_digit(record_num) == expected


def test_sierra_check_digit_handles_string_input() -> None:
    """Streamed numeric columns may come as either int or str depending
    on the SQL driver; both must produce the same check digit."""
    assert sierra_check_digit("1389248") == sierra_check_digit(1389248)


def test_sierra_check_digit_x_for_remainder_10() -> None:
    """Find any record_num whose weighted-sum mod 11 = 10 and verify
    the algorithm returns the literal ``'x'`` (not the digit 10)."""
    # 19: 2*9 + 3*1 = 18 + 3 = 21; 21 % 11 = 10 → 'x'.
    assert sierra_check_digit(19) == "x"


# --- helper regex sanitisers --------------------------------------------


def test_strip_subfield_prefix_removes_leading_delimiter_pair() -> None:
    """Legacy Sierra varfield rows occasionally carry ``|a`` (or the
    actual ``\\x1f`` US byte) on control-field content. One pair is
    stripped; subsequent occurrences in the body stay intact."""
    assert _strip_subfield_prefix("|a20240101120000.0") == "20240101120000.0"
    assert _strip_subfield_prefix("\x1fa20240101120000.0") == "20240101120000.0"


def test_strip_subfield_prefix_leaves_clean_value_alone() -> None:
    assert _strip_subfield_prefix("20240101120000.0") == "20240101120000.0"


def test_strip_subfield_prefix_handles_none_and_empty() -> None:
    assert _strip_subfield_prefix(None) == ""
    assert _strip_subfield_prefix("") == ""


def test_flatten_inline_subfields_replaces_delimiters_with_spaces() -> None:
    """Item varfields encode subfields inline as ``|a...|b...`` in
    legacy exports; M2's 852 ``$h`` doesn't want subfield boundaries
    bleeding through, so flatten to a single string."""
    assert _flatten_inline_subfields("|aQA76|bM4") == "QA76 M4"


def test_flatten_inline_subfields_strips_trailing_whitespace() -> None:
    assert _flatten_inline_subfields("QA76 M4 ") == "QA76 M4"


def test_format_005_canonical_marc_shape() -> None:
    """MARC 21 005 = ``YYYYMMDDHHMMSS.F``."""
    ts = datetime(2026, 5, 11, 13, 45, 30, tzinfo=UTC)
    assert _format_005(ts) == "20260511134530.0"


def test_format_005_returns_none_on_missing_or_non_datetime() -> None:
    assert _format_005(None) is None
    assert _format_005("not-a-datetime") is None  # type: ignore[arg-type]


# --- _validate_keys ------------------------------------------------------


def test_validate_keys_passes_on_expected_order() -> None:
    _validate_keys(
        ("record_num", "leader", "varfields", "controlfields", "record_last_updated_gmt", "items"),
    )


def test_validate_keys_raises_on_schema_drift() -> None:
    """A future SELECT-column reordering or rename should fail the
    run loudly rather than silently producing garbled records."""
    with pytest.raises(ValueError, match="Incompatible SQL result keys"):
        _validate_keys(("record_num", "varfields", "leader"))  # wrong order + missing cols


# --- _apply_limit --------------------------------------------------------


def test_apply_limit_appends_top_level_limit_clause() -> None:
    """``--limit 500`` wraps the bundled SQL so only the first N rows
    of the outermost SELECT come back from the streaming session."""
    base = "SELECT * FROM sierra_view.bib_record b\nORDER BY b.record_id"
    assert _apply_limit(base, 500).rstrip().endswith("LIMIT 500")


def test_apply_limit_strips_trailing_semicolon_before_appending() -> None:
    """Postgres rejects ``ORDER BY ...; LIMIT n`` — strip the trailing
    semicolon if the bundled query ends with one."""
    base = "SELECT * FROM sierra_view.bib_record;"
    out = _apply_limit(base, 10)
    assert ";LIMIT" not in out.replace("\n", "").replace(" ", "")
    assert out.rstrip().endswith("LIMIT 10")


def test_apply_limit_rejects_non_positive_n() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        _apply_limit("SELECT 1", 0)
    with pytest.raises(ValueError, match="positive integer"):
        _apply_limit("SELECT 1", -5)


# --- build_leader --------------------------------------------------------


def test_build_leader_emits_24_characters_from_full_leaderfield() -> None:
    lf = Leaderfield(
        record_status_code="n",
        record_type_code="a",
        bib_level_code="m",
        char_encoding_scheme_code="a",
        encoding_level_code=" ",
        descriptive_cat_form_code="i",
        multipart_level_code=" ",
        base_address="00123",
    )
    leader = build_leader(lf)
    assert len(leader) == 24
    # Composition: 5 zeros + nam + ' ' + a + 22 + base + ' i ' + '4500'.
    assert leader.startswith("00000nam a22")
    assert "00123" in leader
    assert leader.endswith("4500")


def test_build_leader_defaults_when_codes_missing() -> None:
    """A leader row with all NULL codes still produces a syntactically
    valid 24-char leader using MARC 21 minimal-record defaults."""
    leader = build_leader(Leaderfield())
    assert len(leader) == 24
    assert "nam" in leader  # nam = bib record, monograph (minimal default)


def test_build_leader_handles_none_input() -> None:
    leader = build_leader(None)
    assert leader == "00000nam a2200000   4500"


def test_build_leader_zero_pads_base_address() -> None:
    lf = Leaderfield(base_address="42")
    assert "00042" in build_leader(lf)


def test_build_leader_invalid_base_address_falls_back_to_zeros() -> None:
    lf = Leaderfield(base_address="not-a-number")
    assert "00000" in build_leader(lf)


# --- build_holdings_fields -----------------------------------------------


def test_build_holdings_emits_one_852_per_item_with_subfields() -> None:
    items = [
        {
            "location_code": "kk",
            "call_number": "QA76",
            "barcode": "31000123456789",
            "copy_num": 1,
        }
    ]
    fields = build_holdings_fields(items)
    assert len(fields) == 1
    field = fields[0]
    assert field.tag == "852"
    codes = [sf.code for sf in field.subfields]
    assert codes == ["b", "h", "p"]


def test_build_holdings_emits_copy_subfield_only_when_copy_num_above_1() -> None:
    """Copy 1 doesn't need ``$t`` — it's the default. Copy 2+ does."""
    f1 = build_holdings_fields([{"location_code": "kk", "copy_num": 1}])
    assert [sf.code for sf in f1[0].subfields] == ["b"]
    f2 = build_holdings_fields([{"location_code": "kk", "copy_num": 2}])
    codes = [sf.code for sf in f2[0].subfields]
    assert "t" in codes


def test_build_holdings_skips_items_with_no_populated_subfield() -> None:
    """Empty / NULL-only items don't produce an empty 852."""
    assert build_holdings_fields([{"location_code": None, "barcode": None, "copy_num": 0}]) == []
    assert build_holdings_fields([{}]) == []


def test_build_holdings_flattens_inline_subfield_call_number() -> None:
    """Legacy item rows encode ``|aQA76|bM4`` in the varfield —
    flatten to space-separated before going into ``$h``."""
    fields = build_holdings_fields(
        [{"location_code": "kk", "call_number": "|aQA76|bM4", "copy_num": 1}]
    )
    h_value = next(sf.value for sf in fields[0].subfields if sf.code == "h")
    assert h_value == "QA76 M4"


# --- build_marcxml_for_row (end-to-end synthesis) ------------------------


def _row(
    *,
    record_num: int = 1256526,
    leader: dict[str, str] | None = None,
    varfields: list[dict[str, Any]] | None = None,
    controlfields: list[dict[str, Any]] | None = None,
    record_last_updated_gmt: datetime | None = None,
    items: list[dict[str, Any]] | None = None,
) -> tuple[Any, ...]:
    """Build a streamed-row tuple matching the SELECT's column order."""
    return (
        record_num,
        leader,
        varfields or [],
        controlfields or [],
        record_last_updated_gmt,
        items or [],
    )


def test_row_to_marcxml_synthesises_001_from_record_num() -> None:
    """The SupaRed bug's prevention: records lacking 001 (the BTJ /
    legacy-import case) get one synthesised from the Sierra
    ``record_metadata.record_num`` so marc2bibframe2 produces a
    unique Work URI per record."""
    filename, xml_bytes = build_marcxml_for_row(_row())
    assert filename == "1256526.xml"
    body = xml_bytes.decode("utf-8")
    assert '<controlfield tag="001">1256526</controlfield>' in body


def test_row_to_marcxml_preserves_existing_001_varfield() -> None:
    """A real ``001`` value (Sierra control number or BTJ external ID)
    is not overwritten by the helmet_bib_id synthesis."""
    varfields = [
        {
            "id": 1,
            "marc_tag": "001",
            "marc_ind1": " ",
            "marc_ind2": " ",
            "field_content": "cls0093490",
            "subfields": [],
        }
    ]
    _filename, xml_bytes = build_marcxml_for_row(_row(varfields=varfields))
    body = xml_bytes.decode("utf-8")
    assert '<controlfield tag="001">cls0093490</controlfield>' in body
    assert "1256526" not in body.split('<controlfield tag="001">', 1)[1].split("</", 1)[0]


def test_row_to_marcxml_synthesises_003_with_agency_code() -> None:
    _filename, xml_bytes = build_marcxml_for_row(_row())
    body = xml_bytes.decode("utf-8")
    assert f'<controlfield tag="003">{AGENCY_CODE}</controlfield>' in body


def test_row_to_marcxml_synthesises_005_from_record_last_updated() -> None:
    ts = datetime(2024, 6, 15, 9, 30, 0, tzinfo=UTC)
    _filename, xml_bytes = build_marcxml_for_row(_row(record_last_updated_gmt=ts))
    body = xml_bytes.decode("utf-8")
    assert '<controlfield tag="005">20240615093000.0</controlfield>' in body


def test_row_to_marcxml_synthesises_907_with_sierra_check_digit() -> None:
    """907 ``$a`` carries the full Sierra system number with check
    digit: ``.b<num><check>``."""
    _filename, xml_bytes = build_marcxml_for_row(_row(record_num=1389248))
    body = xml_bytes.decode("utf-8")
    assert f'tag="{SIERRA_LOCAL_TAG}"' in body
    assert ".b13892484" in body  # 1389248 + check '4'


def test_row_to_marcxml_emits_852_for_each_item() -> None:
    items = [
        {
            "location_code": "kk",
            "call_number": "QA76",
            "barcode": "31000123456789",
            "copy_num": 1,
        },
        {
            "location_code": "etk",
            "call_number": "PG2632",
            "barcode": "31000987654321",
            "copy_num": 2,
        },
    ]
    _filename, xml_bytes = build_marcxml_for_row(_row(items=items))
    body = xml_bytes.decode("utf-8")
    assert body.count('tag="852"') == 2


def test_row_to_marcxml_round_trips_through_pymarc() -> None:
    """The MARCXML the export emits parses cleanly back into a pymarc
    :class:`Record` — same shape as a marc2bibframe2 input expects."""
    _filename, xml_bytes = build_marcxml_for_row(
        _row(
            varfields=[
                {
                    "id": 100,
                    "marc_tag": "245",
                    "marc_ind1": "1",
                    "marc_ind2": "0",
                    "field_content": "",
                    "subfields": [
                        {"tag": "a", "content": "Sota ja rauha", "display_order": 0},
                    ],
                }
            ],
            record_last_updated_gmt=datetime(2026, 5, 11, tzinfo=UTC),
        )
    )
    records = marcxml.parse_xml_to_array(io.BytesIO(xml_bytes))
    assert len(records) == 1
    record: Record = records[0]
    title_245 = record.get_fields("245")
    assert len(title_245) == 1
    assert title_245[0].get_subfields("a") == ["Sota ja rauha"]


def test_build_record_orders_tags_in_marc_standard_order() -> None:
    """Mixing varfields + controlfields + extra Fields and re-merging
    by tag order is what makes the resulting MARCXML render cleanly
    in downstream tooling — verify the sort holds."""
    varfields = [
        Varfield(
            id=10,
            marc_tag="245",
            marc_ind1="1",
            marc_ind2="0",
            field_content="",
            subfields=[Subfield(tag="a", content="Title", display_order=0)],
        )
    ]
    controlfields = [("001", "12345"), ("003", "FI-HELME"), ("005", "20260511.0")]
    record = build_record(None, varfields, controlfields)
    tags = [f.tag for f in record.get_fields()]
    assert tags == sorted(tags)
