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

All tokens are normalised by NFKD-decomposing, dropping combining marks
(accent fold), case-folding, and stripping non-alphanumerics. Diacritics
are folded here on purpose — at blocking time we want
``Tolstoï`` / ``Tolstoy`` / ``Толстой`` to land in the same bucket so
M5 / M6 can examine them. Diacritics remain *preserved* in canonical
URI minting (``uris.py``); the two stages serve different goals.
"""

from __future__ import annotations

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


def _accent_fold(s: str) -> str:
    """NFKD decompose; drop combining marks. ``Tolstoï`` -> ``Tolstoi``."""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def _normalize_token(s: str) -> str:
    """Accent-fold, casefold, drop everything that isn't alphanumeric."""
    folded = _accent_fold(s).casefold()
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


__all__ = ["compute_blocking_key"]
