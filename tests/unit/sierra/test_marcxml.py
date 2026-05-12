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
        (
            "record_num",
            "leader",
            "varfields",
            "controlfields",
            "record_last_updated_gmt",
            "items",
            "material_code",
        ),
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
    material_code: str | None = None,
) -> tuple[Any, ...]:
    """Build a streamed-row tuple matching the SELECT's column order."""
    return (
        record_num,
        leader,
        varfields or [],
        controlfields or [],
        record_last_updated_gmt,
        items or [],
        material_code,
    )


def _item(
    *,
    itype: int | None = None,
    location: str = "kk",
    copy_num: int = 1,
    call_number: str | None = None,
    barcode: str | None = None,
) -> dict[str, Any]:
    """Build a single ``items`` row dict — keeps the per-test ``items``
    literals readable (the 33X synth tests only care about ``item_type_num``)."""
    return {
        "location_code": location,
        "copy_num": copy_num,
        "item_type_num": itype,
        "barcode": barcode,
        "call_number": call_number,
    }


def test_row_to_marcxml_writes_sierra_bib_id_as_filename_and_001() -> None:
    """The canonical Sierra bib ID (``b<num><check>``) is the
    cataloguer-facing identifier; it is written both as the MARCXML
    filename and as the ``001`` controlfield content. Prevents the
    ``id1``-placeholder collision in marc2bibframe2 that collapsed
    734 empty-001 records into a single canonical Work on the
    2026-05-12 5k run."""
    filename, xml_bytes = build_marcxml_for_row(_row())
    assert filename == "b1256526x.xml"  # sierra_check_digit(1256526) == 'x'
    body = xml_bytes.decode("utf-8")
    assert '<controlfield tag="001">b1256526x</controlfield>' in body


def test_row_to_marcxml_overwrites_existing_001_varfield() -> None:
    """Per cataloguer spec (2026-05-12 review), the ``001`` controlfield
    is set unconditionally to ``b<num><check>`` — any Sierra-supplied
    varfield with ``marc_tag="001"`` is dropped and replaced. The
    previous behaviour ("preserve existing 001") let empty-content
    varfields pass through, which pymarc then dropped on serialisation
    and produced MARCXML with no 001 at all."""
    varfields = [
        {
            "id": 1,
            "marc_tag": "001",
            "marc_ind1": " ",
            "marc_ind2": " ",
            "field_content": "cls0093490",  # legacy / BTJ external ID
            "subfields": [],
        }
    ]
    _filename, xml_bytes = build_marcxml_for_row(_row(varfields=varfields))
    body = xml_bytes.decode("utf-8")
    assert '<controlfield tag="001">b1256526x</controlfield>' in body
    # The legacy ID is no longer in the document under any controlfield.
    assert "cls0093490" not in body


def test_row_to_marcxml_overwrites_empty_001_varfield() -> None:
    """Sierra occasionally serialises varfield rows with
    ``marc_tag="001"`` and ``field_content=""`` — exactly the shape
    that drove the "Nyt" over-merge (an empty Field that pymarc
    drops on serialisation, leaving no 001 at all). The unconditional
    overwrite handles this case the same as a missing 001."""
    varfields = [
        {
            "id": 1,
            "marc_tag": "001",
            "marc_ind1": " ",
            "marc_ind2": " ",
            "field_content": "",
            "subfields": [],
        }
    ]
    _filename, xml_bytes = build_marcxml_for_row(_row(varfields=varfields))
    body = xml_bytes.decode("utf-8")
    assert '<controlfield tag="001">b1256526x</controlfield>' in body
    assert body.count('tag="001"') == 1  # exactly one 001 controlfield


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


def _subfields_of(record: Record, tag: str) -> list[dict[str, str]]:
    """Return one dict per datafield with the given tag, mapping subfield
    code → content. Used to assert RDA 33X synthesis encoding-
    agnostically (pymarc HTML-encodes the Finnish diacritics in the raw
    bytes, so direct string-matching is brittle)."""
    out: list[dict[str, str]] = []
    for field in record.get_fields(tag):
        out.append({sf.code: sf.value for sf in field.subfields})
    return out


