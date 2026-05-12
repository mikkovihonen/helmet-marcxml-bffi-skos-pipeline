"""Unit tests for ``marcxml_export_pipeline.sierra.rda_signals``.

Exercises each cascade layer in isolation + the slot-wise composer.
End-to-end behaviour (cascade → MARCXML output) is covered separately
by the ``build_marcxml_for_row`` tests in ``test_marcxml.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from marcxml_export_pipeline.sierra.dtos import Subfield, Varfield
from marcxml_export_pipeline.sierra.itype_to_rda import (
    MAP_SHEET,
    NOTATED_MUSIC_UNMEDIATED_VOLUME,
    PERFORMED_MUSIC_AUDIO_CASSETTE,
    PERFORMED_MUSIC_AUDIO_DISC,
    TEXT_UNMEDIATED_VOLUME,
    VIDEO_VIDEODISC,
)
from marcxml_export_pipeline.sierra.rda_signals import (
    COMPUTER_ONLINE,
    DEFAULT_LAYERS,
    LEADER_008_TO_RDA,
    TEXT_BRAILLE,
    TEXT_LARGE_PRINT,
    TEXT_MICROFICHE,
    TEXT_MICROFILM,
    TEXT_ONLINE,
    THREE_D_OBJECT,
    TWO_D_GRAPHIC_SHEET,
    VIDEO_ONLINE,
    VIDEO_VIDEOCASSETTE,
    PartialRda,
    RecordContext,
    from_300_a_extent,
    from_items_itype,
    from_leader_and_008,
    from_marc_007,
    from_material_code,
    partial_from_carrier,
    resolve_rda,
)

# --- Test helpers --------------------------------------------------------


def _ctx(
    *,
    leader: str | None = None,
    controlfields: Sequence[tuple[str, str]] = (),
    varfields: tuple[Any, ...] = (),
    items: Sequence[Mapping[str, Any]] = (),
    material_code: str | None = None,
) -> RecordContext:
    """Build a RecordContext for a single layer-test scenario."""
    return RecordContext(
        leader_record_type=leader,
        controlfields=tuple(controlfields),
        varfields=tuple(varfields),
        items=tuple(items),
        material_code=material_code,
    )


def _padded_008(form: str, leader_06: str = "a") -> str:
    """Build a 40-char 008 string with ``form`` at the right position
    for the given leader/06 class. Pads the rest with spaces."""
    # Books/computer/music/sound recordings → 008/23; visual/cartographic → 008/29.
    pos = 29 if leader_06 in {"e", "f", "g", "k", "o", "r"} else 23
    return " " * pos + form + " " * (40 - pos - 1)


def _vf_300(extent: str) -> Varfield:
    """Build a synthetic 300 :class:`Varfield` with one ``$a`` subfield."""
    vf = Varfield(
        id=None,
        marc_tag="300",
        marc_ind1=" ",
        marc_ind2=" ",
        field_content="",
    )
    vf.subfields = [Subfield(tag="a", content=extent, display_order=0)]
    return vf


# --- PartialRda mechanics ------------------------------------------------


def test_partial_rda_merge_below_fills_only_empty_slots() -> None:
    """``merge_below`` is the cascade's load-bearing primitive — a
    higher-priority layer's filled slots stay; empty slots get filled
    from below."""
    upper = PartialRda(media=("audio", "s"))
    lower = PartialRda(
        content_types=(("teksti", "txt"),),
        media=("käytettävissä ilman laitetta", "n"),
        carrier=("nide", "nc"),
    )
    merged = upper.merge_below(lower)
    assert merged.content_types == (("teksti", "txt"),)
    assert merged.media == ("audio", "s")  # upper wins on this slot
    assert merged.carrier == ("nide", "nc")


def test_partial_rda_is_complete_only_when_all_three_slots_filled() -> None:
    assert not PartialRda().is_complete()
    assert not PartialRda(media=("audio", "s")).is_complete()
    assert PartialRda(
        content_types=(("teksti", "txt"),),
        media=("käytettävissä ilman laitetta", "n"),
        carrier=("nide", "nc"),
    ).is_complete()


def test_partial_rda_to_carrier_returns_none_when_incomplete() -> None:
    assert PartialRda(media=("audio", "s")).to_carrier() is None


def test_partial_from_carrier_roundtrips_a_complete_rda() -> None:
    """A complete ``RdaCarrier`` survives ``partial_from_carrier →
    PartialRda.to_carrier`` round-trip unchanged."""
    p = partial_from_carrier(TEXT_UNMEDIATED_VOLUME)
    rt = p.to_carrier()
    assert rt == TEXT_UNMEDIATED_VOLUME


def test_partial_from_carrier_handles_none() -> None:
    assert partial_from_carrier(None) == PartialRda()


# --- resolve_rda composer ------------------------------------------------


def test_resolve_rda_short_circuits_when_first_layer_completes() -> None:
    """If the highest-priority layer fills all three slots, the cascade
    stops there. Verified via a layer-call counter."""
    calls = []

    def layer_complete(_: RecordContext) -> PartialRda:
        calls.append("complete")
        return partial_from_carrier(TEXT_UNMEDIATED_VOLUME)

    def layer_unreachable(_: RecordContext) -> PartialRda:
        calls.append("unreachable")
        return PartialRda()

    rda = resolve_rda(_ctx(), (layer_complete, layer_unreachable))
    assert rda == TEXT_UNMEDIATED_VOLUME
    assert "unreachable" not in calls


def test_resolve_rda_composes_slots_from_multiple_layers() -> None:
    """Different layers can each fill a different slot — the composer
    combines them into one complete tuple."""

    def layer_336(_: RecordContext) -> PartialRda:
        return PartialRda(content_types=(("teksti", "txt"),))

    def layer_337(_: RecordContext) -> PartialRda:
        return PartialRda(media=("audio", "s"))

    def layer_338(_: RecordContext) -> PartialRda:
        return PartialRda(carrier=("äänilevy", "sd"))

    rda = resolve_rda(_ctx(), (layer_336, layer_337, layer_338))
    assert rda is not None
    assert rda.content_types == (("teksti", "txt"),)
    assert rda.media == ("audio", "s")
    assert rda.carrier == ("äänilevy", "sd")


def test_resolve_rda_returns_none_when_any_slot_unfilled() -> None:
    def layer_336_only(_: RecordContext) -> PartialRda:
        return PartialRda(content_types=(("teksti", "txt"),))

    assert resolve_rda(_ctx(), (layer_336_only,)) is None


# --- from_marc_007 -------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected_media", "expected_carrier"),
    [
        # 007 cat 's' (sound recording) — 007/01 disc vs cassette.
        ("sd ", ("audio", "s"), ("äänilevy", "sd")),
        ("ss ", ("audio", "s"), ("äänikasetti", "ss")),
        # 007 cat 'v' (video) — 007/04 disc / cassette / cartridge.
        # 5-char fixture: 'v' + 3 spaces + carrier-char at index 4.
        ("v   v", ("video", "v"), ("videolevy", "vd")),
        ("v   s", ("video", "v"), ("videokasetti", "vf")),
        ("v   f", ("video", "v"), ("videopatruuna", "vc")),
        # 007 cat 'c' (computer/electronic) — 007/01 disc vs remote.
        ("co ", ("tietokonekäyttöinen", "c"), ("tietolevy", "cd")),
        ("cr ", ("tietokonekäyttöinen", "c"), ("verkkoaineisto", "cr")),
        # 007 cat 't' (text) — 007/01 regular vs large print.
        ("ta ", ("käytettävissä ilman laitetta", "n"), ("nide", "nc")),
        ("tb ", ("käytettävissä ilman laitetta", "n"), ("isotekstinen nide", "nc")),
    ],
)
def test_from_marc_007_resolves_carrier_for_each_category(
    content: str, expected_media: tuple[str, str], expected_carrier: tuple[str, str]
) -> None:
    ctx = _ctx(controlfields=[("007", content)])
    p = from_marc_007(ctx)
    assert p.media == expected_media
    assert p.carrier == expected_carrier
    # 007 never fills the content slot — layer 2 does that.
    assert p.content_types is None


def test_from_marc_007_returns_empty_when_007_absent() -> None:
    assert from_marc_007(_ctx()) == PartialRda()


def test_from_marc_007_unmapped_category_returns_empty() -> None:
    """007 cat 'm' (motion picture) is not in the initial table — the
    layer skips it cleanly and lower-priority layers fill the slots."""
    assert from_marc_007(_ctx(controlfields=[("007", "mr ")])) == PartialRda()


def test_from_marc_007_known_cat_with_unmapped_subposition_fills_media_only() -> None:
    """007=s with an obscure 007/01 (say 'u' unspecified): media still
    fills (audio is implied by cat=s), carrier stays empty so the
    cascade's lower layers can fill it."""
    p = from_marc_007(_ctx(controlfields=[("007", "su ")]))
    assert p.media == ("audio", "s")
    assert p.carrier is None


