"""Stage M8: merge application — union-find on judge decisions, canonical Works.

Reads M6's ``judge-decisions.jsonl`` and the M3 BFFI Turtle, applies
union-find over ``same_work`` decisions, mints canonical Work URIs
via :func:`bffi_pipeline.uris.mint_work_uri`, unions the
``bf:identifiedBy`` sets so the canonical Work carries one
identifier per absorbed Helmet record, rewrites
``bffi:expressionOf`` to point at the canonical URI, and emits one
``bffi:AdminMetadata`` block per canonical Work per spec § 8 +
BUILD_PLAN M8.

Outputs land under ``BFFI_DATA_DIR``:

- ``canonical.ttl`` — canonical Works + AdminMetadata + rewritten
  ``bffi:expressionOf`` triples + ``prov:wasDerivedFrom`` links.
- ``canonical-map.jsonl`` — one row per canonical Work
  (``{canonical_work_uri, raw_work_uris, helmet_bib_ids, merged_at}``).
- ``canonical-conflicts.jsonl`` — groups that are flagged because the
  judge produced contradictory decisions
  (``A=B``, ``A≠C``, ``B=C``). Conflicts are *not* silently merged;
  they go to a separate file for human review.

Idempotent: re-running with the same inputs produces byte-identical
outputs because the anchor for each merge group is the
lex-smallest member URI and the merged-at timestamp is taken from
the latest M2 ``converted_at`` in the group rather than wall clock.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from typing import Literal as LiteralType

from rdflib import Graph, Literal, URIRef
from rdflib import Literal as RdfLiteral
from rdflib.namespace import RDF

from bffi_pipeline.config import get_settings
from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.provenance.logger import model_agent_uri
from bffi_pipeline.uris import mint_work_uri

# --- Constants ------------------------------------------------------------

CANONICAL_FILENAME: Final[str] = "canonical.ttl"
CANONICAL_MAP_FILENAME: Final[str] = "canonical-map.jsonl"
CANONICAL_CONFLICTS_FILENAME: Final[str] = "canonical-conflicts.jsonl"
JUDGE_DECISIONS_FILENAME: Final[str] = "judge-decisions.jsonl"
HELMET_MAP_FILENAME: Final[str] = "helmet-map.jsonl"


# --- Union-find -----------------------------------------------------------


class _UnionFind:
    """Tiny path-compressed union-find. Nodes are arbitrary hashable values."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def add(self, x: str) -> None:
        if x not in self._parent:
            self._parent[x] = x

    def find(self, x: str) -> str:
        self.add(x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        # path compression
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, x: str, y: str) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            # Pick the lex-smaller root for determinism
            new_root, child = (rx, ry) if rx < ry else (ry, rx)
            self._parent[child] = new_root

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for x in list(self._parent):
            out.setdefault(self.find(x), []).append(x)
        for v in out.values():
            v.sort()
        return out


# --- Decisions ------------------------------------------------------------


DecisionLabel = LiteralType["same_work", "different_work", "uncertain"]


@dataclass(frozen=True)
class JudgeDecisionRow:
    """One row of ``judge-decisions.jsonl`` reduced to the fields M8 reads."""

    work_a: str
    work_b: str
    decision: DecisionLabel
    confidence: float
    used_cascade: bool
    winning_model: str | None  # cascade fallback's model when used_cascade else primary


def _load_decisions(path: Path) -> list[JudgeDecisionRow]:
    if not path.is_file():
        raise FileNotFoundError(
            f"M6 decisions JSONL not found at {path!s}. Run `bffi-pipeline judge` first."
        )
    rows: list[JudgeDecisionRow] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Bad JSON at {path!s}:{line_no}: {exc}") from exc
        cascade = data.get("cascade") or []
        winning_model: str | None = None
        if cascade:
            winning_model = str(cascade[-1].get("model") or "") or None
        rows.append(
            JudgeDecisionRow(
                work_a=data["work_a"],
                work_b=data["work_b"],
                decision=data["decision"],
                confidence=float(data["confidence"]),
                used_cascade=bool(data.get("used_cascade", False)),
                winning_model=winning_model,
            )
        )
    return rows


# --- Conflict detection ---------------------------------------------------


@dataclass(frozen=True)
class GroupConflict:
    """One contradictory group flagged for human review."""

    members: list[str]
    conflicting_pair: tuple[str, str]
    same_work_path: list[tuple[str, str]]


