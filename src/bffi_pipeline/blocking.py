"""Stage-1 deterministic blocking-key composition (pure utility).

The key is a cheap rule-based identifier — *not* a Work URI — used to
shrink the candidate-pair space before any embedding or LLM runs. Both
the M4 statistics stage (``stages/workkey``) and the M5 embedding stage
(``stages/embeddings``) need the *same* key for the same Work; keeping
the composition here means there is exactly one definition.

The key is the concatenation of three normalised tokens:

* normalised creator **surname** (everything before the first comma, or
  the first whitespace-delimited token);
* the first **significant** title token (skipping a small multilingual
  stop-word list — articles in fi / sv / en / de / fr / it / es);
* a short **content type** code (e.g. ``txt``, ``ntm``).

All tokens are normalised by case-folding, stripping non-alphanumerics,
and applying :func:`fold_diacritics` — a *selective* combining-mark
fold that **preserves native åäö** (Finnish / Swedish, both cases) but
folds every other Latin diacritic. Native diacritics carry lexemic
meaning in Finnish cataloguer-supplied input (``Häme`` vs ``hame``,
``Hämeenlinna`` vs ``Hameenlinna``, ``Yrjö`` vs ``Yrjo``); folding
them at Stage 1 would mash unrelated topics into one block.

Other diacritics (``é``, ``ï``, ``ñ``, ``ü``, ``ç``, …) are foreign
to Finnish cataloguers, may be inconsistently transcribed across
source records, and are folded so e.g. ``Müller`` / ``Muller`` and
``LINDGRÉN`` / ``Lindgren`` block together.

Cross-script transliteration variants (``Tolstoï`` vs ``Tolstoy`` vs
``Толстой``) are *not* bridged at Stage 1; that's M5's job via the
HNSW embedding index, which is multilingual and handles Cyrillic ↔
Latin natively. Diacritics remain *preserved* in canonical URI
minting (``uris.py``); the three stages serve different goals.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

_PLACEHOLDER_CREATOR: Final[str] = "anon"
_PLACEHOLDER_TITLE: Final[str] = "untitled"
_PLACEHOLDER_CONTENT: Final[str] = "unk"
_KEY_SEPARATOR: Final[str] = "|"

# Articles / leading function words this pipeline treats as non-significant.
# Multilingual; deliberately small. Entries are stored already-normalised
# (ASCII-only, casefolded) so lookups happen after normalisation.
_TITLE_STOP_WORDS: Final[frozenset[str]] = frozenset(
    {
        # English
        "the",
        "a",
        "an",
        # Swedish
        "en",
        "ett",
        "den",
        "det",
        "de",
        # German
        "der",
        "die",
        "das",
        "ein",
        "eine",
        # French
        "le",
        "la",
        "les",
        "un",
        "une",
        "des",
        "du",
        "l",
        # Italian
        "il",
        "lo",
        "gli",
        # Spanish
        "el",
        "los",
        "las",
        "una",
        "uno",
    }
)


#: Native Finnish / Swedish diacritics. Preserved by :func:`fold_diacritics`
#: because their presence carries lexemic meaning (``Häme`` vs ``hame``).
#: KANTO and other Finto authorities use proper orthography for these
#: characters, so cataloguer-supplied input matches authority labels
#: only when åäö are kept.
_NATIVE_DIACRITICS: Final[frozenset[str]] = frozenset("åäöÅÄÖ")


#: Trailing parenthetical-date pattern stripped by
#: :func:`strip_label_decoration`. Examples matched:
#: ``"Tolkien, J. R. R. (1892-1973)"`` → ``"Tolkien, J. R. R."``;
#: ``"Hamilton, Guy. (1923-)"`` → ``"Hamilton, Guy."``;
#: ``"Bond, James (fiktiivinen hahmo)"`` → unchanged (no digits).
_TRAILING_PARENTHETICAL_DATE: Final[re.Pattern[str]] = re.compile(
    r"\s*\(\d{4}\s*(?:[-–]\s*\d{0,4}\s*)?\)\s*$"  # noqa: RUF001 — en-dash is intentional (KANTO date form)
)

#: MARC subfield ``$e`` role-marker whitelist (Finnish / Swedish) stripped
#: by :func:`strip_label_decoration` when they trail an agent literal
#: separated by a comma. Kept conservative: the entry only fires when
#: the last comma-separated segment is *exactly* one of these tokens
#: after casefolding + whitespace strip. Expand cautiously — the test
#: corpus needs to confirm a token isn't a legitimate part of a name.
_ROLE_MARKER_TOKENS: Final[frozenset[str]] = frozenset(
    {
        # Finnish
        "ohjaaja",
        "näyttelijä",
        "näyttelijät",
        "kuvaaja",
        "käsikirjoittaja",
        "säveltäjä",
        "sovittaja",
        "kääntäjä",
        "toimittaja",
        "kirjoittaja",
        "tuottaja",
        "esittäjä",
        # Swedish
        "regissör",
        "skådespelare",
        "fotograf",
        "manusförfattare",
        "kompositör",
        "översättare",
        "redaktör",
        "författare",
        "producent",
        "dirigent",
    }
)


def strip_label_decoration(s: str) -> str:
    """Strip trailing parenthetical dates + MARC ``$e`` role markers.

    Designed for tier-0 lookups in
    :mod:`bffi_pipeline.stages.local_concept_resolver` — the cataloguer-
    side literal often carries decoration the authority's prefLabel
    doesn't, blocking exact matches. Applied symmetrically to both
    cataloguer literals and authority labels at fold time so the
    folded forms align.

    Rules (applied in order, both idempotent):

    1. **Trailing parenthetical date**: ``"Foo (1892-1973)"`` →
       ``"Foo"``. Pattern :data:`_TRAILING_PARENTHETICAL_DATE`.
    2. **Trailing role marker**: if the final comma-separated segment
       is in :data:`_ROLE_MARKER_TOKENS` after casefolding, strip the
       trailing ``, <token>`` (and any whitespace around it). Example:
       ``"Hamilton, Guy, ohjaaja"`` → ``"Hamilton, Guy"``. The
       ``(fiktiivinen hahmo)`` / ``(fiktiv gestalt)`` qualifiers are
       *not* in the whitelist — those are deliberately preserved so
       the upstream fictional-character marker path keeps routing
       them correctly.
    """
    out = _TRAILING_PARENTHETICAL_DATE.sub("", s).rstrip()
    if "," in out:
        head, _, tail = out.rpartition(",")
        if tail.strip().casefold() in _ROLE_MARKER_TOKENS:
            out = head.rstrip()
    return out


def fold_label(s: str) -> str:
    """Canonical label fold for tier-0 normalised lookup.

    Composes the four steps that align cataloguer-supplied literals
    with authority labels at the lexical level:

    1. :func:`strip_label_decoration` — drop trailing parenthetical
       dates and MARC role markers.
    2. NFKC Unicode normalisation — collapse compatibility variants
       like fullwidth digits.
    3. :func:`fold_diacritics` — fold foreign Latin diacritics,
       preserve Finnish / Swedish ``åäö``.
    4. ``str.casefold`` — lower-case for case-insensitive match.
    5. Whitespace collapse — ``" ".join(s.split())``.

    The same function is used by ``load-finto`` to materialise
    ``bffi:foldedLabel`` triples on every concept's ``skos:prefLabel``
    and ``skos:altLabel``, and by ``FusekiConceptResolver`` to fold
    the cataloguer literal before querying. Both sides go through
    *exactly* the same composition so the folded forms align.
    """
    stripped = strip_label_decoration(s)
    nfkc = unicodedata.normalize("NFKC", stripped)
    folded = fold_diacritics(nfkc).casefold()
    return " ".join(folded.split())


def fold_diacritics(s: str) -> str:
    """Selectively fold Latin diacritics; preserve Finnish / Swedish åäö.

    NFC-normalises the input, then walks character-by-character: native
    åäö (case-insensitive) pass through untouched; every other character
    is NFKD-decomposed and its combining marks dropped. Examples:

    - ``Häme`` → ``Häme`` (``ä`` protected — distinct from ``hame``)
    - ``Hämeenlinna`` → ``Hämeenlinna`` (both ``ä`` protected)
    - ``Ångström`` → ``Ångström`` (``Å`` and ``ö`` protected)
    - ``Tolstoï`` → ``Tolstoi`` (``ï`` not native; combining diaeresis dropped)
    - ``Müller`` → ``Muller`` (``ü`` not native; folded)
    - ``LINDGRÉN`` → ``LINDGREN`` (``É`` not native; folded)

    Non-decomposable Latin letters (``ø``, ``þ``, ``Ł``, ``ß``) are
    not affected by this step; ``ß`` becomes ``ss`` only when the
    caller subsequently casefolds.
    """
    nfc = unicodedata.normalize("NFC", s)
    out: list[str] = []
    for ch in nfc:
        if ch in _NATIVE_DIACRITICS:
            out.append(ch)
            continue
        decomposed = unicodedata.normalize("NFKD", ch)
        out.append("".join(c for c in decomposed if not unicodedata.combining(c)))
    return "".join(out)


def _normalize_token(s: str) -> str:
    """Selectively-fold diacritics, casefold, drop everything that isn't alphanumeric."""
    folded = fold_diacritics(s).casefold()
    return "".join(ch for ch in folded if ch.isalnum())


