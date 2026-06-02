"""P-41 Phase B1 — deterministic 245$c statement-of-responsibility parser.

MARC 245$c carries the statement of responsibility — the cataloguer's
free-text declaration of who's responsible for the work. Common Finnish
+ Swedish + English shapes mined from the Helmet corpus:

- ``"by Margaret Atwood"`` — English, single author.
- ``"av August Strindberg"`` — Swedish, single author.
- ``"Tove Jansson"`` — bare name (Finnish convention).
- ``"kirjoittanut Mika Waltari"`` — Finnish, role-tagged.
- ``"toimittanut Helena Ruuska"`` — Finnish, editor-tagged.
- ``"toim. Helena Ruuska"`` — abbreviated.
- ``"Liisa Louhela ja Pekka Halonen"`` — Finnish "and".
- ``"Atwood, Margaret"`` — surname-first.
- ``"Pekka Halonen, Liisa Louhela ja Mika Waltari"`` — three authors.

The parser is conservative — it returns nothing on shapes it can't
parse cleanly, rather than guessing. The B1 LLM cascade (separate
module) picks up the long tail; B3 sentinel catches everything else.

Returned :class:`ParsedAgent` carries the *verbatim* name as it
appeared in the input (after trimming role markers and punctuation),
so downstream consumers can audit-trail back to MARC 245$c without
fuzzy matching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

#: MARC 245$c is free-text, but the corpus shows a strong pattern: a
#: leading role marker ("by", "av", "kirjoittanut", "toim.") optionally
#: precedes a comma- or "ja"/"and"-separated list of names. Each name
#: is either ``First Last`` or ``Last, First``. The regexes below split
#: on those markers + boundaries.

#: Finnish + Swedish + English + German role markers, mapped to the role
#: tag we emit on the synthesised 7XX$e. Order matters in the
#: alternation — longer phrases first so ``"kirjoittanut"`` doesn't
#: get partial-matched as ``"toim."`` and ``"herausgegeben von"``
#: gets stripped as the full phrase rather than leaving ``"von"`` as a
#: lone token that would corrupt the parsed name. German markers were
#: added 2026-06-02 after the P-41 B.8 5k bench found B1 mis-parsing
#: ``"herausgegeben von L. Richter"`` as a 4-token name (synthesised
#: value = the full string with role marker prefix). The corpus has
#: enough German-source records that the additional coverage moves
#: real volume from B3 sentinel to B1 regex.
_ROLE_MARKERS: Final[tuple[tuple[str, str], ...]] = (
    # Finnish.
    ("kirjoittanut", "author"),
    ("toimittanut", "editor"),
    ("toim.", "editor"),
    ("kääntänyt", "translator"),
    ("suomentanut", "translator"),
    ("käänt.", "translator"),
    ("kuvittanut", "illustrator"),
    ("kuv.", "illustrator"),
    # English.
    ("written by", "author"),
    ("translated by", "translator"),
    ("edited by", "editor"),
    ("illustrated by", "illustrator"),
    ("compiled by", "compiler"),
    ("trans.", "translator"),
    ("ed.", "editor"),
    ("by", "author"),
    # Swedish.
    ("översatt av", "translator"),
    ("redigerad av", "editor"),
    ("illustrerad av", "illustrator"),
    ("av", "author"),
    # German. ``"herausgegeben von"`` must be matched as the full
    # phrase to strip both the role verb and the German nobility /
    # family-name particle ``"von"`` that legitimately appears
    # mid-name in other contexts (e.g. "Johann Wolfgang von Goethe").
    ("herausgegeben von", "editor"),
    ("übersetzt von", "translator"),
    ("herausgegeben", "editor"),
    ("hrsg. von", "editor"),
    ("hrsg.", "editor"),
    # Music / multimedia role markers added 2026-06-02 after the
    # P-41 B.8 5 k bench surfaced 5 records where these prefixes
    # leaked into the synthesised agent name. ``"arranged by"`` and
    # ``"recordings by"`` are mapped to ``"unknown"`` because the
    # salvage role enum (author/editor/translator/illustrator/
    # compiler/unknown) doesn't carry an arranger or
    # recording-engineer tag and the downstream BIBFRAME conversion
    # only consumes the relator term in ``$e`` — a generic
    # ``tekijä`` is more honest than a wrong-role tag.
    ("arranged by", "unknown"),
    ("recordings by", "unknown"),
    ("ed. by", "editor"),
    ("comp. by", "compiler"),
    ("arr. by", "unknown"),
)

#: Conjunctions / separators that split a multi-author 245$c into
#: individual names. ``"ja"`` is Finnish "and"; ``"och"`` is Swedish;
#: ``"&"`` and ``"and"`` are common. Order-insensitive (we split on any).
_SEPARATORS_RE: Final[re.Pattern[str]] = re.compile(
    r"\s+(?:ja|och|and|&)\s+|\s*,\s*",
    flags=re.IGNORECASE,
)

#: Tokens that, when present, indicate the name is a corporate entity
#: (city government, ministry, foundation, etc.) — the B1 personal-name
#: tier rejects these and falls through to B2 or B3. Conservative list
#: mined from the corpus.
_CORPORATE_MARKERS: Final[tuple[str, ...]] = (
    "kaupunki",
    "kunta",
    "valtio",
    "ministeriö",
    "säätiö",
    "ry",
    "oy",
    "ab",
    "yhdistys",
    "seura",
    "förening",
    "stiftelse",
    "city",
    "ministry",
    "foundation",
    "society",
    "association",
    "company",
    "institute",
    "university",
)

#: A name token is valid if it is letters (incl. accented), optionally
#: followed by a single trailing period (for initialisations like
#: ``"J. R. R. Tolkien"``). Rejects names containing digits or runs of
#: punctuation longer than what initials produce.
_NAME_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-zÀ-ÖØ-öø-ÿŠšŽžĀ-ž]+\.?$",
)

#: Tail punctuation we strip from name candidates: trailing dots,
#: commas, semicolons, the period that ends a 245$c statement of
#: responsibility, and the close-paren that closes a role parenthetical.
_TAIL_PUNCT_RE: Final[re.Pattern[str]] = re.compile(r"[.,;:)]+\s*$")

#: Lead-in punctuation we strip from name candidates: leading
#: open-paren (role parentheticals), forward slashes (MARC ISBD
#: punctuation), whitespace.
_LEAD_PUNCT_RE: Final[re.Pattern[str]] = re.compile(r"^[(/\s]+")

#: Min / max token count for a personal name. ``"Tove Jansson"`` is
#: 2 tokens; ``"J. R. R. Tolkien"`` is 4; ``"Hans Christian Andersen"``
#: is 3. Reject anything below 2 (single bare word — probably a
#: corporate stub or a role marker the role-pass missed) or above 5
#: (probably picked up extra text by accident).
_MIN_NAME_TOKENS: Final[int] = 2
_MAX_NAME_TOKENS: Final[int] = 5


@dataclass(frozen=True)
class ParsedAgent:
    """One agent extracted from 245$c.

    ``name`` is the verbatim form as it appeared in the MARC field
    (after trimming role markers and surrounding punctuation). ``role``
    is one of the five tags from :data:`_ROLE_MARKERS`'s right column
    plus ``"unknown"`` when no role marker was detected. ``confidence``
    reflects how confident the regex is: 0.8 for explicit role-marked
    parses, 0.6 for unmarked single-name parses, 0.5 for multi-author
    splits."""

    name: str
    role: Literal["author", "editor", "translator", "illustrator", "compiler", "unknown"]
    confidence: float


def _looks_corporate(candidate: str) -> bool:
    """Return True if the candidate token list contains any corporate
    marker. Case-insensitive substring match — these markers are
    distinctive enough that false positives are rare."""
    lowered = candidate.lower()
    return any(marker in lowered for marker in _CORPORATE_MARKERS)


#: Max recursion depth for role-marker stripping. The 2026-06-02 5 k
#: bench surfaced records with nested role markers like
#: ``"ED. BY BYRON MIKELLIDES"`` where the parser only stripped the
#: first marker (``"ed."``) and left ``"BY BYRON MIKELLIDES"`` as the
#: synthesised agent name. Recursive stripping handles arbitrary
#: nesting (``"ed. and trans. by"`` etc.) with a sane depth cap so a
#: pathological input can't infinite-loop. ``2`` covers every nesting
#: depth observed in the corpus to date; raise if production surfaces
#: deeper chains.
_MAX_ROLE_PREFIX_PASSES: Final[int] = 2


def _split_role_prefix(text: str) -> tuple[str, str]:
    """Detach a leading role marker from the body. Returns
    ``(role_tag, remainder)``. The role marker check is case-
    insensitive; a missing marker yields ``("unknown", text)``.

    Iterative: re-checks for role markers after each successful strip
    so nested markers like ``"ED. BY X"`` (first ``"ed."``, then
    ``"by"``) are fully consumed. The role tag returned is the
    LAST marker stripped (the outermost is typically the more
    specific role — e.g. ``"ed."`` says editor, the inner ``"by"``
    just generalises to author — and the outer wins on first match
    but if there are nested markers the inner role might be the
    actual one MARC intends). For the common ``"ed. by"`` case the
    first iteration matches ``"ed."`` → editor; the second iteration
    matches ``"by"`` → author but we keep the more-specific
    ``"editor"`` tag from the first match.
    """
    role: Literal["author", "editor", "translator", "illustrator", "compiler", "unknown"] = (
        "unknown"
    )
    remainder = text
    for _ in range(_MAX_ROLE_PREFIX_PASSES):
        lowered = remainder.lower()
        matched = False
        for marker, tag in _ROLE_MARKERS:
            if lowered.startswith(marker + " ") or lowered.startswith(marker + "\t"):
                remainder = remainder[len(marker) :].lstrip()
                # First-match-wins on the role tag: the outer marker
                # (usually more specific) is kept across recursion.
                if role == "unknown":
                    role = tag  # type: ignore[assignment]
                matched = True
                break
        if not matched:
            break
    return (role, remainder)


def _clean_candidate(candidate: str) -> str:
    """Strip leading/trailing punctuation from a split chunk so the
    name comparison below sees a bare name. Keeps internal hyphens,
    apostrophes, and periods (for initials)."""
    candidate = _LEAD_PUNCT_RE.sub("", candidate)
    candidate = _TAIL_PUNCT_RE.sub("", candidate)
    return candidate.strip()


def _validate_name_shape(candidate: str) -> bool:
    """Return True if ``candidate`` looks like ``First Last`` or
    ``Last, First`` — letters-only tokens, 2 to 5 of them, no digits."""
    if not candidate:
        return False
    if _looks_corporate(candidate):
        return False
    tokens = candidate.replace(",", " ").split()
    if not (_MIN_NAME_TOKENS <= len(tokens) <= _MAX_NAME_TOKENS):
        return False
    return all(_NAME_TOKEN_RE.match(token) for token in tokens)


def parse_245c(text: str) -> list[ParsedAgent]:
    """Extract verbatim personal-name agents from a MARC 245$c.

    Returns ``[]`` when the regex can't extract any clean name —
    either the field is empty, contains only role markers, contains a
    corporate marker, or doesn't match the personal-name shape. The
    caller (the salvage dispatcher) then falls through to B2/B3.

    The returned ``ParsedAgent.name`` is verbatim (matching the input
    after punctuation trim); the role tag is inferred from a leading
    role marker if present, else ``"unknown"``.
    """
    text = (text or "").strip()
    if not text:
        return []

    role, body = _split_role_prefix(text)
    body = body.strip()
    if not body:
        return []

    chunks = _SEPARATORS_RE.split(body)
    candidates = [_clean_candidate(chunk) for chunk in chunks]
    candidates = [c for c in candidates if c]

    if not candidates:
        return []

    parsed: list[ParsedAgent] = []
    for candidate in candidates:
        if not _validate_name_shape(candidate):
            # Conservative: if any candidate fails the shape check,
            # bail out of the whole parse. A 245$c like
            # "Some Author Name and 'The Helsinki Daily News'" should
            # NOT produce a half-result; the LLM cascade or B3 takes
            # over instead.
            return []
        parsed.append(
            ParsedAgent(
                name=candidate,
                role=role,  # type: ignore[arg-type]
                confidence=_confidence_for(role, len(candidates)),
            )
        )
    return parsed


def _confidence_for(role: str, agent_count: int) -> float:
    """Confidence band per the synthesis-policy table in
    ``docs/bibliographic-minimum.md``. Explicit role markers raise
    confidence; multi-author splits lower it slightly because the
    separator inference adds a small chance of mis-splitting on a
    name containing ``" ja "`` (rare but possible). Rounded to 4
    decimals to match the TSV output precision and dodge IEEE 754
    subtraction artefacts."""
    base = 0.8 if role != "unknown" else 0.6
    if agent_count > 1:
        return round(base - 0.1, 4)
    return base


__all__ = [
    "ParsedAgent",
    "parse_245c",
]
