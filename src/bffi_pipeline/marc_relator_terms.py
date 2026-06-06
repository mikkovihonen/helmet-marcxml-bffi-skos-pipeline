"""Project-curated mapping from Finnish / Swedish MARC ``$e`` relator
terms to LoC relator URIs.

Grounded in the full-corpus inventory at
``scratchpad/2026-06-07-relator-term-inventory.md`` — every term below
appears at least 40 times in the 800k-record Helmet MARCXML. The top
100 keys cover > 99% of all ``$e`` occurrences across MARC tags
100 / 110 / 111 / 700 / 710 / 711.

Two categories:

1. **Specific creator/contributor roles** map to the matching LoC
   relator code (aut, cmp, trl, edt, ill, ...). MARC-FI cataloguing
   convention closely mirrors LoC's relator vocab on these.

2. **Musical instrument and vocal-range terms** all map to
   ``performer`` (prf). The LoC relator vocab doesn't have per-
   instrument codes, and what cataloguers care about is the
   distinction "this contribution was a performer, not a creator"
   — the instrument specificity lives downstream in the
   ``bf:role`` blank-node ``rdfs:label`` that M3 emits alongside.

Lookups are case-insensitive after the caller normalises (lowercase
+ strip trailing punctuation). Swedish equivalents
(``författare``, ``översättare``, ``konstnär``, ``illustratör``,
``redaktör``, ``fotograf``) collapse to the same LoC URI as their
Finnish twins.

Terms not in this table fall back to the existing M3 free-text
path: ``bf:role`` blank node with ``rdfs:label`` only, no URI
binding. Ambiguous terms like ``johtaja`` (director / conductor /
manager) are intentionally NOT mapped — keeping them ambiguous in
free-text form is more honest than a wrong guess.
"""

from __future__ import annotations

from typing import Final

_RELATORS: Final[str] = "http://id.loc.gov/vocabulary/relators/"

