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


def test_changed_fields_sort_before_identical_within_same_tag_bucket() -> None:
    """Cataloguers reading top-to-bottom should see diffs first; the
    sort surfaces them above the noise of identical fields."""
    orig = _record(
        """
        <datafield tag="245" ind1="1" ind2="0">
          <subfield code="a">Same</subfield>
        </datafield>
        <datafield tag="500" ind1=" " ind2=" ">
          <subfield code="a">Lost note</subfield>
        </datafield>
        """
    )
    recon = _record(
        """
        <datafield tag="245" ind1="1" ind2="0">
          <subfield code="a">Same</subfield>
        </datafield>
        """
    )
    diff = diff_records(bib_id="b1", original=orig, reconstructed=recon)
    statuses = [d.status for d in diff.fields]
    # `lost` (500) should appear before `identical` (245) in the sorted list.
    assert statuses.index("lost") < statuses.index("identical")


def test_path_import_is_used_in_some_assertions() -> None:
    """No-op sanity test — pytest discovers test_ functions and this
    test pulls Path into the module's namespace so future fixture
    extensions don't re-import it."""
    assert Path
