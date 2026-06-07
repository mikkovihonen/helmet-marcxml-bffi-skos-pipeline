"""Unit tests for the MARC round-trip field-by-field diff."""

from __future__ import annotations

import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

from bffi_pipeline.marc_roundtrip.converter import MARC_NAMESPACE, ROUNDTRIP_MARKER
from bffi_pipeline.marc_roundtrip.diff import diff_records

NS = MARC_NAMESPACE


def _record(body: str) -> ET.Element:
    xml = textwrap.dedent(
        f"""\
        <record xmlns="{NS}">
        {body}
        </record>
        """
    )
    return ET.fromstring(xml)


# --- Identical / lost / added classification ----------------------------


def test_identical_record_produces_all_identical_diffs() -> None:
    body = """
        <leader>00000nam  2200000   4500</leader>
        <controlfield tag="001">b1</controlfield>
        <datafield tag="245" ind1="1" ind2="0">
          <subfield code="a">War and Peace</subfield>
        </datafield>
    """
    rec = _record(body)
    diff = diff_records(bib_id="b1", original=rec, reconstructed=rec)
    statuses = {d.status for d in diff.fields}
    assert statuses == {"identical"}


def test_lost_field_classified_when_original_has_it_and_recon_doesnt() -> None:
    orig = _record(
        """
        <datafield tag="084" ind1=" " ind2=" ">
          <subfield code="a">84.2</subfield>
          <subfield code="2">ykl</subfield>
        </datafield>
        """
    )
    recon = _record("")
    diff = diff_records(bib_id="b1", original=orig, reconstructed=recon)
    [d] = diff.fields
    assert d.status == "lost"
    assert d.tag == "084"
    assert d.original is not None
    assert d.reconstructed is None


def test_added_field_classified_when_recon_has_it_and_original_doesnt() -> None:
    orig = _record("")
    recon = _record(
        """
        <datafield tag="100" ind1="1" ind2=" ">
          <subfield code="a">KRAG, THOMAS PETER</subfield>
        </datafield>
        """
    )
    diff = diff_records(bib_id="b1", original=orig, reconstructed=recon)
    [d] = diff.fields
    assert d.status == "added"
    assert d.tag == "100"
    assert d.original is None
    assert d.reconstructed is not None


def test_changed_field_classified_when_subfield_values_differ() -> None:
    orig = _record(
        """
        <datafield tag="245" ind1="1" ind2="0">
          <subfield code="a">War and Peace</subfield>
          <subfield code="c">Leo Tolstoy</subfield>
        </datafield>
        """
    )
    recon = _record(
        """
        <datafield tag="245" ind1="1" ind2="0">
          <subfield code="a">War and Peace</subfield>
        </datafield>
        """
    )
    diff = diff_records(bib_id="b1", original=orig, reconstructed=recon)
    [d] = diff.fields
    assert d.status == "changed"
    assert d.tag == "245"


# --- Round-trip marker ignored on the reconstructed side ----------------


def test_roundtrip_marker_subfield_is_ignored_when_matching() -> None:
    """A reconstructed field with only the round-trip ``$5`` marker
    added should compare equal to the original."""
    orig = _record(
        """
        <datafield tag="100" ind1="1" ind2=" ">
          <subfield code="a">KRAG, THOMAS PETER</subfield>
        </datafield>
        """
    )
    recon = _record(
        f"""
        <datafield tag="100" ind1="1" ind2=" ">
          <subfield code="a">KRAG, THOMAS PETER</subfield>
          <subfield code="5">{ROUNDTRIP_MARKER}</subfield>
        </datafield>
        """
    )
    diff = diff_records(bib_id="b1", original=orig, reconstructed=recon)
    [d] = diff.fields
    assert d.status == "identical"


# --- Repeating fields paired by $a --------------------------------------


def test_multiple_same_tag_fields_paired_by_a_subfield() -> None:
    """Two 655 entries on each side, pair by ``$a`` regardless of order."""
    orig = _record(
        """
        <datafield tag="655" ind1=" " ind2="7">
          <subfield code="a">käännökset</subfield>
        </datafield>
        <datafield tag="655" ind1=" " ind2="7">
          <subfield code="a">norjankielinen kirjallisuus</subfield>
        </datafield>
        """
    )
    recon = _record(
        """
        <datafield tag="655" ind1=" " ind2="7">
          <subfield code="a">norjankielinen kirjallisuus</subfield>
        </datafield>
        <datafield tag="655" ind1=" " ind2="7">
          <subfield code="a">käännökset</subfield>
        </datafield>
        """
    )
    diff = diff_records(bib_id="b1", original=orig, reconstructed=recon)
    statuses = [d.status for d in diff.fields]
    assert statuses.count("identical") == 2


