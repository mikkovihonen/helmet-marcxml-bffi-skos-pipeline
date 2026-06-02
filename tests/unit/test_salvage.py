"""Unit tests for P-41 Phase B — M2 creator-salvage dispatcher."""

from __future__ import annotations

from typing import Final

import pytest
from lxml import etree

from bffi_pipeline.config import Settings
from bffi_pipeline.stages.m2.salvage import (
    SYNTH_MARKER,
    SalvageOutcome,
    try_salvage_minimum_content,
)

_MARC_NS: Final[str] = "http://www.loc.gov/MARC21/slim"


def _settings() -> Settings:
    """Build a fresh Settings instance for each test — Pydantic
    ``model_config(extra="ignore")`` means we can construct it
    without any env vars set. Keeps tests independent of the host
    machine's actual config."""
    return Settings()


def _marc_record(
    *,
    leader: str = "00000nam a2200000 a 4500",
    title: str | None = "Test Title",
    title_subfield_c: str | None = None,
    creator_tag: str | None = None,
    creator_name: str | None = None,
) -> etree._ElementTree:
    """Build a minimal in-memory MARCXML tree for salvage tests.

    Includes leader + 008 + 336/337/338 + 245 by default so the only
    missing-content trigger is the absent 1XX/7XX — which is what
    P-41 Phase B targets. Subset of the real schema needed for
    salvage's structural checks; not strictly valid MARCXML but enough
    for the salvage layer's purpose (the salvage code reads from the
    in-memory tree, not from an XSD-validated wire form).
    """
    root = etree.Element(f"{{{_MARC_NS}}}record")
    leader_el = etree.SubElement(root, f"{{{_MARC_NS}}}leader")
    leader_el.text = leader
    cf008 = etree.SubElement(root, f"{{{_MARC_NS}}}controlfield", attrib={"tag": "008"})
    cf008.text = "001212s2000    fi            000 0 fin d"
    for rda_tag in ("336", "337", "338"):
        df = etree.SubElement(
            root,
            f"{{{_MARC_NS}}}datafield",
            attrib={"tag": rda_tag, "ind1": " ", "ind2": " "},
        )
        a = etree.SubElement(df, f"{{{_MARC_NS}}}subfield", attrib={"code": "a"})
        a.text = "stub"
    if title is not None:
        df245 = etree.SubElement(
            root,
            f"{{{_MARC_NS}}}datafield",
            attrib={"tag": "245", "ind1": "0", "ind2": "0"},
        )
        a = etree.SubElement(df245, f"{{{_MARC_NS}}}subfield", attrib={"code": "a"})
        a.text = title
        if title_subfield_c is not None:
            c = etree.SubElement(df245, f"{{{_MARC_NS}}}subfield", attrib={"code": "c"})
            c.text = title_subfield_c
    if creator_tag is not None and creator_name is not None:
        df_creator = etree.SubElement(
            root,
            f"{{{_MARC_NS}}}datafield",
            attrib={"tag": creator_tag, "ind1": "1", "ind2": " "},
        )
        a = etree.SubElement(df_creator, f"{{{_MARC_NS}}}subfield", attrib={"code": "a"})
        a.text = creator_name
    return etree.ElementTree(root)


def _datafields_by_tag(tree: etree._ElementTree, tag: str) -> list[etree._Element]:
    return [
        df for df in tree.getroot().iterfind(f"{{{_MARC_NS}}}datafield") if df.get("tag") == tag
    ]


def _subfield_text(df: etree._Element, code: str) -> str | None:
    for sf in df.iterfind(f"{{{_MARC_NS}}}subfield"):
        if sf.get("code") == code:
            return sf.text
    return None


class TestNoSalvageNeeded:
    """When the record already has a creator, the dispatcher is a no-op."""

    def test_existing_100_creator_returns_none(self) -> None:
        tree = _marc_record(creator_tag="100", creator_name="Margaret Atwood")
        outcome = try_salvage_minimum_content(tree, bib_id="b001", settings=_settings())
        assert outcome is None
        # No spurious 700/710 added.
        assert _datafields_by_tag(tree, "700") == []
        assert _datafields_by_tag(tree, "710") == []

    def test_existing_700_creator_returns_none(self) -> None:
        tree = _marc_record(creator_tag="700", creator_name="Tove Jansson")
        outcome = try_salvage_minimum_content(tree, bib_id="b002", settings=_settings())
        assert outcome is None


