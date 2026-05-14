"""Stage M8: merge application — union-find on judge decisions, canonical Works.

Reads M6's ``judge-decisions.jsonl`` and the M3 BFFI Turtle, applies
union-find over ``same_work`` decisions, mints canonical Work URIs
via :func:`bffi_pipeline.uris.mint_work_uri`, unions the
``bf:identifiedBy`` sets so the canonical Work carries one
identifier per absorbed Helmet record, rewrites
``bffi:expressionOf`` to point at the canonical URI, and emits one
``bffi:AdminMetadata`` block per canonical Work per spec § 8.

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

from rdflib import BNode, Graph, Literal, URIRef
from rdflib import Literal as RdfLiteral
from rdflib.namespace import DCTERMS, RDF
from rdflib.term import Node

from bffi_pipeline.config import get_settings
from bffi_pipeline.contrib_variants import (
    DEFAULT_SIDECAR_NAME as VARIANTS_SIDECAR_NAME,
)
from bffi_pipeline.contrib_variants import (
    load_variant_claims,
)
from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.provenance.logger import model_agent_uri
from bffi_pipeline.stages.observability import emit_if_active
from bffi_pipeline.uris import mint_work_uri

# --- Constants ------------------------------------------------------------

CANONICAL_FILENAME: Final[str] = "canonical.ttl"
CANONICAL_MAP_FILENAME: Final[str] = "canonical-map.jsonl"
CANONICAL_CONFLICTS_FILENAME: Final[str] = "canonical-conflicts.jsonl"
JUDGE_DECISIONS_FILENAME: Final[str] = "judge-decisions.jsonl"
HELMET_MAP_FILENAME: Final[str] = "helmet-map.jsonl"

#: P-12 Phase D cadence for M8 progress events. Emitted once per N
#: canonical groups processed so the exporter's throughput derivation
#: + dashboard ETA reflect real progress through the per-group mint
#: loop. ~200 emissions across a full 800k-record canonical-group
#: walk feels responsive without saturating the JSONL sidecar.
_M8_PROGRESS_CADENCE: Final[int] = 500


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


@dataclass(frozen=True)
class SubjectTarget:
    """One ``bffi:subject`` or ``bffi:genreForm`` target.

    Either pre-resolved (``uri`` set, label/source unused) — the
    cataloguer supplied a ``$0`` authority URI in MARC 6XX; or
    unresolved (``uri`` None, ``label`` set) — the canonical Work
    carries a blank-node target with an ``rdfs:label`` from MARC
    ``$a`` and a ``bf:source`` literal (e.g. ``"yso/fin"``).

    M9 phase 3 walks the unresolved subjects and binds them to
    authority URIs via Finto. The resolved-URI case is left alone.
    """

    uri: str | None = None
    label: str | None = None
    source: str | None = None

    @property
    def is_resolved(self) -> bool:
        """True iff this target is pre-resolved to an external authority
        URI with no label / source carried locally — the cataloguer
        supplied MARC 6XX ``$0`` and M10's Skosmos resolves the label
        from the loaded authority graph (option 3b). Targets that have
        a URI *and* carry their own label / source (the local
        marc2bibframe2-minted ``Place651-37`` style — MARC ``$2 ysa``
        without ``$0``) report ``False`` here so they go through the
        reconciliation path even though they already have a URI."""
        return self.uri is not None and self.label is None and self.source is None


@dataclass(frozen=True)
class ContributionTarget:
    """One ``bffi:PrimaryContribution`` to propagate to the canonical Work.

    M9's creator reconciliation walks ``<canonical> bffi:contribution
    [a bffi:PrimaryContribution; bffi:agent <uri>]`` and reads the
    agent's ``rdfs:label``. Without these triples on the canonical, M9
    has nothing to reconcile. M8 propagates them from the anchor raw
    Work.

    Only ``PrimaryContribution`` is propagated here — other
    contributions (translators, illustrators, contained-work creators)
    are left on the raw Works for downstream consumers that care.
    """

    agent_uri: str
    agent_label: str


@dataclass(frozen=True)
class ExpressionContribution:
    """One non-primary ``bffi:Contribution`` block from a raw Expression.

    Captures everything M8 needs to re-emit the block on the
    corresponding canonical Expression. Role and agent each have two
    expression forms in real Helmet data and the dataclass carries
    both:

    - **Role**: ``role_uri`` for canonical MARC relator URIs (e.g.
      ``http://id.loc.gov/vocabulary/relators/cnd`` — emitted by the
      M3 contributor-extraction cascade); ``role_label`` for the
      cataloguer's free-text $e form that marc2bibframe2 emits as a
      blank-node ``bf:Role`` with ``rdfs:label`` (e.g. "johtaja",
      "cembalo", "urut" — Finnish text the cataloguer supplied).
    - **Agent**: ``agent_uri`` for marc2bibframe2-minted local URIs
      like ``http://urn.fi/.../#Agent700-24``; ``agent_label`` for
      the M3 cascade's blank-node-with-label form (LLM-extracted
      names not yet reconciled).
    """

    expression_uri: str
    role_uri: str | None = None
    role_label: str | None = None
    agent_uri: str | None = None
    agent_label: str | None = None


@dataclass
class CanonicalWorkInputs:
    """Per-Work data the merge step needs to mint a canonical Work URI."""

    work_uri: str
    creator_uri: str | None
    pref_label: str | None
    """A single prefLabel string used as the deterministic anchor for the
    canonical Work URI mint. The full multi-language set lives in
    :attr:`pref_labels` and is what the canonical Work surfaces to Skosmos."""
    expression_uris: list[str] = field(default_factory=list)
    helmet_identifiers: list[tuple[str, str]] = field(default_factory=list)
    """List of (identifier_uri, helmet_bib_id) pairs as found on bf:identifiedBy."""
    subject_targets: list[SubjectTarget] = field(default_factory=list)
    """``bffi:subject`` targets; M9 phase 3 reconciles unresolved ones."""
    genre_form_targets: list[SubjectTarget] = field(default_factory=list)
    """``bffi:genreForm`` targets; M9 phase 3 reconciles unresolved ones."""
    contribution_targets: list[ContributionTarget] = field(default_factory=list)
    """``bffi:PrimaryContribution`` blocks; M9 reconciles their agents against KANTO/VIAF."""
    expression_labels: list[tuple[str, str, str | None]] = field(default_factory=list)
    """List of (expression_uri, label_text, lang) tuples to propagate onto canonical
    Expressions so Skosmos surfaces them with prefLabels."""
    pref_labels: list[tuple[str, str | None]] = field(default_factory=list)
    """List of (label_text, lang) tuples for *all* skos:prefLabel literals
    on the raw Work. M3's title-language cascade emits one per parallel
    title (e.g. en/fi/ru on the Tšarka pattern); M8 unions these across
    absorbed members and propagates the full set onto the canonical Work
    so Skosmos picks the right per-language label."""
    expression_contributions: list[ExpressionContribution] = field(default_factory=list)
    """Non-primary ``bffi:Contribution`` blocks attached to the raw
    Expressions linked from this Work. Includes both
    cataloguer-supplied 700-fielded contributions (URI agents) and the
    M3 contributor-extraction cascade's blank-node-agent emissions.
    M8 re-emits each on the corresponding canonical Expression with
    deterministic blank-node IDs so Skosmos's canonical-Work view
    surfaces them — without this propagation the cascade's output is
    visible only on per-bib-ID raw Expression pages."""


def _first_pref_label(graph: Graph, subject: URIRef) -> str | None:
    for o in graph.objects(subject, V.SKOS.prefLabel):
        if isinstance(o, RdfLiteral):
            return str(o)
    return None


def _all_pref_labels(graph: Graph, subject: URIRef) -> list[tuple[str, str | None]]:
    """Return the full set of ``(text, lang)`` prefLabels on ``subject``,
    sorted deterministically so re-runs of M8 produce byte-identical
    canonical.ttl. Untagged literals (``lang is None``) sort before any
    language-tagged literal to keep the key total-order safe."""
    return sorted(
        (
            (str(o), o.language)
            for o in graph.objects(subject, V.SKOS.prefLabel)
            if isinstance(o, RdfLiteral)
        ),
        key=lambda t: (t[1] or "", t[0]),
    )


def _primary_agent_uri(graph: Graph, work: URIRef) -> str | None:
    for contrib in graph.objects(work, V.BFFI.contribution):
        if V.BFFI.PrimaryContribution not in set(graph.objects(contrib, RDF.type)):
            continue
        for ag in graph.objects(contrib, V.BFFI.agent):
            if isinstance(ag, URIRef):
                return str(ag)
    return None


def _primary_contribution_targets(graph: Graph, work: URIRef) -> list[ContributionTarget]:
    """Collect ``PrimaryContribution → agent → rdfs:label`` triples on ``work``.

    Deduplicates by ``agent_uri`` because marc2bibframe2's MARC-100 lift
    can emit the same primary contribution N times for an aggregate
    record (one per included Work). One propagated contribution is
    enough for M9 reconciliation.

    Returns an empty list when the Work has no PrimaryContribution with
    a URI agent and rdfs:label — those records are surfaced as M8
    conflicts and don't reach M9 anyway.
    """
    out: list[ContributionTarget] = []
    seen: set[str] = set()
    for contrib in graph.objects(work, V.BFFI.contribution):
        if V.BFFI.PrimaryContribution not in set(graph.objects(contrib, RDF.type)):
            continue
        for agent in graph.objects(contrib, V.BFFI.agent):
            if not isinstance(agent, URIRef):
                continue
            agent_uri = str(agent)
            if agent_uri in seen:
                continue
            label: str | None = None
            for lab in graph.objects(agent, V.RDFS.label):
                if isinstance(lab, RdfLiteral):
                    label = str(lab)
                    break
            if label is None:
                continue
            seen.add(agent_uri)
            out.append(ContributionTarget(agent_uri=agent_uri, agent_label=label))
    return out


def _helmet_identifiers(graph: Graph, work: URIRef) -> list[tuple[str, str]]:
    """Return [(ident_uri, helmet_bib_id), ...] for ``work``'s Helmet identifiers.

    M2 mints the BIBFRAME `bf:identifiedBy` object as a blank node;
    M3's SPARQL CONSTRUCT preserves it as a blank node into the BFFI
    graph. Blank nodes can't be re-emitted as URIs on the canonical
    Work because rdflib's bnode IDs aren't stable across processes.
    For blank-node identifiers, mint a deterministic URI from the
    bib_id (``<graph_base>ident/helmet/<bib_id>``) so M8 produces a
    byte-stable canonical.ttl across runs.
    """
    out: list[tuple[str, str]] = []
    for ident in graph.objects(work, V.BF.identifiedBy):
        sources = set(graph.objects(ident, V.BF.source))
        if V.HELMET_SOURCE_URI not in sources:
            continue
        for value in graph.objects(ident, RDF.value):
            if not isinstance(value, RdfLiteral):
                continue
            bib_id = str(value)
            ident_uri = (
                str(ident)
                if isinstance(ident, URIRef)
                else f"{get_settings().graph_base}ident/helmet/{bib_id}"
            )
            out.append((ident_uri, bib_id))
            break
    return out


def _read_subject_label_and_source(graph: Graph, target: Node) -> tuple[str | None, str | None]:
    """Pull ``rdfs:label`` and ``bf:source`` off a subject target.

    Both shapes coexist in real data. ``rdfs:label`` is always a literal.
    ``bf:source`` is a URIRef (`<…/subjectSchemes/ysa>`) on
    marc2bibframe2's lift of MARC ``$2 ysa`` time-period and place
    fields, but a literal (`"yso/fin"`) on the lift of MARC ``$2
    yso/fin`` topical fields. We accept either, returning the URI form
    as a string so the source-classification helper downstream can
    pattern-match against either shape uniformly.
    """
    label: str | None = None
    for lab in graph.objects(target, V.RDFS.label):
        if isinstance(lab, RdfLiteral):
            label = str(lab)
            break
    source: str | None = None
    for src in graph.objects(target, V.BF.source):
        if isinstance(src, URIRef):
            source = str(src)
            break
        if isinstance(src, RdfLiteral):
            source = str(src)
            break
    return label, source


def _subject_targets(graph: Graph, work: URIRef, predicate: URIRef) -> list[SubjectTarget]:
    """Walk ``work``'s ``bffi:subject`` / ``bffi:genreForm`` targets.

    Returns a :class:`SubjectTarget` per object. Three shapes coexist
    in production data:

    1. **Pre-resolved authority URI** (cataloguer supplied MARC ``$0``):
       a URIRef in a public namespace like ``yso/`` or ``slm:``, with
       no ``rdfs:label`` / ``bf:source`` of its own. Just propagate
       the URI; M10's Skosmos resolves the label from the loaded
       authority graph.
    2. **Local marc2bibframe2-minted URI** (e.g. ``Place651-37`` for a
       MARC 651 ``$2 ysa``): a URIRef in the per-record ``bib:raw/``
       namespace, carrying its own ``rdfs:label`` ("Venäjä") and
       ``bf:source`` (``<…/subjectSchemes/ysa>``) in the source
       BIBFRAME. Capture all three so M9 can reconcile.
    3. **Blank-node target** (the M3 SPARQL CONSTRUCT also
       occasionally emits these for fields without ``$0`` or local
       URI minting): label + source on the blank node, no URI.

    Targets we can't classify (no URI and no label) are skipped.
    """
    out: list[SubjectTarget] = []
    for obj in graph.objects(work, predicate):
        label, source = _read_subject_label_and_source(graph, obj)
        if isinstance(obj, URIRef):
            uri = str(obj)
            # Cases 1 + 2: URIRef target. If label / source are
            # present (case 2: local YSA-style), carry them through
            # so M9 can reconcile against a Finto authority. Without
            # them (case 1: cataloguer-resolved YSO URI), just the
            # URI.
            out.append(SubjectTarget(uri=uri, label=label, source=source))
            continue
        # Case 3: blank node. Need at least a label or source to be useful.
        if label is None and source is None:
            continue
        out.append(SubjectTarget(label=label, source=source))
    return out


def _expression_labels(graph: Graph, work: URIRef) -> list[tuple[str, str, str | None]]:
    """Collect ``skos:prefLabel`` literals on each ``bffi:hasExpression`` target.

    Returns ``[(expression_uri, label_text, lang), ...]``; multiple labels
    per Expression (different languages) produce multiple entries. M8
    re-asserts these on the canonical so Skosmos's UI shows labelled
    Expressions in the Work → Expression hierarchy view.
    """
    out: list[tuple[str, str, str | None]] = []
    for expr in graph.objects(work, V.BFFI.hasExpression):
        if not isinstance(expr, URIRef):
            continue
        for label in graph.objects(expr, V.SKOS.prefLabel):
            if isinstance(label, RdfLiteral):
                out.append((str(expr), str(label), label.language))
    return out


def _read_role(graph: Graph, contrib: Node) -> tuple[str | None, str | None]:
    """Return ``(role_uri, role_label)`` from a contribution's ``bf:role``.

    Two shapes coexist: a controlled-vocabulary URI (M3 cascade emits
    these as ``<relators/cnd>`` etc.); a blank node typed ``bf:Role``
    with an ``rdfs:label`` (marc2bibframe2's lift of MARC $e free-text).
    URI form takes precedence; the first one wins.
    """
    for role in graph.objects(contrib, V.BF.role):
        if isinstance(role, URIRef):
            return str(role), None
        for lab in graph.objects(role, V.RDFS.label):
            if isinstance(lab, RdfLiteral):
                return None, str(lab)
    return None, None


def _read_agent(graph: Graph, contrib: Node) -> tuple[str | None, str | None]:
    """Return ``(agent_uri, agent_label)`` from a contribution's ``bffi:agent``."""
    for agent in graph.objects(contrib, V.BFFI.agent):
        agent_uri = str(agent) if isinstance(agent, URIRef) else None
        agent_label: str | None = None
        for lab in graph.objects(agent, V.RDFS.label):
            if isinstance(lab, RdfLiteral):
                agent_label = str(lab)
                break
        return agent_uri, agent_label
    return None, None


def _expression_contributions(graph: Graph, work: URIRef) -> list[ExpressionContribution]:
    """Collect non-primary ``bffi:Contribution`` blocks on each Expression
    linked to ``work`` via ``bffi:hasExpression``.

    For each: extracts the ``bf:role`` (URI or blank-node-with-label)
    and the agent (URI or blank-node-with-label). A contribution
    typed as ``bffi:PrimaryContribution`` is filtered out — those go
    on the canonical Work via :func:`_propagate_primary_contributions`.
    Contributions whose agent carries neither a URI nor a label are
    dropped (no information to propagate).
    """
    out: list[ExpressionContribution] = []
    for expr in graph.objects(work, V.BFFI.hasExpression):
        if not isinstance(expr, URIRef):
            continue
        for contrib in graph.objects(expr, V.BFFI.contribution):
            if V.BFFI.PrimaryContribution in set(graph.objects(contrib, RDF.type)):
                continue
            role_uri, role_label = _read_role(graph, contrib)
            agent_uri, agent_label = _read_agent(graph, contrib)
            if agent_uri is None and agent_label is None:
                continue
            out.append(
                ExpressionContribution(
                    expression_uri=str(expr),
                    role_uri=role_uri,
                    role_label=role_label,
                    agent_uri=agent_uri,
                    agent_label=agent_label,
                )
            )
    return out


def extract_work_metadata(graph: Graph) -> dict[str, CanonicalWorkInputs]:
    """Walk the combined BFFI + BIBFRAME graph and return per-Work merge inputs."""
    out: dict[str, CanonicalWorkInputs] = {}
    for work in graph.subjects(RDF.type, V.BFFI.Work):
        if not isinstance(work, URIRef):
            continue
        subjects = _subject_targets(graph, work, V.BFFI.subject)
        genres = _subject_targets(graph, work, V.BFFI.genreForm)
        contributions = _primary_contribution_targets(graph, work)
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
            subject_targets=subjects,
            genre_form_targets=genres,
            contribution_targets=contributions,
            expression_labels=_expression_labels(graph, work),
            pref_labels=_all_pref_labels(graph, work),
            expression_contributions=_expression_contributions(graph, work),
        )
    return out