# --- Skipped tags surfaced as lost-converter-gap ------------------------


def test_skipped_tag_is_classified_lost_converter_gap() -> None:
    """When the converter declares it skipped a tag, the diff
    classifies the original's instance as lost-converter-gap, not
    lost — so the HTML can distinguish 'BFFI doesn't carry this' from
    'the converter didn't reproduce this'."""
    orig = _record(
        """
        <datafield tag="852" ind1=" " ind2=" ">
          <subfield code="b">hva1l</subfield>
        </datafield>
        """
    )
    recon = _record("")
    diff = diff_records(bib_id="b1", original=orig, reconstructed=recon, skipped_tags=("852",))
    [d] = diff.fields
    assert d.status == "lost-converter-gap"
    assert any("intentionally skipped" in n for n in d.notes)


# --- Indicator differences surfaced as notes ----------------------------


def test_indicator_differences_become_notes_not_status_changed() -> None:
    """Indicator-level differences are reported as notes (informational)
    but don't drive the field's status if subfields match. Lots of MARC
    indicators don't survive the BFFI round-trip; surfacing every one
    as ``changed`` would drown signal in noise."""
    orig = _record(
        """
        <datafield tag="245" ind1="1" ind2="0">
          <subfield code="a">AADA WILDE</subfield>
        </datafield>
        """
    )
    recon = _record(
        """
        <datafield tag="245" ind1="0" ind2="4">
          <subfield code="a">AADA WILDE</subfield>
        </datafield>
        """
    )
    diff = diff_records(bib_id="b1", original=orig, reconstructed=recon)
    [d] = diff.fields
    assert d.status == "identical"
    assert any("ind1" in n for n in d.notes)
    assert any("ind2" in n for n in d.notes)


# --- Summary + JSON serialisation ---------------------------------------


def test_summary_counts_each_status() -> None:
    orig = _record(
        """
        <datafield tag="245" ind1="1" ind2="0">
          <subfield code="a">Same</subfield>
        </datafield>
        <datafield tag="084" ind1=" " ind2=" ">
          <subfield code="a">84.2</subfield>
        </datafield>
        """
    )
    recon = _record(
        """
        <datafield tag="245" ind1="1" ind2="0">
          <subfield code="a">Same</subfield>
        </datafield>
        <datafield tag="100" ind1="1" ind2=" ">
          <subfield code="a">Added Author</subfield>
        </datafield>
        """
    )
    diff = diff_records(bib_id="b1", original=orig, reconstructed=recon)
    assert diff.summary["identical"] == 1
    assert diff.summary["lost"] == 1
    assert diff.summary["added"] == 1


def test_to_json_is_round_trippable() -> None:
    """The JSON shape is what the HTML reviewer consumes; pin it."""
    orig = _record("")
    recon = _record(
        """
        <datafield tag="100" ind1="1" ind2=" ">
          <subfield code="a">KRAG</subfield>
        </datafield>
        """
    )
    diff = diff_records(bib_id="b1", original=orig, reconstructed=recon)
    payload = diff.to_json()
    assert payload["bib_id"] == "b1"
    [field] = payload["fields"]
    assert field["status"] == "added"
    assert field["reconstructed"]["tag"] == "100"
    assert field["reconstructed"]["subfields"][0]["code"] == "a"
    assert field["reconstructed"]["subfields"][0]["value"] == "KRAG"
    assert field["original"] is None


# --- Field sort order ---------------------------------------------------


