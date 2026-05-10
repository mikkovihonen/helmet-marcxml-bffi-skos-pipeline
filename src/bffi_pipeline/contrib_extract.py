"""Heuristic + cascade orchestration for MARC 245$c contributor extraction.

The heuristic reads 245$c and the existing 100/700 agent labels from a
record's BIBFRAME graph, tokenises both with a multilingual stop-word
filter, and decides whether 245$c contains capitalised name-tokens not
already covered. When it does, the optional LLM extractor is called to
identify new agents and assign MARC relator codes (or to flag
transliteration variants of existing agents).

Heuristic-only mode (no extractor passed) returns the uncovered-token
set so callers can log fire-rate metrics on a corpus pass without
running the LLM. Extractor-driven mode returns the full
:class:`ContribExtractDecision` so callers can emit
``bffi:Contribution`` blocks on the relevant Expression.

Tokenisation and the stop-word list were tuned against a 5,000-record
random sample from the 800k Helmet corpus; see
``docs/BUILD_PLAN.md`` § M3 for the measurement rationale.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from rdflib import Graph, URIRef

from bffi_pipeline.contrib_extract_llm import (
    ContribExtractDecision,
    ContribExtractor,
)

PUNCT_RE: Final[re.Pattern[str]] = re.compile(r"[\.,;:\[\]/=()\"'!?]+")
NAME_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"\b[A-ZÅÄÖÉÜÑ][\wÅÄÖéüáàíóúñ]{2,}\b")

#: Tokens that capitalise for grammatical or structural reasons
#: (sentence start, role marker, language name, common publication
#: noun) but never refer to an agent. Casefolded; covers EN / FI / SV /
#: DE plus the long tail of role markers and language names that
#: surfaced in the v2 5k-record measurement.
STOP_TOKENS: Final[frozenset[str]] = frozenset(
    {
        # Articles + determiners across languages
        "the",
        "a",
        "an",
        "los",
        "las",
        "el",
        "la",
        "lo",
        "le",
        "les",
        "un",
        "une",
        "des",
        "du",
        "der",
        "die",
        "das",
        "den",
        "dem",
        "ein",
        "eine",
        "det",
        "en",
        "et",
        "il",
        "gli",
        # Honorifics / titles
        "sir",
        "lord",
        "lady",
        "dame",
        "dr",
        # English role / publication markers
        "edited",
        "edit",
        "compiled",
        "compile",
        "collected",
        "written",
        "directed",
        "translated",
        "music",
        "photographs",
        "photography",
        "translation",
        "transcription",
        "introduction",
        "preface",
        "foreword",
        "afterword",
        "epilogue",
        "illustration",
        "illustrations",
        "edition",
        "volume",
        "vol",
        "book",
        "story",
        "novel",
        "poems",
        "poem",
        "essays",
        "essay",
        "prologue",
        "epigraph",
        "drawing",
        "drawings",
        "lyrics",
        "score",
        "libretto",
        "screenplay",
        "based",
        "produced",
        "developed",
        "transl",
        "abridged",
        "selected",
        "revised",
        "expanded",
        "annotated",
        # Finnish role markers + common publication nouns
        "toimittanut",
        "toimittaneet",
        "kääntänyt",
        "kääntäneet",
        "suomentanut",
        "suomentaneet",
        "kuvittanut",
        "kuvittaneet",
        "ohjannut",
        "ohjanneet",
        "säveltänyt",
        "sävelsi",
        "sanoittanut",
        "esipuhe",
        "johdanto",
        "alkusanat",
        "kustantaja",
        "valokuvat",
        "valokuva",
        "käännös",
        "käännökset",
        "kuvitus",
        "kuvituksen",
        "tekstit",
        "teksti",
        "sanoitukset",
        "musiikki",
        "sovittanut",
        "toimitus",
        "perevod",
        "toimittajat",
        "valikoinut",
        "suomennos",
        "tarinat",
        "tarina",
        "kertomus",
        "kertomukset",
        # Swedish role markers
        "översatt",
        "översättning",
        "redigerad",
        "redigerade",
        "illustrerad",
        "illustrerade",
        "regisserad",
        "fotografi",
        "fotografier",
        "förord",
        "introduktion",
        "efterord",
        "musiken",
        "anpassad",
        "berättelser",
        # German role markers + common forms
        "übers",
        "übersetzt",
        "übersetzung",
        "herausgegeben",
        "redigiert",
        "illustrationen",
        "schwedischen",
        "deutschen",
        "englischen",
        # Language names (en / fi / sv)
        "english",
        "finnish",
        "swedish",
        "german",
        "french",
        "russian",
        "spanish",
        "italian",
        "latin",
        "greek",
        "arabic",
        "chinese",
        "japanese",
        "korean",
        "danish",
        "norwegian",
        "dutch",
        "portuguese",
        "polish",
        "hungarian",
        "czech",
        "romanian",
        "turkish",
        "hebrew",
        "kannada",
        "swahili",
        "urdu",
        "hindi",
        "estonian",
        "icelandic",
        "ukrainian",
        "bulgarian",
        "englanti",
        "englannin",
        "suomi",
        "suomen",
        "ruotsi",
        "ruotsin",
        "saksa",
        "saksan",
        "ranska",
        "ranskan",
        "venäjä",
        "venäjän",
        "kreikka",
        "kreikan",
        "latina",
        "latinan",
        "italia",
        "espanja",
        "espanjan",
        "viro",
        "viron",
        "norja",
        "norjan",
        "tanska",
        "tanskan",
        "engelska",
        "finska",
        "tyska",
        "franska",
        "ryska",
        "spanska",
        "italienska",
        "danska",
        "norska",
        "estniska",
        # Common medium / format
        "compact",
        "disc",
        "album",
        "anthology",
        "box",
        "set",
        "audio",
        "version",
        "remastered",
    }
)


# --- Tokenisation --------------------------------------------------------


def _tokens(text: str) -> set[str]:
    """Return casefolded capitalised name-token candidates from ``text``,
    minus the multilingual stop-word list."""
    cleaned = PUNCT_RE.sub(" ", text)
    return {m.group().casefold() for m in NAME_TOKEN_RE.finditer(cleaned)} - STOP_TOKENS


def compute_uncovered_tokens(c_subfield: str, existing_agent_labels: tuple[str, ...]) -> set[str]:
    """Return name-tokens in ``c_subfield`` that don't appear in any
    of ``existing_agent_labels``. Empty set means the heuristic
    determines 245$c is fully covered by 100/700 — no LLM needed."""
    c_tokens = _tokens(c_subfield)
    if not c_tokens:
        return set()
    structured: set[str] = set()
    for label in existing_agent_labels:
        structured |= _tokens(label)
    return c_tokens - structured


# --- BIBFRAME readers ----------------------------------------------------


# Only-use-locally: importing rdflib namespaces here keeps the module
# free of cross-stage dependencies. The full BIBFRAME namespace lives
# in ``provenance.vocab`` but pulling it in transitively imports more
# than this module needs.
_BF_RESPONSIBILITY_STATEMENT = URIRef(
    "http://id.loc.gov/ontologies/bibframe/responsibilityStatement"
)
_BF_HAS_INSTANCE = URIRef("http://id.loc.gov/ontologies/bibframe/hasInstance")
_BF_CONTRIBUTION = URIRef("http://id.loc.gov/ontologies/bibframe/contribution")
_BF_AGENT = URIRef("http://id.loc.gov/ontologies/bibframe/agent")
_RDFS_LABEL = URIRef("http://www.w3.org/2000/01/rdf-schema#label")


def read_responsibility_statement(source: Graph, work: URIRef) -> str | None:
    """Return the 245$c text for ``work`` from the source BIBFRAME graph.

    marc2bibframe2 attaches ``bf:responsibilityStatement`` to the
    ``bf:Instance`` linked from a Work via ``bf:hasInstance``. We walk
    the link rather than searching globally so aggregate records (which
    contain multiple Works with their own contained Instances via
    ``bf:associatedResource``) don't cross-contaminate.
    """
    for instance in source.objects(work, _BF_HAS_INSTANCE):
        for stmt in source.objects(instance, _BF_RESPONSIBILITY_STATEMENT):
            text = str(stmt).strip()
            if text:
                return text
    return None


def read_existing_agent_labels(source: Graph, work: URIRef) -> tuple[str, ...]:
    """Return rdfs:labels of agents on ``work``'s ``bf:contribution`` chain.

    Walks ``work bf:contribution → bf:agent → rdfs:label``. The labels
    are the source-of-truth for "what 100/700 already structurally
    captures"; the heuristic compares 245$c tokens against these.
    Returns a sorted tuple so re-runs of the heuristic over the same
    graph produce identical inputs to the LLM.
    """
    seen: set[str] = set()
    for contrib in source.objects(work, _BF_CONTRIBUTION):
        for agent in source.objects(contrib, _BF_AGENT):
            for label in source.objects(agent, _RDFS_LABEL):
                text = str(label).strip()
                if text:
                    seen.add(text)
    return tuple(sorted(seen))


# --- Orchestration -------------------------------------------------------


@dataclass(frozen=True)
class ExtractionInputs:
    """The triple the heuristic + cascade need per record."""

    work_uri: URIRef
    c_subfield: str
    existing_agent_labels: tuple[str, ...]


def gather_inputs(source: Graph, work: URIRef) -> ExtractionInputs | None:
    """Read 245$c + existing-agent labels for ``work``. ``None`` when the
    record has no 245$c (e.g. controlled musical works without a
    statement of responsibility) — the heuristic skips those entirely."""
    c_text = read_responsibility_statement(source, work)
    if c_text is None:
        return None
    return ExtractionInputs(
        work_uri=work,
        c_subfield=c_text,
        existing_agent_labels=read_existing_agent_labels(source, work),
    )


def heuristic_fires(inputs: ExtractionInputs) -> bool:
    """Return True iff the LLM cascade should be called for these inputs."""
    return bool(compute_uncovered_tokens(inputs.c_subfield, inputs.existing_agent_labels))


def extract_contributions(
    inputs: ExtractionInputs,
    *,
    extractor: ContribExtractor | None = None,
) -> ContribExtractDecision | None:
    """Run heuristic; if it fires and ``extractor`` is supplied, call the LLM.

    Returns:
        - ``None`` when the heuristic decides no extraction is needed
          (245$c fully covered) OR when the heuristic fires but no
          extractor was supplied (caller wanted measurement only).
        - The :class:`ContribExtractDecision` from the extractor when
          one is supplied and the heuristic fires.
    """
    if not heuristic_fires(inputs):
        return None
    if extractor is None:
        return None
    return extractor.extract(
        c_subfield=inputs.c_subfield,
        existing_agents=inputs.existing_agent_labels,
    )


__all__ = [
    "NAME_TOKEN_RE",
    "PUNCT_RE",
    "STOP_TOKENS",
    "ExtractionInputs",
    "compute_uncovered_tokens",
    "extract_contributions",
    "gather_inputs",
    "heuristic_fires",
    "read_existing_agent_labels",
    "read_responsibility_statement",
]