# --- helmet-map.jsonl loader ---------------------------------------------


@dataclass(frozen=True)
class HelmetMapEntry:
    """One row from M2's ``helmet-map.jsonl``.

    Joins a raw Work URI to its source Helmet bib_id and the M2
    conversion timestamp; M8 reads this to seed
    ``bffi:descriptionCreationDate`` on each canonical Work.
    """

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
    """One row of ``canonical-map.jsonl``.

    Captures the canonical Work URI plus the raw Works and Helmet
    bib_ids it absorbed at merge time. Joined with ``helmet-map.jsonl``
    this gives O(1) Helmet bib_id → canonical Work URI lookup.
    """

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


def _propagate_subject_targets(
    g: Graph,
    *,
    canonical_uri: URIRef,
    members: list[CanonicalWorkInputs],
    predicate: URIRef,
    attr: LiteralType["subject_targets", "genre_form_targets"],
) -> None:
    """Emit ``predicate`` triples on ``canonical_uri`` from each member's targets.

    Three target shapes (see :class:`SubjectTarget` docstring):

    1. **Pre-resolved authority URI**: emit ``<canonical> predicate <uri>``;
       no further triples on the URI (Skosmos resolves labels from the
       loaded authority graph). Dedup across members by URI.
    2. **Local marc2bibframe2-minted URI** (URI present, label/source
       carried locally — MARC ``$2 ysa`` time/place fields):
       propagate the URI AND re-emit its ``rdfs:label`` + ``bf:source``
       so M9 has the metadata to reconcile against. Dedup by URI.
    3. **Blank-node target**: mint a deterministic blank node, dedup
       by ``(label, source)`` so the same cataloguer subject string
       from N raw Works produces one blank node on the canonical;
       M9 phase 3 reconciles each once.
    """
    seen_uris: set[str] = set()
    seen_blank_keys: set[tuple[str | None, str | None]] = set()
    blank_targets: list[SubjectTarget] = []
    for member in members:
        for target in getattr(member, attr):
            if target.uri is not None:
                if target.uri in seen_uris:
                    continue
                seen_uris.add(target.uri)
                uri_node = URIRef(target.uri)
                g.add((canonical_uri, predicate, uri_node))
                # Case 2: the URI carries its own label / source. Copy
                # them onto the canonical so M9 can reconcile (and
                # Skosmos has fallback labels for the local URI even
                # when no authority binding lands).
                if target.label is not None:
                    g.add((uri_node, V.RDFS.label, Literal(target.label)))
                if target.source is not None:
                    if target.source.startswith(("http://", "https://")):
                        g.add((uri_node, V.BF.source, URIRef(target.source)))
                    else:
                        g.add((uri_node, V.BF.source, Literal(target.source)))
                continue
            key = (target.label, target.source)
            if key in seen_blank_keys:
                continue
            seen_blank_keys.add(key)
            blank_targets.append(target)
    # Order blank-node emission deterministically AND mint stable BNode
    # identifiers from a hash of (canonical, predicate, label, source) so
    # canonical.ttl is byte-stable across runs. rdflib's default
    # BNode() uses a process-local counter, which would otherwise leak
    # non-determinism into the serialised file.
    blank_targets.sort(key=lambda t: (t.label or "", t.source or ""))
    predicate_str = str(predicate)
    for target in blank_targets:
        digest = hashlib.sha1(
            "|".join(
                (
                    str(canonical_uri),
                    predicate_str,
                    target.label or "",
                    target.source or "",
                )
            ).encode("utf-8")
        ).hexdigest()
        node = BNode(f"sub{digest}")
        g.add((canonical_uri, predicate, node))
        if target.label is not None:
            g.add((node, V.RDFS.label, Literal(target.label)))
        if target.source is not None:
            g.add((node, V.BF.source, Literal(target.source)))


