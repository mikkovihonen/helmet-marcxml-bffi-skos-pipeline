"""Correlate BIBFRAME entities to source MARC fields by ``bflc:marcKey``.

Phase A scope: marcKey-bearing entities only (covers 100/240/6XX/7XX
/8XX, the bulk of round-trip mispair volume).

Strategy: for each ``?entity bflc:marcKey "<marckey-literal>"`` in the
BIBFRAME graph, parse the leading tag + subfield sequence from the
literal, find the source MARC field with the same tag + identical
subfield sequence, and attach ``bffi:bib_id:tag:ord`` to the entity as
``bffi-prov:fromMarcField``.

Edge cases handled:

- **Duplicate marcKey literals on different entities.** Multiple source
  fields can produce identical marcKey strings (e.g. two 650s with same
  text). Assignment is first-come-first-served in source-MARC order so
  the per-record token ordinals stay deterministic.

- **No exact subfield match.** Falls back to "same tag and same
  subfield-code multiset" before giving up. Audit-logs the
  near-match so the correlation regression test can pin tolerances.

- **Marckey-less entities.** Out of scope for Phase A; logged as
  uncorrelated and addressed in Phase B (flat fields) and Phase C
  (URI-keyed subjects via SubjectLink reification).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS
from rdflib.term import Node

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m2_post.token import (
    MarcFieldIndex,
    SourceMarcField,
)

#: ``bflc:marcKey`` always starts with ``"<3-char tag><ind1><ind2>"``
#: (the two indicator chars are sometimes ``" "`` or omitted via single
#: space). We pin to 3-char tag + exactly 2 indicator chars (whitespace
#: tolerant) to match marc2bibframe2's output.
_MARC_KEY_HEADER_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<tag>\d{3})(?P<ind1>[\s\dA-Za-z])(?P<ind2>[\s\dA-Za-z])"
)

#: Subfield split pattern. ``bflc:marcKey`` uses ``$<code>`` as the
#: delimiter; the same convention as round-trip's marcKey parser. We
#: split on ``$`` followed by a single subfield-code character. The
#: leading text (before the first ``$``) is the result of the indicator
#: + leading space the cataloguer typed; ignored.
_SUBFIELD_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"\$([A-Za-z0-9])")


@dataclass(frozen=True)
class CorrelationResult:
    """Outcome of a single correlation attempt — used for audit logs."""

    bib_id: str
    entity: str  # URI / blank-node id (string representation)
    marc_key: str
    matched_token: str | None
    matched_via: str  # "exact" | "subfield-codes" | "unmatched"
    reason: str | None = None


def parse_marc_key(marc_key: str) -> tuple[str, tuple[tuple[str, str], ...]] | None:
    """Parse a ``bflc:marcKey`` literal into ``(tag, subfields)``.

    Returns ``None`` when the leading header doesn't parse — defensive
    against malformed marcKey strings that would otherwise produce
    misleading correlations.

    >>> parse_marc_key("7001 $aAndersson, Benny,$esäveltäjä")
    ('700', (('a', 'Andersson, Benny,'), ('e', 'säveltäjä')))
    """
    m = _MARC_KEY_HEADER_RE.match(marc_key)
    if m is None:
        return None
    tag = m.group("tag")
    body = marc_key[m.end() :]
    # ``body`` looks like ``" $aFoo$eBar"`` — the leading space (or
    # nothing) is the gap between indicators and the first subfield
    # delimiter. Strip up to the first ``$`` so split() yields clean
    # subfield pairs.
    first_dollar = body.find("$")
    if first_dollar < 0:
        return tag, ()
    body = body[first_dollar:]
    # Split returns ['', 'a', 'Foo', 'e', 'Bar']: the empty leading
    # element is the text before the first delimiter (which we stripped
    # above, leaving ''); then pairs (code, value).
    parts = _SUBFIELD_SPLIT_RE.split(body)
    subs: list[tuple[str, str]] = []
    i = 1  # skip the leading ''
    while i + 1 < len(parts):
        code = parts[i]
        value = parts[i + 1]
        subs.append((code, value))
        i += 2
    return tag, tuple(subs)


def correlate(
    graph: Graph,
    index: MarcFieldIndex,
) -> list[CorrelationResult]:
    """Walk the BIBFRAME graph and attach
    ``bffi-prov:fromMarcField`` tokens to entities derived from source
    MARC fields.

    Two-pass strategy (P-50 Phase A + B):

    1. **marcKey-bearing entities** — entities the cataloguer-typed
       source subfield string survives on (``bf:Agent`` / ``bf:Hub`` /
       authority-style 6XX / 7XX entities). Match by parsing marcKey's
       leading tag + subfield sequence against the source MARC index.

    2. **Flat-literal entities** — per-record bnodes ``bf:Isbn`` /
       ``bf:Title`` / ``bf:Extent`` / ``bf:Note`` / ``bf:ProvisionActivity``
       that marc2bibframe2 emits without marcKey. Match by literal-value
       comparison at the entity's expected predicate (rdf:value /
       bf:mainTitle / rdfs:label / bflc:simplePlace+simpleAgent+simpleDate)
       against the source MARC subfield content.

    Returns a list of :class:`CorrelationResult` for the audit log —
    one row per attempt, with the matched token or the reason for
    failure.

    Idempotent — rdflib's ``add`` is set-semantics. Re-running against
    an already-correlated graph leaves the triple set unchanged.
    """
    results: list[CorrelationResult] = []

    # Per-tag pool of source fields, copied into a deque-like list so
    # we can pop as we assign. Same source field never gets matched to
    # two BIBFRAME entities — protects against M9 / M8 binding-induced
    # duplicates if the M2-post stage ran more than once on the same
    # output.
    pool: dict[str, list[SourceMarcField]] = {}
    for field in index.fields:
        pool.setdefault(field.tag, []).append(field)

    # Iterate marcKey triples in graph-order. Since rdflib's iteration
    # is hash-table-ordered (not source-MARC-order), we collect first
    # and sort by parsed tag + a content-key so the same input always
    # produces the same correlation outcome regardless of triple-store
    # iteration order. Within a tag bucket we sort by the marcKey
    # literal so duplicate-content marcKeys are deterministically
    # assigned to source fields in source-MARC order.
    marckey_triples = [
        (subject, str(literal))
        for subject, _, literal in graph.triples((None, V.BFLC.marcKey, None))
        if isinstance(literal, Literal)
    ]

    def _sort_key(item: tuple[object, str]) -> tuple[str, str]:
        parsed = parse_marc_key(item[1])
        tag = parsed[0] if parsed else "ZZZ"
        return (tag, item[1])

    marckey_triples.sort(key=_sort_key)

    for subject, marc_key in marckey_triples:
        parsed = parse_marc_key(marc_key)
        if parsed is None:
            results.append(
                CorrelationResult(
                    bib_id=index.bib_id,
                    entity=str(subject),
                    marc_key=marc_key,
                    matched_token=None,
                    matched_via="unmatched",
                    reason="malformed-marc-key",
                )
            )
            continue
        tag, subs = parsed
        bucket = pool.get(tag, [])
        if not bucket:
            results.append(
                CorrelationResult(
                    bib_id=index.bib_id,
                    entity=str(subject),
                    marc_key=marc_key,
                    matched_token=None,
                    matched_via="unmatched",
                    reason=f"no-source-field-with-tag-{tag}",
                )
            )
            continue

        match_idx = _find_match(bucket, subs)
        if match_idx is None:
            results.append(
                CorrelationResult(
                    bib_id=index.bib_id,
                    entity=str(subject),
                    marc_key=marc_key,
                    matched_token=None,
                    matched_via="unmatched",
                    reason=f"no-matching-subfields-in-tag-{tag}",
                )
            )
            continue

        matched_field, matched_via = match_idx
        token = matched_field.token
        graph.add((subject, V.fromMarcField, Literal(token)))
        bucket.remove(matched_field)
        results.append(
            CorrelationResult(
                bib_id=index.bib_id,
                entity=str(subject),
                marc_key=marc_key,
                matched_token=token,
                matched_via=matched_via,
            )
        )

    # Phase B: flat-literal entities (no marcKey). Walks the BIBFRAME
    # graph for per-record bnodes typed bf:Isbn / bf:Title / bf:Extent /
    # bf:Note / bf:ProvisionActivity, finds the matching source field by
    # value comparison, attaches the token. ``pool`` is shared with the
    # marcKey pass so the same source field never gets two tokens.
    results.extend(_correlate_flat_literal_entities(graph, index, pool))

    # Phase C: SubjectLink reification for 6XX / 655. For each source
    # subject field, mint a per-record link node anchoring the
    # provenance token — necessary because $0-keyed subjects share
    # their target URI (YSO / finaf / etc.) across every record,
    # leaving no per-occurrence place for the flat ``fromMarcField``
    # triple to live without overwriting other records' tokens.
    results.extend(_mint_subject_links(graph, index))

    return results


def _find_match(
    candidates: list[SourceMarcField],
    target_subs: tuple[tuple[str, str], ...],
) -> tuple[SourceMarcField, str] | None:
    """Pick the source field in ``candidates`` that best matches the
    marcKey's subfield sequence. Two tiers:

    1. **Exact** — identical subfield sequence ``(code, value)``.
    2. **Subfield-codes** — same multiset of subfield codes (value
       drift tolerated for ISBD punctuation / whitespace normalisation).

    Returns ``(field, via)`` or ``None`` when no candidate matches.
    Within a tier, candidates earlier in the source-MARC order win —
    so duplicate-content marcKeys are assigned in document order.
    """
    # Tier 1 — exact subfield sequence
    for field in candidates:
        if field.subfields == target_subs:
            return field, "exact"
    # Tier 2 — same multiset of codes
    target_codes = sorted(c for c, _ in target_subs)
    for field in candidates:
        candidate_codes = sorted(c for c, _ in field.subfields)
        if candidate_codes == target_codes:
            return field, "subfield-codes"
    return None


# --- Phase B: flat-literal matchers ---------------------------------------

_BF: Final[str] = "http://id.loc.gov/ontologies/bibframe/"
_BFLC: Final[str] = "http://id.loc.gov/ontologies/bflc/"

_TYPE_ISBN: Final[URIRef] = URIRef(_BF + "Isbn")
_TYPE_AUDIO_ISSUE: Final[URIRef] = URIRef(_BF + "AudioIssueNumber")
_TYPE_LOCAL: Final[URIRef] = URIRef(_BF + "Local")
_TYPE_TITLE: Final[URIRef] = URIRef(_BF + "Title")
_TYPE_VARIANT_TITLE: Final[URIRef] = URIRef(_BF + "VariantTitle")
_TYPE_EXTENT: Final[URIRef] = URIRef(_BF + "Extent")
_TYPE_NOTE: Final[URIRef] = URIRef(_BF + "Note")
_TYPE_PROVISION: Final[URIRef] = URIRef(_BF + "ProvisionActivity")
_TYPE_PUBLICATION: Final[URIRef] = URIRef(_BF + "Publication")

_BF_MAIN_TITLE: Final[URIRef] = URIRef(_BF + "mainTitle")
_BF_SUBTITLE: Final[URIRef] = URIRef(_BF + "subtitle")
_RDF_VALUE: Final[URIRef] = URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#value")
_BFLC_SIMPLE_PLACE: Final[URIRef] = URIRef(_BFLC + "simplePlace")
_BFLC_SIMPLE_AGENT: Final[URIRef] = URIRef(_BFLC + "simpleAgent")
_BFLC_SIMPLE_DATE: Final[URIRef] = URIRef(_BFLC + "simpleDate")


def _norm(s: str) -> str:
    """Whitespace + case normalisation for value comparison. The
    matchers need tolerance for marc2bibframe2's ISBD-stripping (e.g.
    source ``$a "Foo,"`` → BIBFRAME ``"Foo"``), so comparisons are
    done on a normalised form rather than byte-equal."""
    return " ".join(s.strip().split()).lower()


def _value_of(graph: Graph, entity: URIRef | object, predicate: URIRef) -> str | None:
    """First Literal value of ``?entity predicate ?o``, normalised.
    Returns ``None`` when no literal matches the pattern."""
    for o in graph.objects(entity, predicate):  # type: ignore[arg-type]
        if isinstance(o, Literal):
            return _norm(str(o))
    return None


def _correlate_flat_literal_entities(
    graph: Graph,
    index: MarcFieldIndex,
    pool: dict[str, list[SourceMarcField]],
) -> list[CorrelationResult]:
    """Walk each Phase B entity-type pattern and attach tokens by
    value-matching against source MARC subfields.

    Skips entities that already carry ``bffi-prov:fromMarcField`` from
    the Phase A marcKey pass — protects against double-tokenisation for
    entities that have both (rare; marc2bibframe2 emits marcKey on
    bf:Hub but not on the bf:Title nested inside it, so this guard
    matters mostly for future-proofing)."""
    results: list[CorrelationResult] = []
    pre_tokenised: set[object] = set(graph.subjects(V.fromMarcField, None))

    # Each spec: (target_type, value_fn, candidate_tags, label).
    # value_fn(graph, entity) returns the normalised value to match
    # against; candidate_tags is the list of source MARC tags whose
    # value-extractor produces the same shape. "label" is the audit
    # tag.
    specs: list[
        tuple[
            URIRef,
            Callable[[Graph, object], str | None],
            tuple[tuple[str, Callable[[SourceMarcField], str | None]], ...],
            str,
        ]
    ] = [
        (
            _TYPE_ISBN,
            lambda g, e: _value_of(g, e, _RDF_VALUE),
            (("020", _norm_isbn_subfield_a),),
            "isbn",
        ),
        (
            _TYPE_AUDIO_ISSUE,
            lambda g, e: _value_of(g, e, _RDF_VALUE),
            (("028", lambda f: _norm(f.subfield_value("a") or "")),),
            "audio-issue-number",
        ),
        (
            _TYPE_LOCAL,
            lambda g, e: _value_of(g, e, _RDF_VALUE),
            # 035 system-control-number $a; also 001 / 907 land as
            # bf:Local but those have a Helmet-source qualifier the
            # round-trip handles separately. Phase B tokenises 035
            # only.
            (("035", lambda f: _norm(f.subfield_value("a") or "")),),
            "local-identifier",
        ),
        (
            _TYPE_TITLE,
            _title_value,
            (
                ("245", _title_245_value),
                ("240", _title_240_value),
                ("730", _title_730_value),
                ("740", _title_740_value),
            ),
            "title",
        ),
        (
            _TYPE_VARIANT_TITLE,
            lambda g, e: _value_of(g, e, _BF_MAIN_TITLE),
            (("246", lambda f: _norm(f.subfield_value("a") or "")),),
            "variant-title",
        ),
        (
            _TYPE_EXTENT,
            lambda g, e: _value_of(g, e, RDFS.label),
            (("300", _extent_300_value),),
            "extent",
        ),
        (
            _TYPE_NOTE,
            lambda g, e: _value_of(g, e, RDFS.label),
            tuple(
                (tag, lambda f: _norm(f.subfield_value("a") or ""))
                for tag in ("500", "505", "504", "520", "546", "563", "586")
            ),
            "note",
        ),
        (
            _TYPE_PROVISION,
            _provision_value,
            (("264", _provision_264_value), ("260", _provision_264_value)),
            "provision-activity",
        ),
    ]

    for target_type, value_fn, tag_candidates, label in specs:
        for entity in graph.subjects(RDF.type, target_type):
            if entity in pre_tokenised:
                continue
            entity_value = value_fn(graph, entity)
            if entity_value is None:
                continue
            matched = _match_flat_literal(entity_value, pool, tag_candidates)
            if matched is None:
                results.append(
                    CorrelationResult(
                        bib_id=index.bib_id,
                        entity=str(entity),
                        marc_key=f"({label} value={entity_value!r})",
                        matched_token=None,
                        matched_via="unmatched",
                        reason=f"no-matching-source-for-{label}",
                    )
                )
                continue
            matched_field, source_tag = matched
            token = matched_field.token
            graph.add((entity, V.fromMarcField, Literal(token)))
            pool[source_tag].remove(matched_field)
            results.append(
                CorrelationResult(
                    bib_id=index.bib_id,
                    entity=str(entity),
                    marc_key=f"({label} value={entity_value!r})",
                    matched_token=token,
                    matched_via=f"flat-literal-{label}",
                )
            )
    return results


def _match_flat_literal(
    entity_value: str,
    pool: dict[str, list[SourceMarcField]],
    tag_candidates: tuple[tuple[str, Callable[[SourceMarcField], str | None]], ...],
) -> tuple[SourceMarcField, str] | None:
    """Walk each candidate tag's source field pool, comparing the
    normalised ``entity_value`` to each field's value-extractor.

    Tag candidates are tried in declaration order, so a 245 source
    field beats a 240 source field when a bf:Title bnode could match
    either. Within a tag, candidates earlier in source-MARC order win
    (duplicates assigned in document order).
    """
    for tag, extractor in tag_candidates:
        bucket = pool.get(tag, [])
        for field in bucket:
            candidate_value = extractor(field)
            if candidate_value is None:
                continue
            if _norm(candidate_value) == entity_value:
                return field, tag
    return None


def _norm_isbn_subfield_a(field: SourceMarcField) -> str:
    """ISBN comparison drops hyphens + whitespace: source MARC may
    type ``"978-0-7119-7011-4"`` while BIBFRAME's rdf:value strips to
    ``"9780711970114"`` or the cataloguer-typed bare digits ``"0711970114"``."""
    raw = field.subfield_value("a") or ""
    return "".join(c for c in raw if c.isalnum()).lower()


def _title_value(graph: Graph, entity: object) -> str | None:
    """Concatenate the entity's bf:mainTitle + bf:subtitle for
    comparison (marc2bibframe2 emits both as separate triples on
    bf:Title; the round-trip wants them combined for the 245 $a/$b
    pair)."""
    main = _value_of(graph, entity, _BF_MAIN_TITLE)
    if main is None:
        return None
    sub = _value_of(graph, entity, _BF_SUBTITLE)
    return f"{main} {sub}" if sub else main


def _title_245_value(field: SourceMarcField) -> str:
    a = field.subfield_value("a") or ""
    b = field.subfield_value("b") or ""
    return _norm(f"{a} {b}" if b else a)


def _title_240_value(field: SourceMarcField) -> str:
    """240 has $a + $n + $p + $l. marc2bibframe2 concatenates them
    into bf:mainTitle. Normalise the source side the same way."""
    parts = [field.subfield_value(c) or "" for c in ("a", "n", "p", "l")]
    return _norm(" ".join(p for p in parts if p))


def _title_730_value(field: SourceMarcField) -> str:
    """730 has $a + $g (and others). Concatenate for comparison."""
    parts = [field.subfield_value(c) or "" for c in ("a", "g")]
    return _norm(" / ".join(p for p in parts if p))


def _title_740_value(field: SourceMarcField) -> str:
    return _title_730_value(field)


def _extent_300_value(field: SourceMarcField) -> str:
    """300 has $a + $b + $c. marc2bibframe2 flattens into rdfs:label."""
    parts = [field.subfield_value(c) or "" for c in ("a", "b", "c")]
    return _norm(" ".join(p for p in parts if p))


def _provision_value(graph: Graph, entity: object) -> str | None:
    """Concatenate place / agent / date from the bf:ProvisionActivity
    bnode for matching against 264/260 $a/$b/$c."""
    place = _value_of(graph, entity, _BFLC_SIMPLE_PLACE)
    agent = _value_of(graph, entity, _BFLC_SIMPLE_AGENT)
    date = _value_of(graph, entity, _BFLC_SIMPLE_DATE)
    parts = [p for p in (place, agent, date) if p]
    if not parts:
        return None
    return " ".join(parts)


def _provision_264_value(field: SourceMarcField) -> str:
    parts = [field.subfield_value(c) or "" for c in ("a", "b", "c")]
    return _norm(" ".join(p for p in parts if p))


# --- Phase C: SubjectLink reification -------------------------------------

_TYPE_WORK: Final[URIRef] = URIRef(_BF + "Work")
_BF_SUBJECT: Final[URIRef] = URIRef(_BF + "subject")
_BF_GENRE_FORM: Final[URIRef] = URIRef(_BF + "genreForm")

#: marc2bibframe2 routes MARC 655 (Index Term - Genre/Form) to
#: ``bf:genreForm`` rather than ``bf:subject``; every other 6XX tag
#: in :data:`_SUBJECT_TAGS` goes through ``bf:subject``. The matcher
#: walks both predicates and the per-tag table picks the right one.
_PREDICATE_BY_TAG: Final[dict[str, URIRef]] = {
    "600": _BF_SUBJECT,
    "610": _BF_SUBJECT,
    "611": _BF_SUBJECT,
    "630": _BF_SUBJECT,
    "648": _BF_SUBJECT,
    "650": _BF_SUBJECT,
    "651": _BF_SUBJECT,
    "655": _BF_GENRE_FORM,
}

#: Source MARC tags whose subjects need link-node reification. Covers
#: the standard 6XX subject family + 655 (genre / form). 800 / 810 /
#: 811 / 830 (series-tracing entries) carry their own per-record entity
#: types and don't share URIs the same way, so they're left to Phase A
#: + B coverage.
_SUBJECT_TAGS: Final[tuple[str, ...]] = (
    "600",
    "610",
    "611",
    "630",
    "648",
    "650",
    "651",
    "655",
)


def _subject_statement_uri(bib_id: str, tag: str, ordinal: int) -> URIRef:
    """Deterministic per-occurrence ``rdf:Statement`` URI for subject
    reification. Format:
    ``http://urn.fi/URN:NBN:fi:bib:subject-statement:<bib_id>:<tag>:<ord>``.

    Lives in the bib namespace (per CLAUDE.md "Committed identifiers";
    no separate namespace needed — statement URIs are per-record and
    never merge). The URI is human-greppable: a cataloguer reading the
    canonical graph can find every reified subject statement from one
    source record by its bib_id prefix.
    """
    return URIRef(f"http://urn.fi/URN:NBN:fi:bib:subject-statement:{bib_id}:{tag}:{ordinal}")


def _mint_subject_links(
    graph: Graph,
    index: MarcFieldIndex,
) -> list[CorrelationResult]:
    """For each source 6XX / 655 datafield, emit a W3C-standard
    ``rdf:Statement`` reification that ties the source field to its
    subject target:

    ``<stmt> a rdf:Statement ;``
    ``       rdf:subject   <work> ;``
    ``       rdf:predicate bffi:subject ;``
    ``       rdf:object    <target> ;``
    ``       bffi-prov:fromMarcField "<token>" .``

    Matching strategy:

    1. **`$0` (authority URI) match** — when the source has
       ``$0 <uri>``, find the bf:subject pointing to that URI.
    2. **`$a` label match** — bf:subject target with ``rdfs:label``
       equal to source ``$a``.
    3. **Raw URI fragment match** — bf:subject target URI whose
       fragment contains the source tag + ordinal.

    The reified statement's ``fromMarcField`` token is the same one
    Phase A would have computed; the carrier is the reified statement
    URI (per-occurrence) instead of the shared target URI.
    """
    results: list[CorrelationResult] = []
    bib_id = index.bib_id

    works: list[Node] = list(graph.subjects(RDF.type, _TYPE_WORK))
    if not works:
        return results
    primary_work = _select_primary_work(graph, works)
    if primary_work is None:
        return results

    targets_used: set[Node] = set()

    for tag in _SUBJECT_TAGS:
        for field in index.fields_by_tag(tag):
            target = _find_subject_target(graph, primary_work, field, targets_used)
            if target is None:
                results.append(
                    CorrelationResult(
                        bib_id=bib_id,
                        entity=f"(subject-statement source-field {field.token})",
                        marc_key=f"(source {tag})",
                        matched_token=None,
                        matched_via="unmatched",
                        reason=f"no-bf-subject-target-for-{tag}",
                    )
                )
                continue
            targets_used.add(target)
            stmt = _subject_statement_uri(bib_id, tag, field.ordinal)
            graph.add((stmt, RDF.type, RDF.Statement))
            graph.add((stmt, RDF.subject, primary_work))
            graph.add((stmt, RDF.predicate, V.reifiedSubjectPredicate))
            graph.add((stmt, RDF.object, target))
            graph.add((stmt, V.fromMarcField, Literal(field.token)))
            results.append(
                CorrelationResult(
                    bib_id=bib_id,
                    entity=str(stmt),
                    marc_key=f"(subject-statement tag={tag} ord={field.ordinal})",
                    matched_token=field.token,
                    matched_via="subject-statement",
                )
            )
    return results


def _select_primary_work(graph: Graph, works: list[Node]) -> Node | None:
    """Pick the main bf:Work for the record — the one that is NOT
    contained inside another work (no inverse ``bf:associatedResource``
    link). Mirrors the M3 ``FILTER NOT EXISTS`` filter in
    ``bf_to_bffi_work.rq``.
    """
    contained: set[Node] = set()
    bf_associated = URIRef(_BF + "associatedResource")
    for _s, _p, o in graph.triples((None, bf_associated, None)):
        contained.add(o)
    for work in works:
        if work not in contained:
            return work
    return works[0] if works else None


def _find_subject_target(
    graph: Graph,
    work: Node,
    field: SourceMarcField,
    targets_used: set[Node],
) -> Node | None:
    """Match a source MARC 6XX / 655 field to the BIBFRAME target
    that marc2bibframe2 emitted from it.

    The carrier predicate depends on the tag: 655 (Genre/Form) lands
    on ``bf:genreForm``; every other 6XX tag lands on ``bf:subject``.
    The matcher uses :data:`_PREDICATE_BY_TAG` to pick the right one
    (defaulting to ``bf:subject`` for unmapped tags).

    Three tiers:

    1. **``$0`` URI match** — exact URI equality between source ``$0``
       and a candidate target.
    2. **``$a`` label match** — candidate target with rdfs:label
       equal to source ``$a`` (whitespace normalised).
    3. **Raw URI fragment match** — candidate target URI whose
       fragment contains the source tag + ordinal (matches
       marc2bibframe2's ``#Topic650-N`` / ``#GenreForm655-N`` raw
       URIs even when M3's counter isn't 1-indexed-within-tag).
    """
    predicate = _PREDICATE_BY_TAG.get(field.tag, _BF_SUBJECT)
    source_a = field.subfield_value("a") or ""
    source_a_norm = _norm(source_a)
    source_zero = field.subfield_value("0") or ""

    # Tier 1 — $0 URI exact match
    if source_zero:
        candidate = URIRef(source_zero.strip())
        for _, _, o in graph.triples((work, predicate, candidate)):
            if o not in targets_used:
                return o

    # Tier 2 — $a label exact match
    for o in graph.objects(work, predicate):
        if o in targets_used:
            continue
        for label in graph.objects(o, RDFS.label):
            if isinstance(label, Literal) and _norm(str(label)) == source_a_norm:
                return o

    # Tier 3 — raw URI fragment match (#Topic<tag>-N, #GenreForm655-N)
    fragment_marker = f"{field.tag}-"
    for o in graph.objects(work, predicate):
        if o in targets_used:
            continue
        if isinstance(o, URIRef) and fragment_marker in str(o):
            return o
    return None
