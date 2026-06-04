"""M3 prefLabel language-tag detection.

Walks the source ``bf:Work``'s ``bf:language`` declarations to build a
BCP-47 candidate set, then re-tags untagged ``skos:prefLabel`` literals
via the (Lingua + optional local-LLM) detector cascade.

P-38 Phase B: extracted from m3/runner.py to keep the runner focused
on the conversion orchestration. No logic change — moves only.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any, Final, cast

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from bffi_pipeline.provenance import vocab as V

_LANG_URI_PREFIX: Final[str] = "http://id.loc.gov/vocabulary/languages/"
# 3-letter MARC language code -> BCP-47 2-letter. The first three —
# fi/sv/en — are the *primary* display languages per CLAUDE.md and the
# ones the Lingua + LLM title-language detector is calibrated against.
# The rest are *declared-only* languages: when MARC 041 says the record
# is in (say) German, we trust the cataloguer's declaration and tag the
# prefLabel ``@de`` — but we don't try to disambiguate German from any
# other Latin-script language via detection. The single-declared-
# language fast path in ``_retag_pref_labels`` handles the typical
# case (one MARC 041 code, no parallel-title separator).
_LANG_3_TO_2: Final[dict[str, str]] = {
    # Primary display languages — Lingua/LLM-detectable.
    "fin": "fi",
    "swe": "sv",
    "eng": "en",
    "rus": "ru",
    # Other European languages common in the Helmet collection.
    "ger": "de",
    "fre": "fr",
    "spa": "es",
    "ita": "it",
    "por": "pt",
    "dan": "da",
    "nor": "no",
    "ice": "is",
    "est": "et",
    "pol": "pl",
    "gre": "el",
    "hun": "hu",
    "cze": "cs",
    "ukr": "uk",
    "lat": "la",
    # Major immigrant-collection languages.
    "ara": "ar",
    "per": "fa",
    "tur": "tr",
    "chi": "zh",
    "jpn": "ja",
    "kor": "ko",
    "vie": "vi",
    "tha": "th",
    "hin": "hi",
    "urd": "ur",
    "som": "so",
    "swa": "sw",
    "kur": "ku",
}
# Subset that the Lingua/LLM detector knows how to disambiguate — these
# are the codes ``tag_title`` will actually try to identify on
# whitespace-segmented parallel titles. Codes in ``_LANG_3_TO_2`` but
# not here only get applied via the single-declared-language fast path.
_DETECTABLE_LANGS: Final[frozenset[str]] = frozenset({"fi", "sv", "en", "ru"})

# RDA-style parallel-title separators. Matches ``title_lang._RDA_SEPARATORS``
# but kept local to avoid the import cycle the deferred title_lang import
# in ``_retag_pref_labels`` exists to dodge.
_RDA_PARALLEL_SEPARATORS: Final[tuple[str, ...]] = (" = ", " / ", " -- ", " — ", " | ")

SKOS_prefLabel: Final[URIRef] = URIRef("http://www.w3.org/2004/02/skos/core#prefLabel")


def _candidate_languages(source: Graph) -> frozenset[str]:
    """Return BCP-47 candidate codes from the main ``bf:Work``'s ``bf:language``.

    Only walks URIRef-typed ``bf:Work`` subjects that aren't referenced
    via ``bf:associatedResource`` — i.e. only the main Work counts.
    marc2bibframe2 emits a separate ``Note otx`` sub-node carrying
    ``bf:language`` for the *translated-from* language (MARC 041 $h);
    aggregate records emit ``bf:language`` on contained Works too.
    Both pollute downstream language detection if not filtered.
    """
    contained: set[URIRef] = {
        o
        for _, _, o in source.triples((None, V.BF.associatedResource, None))
        if isinstance(o, URIRef)
    }
    codes: set[str] = set()
    for work in source.subjects(RDF.type, V.BF.Work):
        if not isinstance(work, URIRef) or work in contained:
            continue
        for lang in source.objects(work, V.BF.language):
            if isinstance(lang, URIRef) and str(lang).startswith(_LANG_URI_PREFIX):
                code3 = str(lang)[len(_LANG_URI_PREFIX) :]
                if code3 in _LANG_3_TO_2:
                    codes.add(_LANG_3_TO_2[code3])
    return frozenset(codes)


def _retag_pref_labels(
    graph: Graph,
    candidates: frozenset[str],
    *,
    llm_detector: object | None = None,
    audit_log_path: Path | None = None,
) -> None:
    """Replace untagged ``skos:prefLabel`` literals with split + per-language ones.

    For each untagged ``skos:prefLabel`` literal, runs
    :func:`bffi_pipeline.title_lang.tag_title` against the cataloguer's
    declared language candidates. Emits one labeled prefLabel per
    confidently-detected segment (or one fallback label on the whole
    string when splitting / detection didn't help).

    When ``llm_detector`` is supplied, the local-LLM cascade fires for
    ambiguous titles where every Lingua segment came back the same
    language despite the cataloguer declaring multiple — typically
    Latin-script parallel titles ("Tšarka : the Russian charka =
    venäläinen tšarkka = russkaja tšarka"). The detector's
    per-segment assignment overrides Lingua's verdict.
    """
    from bffi_pipeline.title_lang_llm import TitleLangDetector

    # The Protocol isn't runtime-checkable; trust the caller to pass the
    # right shape (or None). The annotation casts for mypy's benefit.
    typed_detector = cast("TitleLangDetector | None", llm_detector)

    # Single-declared-language fast path: when the cataloguer declared
    # exactly one language in MARC 041 (e.g. ``ger``) and the literal
    # has no RDA parallel-title separator, tag the whole literal with
    # that BCP-47 code — no detection needed, the cataloguer's
    # declaration is authoritative for mono-language titles. This is
    # what gives us ``@de``, ``@fr``, ``@ar`` etc. tags on records
    # whose declared language is outside the Lingua-detectable set.
    declared_only: str | None = None
    if len(candidates) == 1:
        declared_only = next(iter(candidates))

    # Lazy imports for the audit-log helper + bib-id resolver — keep
    # the M3 hot path free of unused module imports when the
    # llm_detector is None (heuristic-only runs).
    audit_helpers: object | None = None
    if audit_log_path is not None and typed_detector is not None:
        from bffi_pipeline.stages.m3 import title_lang_audit as _tla
        from bffi_pipeline.stages.m3.contributions import (
            _read_helmet_bib_id,
        )

        audit_helpers = (_tla, _read_helmet_bib_id)

    # Group untagged-prefLabel triples by literal text. BIBFRAME emits
    # the same title on multiple subjects per record (the bffi:Work and
    # at least one bffi:Expression — both carry the same prefLabel
    # literal). Detecting language per unique text saves the redundant
    # LLM calls and produces one audit row per unique title instead of
    # one per (subject, title) pair.
    by_text: dict[str, list[tuple[URIRef, Literal]]] = {}
    for s, _, o in graph.triples((None, SKOS_prefLabel, None)):
        if not isinstance(o, Literal) or o.language or not isinstance(s, URIRef):
            continue
        by_text.setdefault(str(o), []).append((s, o))

    to_remove: list[tuple[URIRef, URIRef, Literal]] = []
    to_add: list[tuple[URIRef, URIRef, Literal]] = []
    for text, occurrences in by_text.items():
        _retag_one_text(
            text=text,
            occurrences=occurrences,
            candidates=candidates,
            declared_only=declared_only,
            typed_detector=typed_detector,
            audit_helpers=audit_helpers,
            audit_log_path=audit_log_path,
            graph=graph,
            to_remove=to_remove,
            to_add=to_add,
        )
    for triple in to_remove:
        graph.remove(triple)
    for triple in to_add:
        graph.add(triple)


def _retag_one_text(
    *,
    text: str,
    occurrences: list[tuple[URIRef, Literal]],
    candidates: frozenset[str],
    declared_only: str | None,
    typed_detector: Any,  # TitleLangDetector | None (Protocol — lazy-import in caller)
    audit_helpers: Any,  # (title_lang_audit_module, _read_helmet_bib_id_fn) | None
    audit_log_path: Path | None,
    graph: Graph,
    to_remove: list[tuple[URIRef, URIRef, Literal]],
    to_add: list[tuple[URIRef, URIRef, Literal]],
) -> None:
    """Retag every (subject, original_literal) occurrence of one
    untagged prefLabel literal. The LLM fires at most once per unique
    literal text, regardless of how many subjects carry it in the
    record's BIBFRAME graph (typically two: bffi:Work + bffi:Expression).
    """
    from bffi_pipeline.title_lang import tag_title

    if declared_only and not any(sep in text for sep in _RDA_PARALLEL_SEPARATORS):
        for s, o in occurrences:
            to_remove.append((s, SKOS_prefLabel, o))
            to_add.append((s, SKOS_prefLabel, Literal(text, lang=declared_only)))
        return

    # Detector-driven path: only fi/sv/en/ru get disambiguated. When
    # ``candidates`` contains codes outside that set (e.g. de),
    # ``tag_title`` intersects internally and returns nothing → this
    # label stays untagged. That's intentional: we don't claim
    # per-segment language for a parallel German/French title we
    # can't actually disambiguate.
    detectable = candidates & _DETECTABLE_LANGS
    # Clear any stale telemetry from a prior text so we can tell
    # whether the LLM fired this iteration.
    if typed_detector is not None:
        with contextlib.suppress(AttributeError):
            typed_detector._last_call = None
    tagged = tag_title(text, detectable, llm_detector=typed_detector)
    if audit_helpers is not None and typed_detector is not None and audit_log_path is not None:
        telemetry = getattr(typed_detector, "_last_call", None)
        if telemetry is not None:
            _tla, _read_helmet_bib_id = audit_helpers
            # Use the first occurrence's subject for the audit row's
            # work_uri — typically the bffi:Work URI since BIBFRAME
            # emits Work before Expression. The cataloguer reviews
            # per title, not per URI; MARC sidecar lookup uses
            # helmet_bib_id (record-level).
            first_subject = occurrences[0][0]
            bib_id = _read_helmet_bib_id(graph, first_subject)
            _tla.append_audit_row(
                audit_log_path,
                helmet_bib_id=bib_id,
                work_uri=str(first_subject),
                telemetry=telemetry,
            )
    if not tagged:
        return
    for s, o in occurrences:
        to_remove.append((s, SKOS_prefLabel, o))
        for seg in tagged:
            literal = Literal(seg.text, lang=seg.lang) if seg.lang else Literal(seg.text)
            to_add.append((s, SKOS_prefLabel, literal))