def _propagate_expressions(
    g: Graph,
    *,
    canonical_uri: URIRef,
    members: list[CanonicalWorkInputs],
) -> None:
    """Re-assert Expression typing + ``bffi:hasExpression`` /
    ``expressionOf`` + prefLabel + non-primary Contribution blocks.

    The typing and link triples make M10's Skosify dual-type
    Expressions as ``skos:Concept``. The prefLabel literal is what
    Skosmos surfaces in the Work → Expression hierarchy view; without
    it the UI renders Expressions with empty labels.

    Non-primary contributions (M3 cascade-emitted + 700-fielded
    translators / illustrators / performers) are dedup'd across
    members by ``(expr_uri, agent, role)`` and re-emitted on the
    canonical Expression with deterministic SHA-1 blank-node IDs so
    canonical.ttl stays byte-stable across re-runs.
    """
    seen_exprs: set[str] = set()
    seen_labels: set[tuple[str, str, str | None]] = set()
    seen_contribs: set[tuple[str, str, str, str, str]] = set()
    for member in members:
        for expr_uri in member.expression_uris:
            if expr_uri in seen_exprs:
                continue
            seen_exprs.add(expr_uri)
            expr = URIRef(expr_uri)
            g.add((expr, RDF.type, V.BFFI.Expression))
            g.add((canonical_uri, V.BFFI.hasExpression, expr))
            g.add((expr, V.BFFI.expressionOf, canonical_uri))
        for expr_uri, label_text, lang in member.expression_labels:
            key = (expr_uri, label_text, lang)
            if key in seen_labels:
                continue
            seen_labels.add(key)
            literal = Literal(label_text, lang=lang) if lang else Literal(label_text)
            g.add((URIRef(expr_uri), V.SKOS.prefLabel, literal))
        for ec in member.expression_contributions:
            key_t = (
                ec.expression_uri,
                ec.agent_uri or "",
                ec.agent_label or "",
                ec.role_uri or "",
                ec.role_label or "",
            )
            if key_t in seen_contribs:
                continue
            seen_contribs.add(key_t)
            digest = hashlib.sha1("|".join(key_t).encode("utf-8")).hexdigest()
            contrib_node = BNode(f"econ{digest}")
            expr = URIRef(ec.expression_uri)
            g.add((expr, V.BFFI.contribution, contrib_node))
            g.add((contrib_node, RDF.type, V.BFFI.Contribution))
            if ec.role_uri is not None:
                g.add((contrib_node, V.BF.role, URIRef(ec.role_uri)))
            elif ec.role_label is not None:
                # Free-text role from the cataloguer's $e ("johtaja" /
                # "cembalo" / etc.) — re-emit the marc2bibframe2 shape
                # `bf:role [a bf:Role; rdfs:label "..."]` so Skosmos
                # surfaces the cataloguer-supplied role text alongside
                # any controlled-vocabulary URIs other contributions
                # carry.
                role_node = BNode(f"erol{digest}")
                g.add((contrib_node, V.BF.role, role_node))
                g.add((role_node, RDF.type, V.BF.Role))
                g.add((role_node, V.RDFS.label, Literal(ec.role_label)))
            agent_node: URIRef | BNode
            if ec.agent_uri is not None:
                agent_node = URIRef(ec.agent_uri)
            else:
                agent_node = BNode(f"eag{digest}")
                g.add((agent_node, RDF.type, V.BFFI.Agent))
            g.add((contrib_node, V.BFFI.agent, agent_node))
            if ec.agent_label is not None:
                g.add((agent_node, V.RDFS.label, Literal(ec.agent_label)))


