"""Sierra item-type → RDA 336/337/338 mapping for MARCXML export synthesis.

P-02's 2026-05-12 5k production-style run dropped **525 of 5 000 records
(10.5 %)** at the M2 ``marcxml-content-minimum`` gate solely on missing
``336/337/338`` (RDA content / media / carrier). Most of those are
pre-RDA records (Helmet adopted RDA cataloguing around 2015-16) that
never received the 33X coding new records get automatically.

Cataloguer-approved fix: synthesise the 33X datafields at Sierra-export
time from the bib's linked items' ``item_record.itype_code_num`` (joined
to ``sierra_view.itype_property_myuser.code``). Items already carry the
cataloguer-assigned item-type code, and the table below maps each
**load-bearing** item type to the RDA tuple cataloguers have historically
coded onto bibs of that type.

The mapping was derived empirically by joining items against bibs that
DO already have 33X coded, then taking the top-1 (highest-``n_bibs``)
``(336$a, 336$b, 337$a, 337$b, 338$a, 338$b)`` per itype as the
cataloguer-validated mapping. Source: ``scratchpad/sierra-itype-discovery``
output, 2026-05-12. Long-tail itypes with insufficient data or
ambiguous votes (objects 151-156/251-253, niche video types, some
``other material`` itypes) are intentionally left out — bibs of those
itypes still drop on the 33X gate, preserving the strict behaviour
where the empirical evidence is too thin to vote.

The synth path runs in :func:`marcxml_export_pipeline.sierra.marcxml.build_marcxml_for_row`
only when **no** Sierra-side 33X varfield is present. Cataloguer-supplied
33X always wins.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final


@dataclass(frozen=True)
class RdaCarrier:
    """RDA 33X triple for one BIBFRAME Instance.

    ``content_types`` carries one or more ``(label, code)`` pairs because
    some itypes (console games, illustrated books) idiomatically receive
    multiple 336 datafields. ``media`` and ``carrier`` are single tuples
    because cataloguers never code multiple 337 / 338 on the same
    manifestation.
    """

    content_types: tuple[tuple[str, str], ...]
    media: tuple[str, str]
    carrier: tuple[str, str]


# Reusable atomic carriers (cuts repetition + keeps the canonical form
# under one definition each).
_TEXT_UNMEDIATED_VOLUME = RdaCarrier(
    content_types=(("teksti", "txt"),),
    media=("käytettävissä ilman laitetta", "n"),
    carrier=("nide", "nc"),
)
_NOTATED_MUSIC_UNMEDIATED_VOLUME = RdaCarrier(
    content_types=(("nuottikirjoitus", "ntm"),),
    media=("käytettävissä ilman laitetta", "n"),
    carrier=("nide", "nc"),
)
_PERFORMED_MUSIC_AUDIO_DISC = RdaCarrier(
    content_types=(("esitetty musiikki", "prm"),),
    media=("audio", "s"),
    carrier=("äänilevy", "sd"),
)
_PERFORMED_MUSIC_AUDIO_CASSETTE = RdaCarrier(
    content_types=(("esitetty musiikki", "prm"),),
    media=("audio", "s"),
    carrier=("äänikasetti", "ss"),
)
_SPOKEN_WORD_AUDIO_DISC = RdaCarrier(
    content_types=(("puhe", "spw"),),
    media=("audio", "s"),
    carrier=("äänilevy", "sd"),
)
_VIDEO_VIDEODISC = RdaCarrier(
    content_types=(("kaksiulotteinen liikkuva kuva", "tdi"),),
    media=("video", "v"),
    carrier=("videolevy", "vd"),
)
_COMPUTER_GAME_COMPUTER_DISC = RdaCarrier(
    # Console / computer games idiomatically carry TWO 336 entries —
    # ``tdi`` (the in-game visuals are 2-D moving image) AND ``cop``
    # (the game is a computer program). Empirical near-tie in the
    # cataloguer-vote query confirms this is intentional.
    content_types=(
        ("kaksiulotteinen liikkuva kuva", "tdi"),
        ("tietokoneohjelma", "cop"),
    ),
    media=("tietokonekäyttöinen", "c"),
    carrier=("tietolevy", "cd"),
)
_BOARD_GAME = RdaCarrier(
    content_types=(("kolmiulotteinen muoto", "tdf"),),
    media=("käytettävissä ilman laitetta", "n"),
    carrier=("objekti", "nr"),
)
_MAP_SHEET = RdaCarrier(
    content_types=(("kartografinen kuva", "cri"),),
    media=("käytettävissä ilman laitetta", "n"),
    carrier=("arkki", "nb"),
)


#: Sierra ``item_record.itype_code_num`` → cataloguer-canonical RDA
#: 33X tuple. Empirically derived from ``scratchpad/sierra-itype-
#: discovery`` (2026-05-12); the comment after each entry shows the
#: itype's ``itype_property_myuser.name``.
ITYPE_TO_RDA: Final[dict[int, RdaCarrier]] = {
    # --- Books, journals (text/unmediated/volume) -----------------------
    100: _TEXT_UNMEDIATED_VOLUME,  # Adult book 28
    101: _TEXT_UNMEDIATED_VOLUME,  # Adult book special 28
    102: _TEXT_UNMEDIATED_VOLUME,  # Adult book bestseller 14
    103: _TEXT_UNMEDIATED_VOLUME,  # Adult journal 28
    200: _TEXT_UNMEDIATED_VOLUME,  # Juvenile book 28
    201: _TEXT_UNMEDIATED_VOLUME,  # Juvenile book special 28
    203: _TEXT_UNMEDIATED_VOLUME,  # Juvenile journal 28
    # --- LP vinyl (performed music / audio / audio disc) ----------------
    107: _PERFORMED_MUSIC_AUDIO_DISC,  # Adult LP 28
    207: _PERFORMED_MUSIC_AUDIO_DISC,  # Juvenile LP 28 (inferred — same shape as 107)
    # --- Audio cassette (performed music / audio / audio cassette) ------
    108: _PERFORMED_MUSIC_AUDIO_CASSETTE,  # Adult cassette 28
    # --- CD music (performed music / audio / audio disc) ----------------
    111: _PERFORMED_MUSIC_AUDIO_DISC,  # Adult CD mus 14
    211: _PERFORMED_MUSIC_AUDIO_DISC,  # Juvenile CD mus 14
    # --- CD spoken word / audiobook (spoken word / audio / audio disc) --
    110: _SPOKEN_WORD_AUDIO_DISC,  # Adult CD talk 28
    210: _SPOKEN_WORD_AUDIO_DISC,  # Juvenile CD talk 28
    # --- Console games (two 336: tdi+cop / computer / computer disc) ----
    114: _COMPUTER_GAME_COMPUTER_DISC,  # Adult console game S 14
    115: _COMPUTER_GAME_COMPUTER_DISC,  # Adult console game K07 14
    116: _COMPUTER_GAME_COMPUTER_DISC,  # Adult console game K12 14
    117: _COMPUTER_GAME_COMPUTER_DISC,  # Adult console game K16 14
    118: _COMPUTER_GAME_COMPUTER_DISC,  # Adult console game K18 14
    214: _COMPUTER_GAME_COMPUTER_DISC,  # Juvenile console game S 14
    215: _COMPUTER_GAME_COMPUTER_DISC,  # Juvenile console game K07 14
    216: _COMPUTER_GAME_COMPUTER_DISC,  # Juvenile console game K12 14
    217: _COMPUTER_GAME_COMPUTER_DISC,  # Juvenile console game K16 14
    # --- Board games (3-D form / unmediated / object) -------------------
    126: _BOARD_GAME,  # Adult board game 14
    127: _BOARD_GAME,  # Adult board game special 14
    226: _BOARD_GAME,  # Juvenile board game 14
    # --- DVD video (2-D moving image / video / videodisc) ---------------
    140: _VIDEO_VIDEODISC,  # Adult DVD S 14
    141: _VIDEO_VIDEODISC,  # Adult DVD K07 14
    142: _VIDEO_VIDEODISC,  # Adult DVD K12 14
    143: _VIDEO_VIDEODISC,  # Adult DVD K16 14
    144: _VIDEO_VIDEODISC,  # Adult DVD K18 14
    240: _VIDEO_VIDEODISC,  # Juvenile DVD S 14
    241: _VIDEO_VIDEODISC,  # Juvenile DVD K07 14
    242: _VIDEO_VIDEODISC,  # Juvenile DVD K12 14
    # --- Blu-ray video (same RDA tuple as DVD) --------------------------
    160: _VIDEO_VIDEODISC,  # Adult blu ray S 14
    161: _VIDEO_VIDEODISC,  # Adult blu ray K07 14
    162: _VIDEO_VIDEODISC,  # Adult blu ray K12 14
    163: _VIDEO_VIDEODISC,  # Adult blu ray K16 14
    164: _VIDEO_VIDEODISC,  # Adult blu ray K18 14
    245: _VIDEO_VIDEODISC,  # Juvenile blu ray S 14
    246: _VIDEO_VIDEODISC,  # Juvenile blu ray K07 14
    247: _VIDEO_VIDEODISC,  # Juvenile blu ray K12 14
    248: _VIDEO_VIDEODISC,  # Juvenile blu ray K16 14
    # --- CD/DVD-ROM (juvenile only had clear vote — tdi/v/vd) -----------
    220: _VIDEO_VIDEODISC,  # Juvenile CD/DVD-ROM S 28
    221: _VIDEO_VIDEODISC,  # Juvenile CD/DVD-ROM K07 28
    # --- Sheet music / score (notated music / unmediated / volume) ------
    150: _NOTATED_MUSIC_UNMEDIATED_VOLUME,  # Adult sheet music/score 28
    250: _NOTATED_MUSIC_UNMEDIATED_VOLUME,  # Juvenile sheet music/score 28
    # --- Other material — maps (cartographic image / unmediated / sheet)
    159: _MAP_SHEET,  # Adult other material 28
    #
    # ------------------------------------------------------------------
    # Itypes intentionally OMITTED (insufficient or ambiguous cataloguer
    # vote in the 2026-05-12 discovery query). Bibs whose items carry
    # only these itypes still drop on the 33X content-minimum gate —
    # preserving the strict behaviour where empirical evidence is too
    # thin to commit. Add entries here if the cataloguers later supply
    # an explicit mapping.
    #
    # 130 Adult video S 28 / 132 K12 / 133 K16 — sparse sample
    # 151-156, 251, 253 Adult/Juvenile object — tcf-vs-tdf tied; mixed
    # 159 Adult other material is *included* (clear maps signal); 254
    #     Juvenile other material is mixed.
    # ------------------------------------------------------------------
}


def lookup_rda_for_items(items: Iterable[Mapping[str, Any]]) -> RdaCarrier | None:
    """Pick an :class:`RdaCarrier` for a bib's items, or ``None`` if no
    item carries a mapped itype.

    Convention matches the empirical discovery query (which grouped
    bibs by ``MIN(itype_code_num)``): when a bib has items of multiple
    itypes, the **lowest-numbered** mapped itype wins. Deterministic
    and stable across re-runs.
    """
    candidates = sorted(
        item.get("item_type_num")
        for item in items
        if item is not None and item.get("item_type_num") in ITYPE_TO_RDA
    )
    if not candidates:
        return None
    return ITYPE_TO_RDA[candidates[0]]


__all__ = [
    "ITYPE_TO_RDA",
    "RdaCarrier",
    "lookup_rda_for_items",
]
