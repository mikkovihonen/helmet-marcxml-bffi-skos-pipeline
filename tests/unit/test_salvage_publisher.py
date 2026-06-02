"""Unit tests for P-41 Phase B2 — publisher-as-corporate-creator salvage."""

from __future__ import annotations

from typing import Final

from lxml import etree

from bffi_pipeline.config import Settings
from bffi_pipeline.stages.m2.salvage_publisher import (
    PublisherPromotion,
    build_publisher_datafield,
    try_promote_publisher,
)

_MARC_NS: Final[str] = "http://www.loc.gov/MARC21/slim"


def _record(
    *,
    leader: str = "00000nam a2200000 a 4500",
    publisher_tag: str | None = "260",
    publisher: str | None = "Otava",
) -> etree._Element:
    """Build a bare MARC record element for B2 unit tests. The
    leader's 6th character is the record-type code B2 keys on."""
    root = etree.Element(f"{{{_MARC_NS}}}record")
    leader_el = etree.SubElement(root, f"{{{_MARC_NS}}}leader")
    leader_el.text = leader
    if publisher_tag is not None and publisher is not None:
        df = etree.SubElement(
            root,
            f"{{{_MARC_NS}}}datafield",
            attrib={"tag": publisher_tag, "ind1": " ", "ind2": " "},
        )
        b = etree.SubElement(df, f"{{{_MARC_NS}}}subfield", attrib={"code": "b"})
        b.text = publisher
    return root


def _on_settings(*, codes: str = "a", enabled: bool = True) -> Settings:
    """Build a Settings with B2 enabled for the given leader/06 codes
    (comma-separated, single-char each)."""
    return Settings(  # type: ignore[call-arg]
        BFFI_CREATOR_SALVAGE_B2_PUBLISHER_ENABLED=str(enabled).lower(),
        BFFI_CREATOR_SALVAGE_B2_LEADER06=codes,
    )


class TestFeatureFlagGuards:
    """B2 ships default-off. All three gates (flag, leader/06 set,
    publisher presence) must pass for promotion to fire."""

    def test_default_settings_disable_b2(self) -> None:
        record = _record()
        # Default Settings: flag off, codes empty.
        promotion = try_promote_publisher(record, settings=Settings())
        assert promotion is None

    def test_flag_on_but_no_codes_is_noop(self) -> None:
        record = _record()
        settings = _on_settings(codes="", enabled=True)
        assert try_promote_publisher(record, settings=settings) is None

    def test_flag_off_with_codes_is_noop(self) -> None:
        record = _record()
        settings = _on_settings(codes="a", enabled=False)
        assert try_promote_publisher(record, settings=settings) is None


class TestLeader06Matching:
    def test_matching_leader_06_returns_promotion(self) -> None:
        record = _record(leader="00000nam a2200000 a 4500")  # leader/06 = 'a'
        settings = _on_settings(codes="a")
        promotion = try_promote_publisher(record, settings=settings)
        assert promotion == PublisherPromotion(publisher="Otava", marc_source="260$b")

    def test_non_matching_leader_06_falls_through(self) -> None:
        # leader/06 = 'a' but only 'm' is confirmed
        record = _record(leader="00000nam a2200000 a 4500")
        settings = _on_settings(codes="m")
        assert try_promote_publisher(record, settings=settings) is None

    def test_multi_code_set_accepts_any_matching_code(self) -> None:
        record = _record(leader="00000nem a2200000 a 4500")  # leader/06 = 'e'
        settings = _on_settings(codes="a, e, g")
        promotion = try_promote_publisher(record, settings=settings)
        assert promotion is not None


class TestPublisherReading:
    def test_264b_used_when_260b_absent(self) -> None:
        record = _record(publisher_tag="264", publisher="Otava")
        settings = _on_settings(codes="a")
        promotion = try_promote_publisher(record, settings=settings)
        assert promotion is not None
        assert promotion.marc_source == "264$b"
        assert promotion.publisher == "Otava"

    def test_trailing_isbd_punctuation_is_stripped(self) -> None:
        # Helmet cataloguers often type "Otava ;" or "Helsinki : Otava,"
        record = _record(publisher="Otava ;")
        settings = _on_settings(codes="a")
        promotion = try_promote_publisher(record, settings=settings)
        assert promotion is not None
        assert promotion.publisher == "Otava"

    def test_empty_publisher_returns_none(self) -> None:
        record = _record(publisher="   ")
        settings = _on_settings(codes="a")
        assert try_promote_publisher(record, settings=settings) is None

    def test_no_260b_or_264b_returns_none(self) -> None:
        record = _record(publisher_tag=None, publisher=None)
        settings = _on_settings(codes="a")
        assert try_promote_publisher(record, settings=settings) is None


class TestSynthesisedDatafield:
    """``build_publisher_datafield`` produces the MARC 710 shape the
    marc2bibframe2 XSLT consumes as a corporate-creator added entry."""

    def test_shape_has_corporate_710_with_synth_marker(self) -> None:
        promotion = PublisherPromotion(publisher="Otava", marc_source="260$b")
        df = build_publisher_datafield(promotion, marker="FI-HELME/synth-v1")
        assert df.get("tag") == "710"
        assert df.get("ind1") == "2"  # name in direct order
        subfields = {sf.get("code"): sf.text for sf in df}
        assert subfields == {
            "a": "Otava",
            "e": "tekijä",
            "5": "FI-HELME/synth-v1",
        }