def _propagate_primary_contributions(
    g: Graph,
    *,
    canonical_uri: URIRef,
    members: list[CanonicalWorkInputs],
) -> None:
    """Emit one ``PrimaryContribution → agent → rdfs:label`` block per absorbed agent.

    Deduplicates by ``agent_uri`` across all absorbed members so a
    multi-Work merge group produces one contribution per distinct
    creator. The blank-node identifier is derived from a SHA-1 of
    ``(canonical_uri, agent_uri)`` so canonical.ttl stays byte-stable
    across runs (matching the determinism rule the subject-propagation
    block follows).

    The agent's ``rdfs:label`` is re-asserted on the canonical so the
    M9 walker doesn't need to reach back into per-record BFFI Turtles
    to resolve labels.
    """
    seen_agents: set[str] = set()
    flat: list[ContributionTarget] = []
    for member in members:
        for target in member.contribution_targets:
            if target.agent_uri in seen_agents:
                continue
            seen_agents.add(target.agent_uri)
            flat.append(target)
    flat.sort(key=lambda t: t.agent_uri)
    for target in flat:
        digest = hashlib.sha1(f"{canonical_uri}|{target.agent_uri}".encode()).hexdigest()
        contrib = BNode(f"con{digest}")
        agent = URIRef(target.agent_uri)
        g.add((canonical_uri, V.BFFI.contribution, contrib))
        g.add((contrib, RDF.type, V.BFFI.PrimaryContribution))
        g.add((contrib, V.BFFI.agent, agent))
        g.add((agent, V.RDFS.label, Literal(target.agent_label)))


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
    union_pref_labels: set[tuple[str, str | None]] = set()
    for member in members:
        union_pref_labels.update(member.pref_labels)
    if union_pref_labels:
        for text, lang in sorted(union_pref_labels, key=lambda t: (t[1] or "", t[0])):
            literal = Literal(text, lang=lang) if lang else Literal(text)
            g.add((canonical_uri, V.SKOS.prefLabel, literal))
    elif pref_label is not None:
        # Fallback for synthetic test fixtures that don't populate
        # ``pref_labels`` — production always does, via ``_all_pref_labels``.
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
            g.add((canonical_uri, DCTERMS.identifier, Literal(bib_id)))

    _propagate_expressions(g, canonical_uri=canonical_uri, members=members)

    # Provenance back-links to absorbed raw Works.
    raw_uris_sorted = sorted(m.work_uri for m in members)
    for raw in raw_uris_sorted:
        g.add((canonical_uri, V.PROV.wasDerivedFrom, URIRef(raw)))

    # Propagate bffi:subject + bffi:genreForm onto the canonical Work.
    # Resolved (URI) targets dedupe across members; unresolved (blank-node)
    # targets dedupe by (label, source) so two raw Works carrying the same
    # cataloguer subject string emit one blank node on the canonical, ready
    # for M9 phase 3 to reconcile against Finto.
    _propagate_subject_targets(
        g,
        canonical_uri=canonical_uri,
        members=members,
        predicate=V.BFFI.subject,
        attr="subject_targets",
    )
    _propagate_subject_targets(
        g,
        canonical_uri=canonical_uri,
        members=members,
        predicate=V.BFFI.genreForm,
        attr="genre_form_targets",
    )

    # Propagate bffi:PrimaryContribution → agent → rdfs:label onto the
    # canonical Work so M9's `_iter_creator_requests` walker can find
    # creators to reconcile. Without this, M9 returns 0 entities.
    _propagate_primary_contributions(g, canonical_uri=canonical_uri, members=members)

    # AdminMetadata block — every predicate from spec § 8.
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
    g.bind("dct", DCTERMS)
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
        """Format the merge result as paste-ready text for the merge CLI."""
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
    variants_sidecar_path: Path | None = None,
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
    variants_sidecar_path = variants_sidecar_path or (settings.data_dir / VARIANTS_SIDECAR_NAME)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    merged_at = (now or datetime.now(UTC)).replace(microsecond=0)

    # P-18: emit ``start`` immediately so the dashboard shows M8 as
    # running during the BFFI-corpus load. On the 20 k bench the load
    # is ~8 min wall before any other M8 work; without the early
    # ``start`` the dashboard reports M8 as ``pending`` the whole
    # time. The canonical-group count isn't known yet — it lands in
    # the ``phase_boundary`` event below, once union-find completes.
    emit_if_active(stage="m8", event="start")

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

    # P-18: ``start`` already emitted at the top of the function;
    # this event marks the load + union-find phase boundary and
    # carries the canonical-group count as the ETA denominator for
    # the emit loop below. Pattern mirrors M9's phase_boundary
    # events between Phase 1 / 1.5 / 2 / 3.
    emit_if_active(
        stage="m8",
        event="phase_boundary",
        phase="emit",
        counters={"total": len(groups)},
    )

    g = Graph()
    _bind_prefixes(g)
    canonical_entries: list[CanonicalEntry] = []

    sorted_roots = sorted(groups)
    for processed, root in enumerate(sorted_roots, start=1):
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
        if processed % _M8_PROGRESS_CADENCE == 0 or processed == len(sorted_roots):
            emit_if_active(
                stage="m8",
                event="progress",
                counters={"processed": processed, "total": len(sorted_roots)},
                extra={"canonical_works": len(canonical_entries)},
            )

    # F2: bind variant labels from the M3 cascade's sidecar onto the
    # canonical agents that match (canonical_label → existing rdfs:label
    # on the canonical Work / Expression's agents). Skipped silently
    # when the sidecar is absent (no cascade ran, or it ran without
    # detecting any variants).
    variants_bound = _apply_contrib_variants(
        g,
        variants_sidecar_path=variants_sidecar_path,
        canonical_entries=canonical_entries,
    )

    # Atomic-rename writes
    tmp_ttl = output_path.with_suffix(output_path.suffix + ".tmp")
    g.serialize(destination=str(tmp_ttl), format="turtle")
    tmp_ttl.replace(output_path)
    _emit_canonical_map(map_path, canonical_entries)
    _emit_conflicts(conflicts_path, conflicts)
    del variants_bound  # value is for future telemetry; not yet exposed

    emit_if_active(
        stage="m8",
        event="end",
        counters={
            "total_works": len(work_records),
            "canonical_works": len(canonical_entries),
            "conflict_groups": len(conflicts),
            "same_work_decisions": len(same_work_edges),
            "different_work_decisions": len(different_work_edges),
            "uncertain_decisions": uncertain_count,
        },
    )
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