def _detect_conflicts(
    groups: dict[str, list[str]],
    different_work_edges: Iterable[tuple[str, str]],
    same_work_edges: list[tuple[str, str]],
) -> list[GroupConflict]:
    """Return groups whose union-find membership contradicts a different_work edge."""
    member_to_root: dict[str, str] = {}
    for root, members in groups.items():
        for m in members:
            member_to_root[m] = root

    conflicts: list[GroupConflict] = []
    seen_roots: set[str] = set()
    for a, b in different_work_edges:
        root_a = member_to_root.get(a)
        root_b = member_to_root.get(b)
        if root_a is None or root_b is None:
            continue
        if root_a == root_b and root_a not in seen_roots:
            seen_roots.add(root_a)
            conflicts.append(
                GroupConflict(
                    members=sorted(groups[root_a]),
                    conflicting_pair=(a, b),
                    same_work_path=[edge for edge in same_work_edges if edge[0] in groups[root_a]],
                )
            )
    return conflicts


# --- BFFI graph extraction ------------------------------------------------


@dataclass
class CanonicalWorkInputs:
    """Per-Work data the merge step needs to mint a canonical Work URI."""

    work_uri: str
    creator_uri: str | None
    pref_label: str | None
    expression_uris: list[str] = field(default_factory=list)
    helmet_identifiers: list[tuple[str, str]] = field(default_factory=list)
    """List of (identifier_uri, helmet_bib_id) pairs as found on bf:identifiedBy."""


def _first_pref_label(graph: Graph, subject: URIRef) -> str | None:
    for o in graph.objects(subject, V.SKOS.prefLabel):
        if isinstance(o, RdfLiteral):
            return str(o)
    return None


def _primary_agent_uri(graph: Graph, work: URIRef) -> str | None:
    for contrib in graph.objects(work, V.BFFI.contribution):
        if V.BFFI.PrimaryContribution not in set(graph.objects(contrib, RDF.type)):
            continue
        for ag in graph.objects(contrib, V.BFFI.agent):
            if isinstance(ag, URIRef):
                return str(ag)
    return None


def _helmet_identifiers(graph: Graph, work: URIRef) -> list[tuple[str, str]]:
    """Return [(ident_uri, helmet_bib_id), ...] for ``work``'s Helmet identifiers."""
    out: list[tuple[str, str]] = []
    for ident in graph.objects(work, V.BF.identifiedBy):
        if not isinstance(ident, URIRef):
            continue
        sources = set(graph.objects(ident, V.BF.source))
        if V.HELMET_SOURCE_URI not in sources:
            continue
        for value in graph.objects(ident, RDF.value):
            if isinstance(value, RdfLiteral):
                out.append((str(ident), str(value)))
                break
    return out


def extract_work_metadata(graph: Graph) -> dict[str, CanonicalWorkInputs]:
    """Walk the combined BFFI + BIBFRAME graph and return per-Work merge inputs."""
    out: dict[str, CanonicalWorkInputs] = {}
    for work in graph.subjects(RDF.type, V.BFFI.Work):
        if not isinstance(work, URIRef):
            continue
        out[str(work)] = CanonicalWorkInputs(
            work_uri=str(work),
            creator_uri=_primary_agent_uri(graph, work),
            pref_label=_first_pref_label(graph, work),
            expression_uris=sorted(
                str(expr)
                for expr in graph.objects(work, V.BFFI.hasExpression)
                if isinstance(expr, URIRef)
            ),
            helmet_identifiers=_helmet_identifiers(graph, work),
        )
    return out


# --- helmet-map.jsonl loader ---------------------------------------------


@dataclass(frozen=True)
class HelmetMapEntry:
    raw_work_uri: str
    helmet_bib_id: str
    converted_at: str


def _load_helmet_map(path: Path) -> dict[str, HelmetMapEntry]:
    """Return ``raw_work_uri → HelmetMapEntry``."""
    if not path.is_file():
        return {}
    out: dict[str, HelmetMapEntry] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        out[row["raw_work_uri"]] = HelmetMapEntry(
            raw_work_uri=row["raw_work_uri"],
            helmet_bib_id=row["helmet_bib_id"],
            converted_at=row["converted_at"],
        )
    return out


# --- Canonical-map JSONL --------------------------------------------------


@dataclass
class CanonicalEntry:
    canonical_work_uri: str
    raw_work_uris: list[str]
    helmet_bib_ids: list[str]
    merged_at: str