# --- from_leader_and_008: spot-check the table ---------------------------


@pytest.mark.parametrize(
    ("leader_06", "form", "expected"),
    [
        # Text — the dominant 80 % of drops on the 5k sample.
        ("a", " ", TEXT_UNMEDIATED_VOLUME),
        ("a", "r", TEXT_UNMEDIATED_VOLUME),  # regular print reproduction
        ("a", "d", TEXT_LARGE_PRINT),
        ("a", "f", TEXT_BRAILLE),
        ("a", "a", TEXT_MICROFILM),
        ("a", "b", TEXT_MICROFICHE),
        ("a", "o", TEXT_ONLINE),
        ("a", "s", TEXT_ONLINE),
        ("a", "q", TEXT_ONLINE),
        # Manuscripts share text's default carrier.
        ("t", " ", TEXT_UNMEDIATED_VOLUME),
        # Video — second-largest slice of drops.
        ("g", " ", VIDEO_VIDEODISC),
        ("g", "d", VIDEO_VIDEODISC),
        ("g", "o", VIDEO_ONLINE),
        # Computer file defaults to online (Helmet's dominant pattern).
        ("m", " ", COMPUTER_ONLINE),
        ("m", "o", COMPUTER_ONLINE),
        # Maps / 2-D graphics / 3-D objects.
        ("e", " ", LEADER_008_TO_RDA[("e", " ")]),
        ("k", " ", TWO_D_GRAPHIC_SHEET),
        ("r", " ", THREE_D_OBJECT),
    ],
)
def test_from_leader_and_008_resolves_all_table_entries(
    leader_06: str, form: str, expected: Any
) -> None:
    ctx = _ctx(
        leader=leader_06,
        controlfields=[("008", _padded_008(form, leader_06))],
    )
    p = from_leader_and_008(ctx)
    assert p.to_carrier() == expected