def test_diff_rows_sort_by_tag_ascending_with_ldr_first() -> None:
    """The diff table reads top-to-bottom in MARC tag order so
    cataloguers can scan it the same way they read a MARC record.
    LDR is pinned to position 0, then 008 / 020 / 100 / 245 / 500
    / 650 / 700 / 730. Status badges signal differences without
    needing the status to drive sort order."""
    orig = _record(
        """
        <datafield tag="650" ind1=" " ind2="7">
          <subfield code="a">Subject A</subfield>
        </datafield>
        <datafield tag="245" ind1="1" ind2="0">
          <subfield code="a">Title</subfield>
        </datafield>
        <datafield tag="500" ind1=" " ind2=" ">
          <subfield code="a">Lost note</subfield>
        </datafield>
        <datafield tag="100" ind1="1" ind2=" ">
          <subfield code="a">Author</subfield>
        </datafield>
        """
    )
    recon = _record(
        """
        <datafield tag="245" ind1="1" ind2="0">
          <subfield code="a">Title</subfield>
        </datafield>
        <datafield tag="100" ind1="1" ind2=" ">
          <subfield code="a">Author</subfield>
        </datafield>
        <datafield tag="650" ind1=" " ind2="7">
          <subfield code="a">Subject A</subfield>
        </datafield>
        """
    )
    diff = diff_records(bib_id="b1", original=orig, reconstructed=recon)
    tags = [d.tag for d in diff.fields]
    # LDR may or may not be present; whatever data tags exist must be
    # in ascending order even though source / recon had them in mixed
    # order and statuses are a mix of identical + lost.
    data_tags = [t for t in tags if t != "LDR"]
    assert data_tags == sorted(data_tags), (
        f"datafield rows must sort ascending by tag, got {data_tags}"
    )


def test_lineage_subfield_strips_from_recon_and_pairs_by_token() -> None:
    """P-48 Phase A: a $9 src=<tag>-<ord> subfield on the recon side
    pairs explicitly to the source field at that position within the
    tag bucket; the subfield is also stripped from the recon's
    subfield list so it doesn't trigger a `changed` status."""
    orig_body = """
    <datafield tag="650" ind1=" " ind2="7">
      <subfield code="a">viihdemusiikki</subfield>
    </datafield>
    """
    # Recon row carries the lineage marker pointing at 650-1.
    recon_body = f"""
    <datafield tag="650" ind1=" " ind2="7">
      <subfield code="a">viihdemusiikki</subfield>
      <subfield code="9">src=650-1</subfield>
      <subfield code="5">{ROUNDTRIP_MARKER}</subfield>
    </datafield>
    """
    diff = diff_records(
        bib_id="b1",
        original=_record(orig_body),
        reconstructed=_record(recon_body),
    )
    # One identical row (lineage paired + subfields equal after strip).
    rows = [d for d in diff.fields if d.tag == "650"]
    assert len(rows) == 1
    assert rows[0].status == "identical"


def test_lineage_pairs_across_tag_buckets_emitting_tag_changed() -> None:
    """The b10303327 Greece bug shape: source 651-1 Kreikka, recon
    emits the same data as 650 carrying ``$9 src=651-1``. The diff
    must NOT classify this as ``lost + added``; it must pair the
    two fields by lineage and report ``status=tag-changed``."""
    orig_body = """
    <datafield tag="651" ind1=" " ind2="7">
      <subfield code="a">Kreikka</subfield>
    </datafield>
    """
    recon_body = f"""
    <datafield tag="650" ind1=" " ind2="7">
      <subfield code="a">Kreikka</subfield>
      <subfield code="0">http://www.yso.fi/onto/yso/p105037</subfield>
      <subfield code="9">src=651-1</subfield>
      <subfield code="5">{ROUNDTRIP_MARKER}</subfield>
    </datafield>
    """
    diff = diff_records(
        bib_id="b1",
        original=_record(orig_body),
        reconstructed=_record(recon_body),
    )
    rows = list(diff.fields)
    assert len(rows) == 1
    assert rows[0].status == "tag-changed"
    assert rows[0].original is not None and rows[0].original.tag == "651"
    assert rows[0].reconstructed is not None and rows[0].reconstructed.tag == "650"
    assert any("651 → reconstructed tag 650" in n for n in rows[0].notes)


