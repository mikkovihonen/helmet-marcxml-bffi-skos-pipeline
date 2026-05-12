"""P-08 RDA 33X synth cascade: slot-wise composition of MARC signals.

When a Sierra bib carries no cataloguer-coded 33X, the synth path in
:func:`marcxml_export_pipeline.sierra.marcxml.build_marcxml_for_row`
composes a default 336/337/338 tuple from the MARC signals on the
record. This module is the cascade engine plus the per-signal layer
functions.

Priority order (cataloguer-coded 33X wins via the pre-gate above the
cascade; layers below run only when no 33X is present):

1. :func:`from_marc_007` — physical-form fixed-field, when present.
   Fills media + carrier deterministically per category.
2. :func:`from_leader_and_008` — universal default keyed on
   leader/06 + 008 "Form of item" position. Fills all three slots
   from the canonical Helmet manifestation for that content type.
3. :func:`from_material_code` — adapter wrapping the existing
   :data:`itype_to_rda.MATERIAL_TO_RDA` table. Slot-fills below
   layers 1-2; in practice fires only for records whose leader/06
   is unmapped.
4. :func:`from_items_itype` — adapter wrapping the existing
   :func:`itype_to_rda.lookup_rda_for_items` lookup. Last fallback
   before "no signal at all → cascade returns None".

A Phase C follow-up will add a 300$a extent regex layer below these.

The Phase A coverage analysis (2026-05-12, 5 000-record sample)
showed 100 % of records dropping on missing 33X carry both a mapped
leader/06 AND an 008 controlfield, so layer 2 alone closes ~all of
the residual on this corpus. Layer 1 (007) refines the 5 % of drops
that carry 007 with a more-specific carrier than the
leader+008 default would supply.

See ``docs/plans/in-progress/p-08-richer-rda-33x-synthesis.md`` for
the cascade design and the coverage analysis findings.

Per the LoC MARC 21 Bibliographic spec for the per-category 007 /
008 / leader position semantics.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, TypeAlias

from marcxml_export_pipeline.sierra.dtos import Varfield
from marcxml_export_pipeline.sierra.itype_to_rda import (
    MAP_SHEET,
    MATERIAL_TO_RDA,
    NOTATED_MUSIC_UNMEDIATED_VOLUME,
    PERFORMED_MUSIC_AUDIO_CASSETTE,
    PERFORMED_MUSIC_AUDIO_DISC,
    SPOKEN_WORD_AUDIO_DISC,
    TEXT_UNMEDIATED_VOLUME,
    VIDEO_VIDEODISC,
    RdaCarrier,
    lookup_rda_for_items,
)

#: Cascade-output version. Bumped manually whenever the cascade's
#: emitted RDA tuple for an existing record would change (new layer
#: ordering, table updates, etc.). Stamped into every synthesised 33X
#: datafield as ``$5 FI-HELME/synth-v<N>`` so a future re-cascading
#: can find synth-v<N> fields and replace them deterministically
#: without disturbing cataloguer-coded fields.
SYNTH_VERSION: Final[int] = 1

# --- Types ---------------------------------------------------------------


@dataclass(frozen=True)
class PartialRda:
    """RDA 33X with optional per-slot resolution.

    The cascade composes a final :class:`RdaCarrier` from slot-wise
    winners. A higher-priority layer can fill any subset of the three
    slots (336 content, 337 media, 338 carrier); lower-priority layers
    fill the slots above left empty.
    """

    content_types: tuple[tuple[str, str], ...] | None = None
    media: tuple[str, str] | None = None
    carrier: tuple[str, str] | None = None

    def merge_below(self, other: PartialRda) -> PartialRda:
        """Return self with any empty slots filled from ``other``."""
        return PartialRda(
            content_types=self.content_types or other.content_types,
            media=self.media or other.media,
            carrier=self.carrier or other.carrier,
        )

    def is_complete(self) -> bool:
        return (
            self.content_types is not None and self.media is not None and self.carrier is not None
        )

    def to_carrier(self) -> RdaCarrier | None:
        """Promote to a complete :class:`RdaCarrier` once all three slots
        are filled; ``None`` otherwise."""
        if not (
            self.content_types is not None and self.media is not None and self.carrier is not None
        ):
            return None
        return RdaCarrier(
            content_types=self.content_types,
            media=self.media,
            carrier=self.carrier,
        )


def partial_from_carrier(rda: RdaCarrier | None) -> PartialRda:
    """Lift an :class:`RdaCarrier` (or ``None``) into a :class:`PartialRda`."""
    if rda is None:
        return PartialRda()
    return PartialRda(
        content_types=rda.content_types,
        media=rda.media,
        carrier=rda.carrier,
    )


@dataclass(frozen=True)
class RecordContext:
    """View over the streamed Sierra row that the cascade layers read.

    Constructed by :func:`marcxml_export_pipeline.sierra.marcxml.build_marcxml_for_row`
    from the already-decoded fields. Layer functions don't see the raw
    SQL row — they read this typed view.

    ``leader_record_type`` is the leader/06 character (one of ``a``,
    ``c``, ``d``, ``e``, ``f``, ``g``, ``i``, ``j``, ``k``, ``m``,
    ``o``, ``p``, ``r``, ``t``, or ``None`` when leader is absent /
    malformed). ``controlfields`` is the list of
    ``(tag, content)`` tuples already built up in
    ``build_marcxml_for_row`` (006/007/008 with fixed-length
    padding applied). ``varfields`` are the typed
    :class:`Varfield` instances. ``items`` is the per-item dict list
    (location_code / copy_num / item_type_num / barcode /
    call_number). ``material_code`` is the bib-level Sierra
    material code, or ``None`` if absent.
    """

    leader_record_type: str | None
    controlfields: tuple[tuple[str, str], ...]
    varfields: tuple[Varfield, ...]
    items: tuple[Mapping[str, Any], ...]
    material_code: str | None


CascadeLayer: TypeAlias = Callable[[RecordContext], PartialRda]


def resolve_rda(ctx: RecordContext, layers: Sequence[CascadeLayer]) -> RdaCarrier | None:
    """Run the cascade in priority order.

    Each layer contributes a :class:`PartialRda`; the composer fills
    slots top-down (highest-priority layer's slots win). Returns a
    complete :class:`RdaCarrier` if all three slots fill; ``None``
    otherwise — the caller leaves 33X un-synthesised and the M2 gate
    drops the bib.
    """
    accum = PartialRda()
    for layer in layers:
        accum = accum.merge_below(layer(ctx))
        if accum.is_complete():
            break
    return accum.to_carrier()


# --- Internal helpers ----------------------------------------------------


def _controlfield(ctx: RecordContext, tag: str) -> str | None:
    """Return the content of the first controlfield matching ``tag``."""
    for t, c in ctx.controlfields:
        if t == tag:
            return c
    return None


# --- Layer 1: 007 refinement ---------------------------------------------


#: 007 mapping: ``cat → (media tuple, refining-position, {sub-char: carrier tuple})``.
#: The refining position is the 007 offset that selects the carrier
#: (1 for audio / computer / text; 4 for video — videos pack a few
#: ancillary positions between 1 and 4). Per LoC § 007.
_MARC_007_CARRIER_TABLES: Final[
    dict[str, tuple[tuple[str, str], int, dict[str, tuple[str, str]]]]
] = {
    "s": (
        ("audio", "s"),
        1,
        {"d": ("äänilevy", "sd"), "s": ("äänikasetti", "ss")},
    ),
    "v": (
        ("video", "v"),
        4,
        {
            "v": ("videolevy", "vd"),
            "s": ("videokasetti", "vf"),
            "f": ("videopatruuna", "vc"),
        },
    ),
    "c": (
        ("tietokonekäyttöinen", "c"),
        1,
        {"o": ("tietolevy", "cd"), "r": ("verkkoaineisto", "cr")},
    ),
    "t": (
        ("käytettävissä ilman laitetta", "n"),
        1,
        {"a": ("nide", "nc"), "b": ("isotekstinen nide", "nc")},
    ),
}


def from_marc_007(ctx: RecordContext) -> PartialRda:
    """Read MARC 007 if present; return media + carrier.

    Covers the four highest-volume 007 category codes (``s`` sound,
    ``v`` video, ``c`` computer/electronic, ``t`` text). The content-
    type slot stays empty — 007 doesn't carry it; layer 2 fills 336
    regardless of whether 007 was present.

    Per LoC MARC 21 Bibliographic spec § 007.
    """
    content = _controlfield(ctx, "007")
    if not content:
        return PartialRda()
    entry = _MARC_007_CARRIER_TABLES.get(content[0:1])
    if entry is None:
        return PartialRda()
    media, sub_pos, carrier_map = entry
    sub = content[sub_pos : sub_pos + 1]
    return PartialRda(media=media, carrier=carrier_map.get(sub))


# --- Layer 2: (leader/06, 008-form) universal default --------------------


#: MARC 008 "Form of item" position per leader/06 class. The position
#: varies by record-type class — books/computer-files at 008/23,
#: visual/cartographic materials at 008/29. Per LoC MARC 21
#: Bibliographic spec § 008 (Form of item).
_LEADER_06_TO_008_FORM_POS: Final[dict[str, int]] = {
    # books / manuscripts → 008/23
    "a": 23,
    "t": 23,
    # visual materials, kits, 3-D, cartographic → 008/29
    "g": 29,
    "k": 29,
    "o": 29,
    "r": 29,
    "e": 29,
    "f": 29,
    # computer files → 008/23
    "m": 23,
    # notated music + sound recordings → 008/23 (M2 exempts these
    # from 33X-required, but mappings included for forward-compat)
    "c": 23,
    "d": 23,
    "i": 23,
    "j": 23,
}


def _get_008_form(ctx: RecordContext) -> str | None:
    """Return the 008 'Form of item' position character.

    ``None`` when leader/06 is unmapped or 008 is missing / too short.
    A literal ``' '`` (space) is the *coded* "no form specified" value
    and is preserved — it's how the lookup table keys the default
    manifestation per content type.
    """
    if not ctx.leader_record_type:
        return None
    pos = _LEADER_06_TO_008_FORM_POS.get(ctx.leader_record_type)
    if pos is None:
        return None
    content = _controlfield(ctx, "008")
    if not content or len(content) <= pos:
        return None
    return content[pos]


# --- New RDA tuples used by the (leader/06, 008-form) table -------------
#
# Some tuples are not present in itype_to_rda's existing constants
# because the original table was empirically built from Helmet's
# *coded* cataloguing patterns, which don't include large-print /
# braille / microform / online manifestations (those have always been
# cataloguer-coded with explicit 33X). Define them here.


TEXT_LARGE_PRINT: Final[RdaCarrier] = RdaCarrier(
    content_types=(("teksti", "txt"),),
    media=("käytettävissä ilman laitetta", "n"),
    carrier=("isotekstinen nide", "nc"),
)
TEXT_BRAILLE: Final[RdaCarrier] = RdaCarrier(
    content_types=(("taktiili teksti", "tct"),),
    media=("käytettävissä ilman laitetta", "n"),
    carrier=("nide", "nc"),
)
TEXT_MICROFILM: Final[RdaCarrier] = RdaCarrier(
    content_types=(("teksti", "txt"),),
    media=("mikromuoto", "h"),
    carrier=("mikrofilmirulla", "hf"),
)
TEXT_MICROFICHE: Final[RdaCarrier] = RdaCarrier(
    content_types=(("teksti", "txt"),),
    media=("mikromuoto", "h"),
    carrier=("mikrofilmikortti", "he"),
)
TEXT_ONLINE: Final[RdaCarrier] = RdaCarrier(
    content_types=(("teksti", "txt"),),
    media=("tietokonekäyttöinen", "c"),
    carrier=("verkkoaineisto", "cr"),
)
VIDEO_ONLINE: Final[RdaCarrier] = RdaCarrier(
    content_types=(("kaksiulotteinen liikkuva kuva", "tdi"),),
    media=("tietokonekäyttöinen", "c"),
    carrier=("verkkoaineisto", "cr"),
)
VIDEO_VIDEOCASSETTE: Final[RdaCarrier] = RdaCarrier(
    content_types=(("kaksiulotteinen liikkuva kuva", "tdi"),),
    media=("video", "v"),
    carrier=("videokasetti", "vf"),
)
COMPUTER_ONLINE: Final[RdaCarrier] = RdaCarrier(
    content_types=(("tietokoneohjelma", "cop"),),
    media=("tietokonekäyttöinen", "c"),
    carrier=("verkkoaineisto", "cr"),
)
TWO_D_GRAPHIC_SHEET: Final[RdaCarrier] = RdaCarrier(
    content_types=(("stillkuva", "sti"),),
    media=("käytettävissä ilman laitetta", "n"),
    carrier=("arkki", "nb"),
)
THREE_D_OBJECT: Final[RdaCarrier] = RdaCarrier(
    content_types=(("kolmiulotteinen muoto", "tdf"),),
    media=("käytettävissä ilman laitetta", "n"),
    carrier=("objekti", "nr"),
)


#: ``(leader/06, 008-form)`` → full RDA tuple. Maps the dominant
#: Helmet manifestation per content type; 008-form refines the
#: carrier for non-default cases (large print, braille, microform,
#: online). A coded ``' '`` (space) in 008-form means "no form
#: specified" — the default for that content type.
#:
#: This is the load-bearing layer of the cascade: Phase A measured
#: 100 % drop-list coverage for the (leader/06, 008) pair on the 5k
#: sample.
LEADER_008_TO_RDA: Final[dict[tuple[str, str], RdaCarrier]] = {
    # leader/06 = 'a' (language material — books). 008/23 refines.
    ("a", " "): TEXT_UNMEDIATED_VOLUME,
    ("a", "r"): TEXT_UNMEDIATED_VOLUME,  # regular print reproduction
    ("a", "d"): TEXT_LARGE_PRINT,
    ("a", "f"): TEXT_BRAILLE,
    ("a", "a"): TEXT_MICROFILM,
    ("a", "b"): TEXT_MICROFICHE,
    ("a", "o"): TEXT_ONLINE,
    ("a", "s"): TEXT_ONLINE,
    ("a", "q"): TEXT_ONLINE,  # direct electronic
    # leader/06 = 't' (manuscript language material). 008/23.
    ("t", " "): TEXT_UNMEDIATED_VOLUME,
    # leader/06 = 'g' (projected medium — video). 008/29.
    ("g", " "): VIDEO_VIDEODISC,
    ("g", "d"): VIDEO_VIDEODISC,
    ("g", "o"): VIDEO_ONLINE,
    ("g", "q"): VIDEO_ONLINE,
    ("g", "s"): VIDEO_ONLINE,
    # leader/06 = 'm' (computer file). 008/23.
    ("m", " "): COMPUTER_ONLINE,
    ("m", "o"): COMPUTER_ONLINE,
    ("m", "q"): COMPUTER_ONLINE,
    ("m", "s"): COMPUTER_ONLINE,
    # leader/06 = 'e'/'f' (cartographic). 008/29.
    ("e", " "): MAP_SHEET,
    ("e", "d"): MAP_SHEET,
    ("f", " "): MAP_SHEET,
    # leader/06 = 'k' (2-D non-projected graphic). 008/29.
    ("k", " "): TWO_D_GRAPHIC_SHEET,
    # leader/06 = 'r' (3-D artifact). 008/29.
    ("r", " "): THREE_D_OBJECT,
    # leader/06 = 'c'/'d' (notated music) — M2 exempts, but
    # provide entries for forward-compat.
    ("c", " "): NOTATED_MUSIC_UNMEDIATED_VOLUME,
    ("d", " "): NOTATED_MUSIC_UNMEDIATED_VOLUME,
    # leader/06 = 'j' (musical sound recording) — M2 exempts.
    ("j", " "): PERFORMED_MUSIC_AUDIO_DISC,
    # leader/06 = 'i' (nonmusical sound recording) — M2 exempts.
    ("i", " "): SPOKEN_WORD_AUDIO_DISC,
}


#: leader/06 alone fallback when ``(leader/06, 008-form)`` is not in
#: :data:`LEADER_008_TO_RDA` (008-form coded as something we don't
#: recognise; or 008 missing entirely). Picks the Helmet-canonical
#: default manifestation for that content type.
LEADER_06_FALLBACK: Final[dict[str, RdaCarrier]] = {
    "a": TEXT_UNMEDIATED_VOLUME,
    "t": TEXT_UNMEDIATED_VOLUME,
    "g": VIDEO_VIDEODISC,
    "m": COMPUTER_ONLINE,
    "e": MAP_SHEET,
    "f": MAP_SHEET,
    "k": TWO_D_GRAPHIC_SHEET,
    "r": THREE_D_OBJECT,
    "c": NOTATED_MUSIC_UNMEDIATED_VOLUME,
    "d": NOTATED_MUSIC_UNMEDIATED_VOLUME,
    "j": PERFORMED_MUSIC_AUDIO_DISC,
    "i": SPOKEN_WORD_AUDIO_DISC,
}


def from_leader_and_008(ctx: RecordContext) -> PartialRda:
    """Universal default RDA tuple keyed on (leader/06, 008-form).

    Returns the canonical Helmet manifestation for the content type,
    with 008-form refining the carrier for non-default cases. Falls
    back to a leader/06-only default when 008-form is unmapped, and
    returns empty when leader/06 itself is unmapped (e.g. ``o`` kit,
    ``p`` mixed material, or any obsolete / undefined code).

    Per LoC MARC 21 Bibliographic spec § Leader/06 + § 008 Form of item.
    """
    rt = ctx.leader_record_type
    if not rt:
        return PartialRda()
    form = _get_008_form(ctx)
    if form is not None:
        rda = LEADER_008_TO_RDA.get((rt, form))
        if rda is not None:
            return partial_from_carrier(rda)
    fallback = LEADER_06_FALLBACK.get(rt)
    if fallback is None:
        return PartialRda()
    return partial_from_carrier(fallback)


# --- Layer 3: material_code adapter (existing) ---------------------------


def from_material_code(ctx: RecordContext) -> PartialRda:
    """Slot-filler wrapping the existing bib-level
    :data:`itype_to_rda.MATERIAL_TO_RDA` table. Fires below
    :func:`from_leader_and_008` so its coarser signal only applies to
    records the universal default couldn't resolve (extremely rare on
    Helmet, since leader/06 is always present)."""
    if ctx.material_code is None:
        return PartialRda()
    return partial_from_carrier(MATERIAL_TO_RDA.get(ctx.material_code))


# --- Layer 4: item itype adapter (existing) ------------------------------


def from_items_itype(ctx: RecordContext) -> PartialRda:
    """Slot-filler wrapping the existing item-level
    :func:`itype_to_rda.lookup_rda_for_items` lookup. Last fallback
    before "no signal at all → cascade returns None"."""
    return partial_from_carrier(lookup_rda_for_items(ctx.items))


# --- Layer 5: 300$a extent regex (last-resort textual fallback) ----------


#: 300$a extent tokens → canonical RDA tuple. Conservative Finnish +
#: English vocabulary; matches a word stem at a word boundary so
#: declensions (``kirjaa``, ``kirjat``, ``nidettä``) still hit. Order
#: matters: longer/more-specific tokens must precede shorter ones
#: (``DVD-levy`` before ``levy`` alone) since the first match wins.
_EXTENT_TOKEN_RDA: Final[tuple[tuple[re.Pattern[str], RdaCarrier], ...]] = (
    # Video — DVD / Blu-ray / videocassette. Specific first.
    (re.compile(r"\bDVD[-\s]?levy\w*\b", re.IGNORECASE), VIDEO_VIDEODISC),
    (re.compile(r"\bBlu[-\s]?ray\w*\b", re.IGNORECASE), VIDEO_VIDEODISC),
    (re.compile(r"\bDVD\b", re.IGNORECASE), VIDEO_VIDEODISC),
    (re.compile(r"\bvideolevy\w*\b", re.IGNORECASE), VIDEO_VIDEODISC),
    (re.compile(r"\bvideokasetti\w*\b", re.IGNORECASE), VIDEO_VIDEOCASSETTE),
    # Audio — LP / cassette / generic audio disc. Music as the default
    # 336 (matches ``MATERIAL_TO_RDA["3"]`` convention; ambiguous CDs
    # default to music — cataloguer-coded 33X always wins anyway).
    (re.compile(r"\bLP[-\s]?levy\w*\b", re.IGNORECASE), PERFORMED_MUSIC_AUDIO_DISC),
    (re.compile(r"\bLP\b"), PERFORMED_MUSIC_AUDIO_DISC),
    (re.compile(r"\bäänikasetti\w*\b", re.IGNORECASE), PERFORMED_MUSIC_AUDIO_CASSETTE),
    (re.compile(r"\bC[-\s]?kasetti\w*\b", re.IGNORECASE), PERFORMED_MUSIC_AUDIO_CASSETTE),
    (re.compile(r"\bäänilevy\w*\b", re.IGNORECASE), PERFORMED_MUSIC_AUDIO_DISC),
    (re.compile(r"\bCD[-\s]?levy\w*\b", re.IGNORECASE), PERFORMED_MUSIC_AUDIO_DISC),
    (re.compile(r"\bCD\b"), PERFORMED_MUSIC_AUDIO_DISC),
    # Notated music
    (re.compile(r"\bnuotti\w*\b", re.IGNORECASE), NOTATED_MUSIC_UNMEDIATED_VOLUME),
    # Cartographic
    (re.compile(r"\bkartta\w*\b", re.IGNORECASE), MAP_SHEET),
    # 3-D objects
    (re.compile(r"\besine\w*\b", re.IGNORECASE), THREE_D_OBJECT),
    # Text — books / pages. Most generic, so last.
    (re.compile(r"\bkuvateos\w*\b", re.IGNORECASE), TEXT_UNMEDIATED_VOLUME),
    (re.compile(r"\bkirja\w*\b", re.IGNORECASE), TEXT_UNMEDIATED_VOLUME),
    (re.compile(r"\bnide\w*\b", re.IGNORECASE), TEXT_UNMEDIATED_VOLUME),
    (re.compile(r"\bnidettä\b", re.IGNORECASE), TEXT_UNMEDIATED_VOLUME),
    (re.compile(r"\bsivua\b", re.IGNORECASE), TEXT_UNMEDIATED_VOLUME),
    (re.compile(r"\bsivu[aät]?\w*\b", re.IGNORECASE), TEXT_UNMEDIATED_VOLUME),
    (re.compile(r"\bpages?\b", re.IGNORECASE), TEXT_UNMEDIATED_VOLUME),
)


def from_300_a_extent(ctx: RecordContext) -> PartialRda:
    """Last-resort textual fallback — scan MARC 300$a for a carrier-
    naming token.

    Phase A measured 17 % of the 5k drop list carries such a token,
    but in practice this layer fires on essentially no records on
    that sample because Phase B's universal default resolves
    everything first. The layer ships as insurance for the long-
    tail leader/06 distribution on the full 800k corpus (``o`` kits,
    ``p`` mixed material, obsolete codes) plus records with no
    material/itype signal at all.

    First matching token in :data:`_EXTENT_TOKEN_RDA` wins. Tokens are
    ordered specificity-first (``DVD-levy`` before ``DVD``; specific
    text forms before generic ``sivua``).
    """
    extents: list[str] = []
    for vf in ctx.varfields:
        if vf.marc_tag != "300":
            continue
        for sf in vf.subfields:
            if sf.tag == "a" and sf.content:
                extents.append(sf.content)
    if not extents:
        return PartialRda()
    haystack = " ".join(extents)
    for pattern, rda in _EXTENT_TOKEN_RDA:
        if pattern.search(haystack):
            return partial_from_carrier(rda)
    return PartialRda()


# --- Cascade entry point -------------------------------------------------


#: The default cascade ordering used by
#: :func:`marcxml_export_pipeline.sierra.marcxml.build_marcxml_for_row`.
#: Higher-priority layers run first; each layer fills only the slots
#: above-priority layers left empty.
DEFAULT_LAYERS: Final[tuple[CascadeLayer, ...]] = (
    from_marc_007,
    from_leader_and_008,
    from_material_code,
    from_items_itype,
    from_300_a_extent,
)


__all__ = [
    "COMPUTER_ONLINE",
    "DEFAULT_LAYERS",
    "LEADER_008_TO_RDA",
    "LEADER_06_FALLBACK",
    "SYNTH_VERSION",
    "TEXT_BRAILLE",
    "TEXT_LARGE_PRINT",
    "TEXT_MICROFICHE",
    "TEXT_MICROFILM",
    "TEXT_ONLINE",
    "THREE_D_OBJECT",
    "TWO_D_GRAPHIC_SHEET",
    "VIDEO_ONLINE",
    "VIDEO_VIDEOCASSETTE",
    "PartialRda",
    "RecordContext",
    "from_300_a_extent",
    "from_items_itype",
    "from_leader_and_008",
    "from_marc_007",
    "from_material_code",
    "partial_from_carrier",
    "resolve_rda",
]
