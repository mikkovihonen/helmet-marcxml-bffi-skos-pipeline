"""Stage M3: BIBFRAME to BFFI Work + Expression.

Runs the two CONSTRUCTs in ``sparql/`` against each ``<output_dir>/bibframe/<id>.rdf``,
combines them, post-processes ``skos:prefLabel`` with language tags derived
from ``bf:language``, validates against ``config/shapes/bffi.shape.ttl``
(Boundary 3 — *non-blocking*), and writes a Turtle file per record.

Per ``docs/BUILD_PLAN.md`` M3 the SHACL failures do not halt the pipeline.
Counts and per-record validation reports go to
``<output_dir>/bffi/_validation.jsonl``; the CLI prints a summary warning.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Final, cast

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, RDF, RDFS
from rdflib.term import Node

from bffi_pipeline.config import get_settings
from bffi_pipeline.contrib_variants import (
    DEFAULT_SIDECAR_NAME,
    ContribVariantClaim,
    append_variant_claims,
    truncate_sidecar,
)
from bffi_pipeline.helmet import format_sierra_bib_id
from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.uris import (
    mint_raw_expression_uri,
    mint_raw_work_uri,
    register_sparql_functions,
)
from bffi_pipeline.validation.bffi import validate_graph

_BFFI_PIPELINE_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
_SPARQL_DIR: Final[Path] = _BFFI_PIPELINE_REPO_ROOT / "sparql"

_LANG_URI_PREFIX: Final[str] = "http://id.loc.gov/vocabulary/languages/"
# 3-letter MARC language code -> BCP-47 2-letter for the languages this
# pipeline displays (fi/sv/en); other codes leave prefLabel untagged.
_LANG_3_TO_2: Final[dict[str, str]] = {
    "fin": "fi",
    "swe": "sv",
    "eng": "en",
}

SKOS_prefLabel: Final[URIRef] = URIRef("http://www.w3.org/2004/02/skos/core#prefLabel")


# --- Public dataclasses ---------------------------------------------------


@dataclass(frozen=True)
class ValidationRow:
    """One row of ``_validation.jsonl`` per (Boundary-3-failing) record."""

    helmet_bib_id: str
    output_file: str
    conforms: bool
    report_text: str


@dataclass
class BffiSummary:
    """Aggregate counts for an end-of-run report."""

    converted: list[str] = field(default_factory=list)
    skipped_idempotent: list[str] = field(default_factory=list)
    failed_shape: list[str] = field(default_factory=list)
    errored: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        """Total number of input files seen, excluding shape-only flags."""
        return len(self.converted) + len(self.skipped_idempotent) + len(self.errored)

    def render(self) -> str:
        """Format this summary as paste-ready text for the bf-to-bffi CLI."""
        lines = [
            f"BIBFRAME to BFFI conversion summary ({self.total} input file(s))",
            f"  converted: {len(self.converted)}",
            f"  skipped (already converted): {len(self.skipped_idempotent)}",
            f"  shape-failing (kept; flagged): {len(self.failed_shape)}",
            f"  errored: {len(self.errored)}",
        ]
        if self.failed_shape:
            lines.append("Shape-failing records:")
            lines.extend(f"  - {bib}" for bib in self.failed_shape)
        if self.errored:
            lines.append("Hard errors (record skipped):")
            lines.extend(f"  - {bib}: {msg}" for bib, msg in self.errored)
        return "\n".join(lines)


# --- Caching --------------------------------------------------------------


@lru_cache(maxsize=1)
def _work_query() -> str:
    return (_SPARQL_DIR / "bf_to_bffi_work.rq").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _expression_query() -> str:
    return (_SPARQL_DIR / "bf_to_bffi_expression.rq").read_text(encoding="utf-8")


# --- CONSTRUCT runner -----------------------------------------------------


_WHITESPACE_PERCENT_ENCODE: Final[dict[str, str]] = {
    " ": "%20",
    "\t": "%09",
    "\n": "%0A",
    "\r": "%0D",
}


def _sanitize_uri(uri: str) -> str:
    """Strip leading/trailing whitespace from a URI string and percent-
    encode any remaining internal whitespace.

    Cataloguer-supplied ``$0`` values occasionally carry stray
    whitespace (trailing newlines, embedded spaces from two IDs
    accidentally concatenated). rdflib refuses to serialize those as
    N3/Turtle. Stripping is safe for leading/trailing — the URI was
    typo'd, not semantically different. Internal whitespace gets
    percent-encoded so the URI remains lexically valid and auditable
    rather than dropped silently.
    """
    stripped = uri.strip()
    if not any(ws in stripped for ws in _WHITESPACE_PERCENT_ENCODE):
        return stripped
    result = stripped
    for ws, encoded in _WHITESPACE_PERCENT_ENCODE.items():
        result = result.replace(ws, encoded)
    return result


def _sanitize_uri_whitespace(graph: Graph) -> int:
    """Rewrite URIRef terms in ``graph`` so none carry literal whitespace.

    Walks every position (subject, predicate, object) and rebuilds the
    affected triples in place. Returns the number of distinct URIs
    rewritten — callers can log this if they want to surface cataloguer
    data-quality counts.
    """
    rewrites: dict[URIRef, URIRef] = {}
    for term in set(graph.all_nodes()):
        if not isinstance(term, URIRef):
            continue
        sanitized = _sanitize_uri(str(term))
        if sanitized != str(term):
            rewrites[term] = URIRef(sanitized)
    # rdflib's predicates aren't returned by all_nodes(); walk them too.
    for _s, p, _o in graph:
        if isinstance(p, URIRef) and p not in rewrites:
            sanitized = _sanitize_uri(str(p))
            if sanitized != str(p):
                rewrites[p] = URIRef(sanitized)
    if not rewrites:
        return 0
    triples_to_replace: list[tuple[tuple[Node, Node, Node], tuple[Node, Node, Node]]] = []
    for s, p, o in graph:
        new_s = rewrites.get(s, s) if isinstance(s, URIRef) else s
        new_p = rewrites.get(p, p) if isinstance(p, URIRef) else p
        new_o = rewrites.get(o, o) if isinstance(o, URIRef) else o
        if (new_s, new_p, new_o) != (s, p, o):
            triples_to_replace.append(((s, p, o), (new_s, new_p, new_o)))
    for old, new in triples_to_replace:
        graph.remove(old)
        graph.add(new)
    return len(rewrites)


_XSD_DATETIME: Final[URIRef] = URIRef("http://www.w3.org/2001/XMLSchema#dateTime")
_XSD_DATE: Final[URIRef] = URIRef("http://www.w3.org/2001/XMLSchema#date")
_XSD_GYEAR: Final[URIRef] = URIRef("http://www.w3.org/2001/XMLSchema#gYear")
_XSD_GYEAR_MONTH: Final[URIRef] = URIRef("http://www.w3.org/2001/XMLSchema#gYearMonth")

#: XSD datatypes that rdflib coerces into Python ``datetime``/``date``
#: at load time. A bad lexical form (cataloguer-supplied
#: ``'19  -  -  T00:00:00'``, etc.) raises ValueError during
#: coercion — and crashes the downstream merge load. Strip the
#: datatype on parse failure so the literal survives as plain text.
_DATE_DATATYPES: Final[tuple[URIRef, ...]] = (
    _XSD_DATETIME,
    _XSD_DATE,
    _XSD_GYEAR,
    _XSD_GYEAR_MONTH,
)

_GYEAR_LENGTH: Final[int] = 4
_GYEAR_MONTH_LENGTH: Final[int] = 7
_MAX_MONTH: Final[int] = 12


def _gyear_month_is_valid(lexical: str) -> bool:
    s = lexical.strip()
    if len(s) != _GYEAR_MONTH_LENGTH or s[_GYEAR_LENGTH] != "-":
        return False
    year, month = s[:_GYEAR_LENGTH], s[_GYEAR_LENGTH + 1 :]
    return year.isdigit() and month.isdigit() and 1 <= int(month) <= _MAX_MONTH


def _datetime_is_valid(lexical: str) -> bool:
    from datetime import datetime

    try:
        datetime.fromisoformat(lexical)
    except ValueError:
        return False
    return True


def _date_is_valid(lexical: str) -> bool:
    from datetime import date

    try:
        date.fromisoformat(lexical)
    except ValueError:
        return False
    return True


def _gyear_is_valid(lexical: str) -> bool:
    s = lexical.strip()
    return len(s) == _GYEAR_LENGTH and s.isdigit()


_DATE_VALIDATORS: Final[dict[URIRef, Callable[[str], bool]]] = {
    _XSD_DATETIME: _datetime_is_valid,
    _XSD_DATE: _date_is_valid,
    _XSD_GYEAR: _gyear_is_valid,
    _XSD_GYEAR_MONTH: _gyear_month_is_valid,
}


def _is_parseable_date(lexical: str, datatype: URIRef) -> bool:
    """Return True iff ``lexical`` is a valid form for ``datatype``.

    Per-type validators in :data:`_DATE_VALIDATORS`. Unknown datatypes
    pass through (we don't know how to validate them; rdflib's own
    coercion will catch any issues).
    """
    validator = _DATE_VALIDATORS.get(datatype)
    return True if validator is None else validator(lexical)


def _sanitize_date_literals(graph: Graph) -> int:
    """Strip the typed datatype from date literals whose lexical form
    doesn't parse — keeps the value visible as plain text and stops
    downstream rdflib loads from crashing on the malformed record.

    Returns the count of literals stripped, for operator visibility.
    Cataloguer-supplied placeholders like ``'19  -  -  T00:00:00'``
    are the typical trigger (likely a date-not-yet-entered marker).
    """
    rewrites: list[tuple[tuple[Node, Node, Node], tuple[Node, Node, Node]]] = []
    for s, p, o in graph:
        if not isinstance(o, Literal):
            continue
        if o.datatype is None or o.datatype not in _DATE_DATATYPES:
            continue
        lexical = str(o)
        if _is_parseable_date(lexical, o.datatype):
            continue
        plain = Literal(lexical)
        rewrites.append(((s, p, o), (s, p, plain)))
    for old, new in rewrites:
        graph.remove(old)
        graph.add(new)
    return len(rewrites)


def construct_bffi(source: Graph) -> Graph:
    """Run both CONSTRUCT passes against ``source`` and merge into one graph."""
    register_sparql_functions()
    out = Graph()
    for query in (_work_query(), _expression_query()):
        result = source.query(query)
        for triple in cast("Iterable[tuple[Node, Node, Node]]", result):
            out.add(triple)
    return out


# --- Post-processing ------------------------------------------------------


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
                elif code3 == "rus":
                    codes.add("ru")
    return frozenset(codes)


def _retag_pref_labels(
    graph: Graph,
    candidates: frozenset[str],
    *,
    llm_detector: object | None = None,
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
    from bffi_pipeline.title_lang import tag_title
    from bffi_pipeline.title_lang_llm import TitleLangDetector

    # The Protocol isn't runtime-checkable; trust the caller to pass the
    # right shape (or None). The annotation casts for mypy's benefit.
    typed_detector = cast("TitleLangDetector | None", llm_detector)

    to_remove: list[tuple[URIRef, URIRef, Literal]] = []
    to_add: list[tuple[URIRef, URIRef, Literal]] = []
    for s, _, o in graph.triples((None, SKOS_prefLabel, None)):
        if not isinstance(o, Literal) or o.language or not isinstance(s, URIRef):
            continue
        tagged = tag_title(str(o), candidates, llm_detector=typed_detector)
        if not tagged:
            continue
        to_remove.append((s, SKOS_prefLabel, o))
        for seg in tagged:
            literal = Literal(seg.text, lang=seg.lang) if seg.lang else Literal(seg.text)
            to_add.append((s, SKOS_prefLabel, literal))
    for triple in to_remove:
        graph.remove(triple)
    for triple in to_add:
        graph.add(triple)


def _propagate_non_primary_roles(bffi_graph: Graph, source: Graph) -> None:
    """Copy ``bf:role`` from source ``bf:Contribution`` blocks onto the
    fresh ``bffi:Contribution`` blank nodes the M3 SPARQL CONSTRUCT
    minted.

    The SPARQL CONSTRUCT that mints non-primary contributions
    (``bf_to_bffi_expression.rq``) drops ``bf:role`` triples — partly
    because role pass-through inside nested OPTIONALs runs into a
    rdflib variable-binding edge case, partly because the source role
    is often a labelled blank node we'd have to re-emit with its
    typing. Doing the copy in Python keeps the SPARQL simple and
    handles both shapes the cataloguer's MARC produces:

    - **URI form** (MARC ``$4`` controlled relator code): copy the
      URI through verbatim as ``bf:role <relators/X>``.
    - **Blank-node form** (MARC ``$e`` free-text — the dominant
      Helmet pattern, ``"kääntäjä"`` / ``"johtaja"`` / etc.):
      mint a fresh blank node typed ``bf:Role`` with
      ``rdfs:label`` carrying the cataloguer's text.

    Match-up: each source ``bf:Contribution`` carries one
    ``bf:agent`` URI; the M3 CONSTRUCT mints exactly one
    ``bffi:Contribution`` per source contribution and routes the
    same agent URI under ``bffi:agent``. Joining on
    (Expression URI, agent URI) gives a 1:1 source ↔ output mapping
    even when the cataloguer entered the same agent multiple times
    in different roles (each 700 occurrence becomes its own source
    contribution).
    """
    # Build agent URI -> queue of source roles, so we can find the role
    # for each output contribution by matching on agent URI. The agent
    # URI is the join key (no 1:1 stable identity between source vs
    # output blank-node Contribution identifiers).
    source_role_by_agent = _index_source_roles_by_agent(source)
    if not source_role_by_agent:
        return

    # Walk output bffi:Contribution blocks and assign one source role
    # per (Contribution, agent) pair. Pop from the per-agent queue so
    # a cataloguer-supplied "Hogwood, Christopher" entered three times
    # with three distinct roles (johtaja / cembalo / urut) routes one
    # role to each minted output contribution rather than fanning the
    # same role across all three.
    for contrib in bffi_graph.subjects(RDF.type, V.BFFI.Contribution):
        if V.BFFI.PrimaryContribution in set(bffi_graph.objects(contrib, RDF.type)):
            continue
        for agent in bffi_graph.objects(contrib, V.BFFI.agent):
            if not isinstance(agent, URIRef):
                continue
            roles = source_role_by_agent.get(agent)
            if not roles:
                continue
            _emit_role_on_contribution(bffi_graph, contrib, *roles.pop(0))
            break


def _index_source_roles_by_agent(
    source: Graph,
) -> dict[URIRef, list[tuple[URIRef | None, str | None]]]:
    """Walk source non-primary contributions and queue ``(role_uri, role_label)``
    per agent URI. The order of insertion matches the source-graph
    iteration order, which is stable within a single rdflib parse."""
    out: dict[URIRef, list[tuple[URIRef | None, str | None]]] = {}
    for src_contrib in source.subjects(RDF.type, V.BF.Contribution):
        if V.BF.PrimaryContribution in set(source.objects(src_contrib, RDF.type)):
            continue
        for agent in source.objects(src_contrib, V.BF.agent):
            if not isinstance(agent, URIRef):
                continue
            for role in source.objects(src_contrib, V.BF.role):
                role_uri = role if isinstance(role, URIRef) else None
                role_label: str | None = None
                if role_uri is None:
                    for lab in source.objects(role, RDFS.label):
                        if isinstance(lab, Literal):
                            role_label = str(lab)
                            break
                out.setdefault(agent, []).append((role_uri, role_label))
    return out


def _emit_role_on_contribution(
    graph: Graph,
    contrib: URIRef | BNode | Node,
    role_uri: URIRef | None,
    role_label: str | None,
) -> None:
    """Add the role triple(s) onto ``contrib`` — URI form when
    ``role_uri`` is set, blank-node-with-label form when only
    ``role_label`` is set, nothing if both are ``None``."""
    if role_uri is not None:
        graph.add((contrib, V.BF.role, role_uri))
    elif role_label is not None:
        role_node = BNode()
        graph.add((contrib, V.BF.role, role_node))
        graph.add((role_node, RDF.type, V.BF.Role))
        graph.add((role_node, RDFS.label, Literal(role_label)))


def _emit_helmet_identifiers(graph: Graph) -> None:
    """For every Work / Expression with a Helmet ``bf:identifiedBy`` link,
    emit a flat ``dct:identifier`` literal in Sierra-style display form
    (e.g. ``"b100000010"``).

    Skosmos can't traverse the structured ``bf:Local`` blank node to
    render the identifier on the concept page; the flat predicate
    surfaces a copy-pasteable bib number cataloguers reference in
    Sierra and the Helmet OPAC. The structured ``bf:identifiedBy``
    stays for BIBFRAME interop.
    """
    to_add: list[tuple[URIRef, URIRef, Literal]] = []
    for s, _, ident in graph.triples((None, V.BF.identifiedBy, None)):
        if not isinstance(s, URIRef):
            continue
        if (ident, V.BF.source, V.HELMET_SOURCE_URI) not in graph:
            continue
        bib_id = graph.value(ident, RDF.value)
        if not isinstance(bib_id, Literal):
            continue
        to_add.append((s, DCTERMS.identifier, Literal(format_sierra_bib_id(str(bib_id)))))
    for triple in to_add:
        graph.add(triple)


def _read_helmet_bib_id(source: Graph, work: URIRef) -> str | None:
    """Walk ``work``'s ``bf:identifiedBy`` chain for the bare Helmet bib ID.

    Returns the ``rdf:value`` literal on the first ``bf:Local`` identifier
    sourced from ``<helmet>`` — the same string M2 records in
    ``helmet-map.jsonl``.
    """
    for ident in source.objects(work, V.BF.identifiedBy):
        if (ident, V.BF.source, V.HELMET_SOURCE_URI) not in source:
            continue
        for value in source.objects(ident, RDF.value):
            if isinstance(value, Literal):
                return str(value)
    return None


def _emit_extracted_contributions(
    bffi_graph: Graph,
    source: Graph,
    *,
    contrib_extractor: object | None = None,
    variants_sidecar_path: Path | None = None,
    now: datetime | None = None,
) -> None:
    """Run the heuristic + optional LLM cascade for MARC 245$c extraction.

    Per main bf:Work in ``source``: read the responsibility-statement
    text and existing 100/700 agent labels, gate on the heuristic, and
    when ``contrib_extractor`` is provided escalate to the LLM. Each
    new agent the LLM returns becomes a non-primary
    ``bffi:Contribution`` block on the corresponding bffi:Expression
    (mirroring the existing M3 routing rule that puts non-primary
    contributions on the Expression).

    Transliteration-variant entries (``transliteration_of`` set) are
    *not* emitted as new Contributions — that would propagate the
    cataloguer's typo'd form. Instead, when ``variants_sidecar_path``
    is supplied, each variant claim is appended to the
    ``contrib-variants.jsonl`` sidecar as a
    :class:`bffi_pipeline.contrib_variants.ContribVariantClaim`. M8's
    binding pass later attaches ``skos:altLabel`` on the canonical
    agent so both forms share the same identity downstream.

    Re-runs against the same source produce byte-identical bffi_graph
    output: blank nodes use SHA-1 of (work_uri, agent_name,
    relator_code) so deterministic.
    """
    from bffi_pipeline.contrib_extract import (
        ExtractionInputs,
        extract_contributions,
        gather_inputs,
    )
    from bffi_pipeline.contrib_extract_llm import (
        DEFAULT_CONTRIB_MODEL,
        RELATOR_URI_PREFIX,
        ContribExtractor,
        contrib_extract_prompt_hash,
    )

    typed_extractor = cast("ContribExtractor | None", contrib_extractor)
    timestamp = (now or datetime.now(UTC)).isoformat()
    prompt_hash = contrib_extract_prompt_hash() if variants_sidecar_path is not None else ""
    extractor_model = (
        getattr(typed_extractor, "model_name", None) or DEFAULT_CONTRIB_MODEL
        if typed_extractor is not None
        else DEFAULT_CONTRIB_MODEL
    )
    pending_claims: list[ContribVariantClaim] = []

    contained: set[URIRef] = {
        o
        for _, _, o in source.triples((None, V.BF.associatedResource, None))
        if isinstance(o, URIRef)
    }
    for work in source.subjects(RDF.type, V.BF.Work):
        if not isinstance(work, URIRef) or work in contained:
            continue
        inputs: ExtractionInputs | None = gather_inputs(source, work)
        if inputs is None:
            continue
        decision = extract_contributions(inputs, extractor=typed_extractor)
        if decision is None or not decision.contributions:
            continue

        expr_uri = URIRef(mint_raw_expression_uri(str(work)))
        bib_id = _read_helmet_bib_id(source, work)
        for cand in decision.contributions:
            if cand.transliteration_of is not None:
                # Variant pointer — record the binding decision in the
                # sidecar so M8 can attach it as a skos:altLabel on
                # the matching canonical agent. Skip Contribution
                # emission either way to avoid propagating the typo'd
                # form as a new agent.
                if variants_sidecar_path is not None and bib_id is not None:
                    pending_claims.append(
                        ContribVariantClaim(
                            helmet_bib_id=bib_id,
                            # Mint the bffi:Work URI rather than passing
                            # the source bf:Work URI: M8's binding pass
                            # joins the sidecar against canonical-map
                            # entries whose raw_work_uris are the bffi
                            # form. Sending the source URI here would
                            # produce a phantom-pointer mismatch.
                            raw_work_uri=mint_raw_work_uri(str(work)),
                            variant_label=cand.name,
                            canonical_label=cand.transliteration_of,
                            relator_code_hint=cand.relator_code,
                            role_text_hint=cand.role_text,
                            rationale=decision.rationale,
                            prompt_hash=prompt_hash,
                            model_id=extractor_model,
                            decided_at=timestamp,
                        )
                    )
                continue
            if cand.relator_code is None:
                continue
            seed = f"{expr_uri}|{cand.name}|{cand.relator_code}"
            digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()
            contrib_node = BNode(f"contrib{digest}")
            agent_node = BNode(f"agent{digest}")
            role_uri = URIRef(RELATOR_URI_PREFIX + cand.relator_code)
            bffi_graph.add((expr_uri, V.BFFI.contribution, contrib_node))
            bffi_graph.add((contrib_node, RDF.type, V.BFFI.Contribution))
            bffi_graph.add((contrib_node, V.BFFI.agent, agent_node))
            bffi_graph.add((contrib_node, V.BF.role, role_uri))
            bffi_graph.add((agent_node, RDF.type, V.BFFI.Agent))
            bffi_graph.add((agent_node, RDFS.label, Literal(cand.name)))

    if pending_claims and variants_sidecar_path is not None:
        append_variant_claims(variants_sidecar_path, pending_claims)


def post_process(
    bffi_graph: Graph,
    source: Graph,
    *,
    llm_detector: object | None = None,
    contrib_extractor: object | None = None,
    variants_sidecar_path: Path | None = None,
    now: datetime | None = None,
) -> Graph:
    """Mutate ``bffi_graph`` in place: tag prefLabels, denormalise Helmet
    identifiers for Skosmos display, optionally extract 245$c
    contributors, bind namespaces.

    ``llm_detector`` enables the M3 title-language cascade;
    ``contrib_extractor`` enables the M3 245$c contributor-extraction
    cascade. Either / both can be ``None`` to keep that stage
    graph-only. ``variants_sidecar_path`` is where the cascade
    appends one row per detected transliteration variant; M8's
    binding pass reads the same file.
    """
    candidates = _candidate_languages(source)
    if candidates:
        _retag_pref_labels(bffi_graph, candidates, llm_detector=llm_detector)
    _emit_helmet_identifiers(bffi_graph)
    _propagate_non_primary_roles(bffi_graph, source)
    _emit_extracted_contributions(
        bffi_graph,
        source,
        contrib_extractor=contrib_extractor,
        variants_sidecar_path=variants_sidecar_path,
        now=now,
    )
    bffi_graph.bind("bf", V.BF)
    bffi_graph.bind("bffi", V.BFFI)
    bffi_graph.bind("bib", V.BIB)
    bffi_graph.bind("dct", DCTERMS)
    bffi_graph.bind("rdf", RDF)
    bffi_graph.bind("rdfs", RDFS)
    bffi_graph.bind("skos", V.SKOS)
    return bffi_graph


# --- Driver ---------------------------------------------------------------


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _is_output_fresh(input_path: Path, output_path: Path) -> bool:
    return output_path.exists() and output_path.stat().st_mtime >= input_path.stat().st_mtime


def _iter_bibframe_files(bibframe_dir: Path) -> Iterator[Path]:
    yield from sorted(p for p in bibframe_dir.glob("*.rdf") if not p.name.startswith("_"))


def _append_jsonl(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _convert_one(
    input_path: Path,
    output_path: Path,
    *,
    llm_detector: object | None = None,
    contrib_extractor: object | None = None,
    variants_sidecar_path: Path | None = None,
    now: datetime | None = None,
) -> Graph:
    source = Graph()
    source.parse(str(input_path), format="xml")
    # Cataloguer $0 values occasionally carry stray whitespace that
    # marc2bibframe2 passes through unchanged; rdflib refuses to
    # serialize those as Turtle and the whole record's M3 conversion
    # would fail hard. Sanitize the parsed source so the CONSTRUCT
    # pass sees clean URIs.
    _sanitize_uri_whitespace(source)
    # Cataloguer-supplied date placeholders (e.g. ``"19  -  -  T00:00:00"``
    # for "year not yet entered") parse as xsd:dateTime in
    # marc2bibframe2's output but raise ValueError when rdflib tries
    # to coerce them at downstream load. Drop the datatype tag so the
    # literal survives as plain text rather than crashing the merge.
    _sanitize_date_literals(source)
    bffi_graph = construct_bffi(source)
    post_process(
        bffi_graph,
        source,
        llm_detector=llm_detector,
        contrib_extractor=contrib_extractor,
        variants_sidecar_path=variants_sidecar_path,
        now=now,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(output_path, bffi_graph.serialize(format="turtle").encode("utf-8"))
    return bffi_graph


def run(
    bibframe_dir: Path | None = None,
    *,
    output_dir: Path | None = None,
    force: bool = False,
    llm_detector: object | None = None,
    contrib_extractor: object | None = None,
    variants_sidecar_path: Path | None = None,
    now: datetime | None = None,
) -> BffiSummary:
    """Convert every ``<bibframe_dir>/<id>.rdf`` to a BFFI Turtle file.

    Pass ``llm_detector`` (a
    :class:`bffi_pipeline.title_lang_llm.TitleLangDetector`) to enable
    the title-language cascade. Pass ``contrib_extractor`` (a
    :class:`bffi_pipeline.contrib_extract_llm.ContribExtractor`) to
    enable 245$c contributor extraction. Without either, M3 stays
    graph-only.

    ``variants_sidecar_path`` defaults to
    ``<output_dir>/contrib-variants.jsonl`` and is the F2 sidecar
    where the contributor cascade persists transliteration claims.
    On ``force=True`` the sidecar is truncated at the start of the
    run so cascade re-runs don't accumulate stale rows.
    """
    base = output_dir or get_settings().data_dir
    bibframe_dir = bibframe_dir or (base / "bibframe")
    summary = BffiSummary()
    validation_path = base / "bffi" / "_validation.jsonl"
    sidecar_path = variants_sidecar_path or (base / DEFAULT_SIDECAR_NAME)
    if force:
        truncate_sidecar(sidecar_path)

    for rdf_path in _iter_bibframe_files(bibframe_dir):
        bib_id = rdf_path.stem
        out_path = base / "bffi" / f"{bib_id}.ttl"
        if not force and _is_output_fresh(rdf_path, out_path):
            summary.skipped_idempotent.append(bib_id)
            continue

        try:
            graph = _convert_one(
                rdf_path,
                out_path,
                llm_detector=llm_detector,
                contrib_extractor=contrib_extractor,
                variants_sidecar_path=sidecar_path,
                now=now,
            )
        except Exception as exc:
            summary.errored.append((bib_id, str(exc)))
            continue

        report = validate_graph(graph)
        if not report.conforms:
            summary.failed_shape.append(bib_id)
            _append_jsonl(
                validation_path,
                asdict(
                    ValidationRow(
                        helmet_bib_id=bib_id,
                        output_file=str(out_path.name),
                        conforms=False,
                        report_text=report.text,
                    )
                ),
            )
        summary.converted.append(bib_id)

    return summary


__all__ = [
    "BffiSummary",
    "ValidationRow",
    "construct_bffi",
    "post_process",
    "run",
]