def _apply_contrib_variants(
    g: Graph,
    *,
    variants_sidecar_path: Path,
    canonical_entries: list[CanonicalEntry],
) -> int:
    """Read F2 ``contrib-variants.jsonl`` and attach ``skos:altLabel``
    on the canonical agent each claim points at. Returns the number
    of altLabels added (zero when sidecar is absent or no claim
    matches).

    Matching: each claim's ``raw_work_uri`` rolls up to the canonical
    Work via :class:`CanonicalEntry`. On every Expression of that
    canonical Work, every agent whose ``rdfs:label`` equals the
    claim's ``canonical_label`` gets ``skos:altLabel <variant_label>``.
    Multiple matches per claim are fine (an agent may be shared
    across Expressions); duplicates are deduplicated by rdflib's
    set-semantics graph add.

    Idempotent: re-running on the same sidecar adds the same
    triples, which is a no-op; canonical.ttl stays byte-stable.
    """
    claims = load_variant_claims(variants_sidecar_path)
    if not claims:
        return 0

    raw_to_canonical: dict[str, str] = {}
    for entry in canonical_entries:
        for raw in entry.raw_work_uris:
            raw_to_canonical[raw] = entry.canonical_work_uri

    added = 0
    for claim in claims:
        canonical_uri = raw_to_canonical.get(claim.raw_work_uri)
        if canonical_uri is None:
            continue
        canonical_node = URIRef(canonical_uri)
        canonical_lit = Literal(claim.canonical_label)
        variant_lit = Literal(claim.variant_label)
        # Walk every Contribution attached to the canonical Work
        # itself (primary contributions — MARC 100$a) AND every
        # Contribution attached to its Expressions (non-primary —
        # MARC 700$a). Both shapes can carry the canonical agent the
        # cascade matched against; missing either path drops a
        # legitimate variant binding.
        contrib_subjects: list[Node] = list(g.objects(canonical_node, V.BFFI.contribution))
        for expr in g.objects(canonical_node, V.BFFI.hasExpression):
            contrib_subjects.extend(g.objects(expr, V.BFFI.contribution))
        for contrib in contrib_subjects:
            for agent in g.objects(contrib, V.BFFI.agent):
                if (agent, V.RDFS.label, canonical_lit) not in g:
                    continue
                if (agent, V.SKOS.altLabel, variant_lit) in g:
                    continue
                g.add((agent, V.SKOS.altLabel, variant_lit))
                added += 1
    return added


