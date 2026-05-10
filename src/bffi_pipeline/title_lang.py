"""Multi-language title detection for M3 ``skos:prefLabel`` tagging.

Helmet cataloguers sometimes pack multiple parallel titles into a single
MARC 245$a per RDA convention (e.g. ``"Tšarka : the Russian charka = venäläinen
tšarkka = russkaja tšarka"``). This module turns those into per-language
``skos:prefLabel`` triples so Skosmos surfaces the right title in the
right UI locale.

Layered detection, cheapest-first:

1. Bound the candidate language set from the source record's
   ``bf:language`` URIs (constrained to the four languages the
   pipeline can disambiguate: ``fi`` / ``sv`` / ``en`` / ``ru``).
2. Split the title by Unicode script boundaries (Latin / Cyrillic).
   That alone separates Cyrillic Russian from the rest deterministically.
3. Within each script chunk, split by RDA separators
   (``" = "``, ``" / "``, ``" -- "``, em-dash with spaces, ``" | "``).
4. Per-segment language detection via :mod:`lingua`, constrained to the
   candidate set, accepted only above a confidence threshold.
5. If splits don't produce confidently-different languages, collapse
   back to a single segment with the whole-string detection. Avoids
   the worst failure mode (all 3 segments wrongly tagged with the same
   language because the segment-level detection wasn't reliable).

Lingua's known limitations on this corpus:

- **Romanized Russian** (e.g. ``"russkaja tšarka : vo vremena Romanovyh"``)
  is misclassified as Finnish/Slovene/Lithuanian because Lingua's
  Russian model is Cyrillic-only. The script-boundary split keeps
  Cyrillic-only segments correctly tagged; transliterated segments
  fall back to untagged + cataloguer review.
- **Short proper-noun-heavy segments** ("Tšarka : the Russian charka")
  bias toward whichever Latin-script language uses ``š``. The
  same-language-collapse heuristic mitigates the impact (we don't
  emit 3 confidently-wrong tags), but doesn't fix the title where
  every segment fools the detector.

The right way to fix the long tail is to cascade to the local LLM for
genuinely ambiguous cases, mirroring M6's judge cascade. That's a
separate future enhancement.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from bffi_pipeline.title_lang_llm import TitleLangDetector

#: RDA-style parallel-title separators (always space-padded). Try the
#: most specific first so ``" -- "`` doesn't get partially matched by
#: a generic dash split.
_RDA_SEPARATORS: Final[tuple[str, ...]] = (" = ", " / ", " -- ", " — ", " | ")

#: 2-letter BCP-47 codes the pipeline can disambiguate via Lingua. Any
#: candidate from outside this set is silently dropped at the candidate
#: bounding step — the segment ends up untagged and the cataloguer can
#: fix it later.
_SUPPORTED_LANGS: Final[frozenset[str]] = frozenset({"fi", "sv", "en", "ru"})

#: Below this Lingua confidence value, we leave the segment untagged
#: rather than guessing. 0.65 was chosen by probing against the curated
#: 13 records: short real titles like "War and Peace" (0.80), "Krig och
#: fred" (0.81) clear it comfortably; the rare genuinely-ambiguous case
#: falls below it. Higher thresholds (0.85+) reject too many real
#: records — Lingua's confidence drops sharply on short n-gram input.
_CONFIDENCE_THRESHOLD: Final[float] = 0.65

#: Unicode block prefix → coarse script bucket. Anything we don't
#: explicitly enumerate here lands in ``OTHER`` and doesn't drive
#: a script-boundary split.
_SCRIPT_PREFIXES: Final[dict[str, str]] = {
    "LATIN": "LATIN",
    "CYRILLIC": "CYRILLIC",
    "GREEK": "GREEK",
    "ARABIC": "ARABIC",
    "HEBREW": "HEBREW",
    "DEVANAGARI": "DEVANAGARI",
    "CJK": "CJK",
    "HIRAGANA": "CJK",
    "KATAKANA": "CJK",
    "HANGUL": "CJK",
}


@dataclass(frozen=True)
class TaggedSegment:
    """One parallel-title segment with its detected language code."""

    text: str
    lang: str | None  # 2-letter BCP-47, or None when low-confidence


def _script_of(ch: str) -> str:
    """Return a coarse script bucket for ``ch`` (LATIN / CYRILLIC / OTHER)."""
    name = unicodedata.name(ch, "")
    if not name:
        return "OTHER"
    head = name.split(" ", 1)[0]
    return _SCRIPT_PREFIXES.get(head, "OTHER")


def split_by_script(text: str) -> list[str]:
    """Split ``text`` where the Unicode script changes.

    OTHER characters (digits, punctuation, whitespace) attach to the
    preceding script chunk so ``"Aatelisrosvo Dubrovskij"`` doesn't
    fragment on the inter-word spaces.
    """
    if not text:
        return []
    pieces: list[list[str]] = [[text[0]]]
    cur = _script_of(text[0])
    for ch in text[1:]:
        s = _script_of(ch)
        if s in ("OTHER", cur):
            pieces[-1].append(ch)
            continue
        if cur == "OTHER":
            cur = s
            pieces[-1].append(ch)
            continue
        pieces.append([ch])
        cur = s
    return [stripped for p in pieces if (stripped := "".join(p).strip())]


def split_by_separators(
    text: str,
    separators: Iterable[str] = _RDA_SEPARATORS,
) -> list[str]:
    """Split on the first RDA separator that occurs in ``text``."""
    text = text.strip()
    if not text:
        return []
    for sep in separators:
        if sep in text:
            return [seg.strip() for seg in text.split(sep) if seg.strip()]
    return [text]


def _lingua_lang_map() -> dict[str, Any]:
    """Lazy import: ``lingua`` adds ~100 MB of n-gram models at startup."""
    from lingua import Language

    return {
        "fi": Language.FINNISH,
        "sv": Language.SWEDISH,
        "en": Language.ENGLISH,
        "ru": Language.RUSSIAN,
    }


@lru_cache(maxsize=1)
def _build_detector() -> Any:
    """Module-level singleton — building the detector takes ~1s."""
    from lingua import LanguageDetectorBuilder

    return LanguageDetectorBuilder.from_languages(*_lingua_lang_map().values()).build()


def detect_segment_lang(text: str, candidates: frozenset[str]) -> tuple[str | None, float]:
    """Return ``(lang_code | None, confidence)`` for one title segment.

    Pure-Cyrillic segments short-circuit to ``"ru"`` when Russian is a
    candidate — Lingua's Russian model is Cyrillic-only and gets the
    transliteration cases wrong, but Cyrillic-only is unambiguous.
    """
    text = text.strip()
    if not text or not candidates:
        return None, 0.0

    has_cyrillic = any(_script_of(c) == "CYRILLIC" for c in text)
    has_latin = any(_script_of(c) == "LATIN" for c in text)
    if has_cyrillic and not has_latin and "ru" in candidates:
        return "ru", 1.0

    detector = _build_detector()
    lang_map = _lingua_lang_map()
    inverse = {lang: code for code, lang in lang_map.items()}
    candidate_confs: dict[str, float] = {}
    for entry in detector.compute_language_confidence_values(text):
        code = inverse.get(entry.language)
        if code is not None and code in candidates:
            candidate_confs[code] = entry.value
    if not candidate_confs:
        return None, 0.0
    best_code, best_conf = max(candidate_confs.items(), key=lambda kv: kv[1])
    if best_conf >= _CONFIDENCE_THRESHOLD:
        return best_code, best_conf
    return None, best_conf


def tag_title(
    text: str,
    candidates: frozenset[str],
    *,
    llm_detector: TitleLangDetector | None = None,
) -> list[TaggedSegment]:
    """Split + tag a cataloguer title string.

    Returns at minimum one ``TaggedSegment``. Multiple segments come
    back only when the title contains both:
    1. A script-boundary or RDA separator that splits cleanly, AND
    2. The resulting segments are confidently detected as *different*
       languages (Lingua agrees that the title actually carries
       multiple languages) — OR an ``llm_detector`` is supplied and
       overrides Lingua's verdict for the ambiguous cases.

    When Lingua collapses (every segment maps to the same language) and
    an ``llm_detector`` is supplied, the cascade fires: the LLM gets
    the full title plus the candidate set and returns a per-segment
    assignment that the caller trusts. Without an ``llm_detector``,
    the same case falls back to the original full-string tag (avoids
    emitting N copies of the same probably-wrong tag).
    """
    text = text.strip()
    if not text:
        return []
    bounded = candidates & _SUPPORTED_LANGS
    if not bounded:
        return [TaggedSegment(text=text, lang=None)]

    script_chunks = split_by_script(text)
    segments: list[str] = []
    if len(script_chunks) <= 1:
        segments = split_by_separators(text)
    else:
        for chunk in script_chunks:
            segments.extend(split_by_separators(chunk))

    if len(segments) <= 1:
        lang, _ = detect_segment_lang(text, bounded)
        return [TaggedSegment(text=text, lang=lang)]

    tagged = [
        TaggedSegment(text=seg, lang=detect_segment_lang(seg, bounded)[0]) for seg in segments
    ]
    non_none = {t.lang for t in tagged if t.lang is not None}
    all_share_one_lang = all(t.lang is not None for t in tagged) and len(non_none) == 1
    if not all_share_one_lang:
        return tagged

    # Tšarka failure mode: every Latin segment confidently misclassifies
    # to the same language. Lingua's signal is unreliable here — escalate
    # to the LLM cascade if available, else fall back to the original
    # full string with whole-string detection.
    if llm_detector is not None:
        decision = llm_detector.detect(title=text, candidates=bounded)
        return [TaggedSegment(text=seg.text, lang=seg.lang) for seg in decision.segments]
    lang, _ = detect_segment_lang(text, bounded)
    return [TaggedSegment(text=text, lang=lang)]


__all__ = [
    "TaggedSegment",
    "detect_segment_lang",
    "split_by_script",
    "split_by_separators",
    "tag_title",
]
