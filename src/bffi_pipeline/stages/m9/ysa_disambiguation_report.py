"""Stage: emit a cataloguer-review CSV for YSA → YSO disambiguation residue.

The 2014-2018 YSA → YSO vocabulary merge replaced many bare YSA
prefLabels (``lapset``, ``sissit``, ``pohjalaismurteet``, …) with
parenthetically-disambiguated YSO forms (``lapset (ikäryhmät)`` +
``lapset (perheenjäsenet)``, etc.) and deliberately did *not* carry
the bare form as ``skos:altLabel``. Helmet MARC records still cite
the bare YSA forms, so they land in M9 ``reconciliation-no-candidate``
or ``reconciliation-fallback``. See ``docs/runbook.md`` for the
operational background.

The cataloguers can't search these from the current ILS, so this
report walks ``canonical.ttl`` and writes a CSV they can sort and
filter in Excel/Sheets. Two case types are surfaced:

- **ambiguous**: the literal has ≥ 2 disambiguated YSO prefLabels —
  cataloguer must pick the right sense (e.g. ``lapset`` →
  ``lapset (ikäryhmät)`` or ``lapset (perheenjäsenet)``).
- **missed-altlabel**: the literal has exactly one disambiguated
  YSO prefLabel — no genuine ambiguity; cataloguer just needs to
  add ``$0`` with the disambiguated URI on the original MARC record.

One CSV row per ``(helmet_bib_id, literal, candidate)`` tuple so
cataloguers can sort by literal (one decision applies across all
records that share it) or by ``helmet_bib_id`` (find each record to
update). Stable column order; UTF-8 with BOM so Excel on macOS
opens it without mangling Finnish characters.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import httpx
from rdflib import Graph, URIRef
from rdflib import Literal as RdfLiteral

from bffi_pipeline.config import get_settings
from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m9.local_concept_resolver import _quote_sparql_literal
from bffi_pipeline.stages.m9.requests import _iter_subject_requests

#: Threshold below which a literal is "missed-altlabel" (one
#: disambiguated candidate, no genuine ambiguity) and at/above which
#: it's "ambiguous" (cataloguer must choose).
_AMBIGUOUS_CANDIDATE_THRESHOLD: Final[int] = 2

#: Default Fuseki named graph queried for YSO disambiguated prefLabels.
#: YSO + YSO-Paikat + YSO-Aika all share this graph after the M11 3b
#: load grouping; the report doesn't care which sub-vocab a concept
#: came from — the cataloguer just needs a labelled URI to bind.
DEFAULT_YSO_GRAPH_URI: Final[str] = "http://www.yso.fi/onto/yso/"

#: Default report output filename relative to ``BFFI_DATA_DIR``.
DEFAULT_REPORT_FILENAME: Final[str] = "ysa-disambiguation-report.csv"

#: Stable CSV column order. ``helmet_bib_id`` is first because the
#: cataloguer workflow starts from finding records in the ILS.
CSV_COLUMNS: Final[tuple[str, ...]] = (
    "helmet_bib_id",
    "canonical_work_uri",
    "source_tag",
    "literal",
    "case_type",
    "n_candidates",
    "candidate_uri",
    "candidate_pref_label",
)

#: URI prefix marc2bibframe2 mints for Helmet identifier nodes. Bib IDs
#: live as the final path segment (e.g.
#: ``<.../ident/helmet/2628274>`` → ``"2628274"``).
_HELMET_IDENT_PREFIX: Final[str] = "http://urn.fi/URN:NBN:fi:bib:graph:ident/helmet/"


@dataclass(frozen=True)
class DisambiguationCandidate:
    """One disambiguated YSO concept found for a bare cataloguer literal."""

    uri: str
    pref_label: str


@dataclass(frozen=True)
class DisambiguationRow:
    """One CSV row — flattened per ``(bib_id, literal, candidate)`` tuple."""

    helmet_bib_id: str
    canonical_work_uri: str
    source_tag: str
    literal: str
    case_type: str  # "ambiguous" or "missed-altlabel"
    n_candidates: int
    candidate_uri: str
    candidate_pref_label: str


@dataclass
class DisambiguationSummary:
    """Aggregate counts reported on the CLI after a report run."""

    rows_written: int = 0
    distinct_literals: int = 0
    ambiguous_literals: int = 0
    missed_altlabel_literals: int = 0
    helmet_bib_ids: set[str] = field(default_factory=set)

    def render(self) -> str:
        """Format the summary as paste-ready text for the CLI."""
        return "\n".join(
            (
                "YSA → YSO disambiguation report complete",
                f"  distinct literals flagged:    {self.distinct_literals:,}",
                f"    ambiguous (cataloguer chooses): {self.ambiguous_literals:,}",
                f"    missed-altlabel (1 candidate): {self.missed_altlabel_literals:,}",
                f"  Helmet bib_ids affected:      {len(self.helmet_bib_ids):,}",
                f"  CSV rows written:             {self.rows_written:,}",
            )
        )


def _query_disambiguation_candidates(
    http_client: httpx.Client,
    fuseki_url: str,
    literal: str,
    graph_uri: str = DEFAULT_YSO_GRAPH_URI,
    *,
    timeout_seconds: float = 5.0,
) -> list[DisambiguationCandidate]:
    """Return YSO concepts whose Finnish ``skos:prefLabel`` is
    ``"<literal> (<qualifier>)"`` — the YSA → YSO disambiguation form.

    The pre-2018 YSA bare lemma is matched as the prefix of a
    parenthetical-qualified YSO prefLabel. Only ``@fi`` labels are
    considered because the bare YSA literal cataloguers carry is
    Finnish; Swedish/English equivalents are out of scope for this
    report (the cataloguers want to fix Finnish-side MARC records).
    """
    quoted = _quote_sparql_literal(literal)
    prefix_match = _quote_sparql_literal(f"{literal} (")
    query = (
        "PREFIX skos: <http://www.w3.org/2004/02/skos/core#>\n"
        f"SELECT ?uri ?label WHERE {{\n"
        f"  GRAPH <{graph_uri}> {{\n"
        "    ?uri skos:prefLabel ?label .\n"
        '    FILTER (lang(?label) = "fi" && '
        f"strstarts(str(?label), {prefix_match}) && "
        f"str(?label) != {quoted})\n"
        "  }\n"
        "}\n"
        "ORDER BY ?uri\n"
        "LIMIT 20\n"
    )
    response = http_client.post(
        f"{fuseki_url.rstrip('/')}/sparql",
        data={"query": query},
        headers={"Accept": "application/sparql-results+json"},
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    out: list[DisambiguationCandidate] = []
    for row in payload.get("results", {}).get("bindings", []):
        uri = row.get("uri", {}).get("value")
        label = row.get("label", {}).get("value", "")
        if uri:
            out.append(DisambiguationCandidate(uri=str(uri), pref_label=str(label)))
    return out


def _helmet_bib_ids_for_work(graph: Graph, work: URIRef) -> list[str]:
    """Extract the Helmet bib IDs identified-by the canonical Work.

    Multi-record canonicals (M8 merge consolidated several raw bibs)
    carry one ``bf:identifiedBy`` per absorbed Helmet record; the
    cataloguer needs every bib ID so each underlying MARC record can
    be updated.
    """
    ids: list[str] = []
    for ident in graph.objects(work, V.BF.identifiedBy):
        if not isinstance(ident, URIRef):
            continue
        s = str(ident)
        if s.startswith(_HELMET_IDENT_PREFIX):
            ids.append(s[len(_HELMET_IDENT_PREFIX) :])
    return sorted(ids)


def _source_tag_for_literal(
    graph: Graph, work: URIRef, predicate: URIRef, literal_value: str
) -> str:
    """Read the ``bf:source`` token that tagged ``literal_value`` on the
    given canonical-Work predicate. Falls back to ``"(none)"`` when the
    cataloguer omitted ``$2`` — common with local subject schemes."""
    for target in graph.objects(work, predicate):
        for label in graph.objects(target, V.RDFS.label):
            if not isinstance(label, RdfLiteral) or str(label) != literal_value:
                continue
            for src in graph.objects(target, V.BF.source):
                if isinstance(src, URIRef):
                    return str(src).rsplit("/", 1)[-1]
                if isinstance(src, RdfLiteral):
                    return str(src)
            return "(none)"
    return "(none)"


def walk_disambiguation_residue(
    canonical_graph: Graph,
    http_client: httpx.Client,
    *,
    fuseki_url: str,
    graph_uri: str = DEFAULT_YSO_GRAPH_URI,
) -> Iterator[DisambiguationRow]:
    """Yield one report row per ``(bib_id, literal, candidate)`` tuple.

    Walks the canonical graph's subject-walker requests, queries Fuseki
    for disambiguated YSO prefLabels, and emits rows for the cases the
    cataloguers need to review. Deterministic ordering: literals are
    grouped, candidates ordered by URI, bib IDs sorted lexically.
    """
    # Group literals by (predicate, source_tag) so we issue exactly one
    # Fuseki round-trip per distinct literal.
    requests_by_literal: dict[tuple[str, str, str], list[URIRef]] = defaultdict(list)
    for r in _iter_subject_requests(canonical_graph):
        work_uri = URIRef(r.work_uri)
        predicate = URIRef(r.predicate_uri or str(V.BFFI.subject))
        source_tag = _source_tag_for_literal(canonical_graph, work_uri, predicate, r.literal)
        requests_by_literal[(r.literal, source_tag, r.predicate_uri or "")].append(work_uri)

    for (literal, source_tag, _predicate_uri), works in sorted(requests_by_literal.items()):
        candidates = _query_disambiguation_candidates(
            http_client, fuseki_url, literal, graph_uri=graph_uri
        )
        if not candidates:
            continue
        case_type = (
            "ambiguous" if len(candidates) >= _AMBIGUOUS_CANDIDATE_THRESHOLD else "missed-altlabel"
        )
        for work in sorted(works, key=str):
            for bib_id in _helmet_bib_ids_for_work(canonical_graph, work):
                for candidate in candidates:
                    yield DisambiguationRow(
                        helmet_bib_id=bib_id,
                        canonical_work_uri=str(work),
                        source_tag=source_tag,
                        literal=literal,
                        case_type=case_type,
                        n_candidates=len(candidates),
                        candidate_uri=candidate.uri,
                        candidate_pref_label=candidate.pref_label,
                    )


def write_csv(rows: Iterable[DisambiguationRow], output_path: Path) -> DisambiguationSummary:
    """Write ``rows`` to ``output_path`` as UTF-8-with-BOM CSV.

    BOM keeps Excel on macOS from mangling Finnish diacritics. Atomic
    write via ``.tmp`` + rename so a partial report never overwrites a
    good previous one.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    summary = DisambiguationSummary()
    literals_seen: dict[str, str] = {}  # literal → case_type
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_COLUMNS)
        for row in rows:
            writer.writerow(
                [
                    row.helmet_bib_id,
                    row.canonical_work_uri,
                    row.source_tag,
                    row.literal,
                    row.case_type,
                    str(row.n_candidates),
                    row.candidate_uri,
                    row.candidate_pref_label,
                ]
            )
            summary.rows_written += 1
            summary.helmet_bib_ids.add(row.helmet_bib_id)
            literals_seen[row.literal] = row.case_type
    tmp.replace(output_path)
    summary.distinct_literals = len(literals_seen)
    summary.ambiguous_literals = sum(1 for c in literals_seen.values() if c == "ambiguous")
    summary.missed_altlabel_literals = sum(
        1 for c in literals_seen.values() if c == "missed-altlabel"
    )
    return summary