def test_from_leader_and_008_falls_back_to_leader_only_when_008_form_unmapped() -> None:
    """If 008-form is set to a code not in :data:`LEADER_008_TO_RDA`
    for that leader/06, the layer falls back to
    :data:`LEADER_06_FALLBACK`. Ensures stray 008-form values don't
    leave the slot empty."""
    # 008/23 = 'z' is undefined; fall back to leader/06='a' default.
    ctx = _ctx(leader="a", controlfields=[("008", _padded_008("z"))])
    p = from_leader_and_008(ctx)
    assert p.to_carrier() == TEXT_UNMEDIATED_VOLUME


def test_from_leader_and_008_falls_back_when_008_missing() -> None:
    """A record with leader/06 mapped but no 008 controlfield at all
    still gets the leader's default RDA tuple. Rare in practice
    (Phase A's drop list had 008 on 100 % of records) but correct."""
    p = from_leader_and_008(_ctx(leader="a"))
    assert p.to_carrier() == TEXT_UNMEDIATED_VOLUME


def test_from_leader_and_008_returns_empty_when_leader_unmapped() -> None:
    """leader/06 = 'o' (kit) is intentionally not in
    :data:`LEADER_06_FALLBACK` — kits carry multiple content types
    and need 006 to disambiguate (Phase C). The cascade continues to
    lower-priority layers."""
    assert from_leader_and_008(_ctx(leader="o")) == PartialRda()


def test_from_leader_and_008_returns_empty_when_leader_record_type_none() -> None:
    """A record with no parsed leader (defensive — should not happen
    in production since ``build_leader`` always returns 24 chars)."""
    assert from_leader_and_008(_ctx()) == PartialRda()


# --- Adapter layers ------------------------------------------------------


def test_from_material_code_returns_partial_for_mapped_code() -> None:
    """The bib-level material code adapter delegates to
    :data:`itype_to_rda.MATERIAL_TO_RDA`. 'g' → DVD."""
    p = from_material_code(_ctx(material_code="g"))
    assert p.to_carrier() == VIDEO_VIDEODISC


def test_from_material_code_returns_empty_when_code_unmapped() -> None:
    assert from_material_code(_ctx(material_code="x")) == PartialRda()
    assert from_material_code(_ctx(material_code=None)) == PartialRda()


def test_from_items_itype_picks_lowest_mapped() -> None:
    """The item-level adapter delegates to
    :func:`itype_to_rda.lookup_rda_for_items`, which picks the
    lowest-numbered mapped itype (deterministic; matches the
    empirical discovery query)."""
    items = [
        {"item_type_num": 200, "location_code": "kk", "copy_num": 1},
        {"item_type_num": 100, "location_code": "kk", "copy_num": 1},
    ]
    p = from_items_itype(_ctx(items=items))
    assert p.to_carrier() == TEXT_UNMEDIATED_VOLUME  # itype 100 (Adult book 28)


