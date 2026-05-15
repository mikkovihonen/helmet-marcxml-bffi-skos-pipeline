"""M8 public dataclasses + filename constants + mint-anchor enum.

Carries the value-object surface the rest of M8 (graph-extract,
mint, sidecars, apply) and external callers / tests consume:

- ``JudgeDecisionRow`` / ``GroupConflict`` / ``MintFailure`` — what
  the M6 decisions loader produces and what conflict / mint-failure
  detection raises.
- ``SubjectTarget`` / ``ContributionTarget`` / ``ExpressionContribution``
  / ``CanonicalWorkInputs`` — per-Work metadata extracted from the
  combined BFFI + BIBFRAME graph and consumed by the mint pass.
- ``HelmetMapEntry`` / ``CanonicalEntry`` — the M2 helmet-map join
  view + the canonical-map JSONL row.
- ``MergeResult`` — apply_merge's end-of-run summary.
- ``MintAnchorKind`` — P-34 mint-anchor enum.
- ``CANONICAL_*_FILENAME`` / ``JUDGE_DECISIONS_FILENAME`` /
  ``HELMET_MAP_FILENAME`` — on-disk artefact names.

P-38 Phase D: extracted from m8/runner.py. No logic change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final
from typing import Literal as LiteralType

CANONICAL_FILENAME: Final[str] = "canonical.ttl"
CANONICAL_MAP_FILENAME: Final[str] = "canonical-map.jsonl"
CANONICAL_CONFLICTS_FILENAME: Final[str] = "canonical-conflicts.jsonl"
#: Records that can't be minted because they lack the canonical-key
#: inputs (creator URI + pref_label). Distinct from conflicts —
#: ``canonical-conflicts.jsonl`` stays for real M6 same/different
#: contradictions only. See the docstring on :class:`MintFailure`.
CANONICAL_MINT_FAILURES_FILENAME: Final[str] = "canonical-mint-failures.jsonl"
#: Cataloguer-facing TSV companion to ``canonical-mint-failures.jsonl``.
#: Same row set, but with title + Helmet bib_id columns surfaced so
#: cataloguers can open in Excel / Sheets, sort, filter, and trace
#: each row back to the source MARC without needing a JSON tool.
CANONICAL_MINT_FAILURES_TSV_FILENAME: Final[str] = "canonical-mint-failures.tsv"
JUDGE_DECISIONS_FILENAME: Final[str] = "judge-decisions.jsonl"
HELMET_MAP_FILENAME: Final[str] = "helmet-map.jsonl"

#: P-12 Phase D cadence for M8 progress events. Emitted once per N
#: canonical groups processed so the exporter's throughput derivation
#: + dashboard ETA reflect real progress through the per-group mint
#: loop. ~200 emissions across a full 800k-record canonical-group
#: walk feels responsive without saturating the JSONL sidecar.
_M8_PROGRESS_CADENCE: Final[int] = 500

DecisionLabel = LiteralType["same_work", "different_work", "uncertain"]

#: P-34: which input slot of the canonical-Work mint key produced the
#: anchor. ``"primary"`` is the standard bffi:PrimaryContribution path;
#: ``"first-contributor"`` is the Phase A fallback for anonymous-main-
#: entry records (no MARC 1XX, editor in MARC 700);
#: ``"anonymous-work"`` is the Phase B fallback for truly-anonymous
#: records (no contributors at all) — keyed on (title, content-type,
#: language) instead of a creator URI.
MintAnchorKind = LiteralType["primary", "first-contributor", "anonymous-work"]


@dataclass(frozen=True)
class JudgeDecisionRow:
    """One row of ``judge-decisions.jsonl`` reduced to the fields M8 reads."""

    work_a: str
    work_b: str
    decision: DecisionLabel
    confidence: float
    used_cascade: bool
    winning_model: str | None  # cascade fallback's model when used_cascade else primary


@dataclass(frozen=True)
class GroupConflict:
    """One contradictory group flagged for human review.

    Produced by :func:`decisions._detect_conflicts` when union-find
    membership contradicts an M6 ``different_work`` edge — i.e. the
    judge said A=B *and* A≠B (transitively, via some same/different
    cycle). Cataloguer adjudicates which decision was wrong.

    NOT used for records that simply lack canonical-mint inputs (no
    primary creator, no title). Those go through :class:`MintFailure`
    and land in a separate file so the conflicts review queue stays
    free of noise. See :func:`apply.apply_merge`.
    """

    members: list[str]
    conflicting_pair: tuple[str, str]
    same_work_path: list[tuple[str, str]]


@dataclass(frozen=True)
class MintFailure:
    """One raw Work that can't be minted as a canonical Work.

    The canonical Work URI is keyed on ``(creator_uri, pref_label)``
    via :func:`bffi_pipeline.uris.mint_work_uri`. When either input
    is missing on the anchor of a union-find group, M8 emits one
    of these instead of folding the group into the canonical graph.

    Lives in a separate file (``canonical-mint-failures.jsonl``)
    rather than ``canonical-conflicts.jsonl`` because the two
    failure modes mean different things to cataloguer review: a
    conflict is "M6 contradicted itself; pick which verdict was
    right"; a mint failure is "the record lacks a primary creator
    and/or a title; either fix the source data or accept that it
    won't merge with anything."

    The dominant cause of mint failures is the *anonymous-main-entry*
    cataloguing pattern (MARC 245 ind1=0, no 1XX, contributors in
    700) — edited compilations, anonymous works, anthologies. A
    cataloguer-driven proposal could lift those into the canonical
    graph via an editor-anchored mint key; see P-NN proposed.
    """

    members: list[str]
    """All raw Work URIs that union-find clustered together."""
    anchor_work_uri: str
    """The first member's raw Work URI — the one M8 inspected."""
    missing_inputs: list[str]
    """Which inputs were missing on the anchor. Values: ``creator_uri``,
    ``pref_label``. Both can be present in the same row when the anchor
    has neither."""
    pref_label: str | None = None
    """Title from the anchor Work's ``skos:prefLabel`` (MARC 245$a + $b).
    Surfaced into the cataloguer-facing TSV companion. ``None`` when
    the failure mode itself is ``"pref_label"`` (no title to render)."""
    helmet_bib_ids: list[str] = field(default_factory=list)
    """Helmet ``bib_id`` for each member raw Work that carries one,
    sorted for stable diffs. Cataloguers use this to locate the
    source MARC in Helmet. A single MintFailure can cover multiple
    bib_ids when union-find clustered raw Works before the
    canonical-mint check refused them."""


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
    mint_anchor: MintAnchorKind | None = None
    """Which input slot of the mint key ``creator_uri`` came from
    (P-34). ``"primary"`` when the standard bffi:PrimaryContribution
    path resolved; ``"first-contributor"`` when the P-34 sub-option-(1)
    fallback picked an editor / non-primary contributor; ``None`` when
    no anchor was found (the record ends up in mint-failures)."""
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


@dataclass
class MergeResult:
    """End-of-run summary for :func:`apply.apply_merge`."""

    total_works: int
    same_work_decisions: int
    different_work_decisions: int
    uncertain_decisions: int
    canonical_works: int
    conflict_groups: int
    mint_failures: int
    canonical_path: str
    map_path: str
    conflicts_path: str
    mint_failures_path: str
    mint_failures_tsv_path: str

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
                f"  mint failures:           {self.mint_failures:,}",
                f"  canonical Turtle:        {self.canonical_path}",
                f"  canonical map JSONL:     {self.map_path}",
                f"  conflicts JSONL:         {self.conflicts_path}",
                f"  mint-failures JSONL:     {self.mint_failures_path}",
                f"  mint-failures TSV:       {self.mint_failures_tsv_path}",
            )
        )