def test_row_to_marcxml_synthesises_33x_from_itype_book() -> None:
    """Bib without 336/337/338 + one Adult book 28 item (itype 100)
    → synthesised text/unmediated/volume tuple. Recovers the pre-RDA
    records that drop on the M2 ``marcxml-content-minimum`` gate."""
    _filename, xml_bytes = build_marcxml_for_row(_row(items=[_item(itype=100)]))
    record = marcxml.parse_xml_to_array(io.BytesIO(xml_bytes))[0]
    assert _subfields_of(record, "336") == [
        {"a": "teksti", "b": "txt", "2": "rdacontent"},
    ]
    assert _subfields_of(record, "337") == [
        {"a": "käytettävissä ilman laitetta", "b": "n", "2": "rdamedia"},
    ]
    assert _subfields_of(record, "338") == [
        {"a": "nide", "b": "nc", "2": "rdacarrier"},
    ]


def test_row_to_marcxml_synthesises_two_336_for_console_game() -> None:
    """Console-game itypes idiomatically carry **two** 336 datafields —
    ``tdi`` (2-D moving image: the in-game visuals) AND ``cop`` (the
    game is a computer program). Mapping reproduces both."""
    _filename, xml_bytes = build_marcxml_for_row(_row(items=[_item(itype=116)]))
    record = marcxml.parse_xml_to_array(io.BytesIO(xml_bytes))[0]
    f336 = _subfields_of(record, "336")
    assert len(f336) == 2
    assert {sf["b"] for sf in f336} == {"tdi", "cop"}
    assert _subfields_of(record, "337")[0]["b"] == "c"
    assert _subfields_of(record, "338")[0]["b"] == "cd"


def test_row_to_marcxml_no_33x_synth_when_itype_unmapped() -> None:
    """An itype not in the mapping (e.g. a niche object type or any of
    the unnamed reserved slots) should leave the bib without
    synthesised 33X — the strict M2 gate then drops the record, which
    is the deliberate behaviour while cataloguer input is pending."""
    _filename, xml_bytes = build_marcxml_for_row(_row(items=[_item(itype=999)]))
    body = xml_bytes.decode("utf-8")
    assert 'tag="336"' not in body
    assert 'tag="337"' not in body
    assert 'tag="338"' not in body


def test_row_to_marcxml_preserves_existing_33x_varfield() -> None:
    """Cataloguer-supplied 33X always wins — the synth path runs only
    when no Sierra-side 33X varfield is present. Verifies the gate."""
    varfields = [
        {
            "id": 1,
            "marc_tag": "336",
            "marc_ind1": " ",
            "marc_ind2": " ",
            "field_content": "",
            "subfields": [
                {"tag": "a", "content": "cartographic image", "display_order": 0},
                {"tag": "b", "content": "cri", "display_order": 1},
                {"tag": "2", "content": "rdacontent", "display_order": 2},
            ],
        }
    ]
    _filename, xml_bytes = build_marcxml_for_row(
        _row(varfields=varfields, items=[_item(itype=100)])
    )
    body = xml_bytes.decode("utf-8")
    # The cataloguer's cri / cartographic image wins; teksti / txt
    # would only appear if synth fired (it shouldn't here).
    assert "cartographic image" in body
    assert "teksti" not in body


def test_row_to_marcxml_picks_lowest_mapped_itype_for_mixed_bib() -> None:
    """A bib with multiple items of different itypes — synth path
    picks the lowest-numbered *mapped* itype (matches the
    ``MIN(itype_code_num)`` convention of the empirical discovery
    query). Here itype 116 (Adult console game K12) sorts before
    itype 200 (Juvenile book) so the console-game RDA tuple wins."""
    items = [_item(itype=200), _item(itype=116, location="etk")]
    _filename, xml_bytes = build_marcxml_for_row(_row(items=items))
    record = marcxml.parse_xml_to_array(io.BytesIO(xml_bytes))[0]
    f336 = _subfields_of(record, "336")
    # Console game's two-336 signature, not the book's one-336.
    assert {sf["b"] for sf in f336} == {"tdi", "cop"}


def test_row_to_marcxml_synthesises_33x_from_bib_material_code() -> None:
    """Bib carries ``material_code='g'`` (DVD) and **no items at all** —
    the synth path picks up the bib-level signal alone. Confirms that
    bibs-without-items (which the item-only fallback could never
    recover) now get RDA 33X coded too."""
    _filename, xml_bytes = build_marcxml_for_row(_row(material_code="g", items=[]))
    record = marcxml.parse_xml_to_array(io.BytesIO(xml_bytes))[0]
    assert _subfields_of(record, "336")[0]["b"] == "tdi"
    assert _subfields_of(record, "337")[0]["b"] == "v"
    assert _subfields_of(record, "338")[0]["b"] == "vd"


