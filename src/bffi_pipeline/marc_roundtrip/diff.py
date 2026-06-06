"""Field-by-field MARC diff for the cataloguer round-trip review.

Compares an original MARC record (from Helmet's MARCXML) against a
reconstructed record (produced by :func:`bffi_pipeline.marc_roundtrip.
reconstruct_marc` from the BFFI graph). Pairs fields by ``tag`` +
primary ``$a`` subfield and classifies each pair as

  - ``identical``  — every cataloguer-relevant subfield matches.
  - ``lost``       — original had the field; reconstructed doesn't.
  - ``added``      — reconstructed has a field the original didn't.
  - ``changed``    — both sides have the field but a subfield differs.

The diff is intentionally semantic, not textual: field order doesn't
matter; indicator differences are noted but don't drive `changed`
classification unless the indicator carries meaning the cataloguer
loses (``245`` ind2 non-filing chars, ``650/655`` ind2 thesaurus
flag — surfaced as notes). Round-trip markers (``$5 FI-HELME/bffi-
roundtrip``) on the reconstructed side are ignored when matching.

The result is a JSON-serialisable :class:`RecordDiff` that the HTML
reviewer renders as a side-by-side table per bib.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any, Final, Literal

from bffi_pipeline.marc_roundtrip.converter import (
    LINEAGE_SUBFIELD,
    LINEAGE_VALUE_PREFIX,
    MARC_NAMESPACE,
    ROUNDTRIP_MARKER,
)

_NS: Final[dict[str, str]] = {"m": MARC_NAMESPACE}

#: Subfield codes that don't drive `changed` classification — the round-trip
#: marker is the canonical example; cataloguer-set ``$5`` codes on the original
#: are kept and compared, but the marker we add on the reconstructed side is
#: filtered to avoid false-positive diffs.
_NOISE_SUBFIELD_VALUES: Final[frozenset[str]] = frozenset({ROUNDTRIP_MARKER})

#: MARC tags whose presence in ``ReconstructedRecord.skipped_tags`` means the
#: converter intentionally didn't reproduce them. We classify the original's
#: instance as ``lost-converter-gap`` so the HTML can distinguish "BFFI
#: doesn't carry this" from "the converter could carry this but didn't".
#:
#: ``tag-changed`` (P-48 Phase A): the lineage token pairs an original field
#: to a reconstructed field with a DIFFERENT tag (e.g. source 651 Kreikka
#: paired with a recon 650 — the b10303327 Greece bug shape). Surfaces in
#: the HTML viewer as its own colour band so cataloguers see misroutes
#: directly.
DiffStatus = Literal["identical", "lost", "added", "changed", "lost-converter-gap", "tag-changed"]


@dataclass(frozen=True)
class SubfieldRecord:
    code: str
    value: str

    def to_json(self) -> dict[str, str]:
        return {"code": self.code, "value": self.value}


@dataclass(frozen=True)
class FieldRecord:
    """One MARC datafield or controlfield, normalised for diff."""

    tag: str
    is_control: bool
    ind1: str = " "
    ind2: str = " "
    value: str | None = None  # control fields carry their data here
    subfields: tuple[SubfieldRecord, ...] = ()
    #: P-48 Phase A lineage token (``<tag>-<ord>``) parsed off the
    #: reconstructed side's ``$9 src=…`` subfield. ``None`` on the
    #: original side (cataloguer-supplied $9 with other content is
    #: kept as a real subfield, not parsed as lineage) and on
    #: lineage-absent reconstructed fields (the flat Instance-side
    #: predicates pending P-48 Phase B).
    lineage: str | None = None

    def primary_a(self) -> str | None:
        """The first ``$a`` subfield value — the natural pair key for
        datafields with repeated tags (multiple 655s, etc.)."""
        for sf in self.subfields:
            if sf.code == "a":
                return sf.value
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "is_control": self.is_control,
            "ind1": self.ind1,
            "ind2": self.ind2,
            "value": self.value,
            "subfields": [sf.to_json() for sf in self.subfields],
            # ``lineage`` deliberately NOT serialised — it's an internal
            # pairing key, not user-facing data. The HTML viewer reads
            # the per-field ``status`` (incl. the new ``tag-changed``)
            # which already encodes the pairing decision.
        }


@dataclass(frozen=True)
class FieldDiff:
    """One pair of (original, reconstructed) fields after matching."""

    tag: str
    status: DiffStatus
    original: FieldRecord | None
    reconstructed: FieldRecord | None
    notes: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "status": self.status,
            "original": self.original.to_json() if self.original else None,
            "reconstructed": self.reconstructed.to_json() if self.reconstructed else None,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class RecordDiff:
    """Full per-bib diff. Serialised to JSON for the HTML reviewer."""

    bib_id: str
    fields: tuple[FieldDiff, ...]
    skipped_tags: tuple[str, ...]
    summary: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "bib_id": self.bib_id,
            "fields": [f.to_json() for f in self.fields],
            "skipped_tags": list(self.skipped_tags),
            "summary": dict(self.summary),
        }


def diff_records(
    *,
    bib_id: str,
    original: ET.Element,
    reconstructed: ET.Element,
    skipped_tags: Iterable[str] = (),
) -> RecordDiff:
    """Compute the field-by-field diff for one bib. Both elements are
    ``<record>`` nodes in the MARCXML namespace."""
    original_fields = _parse_record(original)
    reconstructed_fields = _parse_record(reconstructed, strip_marker=True)

    skipped = set(skipped_tags)
    pairs = _pair_fields(original_fields, reconstructed_fields)

    diffs: list[FieldDiff] = []
    for orig, recon in pairs:
        if orig is None and recon is not None:
            diffs.append(
                FieldDiff(
                    tag=recon.tag,
                    status="added",
                    original=None,
                    reconstructed=recon,
                )
            )
            continue
        if recon is None and orig is not None:
            if orig.tag in skipped:
                diffs.append(
                    FieldDiff(
                        tag=orig.tag,
                        status="lost-converter-gap",
                        original=orig,
                        reconstructed=None,
                        notes=(
                            "Converter intentionally skipped this tag — see "
                            "ReconstructedRecord.skipped_tags.",
                        ),
                    )
                )
            else:
                diffs.append(
                    FieldDiff(
                        tag=orig.tag,
                        status="lost",
                        original=orig,
                        reconstructed=None,
                    )
                )
            continue
        assert orig is not None and recon is not None
        notes = list(_indicator_notes(orig, recon))
        # P-48 Phase A: lineage-paired across tags = the converter
        # routed the source field to the wrong MARC tag. The data
        # didn't disappear; it's just labelled wrong. Cataloguers
        # need to see this distinctly so they can decide whether
        # the routing change is acceptable (a "yes, 651 IS now 650
        # because we collapsed geographic into topical") or a bug
        # (b10303327's Greece — should stay 651).
        if orig.tag != recon.tag:
            status: DiffStatus = "tag-changed"
            notes.append(f"source tag {orig.tag} → reconstructed tag {recon.tag}")
        elif _subfields_equal(orig, recon):
            status = "identical"
        else:
            status = "changed"
        diffs.append(
            FieldDiff(
                # Original's tag — the cataloguer's authoritative
                # view of what the field IS. The recon's tag (when
                # different) is surfaced via the ``status`` +
                # ``notes`` so the row sorts under the source tag.
                tag=orig.tag,
                status=status,
                original=orig,
                reconstructed=recon,
                notes=tuple(notes),
            )
        )

    summary = _summarise(diffs)
    diffs_sorted = tuple(sorted(diffs, key=_diff_sort_key))
    return RecordDiff(
        bib_id=bib_id,
        fields=diffs_sorted,
        skipped_tags=tuple(sorted(skipped)),
        summary=summary,
    )


def _parse_record(record: ET.Element, *, strip_marker: bool = False) -> list[FieldRecord]:
    """Walk a ``<record>`` element into a flat list of FieldRecords."""
    out: list[FieldRecord] = []
    leader = record.find("m:leader", _NS)
    if leader is not None and leader.text is not None:
        out.append(FieldRecord(tag="LDR", is_control=True, value=leader.text))
    for cf in record.findall("m:controlfield", _NS):
        tag = cf.attrib.get("tag", "")
        out.append(FieldRecord(tag=tag, is_control=True, value=cf.text or ""))
    for df in record.findall("m:datafield", _NS):
        tag = df.attrib.get("tag", "")
        ind1 = df.attrib.get("ind1", " ")
        ind2 = df.attrib.get("ind2", " ")
        subfields: list[SubfieldRecord] = []
        lineage: str | None = None
        for sf in df.findall("m:subfield", _NS):
            code = sf.attrib.get("code", "")
            value = sf.text or ""
            if strip_marker and value in _NOISE_SUBFIELD_VALUES:
                continue
            # P-48 Phase A: parse + strip the lineage subfield on the
            # reconstructed side only. Cataloguer-supplied ``$9`` with
            # other content (not starting with ``src=``) passes
            # through as a normal subfield and survives the diff
            # comparison.
            if strip_marker and code == LINEAGE_SUBFIELD and value.startswith(LINEAGE_VALUE_PREFIX):
                lineage = value[len(LINEAGE_VALUE_PREFIX) :]
                continue
            subfields.append(SubfieldRecord(code=code, value=value))
        out.append(
            FieldRecord(
                tag=tag,
                is_control=False,
                ind1=ind1,
                ind2=ind2,
                subfields=tuple(subfields),
                lineage=lineage,
            )
        )
    return out


def _pair_fields(
    original: list[FieldRecord], reconstructed: list[FieldRecord]
) -> list[tuple[FieldRecord | None, FieldRecord | None]]:
    """Pair fields between the two records.

    Algorithm:
      - Control fields (LDR, 001-009) pair by tag (one-to-one).
      - Datafields: bucket by tag, then within a bucket pair by ``$a``
        when present, falling back to position. Surplus on either side
        becomes lost/added respectively.
    """
    pairs: list[tuple[FieldRecord | None, FieldRecord | None]] = []
    pairs.extend(_pair_control_fields(original, reconstructed))
    pairs.extend(_pair_data_fields(original, reconstructed))
    return pairs


def _pair_control_fields(
    original: list[FieldRecord], reconstructed: list[FieldRecord]
) -> list[tuple[FieldRecord | None, FieldRecord | None]]:
    orig_ctl = {f.tag: f for f in original if f.is_control}
    recon_ctl = {f.tag: f for f in reconstructed if f.is_control}
    return [
        (orig_ctl.get(tag), recon_ctl.get(tag)) for tag in sorted(set(orig_ctl) | set(recon_ctl))
    ]


def _pair_data_fields(
    original: list[FieldRecord], reconstructed: list[FieldRecord]
) -> list[tuple[FieldRecord | None, FieldRecord | None]]:
    """Pair data fields between the two records.

    P-48 Phase A pass: when a reconstructed field carries a
    ``$9 src=<tag>-<ord>`` lineage token, look up the original field
    by (tag, 1-indexed position within the tag bucket) and pair them
    explicitly — even if their tags differ on the two sides (the
    misroute case). Whatever's left unpaired falls through to the
    legacy tag-bucket heuristic.
    """
    orig_data = [f for f in original if not f.is_control]
    recon_data = [f for f in reconstructed if not f.is_control]

    # Build the lineage lookup over the original side. Position
    # within the source's tag bucket IS the second half of the
    # lineage token. M3's positional counter for ``#Topic650-N`` /
    # ``#Place651-N`` is 1-indexed-within-record (not 1-indexed-
    # within-tag-bucket), so we walk the original record once and
    # number each tag's instances in encounter order. That matches
    # marc2bibframe2's own per-record counter.
    orig_by_lineage: dict[str, FieldRecord] = {}
    tag_counters: dict[str, int] = {}
    for f in orig_data:
        tag_counters[f.tag] = tag_counters.get(f.tag, 0) + 1
        orig_by_lineage[f"{f.tag}-{tag_counters[f.tag]}"] = f

    pairs: list[tuple[FieldRecord | None, FieldRecord | None]] = []
    matched_orig_ids: set[int] = set()
    residue_recon: list[FieldRecord] = []
    for recon in recon_data:
        if recon.lineage is None:
            residue_recon.append(recon)
            continue
        orig_match = orig_by_lineage.get(recon.lineage)
        if orig_match is not None and id(orig_match) not in matched_orig_ids:
            matched_orig_ids.add(id(orig_match))
            pairs.append((orig_match, recon))
        else:
            # Lineage points at an original we already matched (the
            # converter emitted two rows from one source field — rare
            # but possible) OR at a token absent from the original
            # (the converter mis-stamped). Fall back to heuristic for
            # this row.
            residue_recon.append(recon)

    residue_orig = [f for f in orig_data if id(f) not in matched_orig_ids]
    pairs.extend(_pair_residue_via_heuristic(residue_orig, residue_recon))
    return pairs


def _pair_residue_via_heuristic(
    original: list[FieldRecord], reconstructed: list[FieldRecord]
) -> list[tuple[FieldRecord | None, FieldRecord | None]]:
    """Pre-P-48 pairing path: bucket by tag, then within a tag match
    by ``$a`` and finally by position. Used for the lineage-absent
    residue (today's flat Instance-side fields + any Phase-B-not-yet
    surface)."""
    orig_df: dict[str, list[FieldRecord]] = {}
    recon_df: dict[str, list[FieldRecord]] = {}
    for f in original:
        orig_df.setdefault(f.tag, []).append(f)
    for f in reconstructed:
        recon_df.setdefault(f.tag, []).append(f)

    pairs: list[tuple[FieldRecord | None, FieldRecord | None]] = []
    for tag in sorted(set(orig_df) | set(recon_df)):
        pairs.extend(_pair_bucket(list(orig_df.get(tag, [])), list(recon_df.get(tag, []))))
    return pairs


def _pair_bucket(
    orig_bucket: list[FieldRecord], recon_bucket: list[FieldRecord]
) -> list[tuple[FieldRecord | None, FieldRecord | None]]:
    """Pair fields within a single tag's bucket — first by $a, then by
    position, lost/added for the surplus."""
    pairs: list[tuple[FieldRecord | None, FieldRecord | None]] = []
    matched_recon: set[int] = set()
    for orig_field in list(orig_bucket):
        a = orig_field.primary_a()
        if a is None:
            continue
        for i, rec in enumerate(recon_bucket):
            if i in matched_recon:
                continue
            if rec.primary_a() == a:
                pairs.append((orig_field, rec))
                matched_recon.add(i)
                orig_bucket.remove(orig_field)
                break
    leftover_recon = [r for i, r in enumerate(recon_bucket) if i not in matched_recon]
    while orig_bucket and leftover_recon:
        pairs.append((orig_bucket.pop(0), leftover_recon.pop(0)))
    for o in orig_bucket:
        pairs.append((o, None))
    for r in leftover_recon:
        pairs.append((None, r))
    return pairs


def _subfields_equal(a: FieldRecord, b: FieldRecord) -> bool:
    """Compare two FieldRecords for cataloguer-relevant equality."""
    if a.is_control != b.is_control:
        return False
    if a.is_control:
        return (a.value or "") == (b.value or "")
    # Order-sensitive subfield compare (catalouguer subfield order matters
    # in MARC for some fields, e.g. 245 $a $b $c).
    return [sf.to_json() for sf in a.subfields] == [sf.to_json() for sf in b.subfields]


def _indicator_notes(orig: FieldRecord, recon: FieldRecord) -> Iterable[str]:
    """Yield human-readable notes for indicator-level differences that
    don't drive the field's `changed` status but cataloguers might
    want to know about."""
    if orig.is_control or recon.is_control:
        return
    if orig.ind1 != recon.ind1:
        yield f"ind1 differs ({orig.ind1!r} → {recon.ind1!r})"
    if orig.ind2 != recon.ind2:
        yield f"ind2 differs ({orig.ind2!r} → {recon.ind2!r})"


def _summarise(diffs: Iterable[FieldDiff]) -> dict[str, int]:
    summary: dict[str, int] = {
        "identical": 0,
        "lost": 0,
        "added": 0,
        "changed": 0,
        "lost-converter-gap": 0,
        "tag-changed": 0,
    }
    for d in diffs:
        summary[d.status] = summary.get(d.status, 0) + 1
    return summary


_MARC_TAG_LEN: Final[int] = 3


def _diff_sort_key(d: FieldDiff) -> tuple[int, str]:
    """Sort: LDR / control fields first by tag; then datafields by tag.
    Within a tag, identical comes after differences so cataloguers see
    diffs first."""
    if d.tag == "LDR":
        return (0, "")
    if d.tag.isdigit() and len(d.tag) == _MARC_TAG_LEN and d.tag < "010":
        return (1, d.tag)
    status_rank = {
        "changed": 0,
        "lost": 1,
        "lost-converter-gap": 2,
        "added": 3,
        "identical": 4,
    }
    return (2 + status_rank.get(d.status, 99), d.tag)


def diff_to_dict(diff: RecordDiff) -> dict[str, Any]:
    """Convenience for JSON serialisation."""
    return diff.to_json()


# Suppress lint complaints about unused imports kept for type hinting.
_ = asdict