#: One mapping that serves every contributor-bearing MARC tag
#: (100/110/111/700/710/711) — the term vocabulary is the same across
#: all six. Keys MUST be lowercased + trailing-punctuation-stripped.
RELATOR_TERM_TO_URI: Final[dict[str, str]] = {
    # --- Creator / writing roles ------------------------------------------
    "kirjoittaja": _RELATORS + "aut",
    "kirjailija": _RELATORS + "aut",
    "författare": _RELATORS + "aut",  # Swedish
    "käsikirjoittaja": _RELATORS + "aus",  # screenplay author
    "sanoittaja": _RELATORS + "lyr",  # lyricist
    "esipuheen kirjoittaja": _RELATORS + "wpr",  # writer of preface
    "soitonoppaan tekijä": _RELATORS + "aut",
    "alkuperäisidean luoja": _RELATORS + "cre",  # creator of original idea
    "alkuperäisteoksen luoja": _RELATORS + "cre",
    "kokoaja": _RELATORS + "com",  # compiler
    # --- Music creation roles ---------------------------------------------
    "säveltäjä": _RELATORS + "cmp",
    "sovittaja": _RELATORS + "arr",
    # --- Editing / translation / illustration -----------------------------
    "toimittaja": _RELATORS + "edt",
    "redaktör": _RELATORS + "edt",  # Swedish
    "kääntäjä": _RELATORS + "trl",
    "översättare": _RELATORS + "trl",  # Swedish
    "kuvittaja": _RELATORS + "ill",
    "illustratör": _RELATORS + "ill",  # Swedish
    "taiteilija": _RELATORS + "art",
    "konstnär": _RELATORS + "art",  # Swedish
    "sarjakuvantekijä": _RELATORS + "art",  # comic-book creator
    "serieskapare": _RELATORS + "art",  # Swedish
    "designer": _RELATORS + "dsr",
    # --- Photography / film / production ----------------------------------
    "valokuvaaja": _RELATORS + "pht",
    "valokuvaaja (ekspressio)": _RELATORS + "pht",
    "valokuvaaja (manifestaatio)": _RELATORS + "pht",
    "fotograf": _RELATORS + "pht",  # Swedish
    "kuvaaja": _RELATORS + "pht",  # photographer / cinematographer
    "ohjaaja": _RELATORS + "drt",  # film director
    "tuottaja": _RELATORS + "pro",  # producer
    "tuotantoyhtiö": _RELATORS + "prn",  # production company
    "näyttelijä": _RELATORS + "act",
    "ääninäyttelijä": _RELATORS + "act",  # voice actor
    "lukija": _RELATORS + "nrt",  # narrator (audiobooks)
    "äänittäjä": _RELATORS + "rce",  # recording engineer
    "miksaaja": _RELATORS + "rce",
    "remiksaaja": _RELATORS + "rce",
    # --- Publishing -------------------------------------------------------
    "kustantaja": _RELATORS + "pbl",
    "julkaisija": _RELATORS + "pbl",
    "utgivare": _RELATORS + "pbl",  # Swedish
    # --- Teaching / interview / cartography -------------------------------
    "opettaja": _RELATORS + "tch",
    "haastattelija": _RELATORS + "ivr",
    "haastateltava": _RELATORS + "ive",
    "kartantekijä": _RELATORS + "ctg",  # cartographer
    # --- Performance (everything else falls here) -------------------------
    "esittäjä": _RELATORS + "prf",
    "esittäjä (manifestaatio)": _RELATORS + "prf",
    "(esittäjä)": _RELATORS + "prf",
    "artisti": _RELATORS + "prf",
    "voc": _RELATORS + "voc",  # vocalist (LoC has it)
    # --- Vocal-range roles → performer ------------------------------------
    "laulaja": _RELATORS + "sng",  # singer
    "laulu": _RELATORS + "sng",
    "sopraano": _RELATORS + "prf",
    "mezzosopraano": _RELATORS + "prf",
    "altto": _RELATORS + "prf",
    "kontra-altto": _RELATORS + "prf",
    "tenori": _RELATORS + "prf",
    "kontratenori": _RELATORS + "prf",
    "baritoni": _RELATORS + "prf",
    "basso": _RELATORS + "prf",
    "ääni": _RELATORS + "prf",
    # --- Instrument terms → performer (prf) -------------------------------
    # Coverage matches the corpus inventory's top 200 terms. The
    # instrument name itself is preserved by the existing free-text
    # rdfs:label that M3 emits alongside the bf:role URI; the
    # cataloguer sees both "Adrian, Esa $4 prf $e piano" so the
    # specificity isn't lost.
    "piano": _RELATORS + "prf",
    "akustinen piano": _RELATORS + "prf",
    "sähköpiano": _RELATORS + "prf",
    "flyygeli": _RELATORS + "prf",
    "urut": _RELATORS + "prf",
    "sähköurut": _RELATORS + "prf",
    "cembalo": _RELATORS + "prf",
    "harmonikka": _RELATORS + "prf",
    "harmoni": _RELATORS + "prf",
    "kosketinsoittimet": _RELATORS + "prf",
    "syntetisaattori": _RELATORS + "prf",
    "celesta": _RELATORS + "prf",
    "viulu": _RELATORS + "prf",
    "alttoviulu": _RELATORS + "prf",
    "sello": _RELATORS + "prf",
    "kontrabasso": _RELATORS + "prf",
    "harppu": _RELATORS + "prf",
    "kitara": _RELATORS + "prf",
    "akustinen kitara": _RELATORS + "prf",
    "sähkökitara": _RELATORS + "prf",
    "bassokitara": _RELATORS + "prf",
    "sähköbasso": _RELATORS + "prf",
    "steel-kitara": _RELATORS + "prf",
    "ukulele": _RELATORS + "prf",
    "banjo": _RELATORS + "prf",
    "mandoliini": _RELATORS + "prf",
    "kantele": _RELATORS + "prf",
    "balalaikka": _RELATORS + "prf",
    "luuttu": _RELATORS + "prf",
    "huilu": _RELATORS + "prf",
    "alttohuilu": _RELATORS + "prf",
    "piccolohuilu": _RELATORS + "prf",
    "nokkahuilu": _RELATORS + "prf",
    "klarinetti": _RELATORS + "prf",
    "bassoklarinetti": _RELATORS + "prf",
    "saksofoni": _RELATORS + "prf",
    "sopraanosaksofoni": _RELATORS + "prf",
    "alttosaksofoni": _RELATORS + "prf",
    "tenorisaksofoni": _RELATORS + "prf",
    "baritonisaksofoni": _RELATORS + "prf",
    "oboe": _RELATORS + "prf",
    "englannintorvi": _RELATORS + "prf",
    "fagotti": _RELATORS + "prf",
    "kontrafagotti": _RELATORS + "prf",
    "trumpetti": _RELATORS + "prf",
    "kornetti": _RELATORS + "prf",
    "pasuuna": _RELATORS + "prf",
    "käyrätorvi": _RELATORS + "prf",
    "tuuba": _RELATORS + "prf",
    "vaskipuhaltimet": _RELATORS + "prf",
    "puhaltimet": _RELATORS + "prf",
    "puupuhaltimet": _RELATORS + "prf",
    "rummut": _RELATORS + "prf",
    "lyömäsoittimet": _RELATORS + "prf",
    "vibrafoni": _RELATORS + "prf",
    "marimba": _RELATORS + "prf",
    "ksylofoni": _RELATORS + "prf",
    "huuliharppu": _RELATORS + "prf",
    "haitari": _RELATORS + "prf",
    "didgeridoo": _RELATORS + "prf",
    "perkussio": _RELATORS + "prf",
    "perkussiot": _RELATORS + "prf",
    # --- DJ / electronic --------------------------------------------------
    "dj": _RELATORS + "prf",
    "elektroniikka": _RELATORS + "prf",
    "ohjelmointi": _RELATORS + "prf",
    "sampleri": _RELATORS + "prf",
    "turntablismi": _RELATORS + "prf",
}


def normalise_relator_term(term: str) -> str:
    """Normalise a raw MARC ``$e`` value the same way the inventory
    script did so dict lookups hit. Lowercase + strip whitespace +
    drop a trailing ``,`` / ``.``.
    """
    return term.strip().rstrip(",.").strip().lower()


def lookup_relator_uri(term: str | None) -> str | None:
    """Return the LoC relator URI for a Finnish/Swedish ``$e`` term, or
    ``None`` when the term isn't in the curated map. Callers fall back
    to the existing free-text role path on a miss."""
    if not term:
        return None
    return RELATOR_TERM_TO_URI.get(normalise_relator_term(term))


__all__ = [
    "RELATOR_TERM_TO_URI",
    "lookup_relator_uri",
    "normalise_relator_term",
]