def test_row_to_marcxml_bib_material_code_wins_over_items() -> None:
    """When both signals are present and disagree, the **bib-level**
    material code wins — RDA 33X describes the manifestation, not the
    specific physical copy. Material ``"1"`` (Book) here overrides an
    item-side itype 116 (console game) that would otherwise produce a
    two-336 ``tdi``/``cop`` tuple."""
    _filename, xml_bytes = build_marcxml_for_row(_row(material_code="1", items=[_item(itype=116)]))
    record = marcxml.parse_xml_to_array(io.BytesIO(xml_bytes))[0]
    f336 = _subfields_of(record, "336")
    assert len(f336) == 1  # the book's single 336, not the game's two
    assert f336[0]["b"] == "txt"
    assert _subfields_of(record, "337")[0]["b"] == "n"
    assert _subfields_of(record, "338")[0]["b"] == "nc"


def test_row_to_marcxml_falls_back_to_items_when_material_unmapped() -> None:
    """``material_code="x"`` (E-material) is intentionally omitted from
    :data:`MATERIAL_TO_RDA` (cataloguer vote split across three tuples on
    only three records). When the bib-level signal yields no tuple, the
    synth path falls back to the item-level itype — preserving the
    behaviour where item-only-mapped bibs still get 33X coded."""
    _filename, xml_bytes = build_marcxml_for_row(_row(material_code="x", items=[_item(itype=100)]))
    record = marcxml.parse_xml_to_array(io.BytesIO(xml_bytes))[0]
    # text/unmediated/volume — from the itype 100 (Adult book 28) fallback.
    assert _subfields_of(record, "336")[0]["b"] == "txt"
    assert _subfields_of(record, "337")[0]["b"] == "n"
    assert _subfields_of(record, "338")[0]["b"] == "nc"


def test_build_record_drops_datafield_with_no_valid_subfields() -> None:
    """A varfield whose subfield list is empty (or whose every subfield
    has an invalid tag) should be skipped entirely — emitting a bare
    ``<datafield ind1=" " ind2=" " tag="..." />`` fails the MARC21slim
    XSD ``Missing child element(s). Expected is ( subfield )`` gate
    and kills the record at M2. Surfaced on bib 2180377 (empty 041
    datafield)."""
    varfields = [
        # Empty subfield list entirely.
        Varfield(
            id=10,
            marc_tag="041",
            marc_ind1="0",
            marc_ind2=" ",
            field_content="",
            subfields=[],
        ),
        # Subfields exist but all have whitespace-only tags — should
        # also drop the field (after our tag-validity filter zeros it
        # out).
        Varfield(
            id=11,
            marc_tag="500",
            marc_ind1=" ",
            marc_ind2=" ",
            field_content="",
            subfields=[
                Subfield(tag=" ", content="x", display_order=0),
                Subfield(tag="", content="y", display_order=1),
            ],
        ),
        # A legitimate field for contrast.
        Varfield(
            id=12,
            marc_tag="245",
            marc_ind1="1",
            marc_ind2="0",
            field_content="",
            subfields=[Subfield(tag="a", content="Title", display_order=0)],
        ),
    ]
    record = build_record(None, varfields, [])
    tags = [f.tag for f in record.get_fields()]
    assert "041" not in tags
    assert "500" not in tags
    assert "245" in tags


def test_build_record_drops_subfields_with_whitespace_only_tag() -> None:
    """Sierra occasionally exports placeholder subfields with
    ``tag=" "`` (a literal space). The MARC21slim XSD pattern rejects
    them and the M2 conversion stage kills the whole record. The
    exporter strips them at source. Surfaced on the 5k production-
    style sample (bib 2553807, 338-field tail subfield)."""
    varfields = [
        Varfield(
            id=10,
            marc_tag="338",
            marc_ind1=" ",
            marc_ind2=" ",
            field_content="",
            subfields=[
                Subfield(tag="a", content="nide", display_order=0),
                Subfield(tag="b", content="nc", display_order=1),
                Subfield(tag="2", content="rdacarrier", display_order=2),
                Subfield(tag=" ", content="", display_order=3),  # the bad one
                Subfield(tag="", content="also bad", display_order=4),
            ],
        )
    ]
    record = build_record(None, varfields, [])
    fields_338 = record.get_fields("338")
    assert len(fields_338) == 1
    codes = [sf.code for sf in fields_338[0].subfields]
    assert codes == ["a", "b", "2"]


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