def test_from_items_itype_returns_empty_when_no_items() -> None:
    assert from_items_itype(_ctx()) == PartialRda()


# --- from_300_a_extent ---------------------------------------------------


@pytest.mark.parametrize(
    ("extent", "expected"),
    [
        # Video forms — specific (DVD-levy) and short (DVD) variants
        # both hit the videodisc rule.
        ("1 DVD-levy", VIDEO_VIDEODISC),
        ("2 DVD-levyä", VIDEO_VIDEODISC),
        ("1 DVD", VIDEO_VIDEODISC),
        ("1 Blu-ray-levy", VIDEO_VIDEODISC),
        ("1 Blu-ray", VIDEO_VIDEODISC),
        ("1 videolevy", VIDEO_VIDEODISC),
        ("1 videokasetti", VIDEO_VIDEOCASSETTE),
        # Audio — LP / cassette / generic disc.
        ("1 LP-levy", PERFORMED_MUSIC_AUDIO_DISC),
        ("2 LP-levyä", PERFORMED_MUSIC_AUDIO_DISC),
        ("1 LP", PERFORMED_MUSIC_AUDIO_DISC),
        ("1 äänikasetti", PERFORMED_MUSIC_AUDIO_CASSETTE),
        ("1 C-kasetti", PERFORMED_MUSIC_AUDIO_CASSETTE),
        ("1 äänilevy", PERFORMED_MUSIC_AUDIO_DISC),
        ("1 CD-levy", PERFORMED_MUSIC_AUDIO_DISC),
        ("1 CD", PERFORMED_MUSIC_AUDIO_DISC),
        # Notated music / cartographic / 3-D.
        ("1 nuotti", NOTATED_MUSIC_UNMEDIATED_VOLUME),
        ("8 nuottia", NOTATED_MUSIC_UNMEDIATED_VOLUME),
        ("1 kartta", MAP_SHEET),
        ("3 karttaa", MAP_SHEET),
        ("1 esine", THREE_D_OBJECT),
        ("12 esinettä", THREE_D_OBJECT),
        # Text — generic forms.
        ("1 kuvateos", TEXT_UNMEDIATED_VOLUME),
        ("1 kirja", TEXT_UNMEDIATED_VOLUME),
        ("3 kirjaa", TEXT_UNMEDIATED_VOLUME),
        ("1 nide", TEXT_UNMEDIATED_VOLUME),
        ("128 sivua", TEXT_UNMEDIATED_VOLUME),
        ("64 pages", TEXT_UNMEDIATED_VOLUME),
    ],
)
def test_from_300_a_extent_resolves_known_tokens(extent: str, expected: object) -> None:
    p = from_300_a_extent(_ctx(varfields=(_vf_300(extent),)))
    assert p.to_carrier() == expected


def test_from_300_a_extent_returns_empty_when_no_300_field() -> None:
    assert from_300_a_extent(_ctx()) == PartialRda()


def test_from_300_a_extent_returns_empty_when_300_a_unrecognised() -> None:
    """A 300$a with no known token (e.g. an obscure unit like
    ``1 mikroskooppimateriaali``) must return empty so the cascade
    can keep walking — never emit a wrong tuple."""
    p = from_300_a_extent(_ctx(varfields=(_vf_300("1 mikroskooppimateriaali"),)))
    assert p == PartialRda()


def test_from_300_a_extent_specific_token_beats_generic() -> None:
    """``DVD-levy`` should hit the video rule, not the audio-disc
    fallback (the table's ordering enforces this — first match wins
    and DVD-levy is listed before generic ``levy``-fragments)."""
    p = from_300_a_extent(_ctx(varfields=(_vf_300("1 DVD-levy, 120 min"),)))
    assert p.to_carrier() == VIDEO_VIDEODISC


def test_from_300_a_extent_concatenates_multiple_300_fields() -> None:
    """Some bibs carry more than one 300 datafield (e.g. accompanying
    material). The layer scans across all 300$a contents — a token in
    any of them wins."""
    vfs = (
        _vf_300("1 DVD-levy"),
        _vf_300("liiteaineisto"),
    )
    p = from_300_a_extent(_ctx(varfields=vfs))
    assert p.to_carrier() == VIDEO_VIDEODISC


def test_from_300_a_extent_ignores_non_300_varfields() -> None:
    """Tokens in 245$h or 500$a shouldn't fire this layer — only 300$a."""
    bogus = Varfield(
        id=None,
        marc_tag="500",
        marc_ind1=" ",
        marc_ind2=" ",
        field_content="",
    )
    bogus.subfields = [Subfield(tag="a", content="1 DVD-levy", display_order=0)]
    p = from_300_a_extent(_ctx(varfields=(bogus,)))
    assert p == PartialRda()


