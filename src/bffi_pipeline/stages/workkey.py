"""Stage M4: Stage 1 deterministic blocking.

Computes a cheap rule-based key per BFFI Work so candidate-pair generation
in M5/M6 only considers Works in the same block — eliminating >99% of
comparisons before any embedding or LLM runs (spec § 6 Stage 1).

The key is the concatenation of three normalised tokens:

* normalised creator **surname** (everything before the first comma, or
  the first whitespace-delimited token);
* the first **significant** title token (skipping a small multilingual
  stop-word list — articles in fi / sv / en / de / fr / it);
* a short **content type** code (e.g. ``txt``, ``ntm``).

All tokens are normalised by NFKD-decomposing, dropping combining marks
(accent fold), case-folding, and stripping non-alphanumerics. Diacritics
are folded here on purpose — at blocking time we want
``Tolstoï``/``Tolstoy``/``Толстой`` to land in the same bucket so the
M5/M6 stages can examine them. Diacritics remain *preserved* in canonical
URI minting (``uris.py``); the two stages serve different goals.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS

from bffi_pipeline.config import get_settings
from bffi_pipeline.provenance import vocab as V

# --- Token normalisation --------------------------------------------------

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

_LANG_URI_PREFIX: Final[str] = "http://id.loc.gov/vocabulary/contentTypes/"


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


# --- Graph extraction -----------------------------------------------------


@dataclass(frozen=True)
class WorkBlockingInput:
    """Per-Work tuple: BFFI Work URI + its blocking-key inputs."""

    work_uri: str
    creator: str | None
    title: str | None
    content_type: str | None


def _first_pref_label(graph: Graph, subject: URIRef) -> str | None:
    for o in graph.objects(subject, V.SKOS.prefLabel):
        if isinstance(o, Literal):
            return str(o)
    return None


def _primary_agent_uris(graph: Graph, work: URIRef) -> list[URIRef]:
    """Return the URIs of agents linked from ``work``'s primary contribution(s)."""
    agents: list[URIRef] = []
    for contrib in graph.objects(work, V.BFFI.contribution):
        types = set(graph.objects(contrib, RDF.type))
        if V.BFFI.PrimaryContribution not in types:
            continue
        for agent in graph.objects(contrib, V.BFFI.agent):
            if isinstance(agent, URIRef):
                agents.append(agent)
    return agents


def _content_type_for(graph: Graph, work: URIRef) -> str | None:
    """Return any one ``bffi:content`` URI fragment for ``work``'s expressions."""
    for expr in graph.objects(work, V.BFFI.hasExpression):
        if not isinstance(expr, URIRef):
            continue
        for ct in graph.objects(expr, V.BFFI.content):
            if isinstance(ct, URIRef):
                value = str(ct)
                if value.startswith(_LANG_URI_PREFIX):
                    return value[len(_LANG_URI_PREFIX) :]
                return value.rsplit("/", 1)[-1]
    return None


def extract_blocking_inputs(graph: Graph) -> Iterator[WorkBlockingInput]:
    """Walk a combined BFFI + BIBFRAME graph and yield per-Work blocking inputs.

    The agent label comes from the BIBFRAME side (``rdfs:label`` on the
    agent URI emitted by marc2bibframe2). The title comes from the BFFI
    Work's ``skos:prefLabel``; the content type from any of its
    Expressions' ``bffi:content``.
    """
    for work in graph.subjects(RDF.type, V.BFFI.Work):
        if not isinstance(work, URIRef):
            continue
        title = _first_pref_label(graph, work)
        creator: str | None = None
        for agent in _primary_agent_uris(graph, work):
            for label in graph.objects(agent, RDFS.label):
                if isinstance(label, Literal):
                    creator = str(label)
                    break
            if creator:
                break
        yield WorkBlockingInput(
            work_uri=str(work),
            creator=creator,
            title=title,
            content_type=_content_type_for(graph, work),
        )


def _iter_corpus_files(base: Path) -> tuple[list[Path], list[Path]]:
    """Return (bffi-turtle-files, bibframe-rdf-files) for ``base``.

    ``base`` may itself be a single ``.ttl`` file (used standalone) or a
    directory. If a directory, ``base/bffi/*.ttl`` and ``base/bibframe/*.rdf``
    are scanned (matching the layout produced by M2 + M3).
    """
    if base.is_file():
        return [base], []
    bffi = sorted((base / "bffi").glob("*.ttl")) if (base / "bffi").exists() else []
    bibframe = sorted(
        p
        for p in (base / "bibframe").glob("*.rdf")
        if (base / "bibframe").exists() and not p.name.startswith("_")
    )
    return bffi, bibframe


def load_corpus(base: Path | None = None) -> Graph:
    """Load every BFFI Turtle and BIBFRAME RDF/XML under ``base`` into one Graph.

    Defaults to ``BFFI_DATA_DIR``. The combined graph is what
    :func:`extract_blocking_inputs` walks.
    """
    base = base or get_settings().data_dir
    g = Graph()
    bffi_files, bibframe_files = _iter_corpus_files(base)
    for path in bffi_files:
        g.parse(str(path), format="turtle")
    for path in bibframe_files:
        g.parse(str(path), format="xml")
    return g


# --- Block-size statistics ------------------------------------------------


@dataclass(frozen=True)
class BlockingStats:
    """Aggregate counts for a workkey-stats run."""

    total_works: int
    blocks: dict[str, list[str]]

    @property
    def block_count(self) -> int:
        return len(self.blocks)

    @property
    def size_distribution(self) -> Counter[int]:
        return Counter(len(works) for works in self.blocks.values())

    def render(self) -> str:
        lines = [
            "Stage-1 blocking-key statistics",
            f"  works:  {self.total_works}",
            f"  blocks: {self.block_count}",
        ]
        if self.total_works:
            singletons = self.size_distribution.get(1, 0)
            lines.append(f"  singleton blocks: {singletons}")
        if self.size_distribution:
            lines.append("  block-size histogram:")
            for size, count in sorted(self.size_distribution.items()):
                lines.append(f"    size={size:>4}  count={count}")
        if self.blocks:
            largest = max(self.blocks.items(), key=lambda kv: len(kv[1]))
            lines.append(f"  largest block: '{largest[0]}' ({len(largest[1])} Works)")
        return "\n".join(lines)


def compute_blocks(graph: Graph) -> BlockingStats:
    """Group Work URIs in ``graph`` by their blocking key and return stats."""
    blocks: dict[str, list[str]] = {}
    total = 0
    for entry in extract_blocking_inputs(graph):
        total += 1
        key = compute_blocking_key(
            {
                "creator": entry.creator,
                "title": entry.title,
                "content_type": entry.content_type,
            }
        )
        blocks.setdefault(key, []).append(entry.work_uri)
    return BlockingStats(total_works=total, blocks=blocks)


__all__ = [
    "BlockingStats",
    "WorkBlockingInput",
    "compute_blocking_key",
    "compute_blocks",
    "extract_blocking_inputs",
    "load_corpus",
]