def run(
    canonical_path: Path | None = None,
    *,
    output_path: Path | None = None,
    fuseki_url: str | None = None,
    graph_uri: str = DEFAULT_YSO_GRAPH_URI,
    http_client: httpx.Client | None = None,
) -> DisambiguationSummary:
    """End-to-end: parse canonical.ttl, walk residue, write CSV.

    Operator entry point used by the ``bffi-pipeline ysa-disambiguation-report``
    CLI subcommand. Tests inject a stub ``http_client`` to skip the
    live Fuseki dependency.
    """
    settings = get_settings()
    canonical_path = canonical_path or (settings.data_dir / "canonical.ttl")
    output_path = output_path or (settings.data_dir / DEFAULT_REPORT_FILENAME)
    fuseki_url = fuseki_url or settings.fuseki_url

    g = Graph()
    g.parse(str(canonical_path), format="turtle")

    owned_client = http_client is None
    if owned_client:
        http_client = httpx.Client(timeout=httpx.Timeout(10.0))
    assert http_client is not None  # narrow for mypy

    try:
        rows = list(
            walk_disambiguation_residue(g, http_client, fuseki_url=fuseki_url, graph_uri=graph_uri)
        )
    finally:
        if owned_client:
            http_client.close()

    return write_csv(rows, output_path)


__all__ = [
    "CSV_COLUMNS",
    "DEFAULT_REPORT_FILENAME",
    "DEFAULT_YSO_GRAPH_URI",
    "DisambiguationCandidate",
    "DisambiguationRow",
    "DisambiguationSummary",
    "run",
    "walk_disambiguation_residue",
    "write_csv",
]
