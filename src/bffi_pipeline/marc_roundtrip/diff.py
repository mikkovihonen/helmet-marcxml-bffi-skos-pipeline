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
    MARCKEY_BYPASS_VALUE,
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
#:
#: ``marckey-bypass`` (P-49 Phase A): the reconstructed field's subfields
#: were built by parsing ``bflc:marcKey`` rather than from BFFI structured
#: properties. The row may match byte-for-byte but the verification
#: failed: the cataloguer's original MARC string is smuggled through the
#: BFFI graph as an opaque text blob. Overrides ``identical``/``changed``
#: so the audit shows where BFFI's structured side is insufficient. See
#: ``docs/plans/proposed/p-49-bffi-structured-fields-vs-marckey.md``.
#:
#: ``language-reconciled``: original and reconstructed point at the same
#: authority URI (``$0``) but the cataloguer-typed ``$a`` differs from
#: the authority's prefLabel. Typically a M9 reconciliation: source
#: ``$a "konst" $0 yso/p1234`` (Swedish term + YSO URI) reconstructs as
#: ``$a "taide" $0 yso/p1234`` (Finnish prefLabel from YSO). Byte-
#: differs but semantically equivalent — the cataloguer's term and the
#: authority's prefLabel are translations of the same concept. Surfaces
#: in cataloguer-review as a separate band so reviewers see "expected
#: language drift" distinctly from real ``changed`` rows.
DiffStatus = Literal[
    "identical",
    "lost",
    "added",
    "changed",
    "lost-converter-gap",
    "tag-changed",
    "marckey-bypass",
    "language-reconciled",
]


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
    #: P-49 Phase A — True when this reconstructed field carried the
    #: ``$9 marckey-bypass`` sentinel, i.e. its subfields were built
    #: from ``bflc:marcKey`` rather than from BFFI structured properties.
    #: Always False on the original side.
    marckey_bypass: bool = False

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
        # ``language-reconciled`` upgrade: when a ``changed`` row's only
        # substantive difference is the ``$a`` literal AND both sides
        # carry the same ``$0`` authority URI, the difference is the
        # M9 reconciliation swapping the cataloguer-typed term for the
        # authority's prefLabel (typically a Swedish ``$a`` → YSO
        # Finnish prefLabel). Semantically equivalent; surfaced in its
        # own band so cataloguer-review distinguishes "expected
        # language drift" from real content change.
        if status == "changed" and _is_language_reconciled(orig, recon):
            status = "language-reconciled"
            notes.append(
                "$a differs but $0 authority URI matches — M9-bound "
                "language drift, semantically equivalent."
            )
        # P-49 Phase A: marcKey-bypass overrides ``identical``/``changed``/
        # ``language-reconciled``. The recon row may match byte-for-byte
        # (or differ only by reconciliation), but verification failed:
        # the subfields came from parsing the cataloguer's MARC string
        # smuggled through ``bflc:marcKey``, not from BFFI structured
        # properties. Surfaces the audit so the cataloguer-review HTML
        # can render the row in its own colour band. Does NOT override
        # ``tag-changed`` (a misroute is a worse problem than a bypass).
        if status in ("identical", "changed", "language-reconciled") and recon.marckey_bypass:
            status = "marckey-bypass"
            notes.append(
                "Subfields reconstructed from bflc:marcKey, not BFFI "
                "structured properties — see P-49 audit."
            )
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
        marckey_bypass = False
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
            # P-49 Phase A: parse + strip the marcKey-bypass sentinel
            # ``$9 marckey-bypass`` on the reconstructed side. Multiple
            # ``$9`` values per datafield are legal in MARC, so this
            # coexists with the lineage subfield above.
            if strip_marker and code == LINEAGE_SUBFIELD and value == MARCKEY_BYPASS_VALUE:
                marckey_bypass = True
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
                marckey_bypass=marckey_bypass,
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

    Three pairing tiers, applied in order:

    1. **P-50 source-MARC-field token** — recon's ``$9 src=<bib>:<tag>:
       <ord>`` matches a source field at the same ``(tag, ordinal)``
       slot, computed from source MARCXML document order. Stable
       against marc2bibframe2 routing decisions and M9 reconciliation.
    2. **P-48 rank-bucket token** — legacy ``$9 src=<tag>-<rank>``
       fallback for entities M2-post couldn't tokenise (records
       processed before P-50 Phase A shipped, or shapes Phase A's
       correlator doesn't cover yet — flat literals, URI-keyed
       subjects). Source side ranks 1-indexed-within-tag-bucket;
       recon side derives rank from raw-URI fragment.
    3. **Tag-bucket heuristic** — final fallback for the residue
       (``$a`` + position pairing). See
       :func:`_pair_residue_via_heuristic`.

    Tier 1 takes precedence: a recon row carrying both token forms
    pairs by the P-50 token. Tiers don't conflict by construction
    (they look up against disjoint maps).
    """
    orig_data = [f for f in original if not f.is_control]
    recon_data = [f for f in reconstructed if not f.is_control]

    # Tier 1: P-50 token — source-side index by (tag, ordinal-in-tag-
    # bucket). Walk the original record once and rank within each tag's
    # bucket. The ordinal is the second half of the recon's
    # ``<bib>:<tag>:<ord>`` token (bib_id is implicit — the diff is
    # per-bib so the bib_id matches by construction).
    orig_by_source_token_suffix: dict[str, FieldRecord] = {}
    # Tier 2: P-48 fallback — same data, but keyed by ``<tag>-<rank>``
    # since the legacy lineage scheme also ranks 1-indexed-within-tag.
    # Identical lookup table, different key shape.
    orig_by_lineage: dict[str, FieldRecord] = {}
    tag_counters: dict[str, int] = {}
    for f in orig_data:
        tag_counters[f.tag] = tag_counters.get(f.tag, 0) + 1
        rank = tag_counters[f.tag]
        orig_by_source_token_suffix[f"{f.tag}:{rank}"] = f
        orig_by_lineage[f"{f.tag}-{rank}"] = f

    pairs: list[tuple[FieldRecord | None, FieldRecord | None]] = []
    matched_orig_ids: set[int] = set()
    residue_recon: list[FieldRecord] = []
    for recon in recon_data:
        orig_match = _lookup_orig_by_token(
            recon.lineage,
            orig_by_source_token_suffix,
            orig_by_lineage,
        )
        if orig_match is not None and id(orig_match) not in matched_orig_ids:
            matched_orig_ids.add(id(orig_match))
            pairs.append((orig_match, recon))
        else:
            # Lineage absent / mis-stamped / pointing at an already-
            # matched original. Fall back to heuristic for this row.
            residue_recon.append(recon)

    residue_orig = [f for f in orig_data if id(f) not in matched_orig_ids]
    pairs.extend(_pair_residue_via_heuristic(residue_orig, residue_recon))
    return pairs


def _lookup_orig_by_token(
    lineage: str | None,
    by_source_token_suffix: dict[str, FieldRecord],
    by_legacy_lineage: dict[str, FieldRecord],
) -> FieldRecord | None:
    """Decode the recon-side lineage string and return the source field
    it points at, or ``None`` when neither token form parses.

    P-50 format: ``"<bib_id>:<tag>:<within-tag-ordinal>"`` (three
    colon-separated tokens; bib_id may itself contain colons in
    URN-style identifiers, so split from the right). The bib_id half
    is dropped because the diff is per-bib — the suffix ``"<tag>:<ord>"``
    is enough.

    P-48 legacy format: ``"<tag>-<rank>"`` (one hyphen, no colons).
    """
    if not lineage:
        return None
    if ":" in lineage:
        # P-50 token. Take the last two colon-separated components.
        suffix = ":".join(lineage.rsplit(":", 2)[-2:])
        return by_source_token_suffix.get(suffix)
    return by_legacy_lineage.get(lineage)


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


def _is_language_reconciled(orig: FieldRecord, recon: FieldRecord) -> bool:
    """Return True when ``orig`` and ``recon`` differ only in their
    ``$a`` literal but share the same ``$0`` authority URI.

    Detection rules:

    - Both sides must be datafields (not controlfields).
    - Both sides must carry at least one ``$0`` and the FIRST ``$0``
      on each side must match exactly. The authority URI is the
      identity signal; if it matches, both sides agree on which
      concept this row is about.
    - The ``$a`` literals must differ. Otherwise the row would be
      ``identical``, not ``changed``.
    - All non-``$a`` subfields must match exactly between the two
      sides (same codes, same values, same order). A ``$2`` or
      ``$c`` divergence is a real content change, not a
      reconciliation.

    Conservative on purpose: any extra difference (multiple ``$0``
    values, indicator mismatch, missing ``$0`` on one side) → return
    False and let the row stay ``changed``. The M9 reconciliation
    case we're catching is narrow: cataloguer typed a label in one
    language, the authority's prefLabel is in another, everything
    else identical.
    """
    if orig.is_control or recon.is_control:
        return False
    orig_zero = _first_subfield(orig, "0")
    recon_zero = _first_subfield(recon, "0")
    if orig_zero is None or recon_zero is None or orig_zero != recon_zero:
        return False
    orig_a = _first_subfield(orig, "a")
    recon_a = _first_subfield(recon, "a")
    if orig_a is None or recon_a is None or orig_a == recon_a:
        return False
    # All non-``$a`` subfields (in order) must match.
    orig_rest = [sf.to_json() for sf in orig.subfields if sf.code != "a"]
    recon_rest = [sf.to_json() for sf in recon.subfields if sf.code != "a"]
    return orig_rest == recon_rest


def _first_subfield(field: FieldRecord, code: str) -> str | None:
    """First subfield value with the given code; ``None`` if absent."""
    for sf in field.subfields:
        if sf.code == code:
            return sf.value
    return None


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
        "marckey-bypass": 0,
        "language-reconciled": 0,
    }
    for d in diffs:
        summary[d.status] = summary.get(d.status, 0) + 1
    return summary


_MARC_TAG_LEN: Final[int] = 3


def _diff_sort_key(d: FieldDiff) -> tuple[int, str]:
    """Sort by MARC field number ascending, LDR first.

    Cataloguers read MARC top-to-bottom by tag (LDR, 008, 020, 100,
    245, 260, ..., 650, 700, 730, ...), so the diff table mirrors that.
    Python's sort is stable, so within a tag bucket rows preserve the
    pairing order from :func:`_pair_data_fields` — which itself is
    source-MARC encounter order via the lineage-rank pass + heuristic
    residue. No status-based grouping; the row's status badge gives
    the cataloguer the same signal at a glance.
    """
    if d.tag == "LDR":
        return (0, "")
    return (1, d.tag)


def diff_to_dict(diff: RecordDiff) -> dict[str, Any]:
    """Convenience for JSON serialisation."""
    return diff.to_json()


# Suppress lint complaints about unused imports kept for type hinting.
_ = asdict