def test_lineage_absent_falls_back_to_heuristic_pairing() -> None:
    """Without a ``$9 src=…`` token, pairing falls through to today's
    tag-bucket + $a heuristic — pre-Phase-A behaviour is preserved
    for the flat Instance-side fields the converter doesn't stamp
    yet."""
    orig_body = """
    <datafield tag="020" ind1=" " ind2=" ">
      <subfield code="a">9780000000002</subfield>
    </datafield>
    """
    recon_body = f"""
    <datafield tag="020" ind1=" " ind2=" ">
      <subfield code="a">9780000000002</subfield>
      <subfield code="5">{ROUNDTRIP_MARKER}</subfield>
    </datafield>
    """
    diff = diff_records(
        bib_id="b1",
        original=_record(orig_body),
        reconstructed=_record(recon_body),
    )
    rows = list(diff.fields)
    assert len(rows) == 1
    assert rows[0].status == "identical"


def test_cataloguer_supplied_dollar9_survives_diff_strip() -> None:
    """A source ``$9 foo`` (cataloguer's local processing code) must
    NOT be stripped by the lineage parser — only ``$9 src=…`` values
    are recognised as round-trip lineage and removed."""
    orig_body = """
    <datafield tag="500" ind1=" " ind2=" ">
      <subfield code="a">Note text</subfield>
      <subfield code="9">FOO</subfield>
    </datafield>
    """
    recon_body = f"""
    <datafield tag="500" ind1=" " ind2=" ">
      <subfield code="a">Note text</subfield>
      <subfield code="5">{ROUNDTRIP_MARKER}</subfield>
    </datafield>
    """
    diff = diff_records(
        bib_id="b1",
        original=_record(orig_body),
        reconstructed=_record(recon_body),
    )
    rows = list(diff.fields)
    assert len(rows) == 1
    # Source has $9 FOO, recon doesn't → changed (the cataloguer's
    # $9 subfield was lost in the round-trip — we want this surfaced,
    # NOT suppressed as a lineage strip).
    assert rows[0].status == "changed"


def test_marckey_bypass_overrides_identical_when_recon_carries_sentinel() -> None:
    """P-49 Phase A: a recon row whose subfields were built by parsing
    ``bflc:marcKey`` carries a ``$9 marckey-bypass`` sentinel. The
    diff classifies the row as ``marckey-bypass`` regardless of
    byte-equality with the original — the verification failed because
    the path was through the raw MARC string, not BFFI structured
    properties. Cataloguer-supplied ``$9`` content (other values)
    is unaffected."""
    orig_body = """
    <datafield tag="700" ind1="1" ind2=" ">
      <subfield code="a">Andersson, Benny,</subfield>
      <subfield code="e">säveltäjä</subfield>
    </datafield>
    """
    recon_body = f"""
    <datafield tag="700" ind1="1" ind2=" ">
      <subfield code="a">Andersson, Benny,</subfield>
      <subfield code="e">säveltäjä</subfield>
      <subfield code="9">marckey-bypass</subfield>
      <subfield code="5">{ROUNDTRIP_MARKER}</subfield>
    </datafield>
    """
    diff = diff_records(
        bib_id="b1",
        original=_record(orig_body),
        reconstructed=_record(recon_body),
    )
    [row] = diff.fields
    assert row.status == "marckey-bypass"
    assert any("bflc:marcKey" in n for n in row.notes)
    # Summary counter increments.
    assert diff.summary.get("marckey-bypass") == 1
    assert diff.summary.get("identical", 0) == 0


def test_marckey_bypass_does_not_override_tag_changed() -> None:
    """A misroute (source tag X → recon tag Y) is a more serious
    diagnostic than a bypass. ``tag-changed`` wins."""
    orig_body = """
    <datafield tag="651" ind1=" " ind2="7">
      <subfield code="a">Kreikka</subfield>
      <subfield code="2">yso</subfield>
    </datafield>
    """
    recon_body = f"""
    <datafield tag="650" ind1=" " ind2="7">
      <subfield code="a">Kreikka</subfield>
      <subfield code="2">yso</subfield>
      <subfield code="9">src=651-1</subfield>
      <subfield code="9">marckey-bypass</subfield>
      <subfield code="5">{ROUNDTRIP_MARKER}</subfield>
    </datafield>
    """
    diff = diff_records(
        bib_id="b1",
        original=_record(orig_body),
        reconstructed=_record(recon_body),
    )
    [row] = diff.fields
    assert row.status == "tag-changed"


def test_path_import_is_used_in_some_assertions() -> None:
    """No-op sanity test — pytest discovers test_ functions and this
    test pulls Path into the module's namespace so future fixture
    extensions don't re-import it."""
    assert Path