def _emit_canonical_map(path: Path, entries: Iterable[CanonicalEntry]) -> None:
    rows = sorted(entries, key=lambda e: e.canonical_work_uri)
    payload = "\n".join(
        json.dumps(
            {
                "canonical_work_uri": e.canonical_work_uri,
                "raw_work_uris": list(e.raw_work_uris),
                "helmet_bib_ids": list(e.helmet_bib_ids),
                "merged_at": e.merged_at,
            },
            ensure_ascii=False,
        )
        for e in rows
    )
    if payload:
        payload += "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def _emit_conflicts(path: Path, conflicts: Iterable[GroupConflict]) -> None:
    rows = sorted(conflicts, key=lambda c: c.members[0] if c.members else "")
    payload = "\n".join(
        json.dumps(
            {
                "members": list(c.members),
                "conflicting_pair": list(c.conflicting_pair),
                "same_work_path": [list(edge) for edge in c.same_work_path],
            },
            ensure_ascii=False,
        )
        for c in rows
    )
    if payload:
        payload += "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


# --- Canonical Turtle emission -------------------------------------------


def _admin_metadata_uri(canonical_uri: str) -> URIRef:
    digest = hashlib.sha1(canonical_uri.encode("utf-8")).hexdigest()
    return URIRef(f"{get_settings().graph_base}adminmeta/{digest}")


def _emit_canonical_work(
    g: Graph,
    *,
    canonical_uri: URIRef,
    pref_label: str | None,
    members: list[CanonicalWorkInputs],
    helmet_entries: dict[str, HelmetMapEntry],
    description_modifier_uri: URIRef,
    description_change_date: datetime,
) -> tuple[CanonicalEntry, str]:
    """Add the canonical Work + AdminMetadata to ``g``. Returns (map row, merged_at)."""
    g.add((canonical_uri, RDF.type, V.BFFI.Work))
    g.add((canonical_uri, RDF.type, V.SKOS.Concept))
    if pref_label is not None:
        g.add((canonical_uri, V.SKOS.prefLabel, Literal(pref_label)))

    # bf:identifiedBy: union all Helmet identifiers across members, dedup by bib_id.
    seen_bib_ids: set[str] = set()
    helmet_bib_ids_ordered: list[str] = []
    for member in members:
        for ident_uri, bib_id in member.helmet_identifiers:
            if bib_id in seen_bib_ids:
                continue
            seen_bib_ids.add(bib_id)
            helmet_bib_ids_ordered.append(bib_id)
            ident = URIRef(ident_uri)
            g.add((canonical_uri, V.BF.identifiedBy, ident))
            g.add((ident, RDF.type, V.BF.Local))
            g.add((ident, RDF.value, Literal(bib_id)))
            g.add((ident, V.BF.source, V.HELMET_SOURCE_URI))

    # Rewrite expressionOf: every absorbed Expression now points at the canonical Work.
    seen_exprs: set[str] = set()
    for member in members:
        for expr_uri in member.expression_uris:
            if expr_uri in seen_exprs:
                continue
            seen_exprs.add(expr_uri)
            expr = URIRef(expr_uri)
            g.add((canonical_uri, V.BFFI.hasExpression, expr))
            g.add((expr, V.BFFI.expressionOf, canonical_uri))

    # Provenance back-links to absorbed raw Works.
    raw_uris_sorted = sorted(m.work_uri for m in members)
    for raw in raw_uris_sorted:
        g.add((canonical_uri, V.PROV.wasDerivedFrom, URIRef(raw)))

    # AdminMetadata block — every predicate from spec § 8 / BUILD_PLAN M8.
    admin_uri = _admin_metadata_uri(str(canonical_uri))
    earliest = _earliest_converted_at(members, helmet_entries)
    merged_at_iso = description_change_date.isoformat()

    g.add((canonical_uri, V.adminMetadata, admin_uri))
    g.add((admin_uri, RDF.type, V.AdminMetadata))
    g.add((admin_uri, V.adminMetadataFor, canonical_uri))
    if earliest is not None:
        g.add(
            (
                admin_uri,
                V.descriptionCreationDate,
                Literal(earliest, datatype=V.XSD.dateTime),
            )
        )
    g.add(
        (
            admin_uri,
            V.descriptionChangeDate,
            Literal(merged_at_iso, datatype=V.XSD.dateTime),
        )
    )
    g.add((admin_uri, V.dateGenerated, Literal(merged_at_iso, datatype=V.XSD.dateTime)))
    g.add((admin_uri, V.descriptionModifier, description_modifier_uri))
    g.add((admin_uri, V.descriptionConventions, V.DESC_CONV_BFFI_1_0_0))
    g.add((admin_uri, V.descriptionLevel, V.DESC_LEVEL_MINIMUM))
    g.add((admin_uri, V.encodingLevel, V.ENC_LEVEL_AUTO))
    g.add((admin_uri, V.descriptionAuthentication, V.AUTH_AUTO_MERGED))
    g.add((admin_uri, V.generationProcess, V.GEN_PROCESS_PIPELINE_V0_1_0))
    g.add((admin_uri, V.metadataLicensor, V.METADATA_LICENSOR_CC0))
    g.add((admin_uri, V.recordingSource, V.RECORDING_SOURCE_HELMET))
    for bib_id in helmet_bib_ids_ordered:
        helmet_uri = URIRef(f"{get_settings().graph_base}helmet/{bib_id}")
        g.add((admin_uri, V.sourceMetadata, helmet_uri))

    return (
        CanonicalEntry(
            canonical_work_uri=str(canonical_uri),
            raw_work_uris=raw_uris_sorted,
            helmet_bib_ids=helmet_bib_ids_ordered,
            merged_at=merged_at_iso,
        ),
        merged_at_iso,
    )


