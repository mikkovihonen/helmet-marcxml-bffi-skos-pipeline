"""Source-MARC-field token computation + per-record indexing.

The token format is ``<bib_id>:<tag>:<within-tag-ordinal>`` (P-50). The
ordinal is 1-indexed position of the field instance within its same-tag
bucket, in source MARCXML document order. Controlfields are
ordinal-1 (one instance per tag for the common ones; pathological
multi-instance controlfields rank by encounter order anyway).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_MARC_NAMESPACE: Final[str] = "http://www.loc.gov/MARC21/slim"
_NS: Final[dict[str, str]] = {"m": _MARC_NAMESPACE}


@dataclass(frozen=True)
class SourceMarcField:
    """One source MARC field instance, normalised for correlation.

    Carries enough subfield + value detail for the M2-post correlator
    to match it against a BIBFRAME entity. Indicators are kept so a
    future per-indicator correlation refinement has the data; today
    only ``tag`` + ``subfields`` are consumed.
    """

    bib_id: str
    tag: str
    ordinal: int  # 1-indexed within its tag bucket
    is_control: bool
    ind1: str = " "
    ind2: str = " "
    #: Control-field value (when ``is_control``); ``None`` for datafields.
    value: str | None = None
    #: Datafield subfields in source-MARC order. ``()`` for controlfields.
    subfields: tuple[tuple[str, str], ...] = ()

    @property
    def token(self) -> str:
        return compute_token(self.bib_id, self.tag, self.ordinal)

    def subfield_value(self, code: str) -> str | None:
        """First subfield with the given code; ``None`` if absent."""
        for c, v in self.subfields:
            if c == code:
                return v
        return None


def compute_token(bib_id: str, tag: str, ordinal: int) -> str:
    """Return the canonical token string for one source-MARC field
    instance. See module docstring for the format."""
    if not bib_id:
        raise ValueError("bib_id must be non-empty")
    if not tag or len(tag) != 3:  # noqa: PLR2004
        raise ValueError(f"tag must be the 3-character MARC tag, got {tag!r}")
    if ordinal < 1:
        raise ValueError(f"ordinal must be >= 1, got {ordinal}")
    return f"{bib_id}:{tag}:{ordinal}"


def parse_token(token: str) -> tuple[str, str, int] | None:
    """Inverse of :func:`compute_token`. Returns ``(bib_id, tag, ord)``
    or ``None`` when the string doesn't match the expected shape.

    Tolerates colons in the bib_id (rare but legal in
    ``URN:NBN:fi:...``-style identifiers) by splitting from the right —
    the last two colon-separated tokens are always ``<tag>:<ordinal>``.
    """
    parts = token.rsplit(":", 2)
    if len(parts) != 3:  # noqa: PLR2004
        return None
    bib_id, tag, ord_s = parts
    if not bib_id or len(tag) != 3:  # noqa: PLR2004
        return None
    try:
        ord_i = int(ord_s)
    except ValueError:
        return None
    if ord_i < 1:
        return None
    return bib_id, tag, ord_i


class MarcFieldIndex:
    """Per-record index of source MARC fields keyed by ``<tag, ordinal>``.

    Built once per record from the source MARCXML. The correlator pulls
    the field shape (subfields, indicators) for each token to decide
    which BIBFRAME entities to attach the token to.

    Usage::

        idx = MarcFieldIndex.from_marcxml_path(Path("b10068004.xml"))
        for field in idx.fields:
            print(field.token, field.tag, field.subfields)
        field_650_3 = idx.get("650", 3)
    """

    def __init__(self, bib_id: str, fields: Iterable[SourceMarcField]) -> None:
        self._bib_id = bib_id
        self._fields: tuple[SourceMarcField, ...] = tuple(fields)
        self._by_key: dict[tuple[str, int], SourceMarcField] = {
            (f.tag, f.ordinal): f for f in self._fields
        }

    @classmethod
    def from_marcxml_path(cls, path: Path) -> MarcFieldIndex:
        """Parse a MARCXML file containing one record (optionally wrapped
        in a ``<collection>``) and return the index."""
        return cls._from_root(ET.parse(str(path)).getroot())

    @classmethod
    def from_marcxml_string(cls, xml: str) -> MarcFieldIndex:
        """Parse a MARCXML literal — convenience for tests."""
        return cls._from_root(ET.fromstring(xml))

    @classmethod
    def _from_root(cls, root: ET.Element) -> MarcFieldIndex:
        # Tolerate both <collection><record>…</record></collection> and
        # bare <record>…</record>.
        if root.tag == f"{{{_MARC_NAMESPACE}}}record":
            record = root
        else:
            found = root.find(f"{{{_MARC_NAMESPACE}}}record")
            if found is None:
                raise ValueError("MARCXML has no <record> element")
            record = found
        bib_id = _find_bib_id(record)
        fields = list(_walk_fields(record, bib_id))
        return cls(bib_id, fields)

    @property
    def bib_id(self) -> str:
        return self._bib_id

    @property
    def fields(self) -> tuple[SourceMarcField, ...]:
        return self._fields

    def get(self, tag: str, ordinal: int) -> SourceMarcField | None:
        return self._by_key.get((tag, ordinal))

    def fields_by_tag(self, tag: str) -> tuple[SourceMarcField, ...]:
        return tuple(f for f in self._fields if f.tag == tag)


def _find_bib_id(record: ET.Element) -> str:
    """Read the bib_id from the source record's ``001`` controlfield.

    Helmet records always carry ``001`` as the bib_id. Falls back to
    empty string when absent so the correlator can audit-log records
    with malformed identifiers rather than crashing.
    """
    for cf in record.findall("m:controlfield", _NS):
        if cf.attrib.get("tag") == "001":
            return (cf.text or "").strip()
    return ""


def _walk_fields(record: ET.Element, bib_id: str) -> Iterable[SourceMarcField]:
    """Walk a ``<record>`` element in document order, yielding one
    ``SourceMarcField`` per controlfield + datafield with the
    within-tag ordinal pre-computed.
    """
    tag_counters: dict[str, int] = {}

    # Leader gets a synthetic tag "LDR" + ordinal 1 (one per record). We
    # still track it so the round-trip can emit a token for the leader's
    # derivations, though Phase A doesn't correlate them.
    leader = record.find("m:leader", _NS)
    if leader is not None:
        tag_counters["LDR"] = 1
        yield SourceMarcField(
            bib_id=bib_id,
            tag="LDR",
            ordinal=1,
            is_control=True,
            value=(leader.text or "").strip(),
        )

    for cf in record.findall("m:controlfield", _NS):
        tag = cf.attrib.get("tag", "")
        if not tag:
            continue
        tag_counters[tag] = tag_counters.get(tag, 0) + 1
        yield SourceMarcField(
            bib_id=bib_id,
            tag=tag,
            ordinal=tag_counters[tag],
            is_control=True,
            value=(cf.text or "").strip(),
        )

    for df in record.findall("m:datafield", _NS):
        tag = df.attrib.get("tag", "")
        if not tag:
            continue
        tag_counters[tag] = tag_counters.get(tag, 0) + 1
        subfields = tuple(
            (sf.attrib.get("code", ""), (sf.text or "")) for sf in df.findall("m:subfield", _NS)
        )
        yield SourceMarcField(
            bib_id=bib_id,
            tag=tag,
            ordinal=tag_counters[tag],
            is_control=False,
            ind1=df.attrib.get("ind1", " "),
            ind2=df.attrib.get("ind2", " "),
            subfields=subfields,
        )