#: P-19 Phase A — matches ``BFFI_CORPUS_FILENAME`` in
#: ``stages/bf_to_bffi.py``. Stages don't import each other per
#: CLAUDE.md "Stage isolation", so the filename is duplicated as a
#: string constant on each side.
_BFFI_CORPUS_FILENAME: Final[str] = "bffi-corpus.ttl"


def _load_work_records_from_corpus(corpus_dir: Path) -> dict[str, CanonicalWorkInputs]:
    """Read every BFFI Turtle + BIBFRAME RDF/XML under ``corpus_dir``.

    Fast-path (P-19 Phase A): when ``<corpus_dir>/bffi-corpus.ttl``
    exists AND is at least as new as every per-record ``bffi/*.ttl``,
    parse the concat in one ``Graph().parse()`` call. Otherwise fall
    back to the per-record walk so partial M3 re-runs (where only a
    handful of records were updated since the last concat) read
    correct data.
    """
    g = Graph()
    bffi_dir = corpus_dir / "bffi"
    bibframe_dir = corpus_dir / "bibframe"
    corpus_file = corpus_dir / _BFFI_CORPUS_FILENAME

    used_fast_path = False
    if corpus_file.is_file() and bffi_dir.is_dir():
        corpus_mtime = corpus_file.stat().st_mtime
        if all(p.stat().st_mtime <= corpus_mtime for p in bffi_dir.glob("*.ttl")):
            g.parse(str(corpus_file), format="turtle")
            used_fast_path = True
    if not used_fast_path and bffi_dir.is_dir():
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
    "ContributionTarget",
    "GroupConflict",
    "HelmetMapEntry",
    "JudgeDecisionRow",
    "MergeResult",
    "SubjectTarget",
    "apply_merge",
    "extract_work_metadata",
]