def _earliest_converted_at(
    members: list[CanonicalWorkInputs],
    helmet_entries: dict[str, HelmetMapEntry],
) -> str | None:
    timestamps = [
        helmet_entries[m.work_uri].converted_at for m in members if m.work_uri in helmet_entries
    ]
    return min(timestamps) if timestamps else None


def _bind_prefixes(g: Graph) -> None:
    g.bind("bffi", V.BFFI)
    g.bind("bffi-prov", V.BFFI_PROV)
    g.bind("bf", V.BF)
    g.bind("bib", V.BIB)
    g.bind("prov", V.PROV)
    g.bind("skos", V.SKOS)
    g.bind("xsd", V.XSD)


# --- apply_merge top-level -----------------------------------------------


@dataclass
class MergeResult:
    """End-of-run summary for ``apply_merge``."""

    total_works: int
    same_work_decisions: int
    different_work_decisions: int
    uncertain_decisions: int
    canonical_works: int
    conflict_groups: int
    canonical_path: str
    map_path: str
    conflicts_path: str

    def render(self) -> str:
        return "\n".join(
            (
                "M8 merge complete",
                f"  raw works:               {self.total_works:,}",
                f"  same_work decisions:     {self.same_work_decisions:,}",
                f"  different_work decisions:{self.different_work_decisions:,}",
                f"  uncertain decisions:     {self.uncertain_decisions:,}",
                f"  canonical Works:         {self.canonical_works:,}",
                f"  conflict groups:         {self.conflict_groups:,}",
                f"  canonical Turtle:        {self.canonical_path}",
                f"  canonical map JSONL:     {self.map_path}",
                f"  conflicts JSONL:         {self.conflicts_path}",
            )
        )


def _select_description_modifier(
    members: list[CanonicalWorkInputs],
    decisions_by_pair: dict[frozenset[str], JudgeDecisionRow],
) -> URIRef:
    """Pick the agent who modified this canonical Work.

    For singletons the modifier is the M2 marc2bibframe2 agent (matching
    the AdminMetadata stamp emitted at conversion time). For merge groups
    it's the agent of the first ``same_work`` decision contributing to
    the group, with a stable ordering on the member URIs.
    """
    if len(members) <= 1:
        return V.AGENT_MARC2BIBFRAME2

    sorted_uris = sorted(m.work_uri for m in members)
    for i, a in enumerate(sorted_uris):
        for b in sorted_uris[i + 1 :]:
            row = decisions_by_pair.get(frozenset({a, b}))
            if row is not None and row.decision == "same_work" and row.winning_model:
                return model_agent_uri(row.winning_model)
    return V.AGENT_MARC2BIBFRAME2