# --- DEFAULT_LAYERS cascade priority -------------------------------------


def test_default_layers_priority_007_overrides_leader_008() -> None:
    """When both 007 and leader+008 fire, 007's media/carrier slots
    win because 007 sits higher in the cascade. The cascade still
    fills 336 from leader+008 because 007 doesn't fill it."""
    # leader/06='g' + 008-form=' ' would say (tdi, v, vd) — videodisc.
    # 007='vs' overrides to videocassette.
    ctx = _ctx(
        leader="g",
        controlfields=[("007", "v   s"), ("008", _padded_008(" ", "g"))],
    )
    rda = resolve_rda(ctx, DEFAULT_LAYERS)
    assert rda is not None
    # 336 from leader: tdi (videodisc and videocassette share content type).
    assert rda.content_types == (("kaksiulotteinen liikkuva kuva", "tdi"),)
    # 337/338 from 007: videocassette wins over the leader+008 default.
    assert rda.media == ("video", "v")
    assert rda.carrier == ("videokasetti", "vf")


def test_default_layers_leader_008_intercepts_before_material_code() -> None:
    """When leader+008 fully resolves, the material_code / itype
    adapters never fire. Verifies that adapters are below leader+008
    in the priority order."""
    # leader='a' + 008-form=' ' → TEXT_UNMEDIATED_VOLUME.
    # material_code='g' would say DVD if it were the deciding layer.
    ctx = _ctx(
        leader="a",
        controlfields=[("008", _padded_008(" "))],
        material_code="g",
    )
    rda = resolve_rda(ctx, DEFAULT_LAYERS)
    assert rda == TEXT_UNMEDIATED_VOLUME


def test_default_layers_falls_through_to_material_when_leader_unmapped() -> None:
    """leader/06='o' (kit, unmapped) + material_code='1' (book) →
    material_code adapter takes over and resolves to book."""
    ctx = _ctx(leader="o", material_code="1")
    rda = resolve_rda(ctx, DEFAULT_LAYERS)
    assert rda == TEXT_UNMEDIATED_VOLUME


def test_default_layers_falls_through_to_items_when_material_unmapped() -> None:
    """leader='o' + material_code='x' (unmapped) + items itype=140 →
    itype adapter resolves to DVD."""
    items = [{"item_type_num": 140, "location_code": "kk", "copy_num": 1}]
    ctx = _ctx(leader="o", material_code="x", items=items)
    rda = resolve_rda(ctx, DEFAULT_LAYERS)
    assert rda == VIDEO_VIDEODISC


def test_default_layers_falls_through_to_extent_when_all_else_fails() -> None:
    """leader='o' + no material + no items + 300$a token → extent
    layer resolves. Verifies the 300$a fallback is wired in at the
    bottom of the cascade."""
    ctx = _ctx(leader="o", varfields=(_vf_300("1 DVD-levy"),))
    rda = resolve_rda(ctx, DEFAULT_LAYERS)
    assert rda == VIDEO_VIDEODISC


def test_default_layers_extent_does_not_override_leader_008() -> None:
    """If leader+008 resolves, the 300$a extent layer must NOT
    re-fill any slot. Verified by giving a contradicting extent and
    checking the leader+008 result wins."""
    ctx = _ctx(
        leader="a",
        controlfields=[("008", _padded_008(" "))],
        varfields=(_vf_300("1 DVD-levy"),),  # would say videodisc
    )
    rda = resolve_rda(ctx, DEFAULT_LAYERS)
    # leader+008 wins → text/n/nc, not video.
    assert rda == TEXT_UNMEDIATED_VOLUME


def test_default_layers_returns_none_when_no_signal_speaks() -> None:
    """leader='o' (unmapped) + no 007 + no 008-form + no
    material/itype → cascade returns None and the caller leaves 33X
    un-synthesised. The M2 gate then drops the record."""
    rda = resolve_rda(_ctx(leader="o"), DEFAULT_LAYERS)
    assert rda is None


def test_default_layers_leader_008_pairs_with_007_audio_for_music() -> None:
    """A music CD record with leader/06='j' (M2-exempt anyway but
    handled for forward-compat): leader+008 → performed music + disc;
    007 'sd' agrees. Cascade returns the complete tuple."""
    ctx = _ctx(
        leader="j",
        controlfields=[("007", "sd "), ("008", _padded_008(" "))],
    )
    rda = resolve_rda(ctx, DEFAULT_LAYERS)
    assert rda == PERFORMED_MUSIC_AUDIO_DISC
