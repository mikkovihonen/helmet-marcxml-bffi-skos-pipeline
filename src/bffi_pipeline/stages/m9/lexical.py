"""M9 lexical-similarity scoring.

Selectively folds non-native diacritics (keeps Finnish/Swedish
``åäö`` distinguishing — see :func:`bffi_pipeline.blocking.fold_diacritics`),
casefolds, collapses whitespace, then runs
:class:`difflib.SequenceMatcher`. Returns ``0`` for disjoint strings,
``1`` for equal after normalisation.

P-38 Phase D: extracted from m9/runner.py. No logic change.
"""

from __future__ import annotations

from difflib import SequenceMatcher

from bffi_pipeline.blocking import fold_diacritics


def _normalise_for_similarity(s: str) -> str:
    """Selectively fold diacritics + casefold + collapse internal whitespace.

    Delegates the diacritic step to
    :func:`bffi_pipeline.blocking.fold_diacritics`, which preserves
    native Finnish / Swedish ``åäö`` (where the diacritic carries
    lexemic meaning — ``Häme`` vs ``hame``) and folds every other Latin
    diacritic (``ï``, ``ñ``, ``ü``, ``é``, …) so cataloguer input still
    matches KANTO's preferred label when the cataloguer dropped a
    foreign mark.
    """
    return " ".join(fold_diacritics(s).split()).casefold()


def lexical_similarity(a: str, b: str) -> float:
    """Return a 0-1 similarity score between two cataloguing strings.

    Uses :class:`difflib.SequenceMatcher` after a normalisation pass
    that selectively folds non-native diacritics, casefolds, and
    collapses whitespace. Production may swap to ``rapidfuzz`` later —
    the contract is "0=disjoint, 1=equal after normalisation".
    """
    return SequenceMatcher(None, _normalise_for_similarity(a), _normalise_for_similarity(b)).ratio()