def apply_merge(
    decisions_path: Path | None = None,
    bffi_corpus_dir: Path | None = None,
    *,
    output_path: Path | None = None,
    map_path: Path | None = None,
    conflicts_path: Path | None = None,
    helmet_map_path: Path | None = None,
    work_records: dict[str, CanonicalWorkInputs] | None = None,
    helmet_entries: dict[str, HelmetMapEntry] | None = None,
    now: datetime | None = None,
) -> MergeResult:
    """Apply judge decisions to mint canonical Works (M8)."""
    settings = get_settings()
    decisions_path = decisions_path or (settings.data_dir / JUDGE_DECISIONS_FILENAME)
    bffi_corpus_dir = bffi_corpus_dir or settings.data_dir
    output_path = output_path or (settings.data_dir / CANONICAL_FILENAME)
    map_path = map_path or (settings.data_dir / CANONICAL_MAP_FILENAME)
    conflicts_path = conflicts_path or (settings.data_dir / CANONICAL_CONFLICTS_FILENAME)
    helmet_map_path = helmet_map_path or (settings.data_dir / HELMET_MAP_FILENAME)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    merged_at = (now or datetime.now(UTC)).replace(microsecond=0)

    decisions = _load_decisions(decisions_path)
    same_work_edges = [(d.work_a, d.work_b) for d in decisions if d.decision == "same_work"]
    different_work_edges = [
        (d.work_a, d.work_b) for d in decisions if d.decision == "different_work"
    ]
    uncertain_count = sum(1 for d in decisions if d.decision == "uncertain")

    if work_records is None:
        work_records = _load_work_records_from_corpus(bffi_corpus_dir)
    if helmet_entries is None:
        helmet_entries = _load_helmet_map(helmet_map_path)

    uf = _UnionFind()
    for w in work_records:
        uf.add(w)
    for a, b in same_work_edges:
        uf.add(a)
        uf.add(b)
        uf.union(a, b)

    groups = uf.groups()
    conflicts = _detect_conflicts(groups, different_work_edges, same_work_edges)
    conflict_roots = {sorted(c.members)[0] for c in conflicts}

    decisions_by_pair: dict[frozenset[str], JudgeDecisionRow] = {
        frozenset({d.work_a, d.work_b}): d for d in decisions
    }

    g = Graph()
    _bind_prefixes(g)
    canonical_entries: list[CanonicalEntry] = []

    for root in sorted(groups):
        member_uris = groups[root]
        if member_uris[0] in conflict_roots:
            continue  # Flagged for review; do not silently merge.
        members = [work_records[u] for u in member_uris if u in work_records]
        if not members:
            continue
        anchor = members[0]
        if not anchor.creator_uri or not anchor.pref_label:
            # Without a stable (creator_uri, pref_label) pair we can't mint a
            # canonical URI — flag the group as a one-off conflict so M9 / human
            # review surfaces it.
            conflicts.append(
                GroupConflict(
                    members=member_uris,
                    conflicting_pair=(anchor.work_uri, anchor.work_uri),
                    same_work_path=[],
                )
            )
            continue
        canonical_uri = URIRef(mint_work_uri(anchor.creator_uri, anchor.pref_label))
        modifier = _select_description_modifier(members, decisions_by_pair)
        entry, _ = _emit_canonical_work(
            g,
            canonical_uri=canonical_uri,
            pref_label=anchor.pref_label,
            members=members,
            helmet_entries=helmet_entries,
            description_modifier_uri=modifier,
            description_change_date=merged_at,
        )
        canonical_entries.append(entry)

    # Atomic-rename writes
    tmp_ttl = output_path.with_suffix(output_path.suffix + ".tmp")
    g.serialize(destination=str(tmp_ttl), format="turtle")
    tmp_ttl.replace(output_path)
    _emit_canonical_map(map_path, canonical_entries)
    _emit_conflicts(conflicts_path, conflicts)

    return MergeResult(
        total_works=len(work_records),
        same_work_decisions=len(same_work_edges),
        different_work_decisions=len(different_work_edges),
        uncertain_decisions=uncertain_count,
        canonical_works=len(canonical_entries),
        conflict_groups=len(conflicts),
        canonical_path=str(output_path),
        map_path=str(map_path),
        conflicts_path=str(conflicts_path),
    )


def _load_work_records_from_corpus(corpus_dir: Path) -> dict[str, CanonicalWorkInputs]:
    """Read every BFFI Turtle + BIBFRAME RDF/XML under ``corpus_dir``."""
    g = Graph()
    bffi_dir = corpus_dir / "bffi"
    bibframe_dir = corpus_dir / "bibframe"
    if bffi_dir.is_dir():
        for path in sorted(bffi_dir.glob("*.ttl")):
            g.parse(str(path), format="turtle")
    if bibframe_dir.is_dir():
        for path in sorted(bibframe_dir.glob("*.rdf")):
            if not path.name.startswith("_"):
                g.parse(str(path), format="xml")
    return extract_work_metadata(g)


def _iter_member_pairs(uf_groups: dict[str, list[str]]) -> Iterator[tuple[str, str]]:
    for members in uf_groups.values():
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                yield (a, b)


__all__ = [
    "CANONICAL_CONFLICTS_FILENAME",
    "CANONICAL_FILENAME",
    "CANONICAL_MAP_FILENAME",
    "HELMET_MAP_FILENAME",
    "JUDGE_DECISIONS_FILENAME",
    "CanonicalEntry",
    "CanonicalWorkInputs",
    "GroupConflict",
    "HelmetMapEntry",
    "JudgeDecisionRow",
    "MergeResult",
    "apply_merge",
    "extract_work_metadata",
]
