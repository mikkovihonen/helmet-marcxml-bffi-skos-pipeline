"""Unit tests for the BFFI → MARC coverage diagnostic.

Locks the field- and subfield-level counting against hand-built MARCXML
fixtures so the report's totals stay reliable across registry changes.
"""

from __future__ import annotations

from pathlib import Path

from bffi_pipeline.diagnostic.marc_coverage import (
    analyse_corpus,
    format_report,
)

_MARC_NS = "http://www.loc.gov/MARC21/slim"


def _write_marcxml(path: Path, body: str) -> None:
    path.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?><collection xmlns="{_MARC_NS}">{body}</collection>',
        encoding="utf-8",
    )


def test_analyse_counts_leader_controlfield_datafield_as_field_rows(tmp_path: Path) -> None:
    """One ``<leader>``, two ``<controlfield>`` and three ``<datafield>``
    rows should produce ``total_fields == 6``. The mix of registered
    (leader, 001, 245) and unregistered (007, 099) tags drives the
    covered count."""
    _write_marcxml(
        tmp_path / "r1.xml",
        """
        <record>
          <leader>00000nam a2200000 a 4500</leader>
          <controlfield tag="001">b1</controlfield>
          <controlfield tag="007">vd cvaiz|</controlfield>
          <datafield tag="245" ind1="0" ind2="0">
            <subfield code="a">Title</subfield>
            <subfield code="b">subtitle</subfield>
          </datafield>
          <datafield tag="099" ind1=" " ind2=" ">
            <subfield code="a">local class</subfield>
          </datafield>
          <datafield tag="100" ind1="1" ind2=" ">
            <subfield code="a">Author</subfield>
            <subfield code="4">aut</subfield>
            <subfield code="e">writer</subfield>
          </datafield>
        </record>
        """,
    )

    report = analyse_corpus(tmp_path)

    assert report.records == 1
    assert report.total_fields == 6
    # Covered: leader, 001, 245, 100. Uncovered: 007, 099.
    assert report.covered_fields == 4


def test_analyse_counts_subfields_per_tag_pair(tmp_path: Path) -> None:
    """Subfield-level coverage is keyed on ``(tag, code)``. ``245 $a $b``
    are covered (245 emits both); ``100 $a $4 $e`` are all covered;
    ``245 $x`` is a covered tag with an uncovered code; ``099`` is a
    wholly uncovered tag."""
    _write_marcxml(
        tmp_path / "r1.xml",
        """
        <record>
          <leader>00000nam a2200000 a 4500</leader>
          <datafield tag="245" ind1="0" ind2="0">
            <subfield code="a">Title</subfield>
            <subfield code="b">subtitle</subfield>
            <subfield code="x">unsupported</subfield>
          </datafield>
          <datafield tag="099" ind1=" " ind2=" ">
            <subfield code="a">local class</subfield>
          </datafield>
          <datafield tag="100" ind1="1" ind2=" ">
            <subfield code="a">Author</subfield>
            <subfield code="4">aut</subfield>
            <subfield code="e">writer</subfield>
          </datafield>
        </record>
        """,
    )

    report = analyse_corpus(tmp_path)

    # Subfields in source: 245 $a $b $x, 099 $a, 100 $a $4 $e = 7
    assert report.total_subfields == 7
    # Covered: 245 $a, 245 $b, 100 $a, 100 $4, 100 $e = 5
    assert report.covered_subfields == 5

    stat_245 = report.per_tag["245"]
    assert stat_245.subfield_total == 3
    assert stat_245.subfield_covered == 2
    assert stat_245.uncovered_subfield_codes == {"x": 1}

    stat_099 = report.per_tag["099"]
    assert stat_099.covered is False
    assert stat_099.subfield_total == 1
    assert stat_099.subfield_covered == 0
    assert stat_099.uncovered_subfield_codes == {"a": 1}


def test_analyse_aggregates_across_multiple_records(tmp_path: Path) -> None:
    """Multi-record collection files and multi-file directories both
    contribute to the running totals."""
    _write_marcxml(
        tmp_path / "a.xml",
        """
        <record>
          <leader>00000nam a2200000 a 4500</leader>
          <datafield tag="245" ind1="0" ind2="0">
            <subfield code="a">A</subfield>
          </datafield>
        </record>
        <record>
          <leader>00000nam a2200000 a 4500</leader>
          <datafield tag="245" ind1="0" ind2="0">
            <subfield code="a">B</subfield>
          </datafield>
        </record>
        """,
    )
    _write_marcxml(
        tmp_path / "b.xml",
        """
        <record>
          <leader>00000nam a2200000 a 4500</leader>
          <datafield tag="245" ind1="0" ind2="0">
            <subfield code="a">C</subfield>
          </datafield>
        </record>
        """,
    )

    report = analyse_corpus(tmp_path)

    assert report.records == 3
    # 3 leaders + 3 245s = 6 field rows; 3 $a subfields
    assert report.total_fields == 6
    assert report.covered_fields == 6
    assert report.total_subfields == 3
    assert report.covered_subfields == 3
    assert report.field_coverage == 1.0
    assert report.subfield_coverage == 1.0


def test_field_and_subfield_coverage_are_independent_signals(tmp_path: Path) -> None:
    """``245`` is in the registry (field-covered), but its ``$x``
    subfield is not. The headline numbers reflect this: 100% field
    coverage, but < 100% subfield coverage."""
    _write_marcxml(
        tmp_path / "r.xml",
        """
        <record>
          <leader>00000nam a2200000 a 4500</leader>
          <datafield tag="245" ind1="0" ind2="0">
            <subfield code="a">Title</subfield>
            <subfield code="x">unsupported code</subfield>
          </datafield>
        </record>
        """,
    )

    report = analyse_corpus(tmp_path)

    assert report.field_coverage == 1.0
    assert report.subfield_coverage == 0.5


def test_format_report_includes_headline_and_per_tag_sections(tmp_path: Path) -> None:
    """The text rendering carries both the headline coverage numbers
    and the per-tag breakdown — a regression here would silently strip
    one of the two diagnostic signals."""
    _write_marcxml(
        tmp_path / "r.xml",
        """
        <record>
          <leader>00000nam a2200000 a 4500</leader>
          <datafield tag="245" ind1="0" ind2="0">
            <subfield code="a">T</subfield>
          </datafield>
          <datafield tag="099" ind1=" " ind2=" ">
            <subfield code="a">local</subfield>
          </datafield>
        </record>
        """,
    )

    text = format_report(analyse_corpus(tmp_path), top_n=10)

    assert "Records analysed: 1" in text
    assert "Field coverage" in text
    assert "Subfield coverage" in text
    assert "Top 10 tags" in text
    assert "Top uncovered tags" in text
    # 099 is uncovered and should appear in the uncovered list.
    assert "099" in text