def _surname(creator: str | None) -> str:
    """Extract the surname from a personal-name string and normalise it."""
    if not creator or not creator.strip():
        return _PLACEHOLDER_CREATOR
    head = creator.split(",", 1)[0].strip()
    if not head:
        return _PLACEHOLDER_CREATOR
    # If the head still has whitespace (e.g. corporate body), take the
    # first token; preserves matching across abbreviated/full institution
    # forms only weakly, but blocking is conservative on purpose.
    first = head.split()[0]
    norm = _normalize_token(first)
    return norm or _PLACEHOLDER_CREATOR


def _significant_title_token(title: str | None) -> str:
    """First non-stop-word token of ``title``, normalised."""
    if not title or not title.strip():
        return _PLACEHOLDER_TITLE
    for raw in title.split():
        norm = _normalize_token(raw)
        if not norm or norm in _TITLE_STOP_WORDS:
            continue
        return norm
    return _PLACEHOLDER_TITLE


def _content_code(content_type: str | None) -> str:
    """Last URL segment / passthrough for a content-type identifier."""
    if not content_type or not content_type.strip():
        return _PLACEHOLDER_CONTENT
    code = content_type.strip().rsplit("/", 1)[-1]
    return _normalize_token(code) or _PLACEHOLDER_CONTENT


def compute_blocking_key(work: dict[str, str | None]) -> str:
    """Deterministic blocking key for a Work.

    ``work`` is a small dict with keys:

    - ``creator`` — agent label as it appears in MARC 100 (``"Surname,
      Given,"``). Translators / illustrators are *not* used; only the
      primary contribution.
    - ``title`` — original-language title or 245 main title.
    - ``content_type`` — short code (``"txt"``, ``"ntm"``, …) or full
      LoC content-type URI.
    """
    surname = _surname(work.get("creator"))
    title_word = _significant_title_token(work.get("title"))
    content = _content_code(work.get("content_type"))
    return _KEY_SEPARATOR.join((surname, title_word, content))


__all__ = [
    "compute_blocking_key",
    "fold_diacritics",
    "fold_label",
    "strip_label_decoration",
]