class TestB1Path:
    """245$c parse hits land in B1 with a synthesised 100 (primary
    author role) or 700 (added entry for editor / translator / etc.)."""

    def test_b1_synthesises_100_for_author_role(self) -> None:
        tree = _marc_record(title_subfield_c="kirjoittanut Mika Waltari")
        outcome = try_salvage_minimum_content(tree, bib_id="b003", settings=_settings())
        assert outcome is not None
        assert outcome.tier == "B1"
        assert len(outcome.records) == 1
        record = outcome.records[0]
        assert record.bib_id == "b003"
        assert record.synthesised_value == "Mika Waltari"
        assert record.tier == "B1"
        assert record.marc_source == "245$c"
        assert record.confidence == 0.8
        assert "marc=100" in record.method

        added = _datafields_by_tag(tree, "100")
        assert len(added) == 1
        assert _subfield_text(added[0], "a") == "Mika Waltari"
        assert _subfield_text(added[0], "e") == "tekijä"
        assert _subfield_text(added[0], "5") == SYNTH_MARKER

    def test_b1_synthesises_700_for_editor_role(self) -> None:
        tree = _marc_record(title_subfield_c="toim. Helena Ruuska")
        outcome = try_salvage_minimum_content(tree, bib_id="b003e", settings=_settings())
        assert outcome is not None
        assert outcome.tier == "B1"
        assert "marc=700" in outcome.records[0].method
        added = _datafields_by_tag(tree, "700")
        assert len(added) == 1
        assert _subfield_text(added[0], "a") == "Helena Ruuska"
        assert _subfield_text(added[0], "e") == "toimittaja"
        # And no 100 was emitted — an editor isn't the primary creator.
        assert _datafields_by_tag(tree, "100") == []

    def test_b1_multi_author_promotes_first_to_100_rest_to_700(self) -> None:
        tree = _marc_record(title_subfield_c="Liisa Louhela ja Pekka Halonen")
        outcome = try_salvage_minimum_content(tree, bib_id="b004", settings=_settings())
        assert outcome is not None
        assert outcome.tier == "B1"
        assert {r.synthesised_value for r in outcome.records} == {
            "Liisa Louhela",
            "Pekka Halonen",
        }
        primary = _datafields_by_tag(tree, "100")
        added = _datafields_by_tag(tree, "700")
        assert len(primary) == 1
        assert len(added) == 1
        assert _subfield_text(primary[0], "a") == "Liisa Louhela"
        assert _subfield_text(added[0], "a") == "Pekka Halonen"


class TestB3SentinelFallthrough:
    """When B1 can't parse 245$c (or 245 is absent / empty), B3 fires."""

    def test_no_245c_falls_through_to_sentinel(self) -> None:
        tree = _marc_record(title="Title without statement of responsibility")
        outcome = try_salvage_minimum_content(tree, bib_id="b005", settings=_settings())
        assert outcome is not None
        assert outcome.tier == "B3"
        assert len(outcome.records) == 1
        record = outcome.records[0]
        assert record.tier == "B3"
        assert record.method == "anonymous-by-convention"
        assert record.confidence == 0.1
        assert record.synthesised_value == "http://urn.fi/URN:NBN:fi:bib:agent:unknown"

        added = _datafields_by_tag(tree, "710")
        assert len(added) == 1
        assert _subfield_text(added[0], "a") == "Tekijä tuntematon"
        assert _subfield_text(added[0], "0") == "http://urn.fi/URN:NBN:fi:bib:agent:unknown"
        assert _subfield_text(added[0], "5") == SYNTH_MARKER

    def test_corporate_245c_falls_through_to_sentinel(self) -> None:
        """When 245$c looks corporate, B1 rejects → B3 fires."""
        tree = _marc_record(title_subfield_c="Helsingin kaupunki")
        outcome = try_salvage_minimum_content(tree, bib_id="b006", settings=_settings())
        assert outcome is not None
        assert outcome.tier == "B3"
        # No 700 was added (B1 didn't fire).
        assert _datafields_by_tag(tree, "700") == []
        # 710 sentinel was added.
        assert len(_datafields_by_tag(tree, "710")) == 1

    def test_empty_245c_falls_through_to_sentinel(self) -> None:
        tree = _marc_record(title_subfield_c="  ")
        outcome = try_salvage_minimum_content(tree, bib_id="b007", settings=_settings())
        assert outcome is not None
        assert outcome.tier == "B3"


class TestMasterFlag:
    """The ``creator_salvage_enabled`` flag is the rollback knob."""

    def test_disabled_flag_short_circuits_to_none(self) -> None:
        tree = _marc_record(title_subfield_c="kirjoittanut Mika Waltari")
        settings = Settings(BFFI_CREATOR_SALVAGE_ENABLED="false")  # type: ignore[call-arg]
        outcome = try_salvage_minimum_content(tree, bib_id="b008", settings=settings)
        assert outcome is None
        # Tree is untouched.
        assert _datafields_by_tag(tree, "700") == []
        assert _datafields_by_tag(tree, "710") == []


class TestCollectionWrapper:
    """``<marc:collection>`` wrapper is handled the same way
    ``validate_minimum_content`` handles it."""

    def test_collection_wrapped_record_salvages_correctly(self) -> None:
        record_tree = _marc_record(title_subfield_c="by Margaret Atwood")
        collection = etree.Element(f"{{{_MARC_NS}}}collection")
        collection.append(record_tree.getroot())
        tree = etree.ElementTree(collection)

        outcome = try_salvage_minimum_content(tree, bib_id="b009", settings=_settings())
        assert outcome is not None
        assert outcome.tier == "B1"
        # 100 was added inside the wrapped record (author role → primary),
        # not at the collection level.
        added = [df for df in tree.iter(f"{{{_MARC_NS}}}datafield") if df.get("tag") == "100"]
        assert len(added) == 1


def test_salvage_outcome_is_immutable() -> None:
    """SalvageOutcome is a frozen dataclass; the audit trail is
    write-once."""
    outcome = SalvageOutcome(tier="B1", records=())
    # Frozen dataclass raises on attribute assignment.
    with pytest.raises(Exception, match="cannot assign"):
        outcome.tier = "B3"  # type: ignore[misc]
