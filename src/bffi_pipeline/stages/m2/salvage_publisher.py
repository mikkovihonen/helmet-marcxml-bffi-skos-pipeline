"""P-41 Phase B2 — publisher-as-corporate-creator salvage tier.

For records missing 1XX/7XX where the leader/06 record-type code is
in the cataloguer-confirmed set
(:attr:`Settings.creator_salvage_b2_leader_06_codes`), promote the
MARC ``260$b`` / ``264$b`` publisher literal to a synthesised MARC 710
corporate-creator added entry. The 710 (corporate added entry) shape
keeps the publisher as a non-primary contribution so M5/M6/M8
union-find don't accidentally collapse Works across publishers; the
``$5 FI-HELME/synth-v1`` marker lets cataloguers and consumers tell
synth-coded contributions apart.

**Default: feature flag OFF.** This tier ships with the code in place
but ``Settings.creator_salvage_b2_publisher_promotion_enabled`` set
to ``False`` pending the cataloguer leader/06 sign-off recorded as
Ask 6 in ``docs/external-dependencies.md``. Activation is a single
config change once the cataloguer team confirms which leader/06 codes
warrant publisher-as-creator promotion (current candidates: ``a``
language material, ``e`` cartographic, ``g`` projected medium, ``m``
computer file).

The dispatcher (:func:`bffi_pipeline.stages.m2.salvage.try_salvage_minimum_content`)
fires this tier between B1 (245$c parse) and B3 (sentinel) when the
flag is on AND the leader/06 matches AND the record has a 260$b or
264$b. Falls through to B3 otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from lxml import etree

from bffi_pipeline.config import Settings

_MARC_NS: Final[str] = "http://www.loc.gov/MARC21/slim"
_DATAFIELD_TAG: Final[str] = f"{{{_MARC_NS}}}datafield"
_SUBFIELD_TAG: Final[str] = f"{{{_MARC_NS}}}subfield"
_LEADER_TAG: Final[str] = f"{{{_MARC_NS}}}leader"

#: MARC leader position 6 holds the record-type code (one char). Lifted
#: out of the position arithmetic for readability.
_LEADER_RECORD_TYPE_POS: Final[int] = 6

#: Confidence band for B2 publisher promotion — kept lower than B1
#: regex (0.5-0.8) because the inference "this publisher is the
#: creator" is structurally weaker than "this 245$c names the author."
B2_CONFIDENCE: Final[float] = 0.3


@dataclass(frozen=True)
class PublisherPromotion:
    """The data the dispatcher needs to emit a synthesis record + a
    MARC 710 datafield. ``publisher`` is the verbatim literal from
    MARC 260$b/264$b after trimming MARC ISBD trailing punctuation."""

    publisher: str
    marc_source: str  # "260$b" or "264$b" — surfaced in the TSV


def _parse_leader_record_type(record: etree._Element) -> str | None:
    """Return the leader/06 character (record-type code), or ``None``
    if the leader is malformed."""
    leader_el = record.find(_LEADER_TAG)
    if leader_el is None or leader_el.text is None:
        return None
    leader = leader_el.text
    if len(leader) <= _LEADER_RECORD_TYPE_POS:
        return None
    return leader[_LEADER_RECORD_TYPE_POS]


def _read_publisher(record: etree._Element) -> tuple[str, str] | None:
    """Read 260$b first, then 264$b. Returns ``(publisher_text,
    source_tag)`` or ``None`` when neither tag carries a non-empty
    publisher literal."""
    for marc_tag, source_key in (("260", "260$b"), ("264", "264$b")):
        for df in record.iterfind(_DATAFIELD_TAG):
            if df.get("tag") != marc_tag:
                continue
            for sf in df.iterfind(_SUBFIELD_TAG):
                if sf.get("code") != "b":
                    continue
                text = (sf.text or "").strip()
                # Strip MARC ISBD trailing punctuation that's common
                # in 260$b ("Helsinki :", "Otava ;").
                text = text.rstrip(",.;:/ ").strip()
                if text:
                    return (text, source_key)
    return None


def _parse_leader_06_codes(raw: str) -> frozenset[str]:
    """Parse the comma-separated ``creator_salvage_b2_leader_06_codes``
    setting into a frozenset of single-character codes. Whitespace and
    empty entries are ignored; longer-than-1-char entries are ignored
    (defensive against typos in the env var)."""
    return frozenset(
        token for token in (chunk.strip() for chunk in raw.split(",")) if len(token) == 1
    )


def try_promote_publisher(
    record: etree._Element,
    *,
    settings: Settings,
) -> PublisherPromotion | None:
    """Run B2's gates. Returns the promotion data or ``None`` when:
    feature flag is off, leader/06 is not in the confirmed set, the
    leader is malformed, or no 260$b/264$b literal is present.
    """
    if not settings.creator_salvage_b2_publisher_promotion_enabled:
        return None
    confirmed_codes = _parse_leader_06_codes(settings.creator_salvage_b2_leader_06_codes)
    if not confirmed_codes:
        # Feature flag on but no leader/06 codes confirmed → no-op.
        # Defensive guard against the misconfiguration where the flag
        # gets flipped before the cataloguer-confirmed set is populated.
        return None
    record_type = _parse_leader_record_type(record)
    if record_type is None or record_type not in confirmed_codes:
        return None
    publisher_pair = _read_publisher(record)
    if publisher_pair is None:
        return None
    publisher_text, source_key = publisher_pair
    return PublisherPromotion(publisher=publisher_text, marc_source=source_key)


def build_publisher_datafield(promotion: PublisherPromotion, *, marker: str) -> etree._Element:
    """Build a synthesised corporate-creator MARC 710 datafield.

    Shape::

      <datafield tag="710" ind1="2" ind2=" ">
        <subfield code="a">{publisher}</subfield>
        <subfield code="e">tekijä</subfield>
        <subfield code="5">FI-HELME/synth-v1</subfield>
      </datafield>

    ``ind1="2"`` = name in direct order (corporate body convention).
    ``$e`` = relator term ``tekijä`` (author / creator — Finnish
    cataloguing convention for the corporate-as-author case).
    """
    df = etree.Element(_DATAFIELD_TAG, attrib={"tag": "710", "ind1": "2", "ind2": " "})
    a = etree.SubElement(df, _SUBFIELD_TAG, attrib={"code": "a"})
    a.text = promotion.publisher
    e = etree.SubElement(df, _SUBFIELD_TAG, attrib={"code": "e"})
    e.text = "tekijä"
    s = etree.SubElement(df, _SUBFIELD_TAG, attrib={"code": "5"})
    s.text = marker
    return df


__all__ = [
    "B2_CONFIDENCE",
    "PublisherPromotion",
    "build_publisher_datafield",
    "try_promote_publisher",
]
