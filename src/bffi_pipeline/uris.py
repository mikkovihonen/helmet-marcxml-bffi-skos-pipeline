"""Deterministic URI minting for BFFI Works and Expressions.

All URI construction in this project goes through this module — never
concatenate URI strings elsewhere (see ``CLAUDE.md`` "Conventions"). URIs
are SHA-1 hashes of canonicalised inputs so re-runs and the merge step in
M8 are idempotent under benign surface variations (whitespace, case,
Unicode normalisation form).

Diacritics are intentionally **preserved** in the canonical form. Helmet
titles are multilingual; folding accents would collide distinct works
(e.g. Finnish ä/ö, Swedish å).
"""

from __future__ import annotations

import hashlib
import unicodedata

from bffi_pipeline.config import get_settings

_FIELD_SEP = "\x00"


def _normalize_uri(uri: str) -> str:
    return uri.strip()


def _normalize_title(title: str) -> str:
    """NFC + collapse internal whitespace + strip + casefold."""
    nfc = unicodedata.normalize("NFC", title)
    return " ".join(nfc.split()).casefold()


def _normalize_language(lang: str) -> str:
    return lang.strip().casefold()


def _sha1(*parts: str) -> str:
    payload = _FIELD_SEP.join(parts).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()


def mint_work_uri(creator_uri: str, original_title: str) -> str:
    """Mint a deterministic Work URI.

    The same canonical (creator, title) inputs always produce the same URI.
    Different creators or substantively different titles yield different
    URIs; whitespace, case, and Unicode normalisation form do not.
    """
    digest = _sha1(_normalize_uri(creator_uri), _normalize_title(original_title))
    return f"{get_settings().work_namespace}{digest}"


def mint_expression_uri(work_uri: str, language: str) -> str:
    """Mint a deterministic Expression URI from a Work URI and a language tag.

    ``language`` is a BCP-47 tag (e.g. ``fi``, ``sv``, ``en``); whitespace
    and casing are normalised away so ``"FI"`` and ``" fi "`` collapse.
    """
    digest = _sha1(_normalize_uri(work_uri), _normalize_language(language))
    return f"{get_settings().expression_namespace}{digest}"
