"""Stage M9: reconciliation against KANTO / VIAF / YSO / KAUNO / MUSO.

Resolves the literal creator / subject strings on canonical Works
into authority URIs. The four-tier decision logic (spec § 6 + BUILD_PLAN
M9) keeps the LLM out of the loop when lexical evidence is decisive:

1. ``"reconciliation-lexical"`` — exactly one candidate has lexical
   similarity ≥ 0.95 *and* every other candidate is below 0.95. Take
   it deterministically.
2. ``"reconciliation-llm"`` — multiple high-similarity candidates.
   Hand the candidate list to the LLM picker; commit if its
   confidence ≥ 0.80 and decision != ``"uncertain"``.
3. ``"reconciliation-fallback"`` — LLM said ``uncertain`` or returned
   confidence < 0.80. Take the highest-lexical candidate but flag the
   canonical Work's AdminMetadata
   ``bffi:descriptionAuthentication`` = ``<bib:auth/needs-review>``.
4. ``"reconciliation-no-candidate"`` — nothing cleared the
   lexical-floor threshold (default 0.70). Leave the literal in place;
   log the attempt.

Phase 1 (this module) ships the schemas, the decision logic, the
HTTP client structure, and the orchestrator that walks
``canonical.ttl`` and writes back the chosen authority URIs +
AdminMetadata updates + provenance Activities. Phase 2 will add a
real LangChain-backed LLM picker, the
``bffi-pipeline reconcile`` CLI subcommand, and live integration
tests against Finto.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Final, Protocol
from typing import Literal as LiteralType

import httpx
from rdflib import Graph, Literal, URIRef
from rdflib import Literal as RdfLiteral
from rdflib.namespace import RDF

from bffi_pipeline.blocking import fold_diacritics
from bffi_pipeline.config import get_settings
from bffi_pipeline.provenance import logger as P
from bffi_pipeline.provenance import vocab as V

# --- Constants ------------------------------------------------------------

#: Spec § 6 thresholds. Tightening these requires a corresponding
#: BUILD_PLAN amendment so policy changes stay visible in review.
LEXICAL_DIRECT_THRESHOLD: Final[float] = 0.95
LEXICAL_FLOOR: Final[float] = 0.70
LLM_CONFIDENCE_THRESHOLD: Final[float] = 0.80

#: Spec-committed authority kinds. KANTO and VIAF cover persons +
#: corporate bodies; YSO/KAUNO/MUSO cover subjects + genre/form. Phase 1
#: wires creators (KANTO+VIAF). Subjects land in phase 2.
AuthorityKind = LiteralType[
    "person",  # → KANTO, VIAF as fallback
    "corporate_body",  # → KANTO, VIAF as fallback
    "subject",  # → YSO
    "genre_form",  # → KAUNO
    "music_form",  # → MUSO
]

#: Source-vocabulary keys logged onto the provenance Activity.
VOCAB_KANTO: Final[str] = "kanto"
VOCAB_YSO: Final[str] = "yso"
VOCAB_KAUNO: Final[str] = "kauno"
VOCAB_MUSO: Final[str] = "muso"
VOCAB_VIAF: Final[str] = "viaf"

#: Stage tags, kept aligned with spec § 8 / BUILD_PLAN M9. These are the
#: same Literal type as :data:`ReconciliationStage` below; declared via
#: forward strings so mypy treats the constants as the narrowed Literal,
#: not just ``str``.
STAGE_LEXICAL: Final[ReconciliationStage] = "reconciliation-lexical"
STAGE_LLM: Final[ReconciliationStage] = "reconciliation-llm"
STAGE_FALLBACK: Final[ReconciliationStage] = "reconciliation-fallback"
STAGE_NO_CANDIDATE: Final[ReconciliationStage] = "reconciliation-no-candidate"

#: Default top-k pulled from the authority for each input literal.
DEFAULT_TOP_K: Final[int] = 10

#: Finto Skosmos REST API endpoint. Free public service; no API key.
FINTO_BASE_URL: Final[str] = "https://api.finto.fi/rest/v1"


# --- Schemas --------------------------------------------------------------


@dataclass(frozen=True)
class EntityRequest:
    """One reconciliation input drawn from a canonical Work."""

    work_uri: str
    literal: str
    kind: AuthorityKind


@dataclass(frozen=True)
class AuthorityCandidate:
    """One candidate URI returned by an authority lookup."""

    uri: str
    pref_label: str
    source_vocabulary: str
    lexical_similarity: float


@dataclass(frozen=True)
class PickerDecision:
    """LLM-picker output: either a chosen URI or ``uncertain``."""

    chosen_uri: str | None
    confidence: float
    rationale: str
    decision: LiteralType["chose", "uncertain"]


ReconciliationStage = LiteralType[
    "reconciliation-lexical",
    "reconciliation-llm",
    "reconciliation-fallback",
    "reconciliation-no-candidate",
]


@dataclass(frozen=True)
class ReconciliationOutcome:
    """Final outcome for one ``EntityRequest``."""

    request: EntityRequest
    stage: ReconciliationStage
    chosen_uri: str | None
    confidence: float
    rationale: str
    candidates: list[AuthorityCandidate]
    needs_review: bool

    @property
    def is_success(self) -> bool:
        """True for any outcome that bound an authority URI (incl. fallback)."""
        return self.chosen_uri is not None


@dataclass
class ReconciliationSummary:
    """Per-tier counts for a full ``apply_reconciliation`` pass."""

    lexical: int = 0
    llm_pick: int = 0
    fallback: int = 0
    no_candidate: int = 0
    total: int = 0

    def render(self) -> str:
        return "\n".join(
            (
                "M9 reconciliation complete",
                f"  total entities:               {self.total:,}",
                f"  reconciliation-lexical:       {self.lexical:,}",
                f"  reconciliation-llm:           {self.llm_pick:,}",
                f"  reconciliation-fallback:      {self.fallback:,}",
                f"  reconciliation-no-candidate:  {self.no_candidate:,}",
            )
        )


# --- Lexical similarity ---------------------------------------------------


def _normalise_for_similarity(s: str) -> str:
    """Selectively fold diacritics + casefold + collapse internal whitespace.

    Delegates the diacritic step to
    :func:`bffi_pipeline.blocking.fold_diacritics`, which preserves
    native Finnish / Swedish ``åäö`` (where the diacritic carries
    lexemic meaning — ``Häme`` vs ``hame``) and folds every other Latin
    diacritic (``ï``, ``ñ``, ``ü``, ``é``, …) so cataloguer input still
    matches KANTO's preferred label when the cataloguer dropped a
    foreign mark.
    """
    return " ".join(fold_diacritics(s).split()).casefold()


def lexical_similarity(a: str, b: str) -> float:
    """Return a 0-1 similarity score between two cataloguing strings.

    Uses :class:`difflib.SequenceMatcher` after a normalisation pass
    that selectively folds non-native diacritics, casefolds, and
    collapses whitespace. Production may swap to ``rapidfuzz`` later —
    the contract is "0=disjoint, 1=equal after normalisation".
    """
    return SequenceMatcher(None, _normalise_for_similarity(a), _normalise_for_similarity(b)).ratio()


# --- Four-tier decision logic ---------------------------------------------


class LLMPicker(Protocol):
    """Protocol for the LLM-driven authority picker.

    The phase-2 LangChain implementation will read
    ``prompts/picker_v1.txt`` and call the local Qwen3 cascade. Tests
    inject a deterministic stub via :class:`StubPicker`.
    """

    def pick(
        self,
        *,
        request: EntityRequest,
        candidates: list[AuthorityCandidate],
    ) -> PickerDecision: ...


@dataclass
class StubPicker:
    """Deterministic test picker keyed on (work_uri, literal)."""

    decisions: dict[tuple[str, str], PickerDecision] = field(default_factory=dict)

    def pick(
        self,
        *,
        request: EntityRequest,
        candidates: list[AuthorityCandidate],
    ) -> PickerDecision:
        key = (request.work_uri, request.literal)
        if key not in self.decisions:
            return PickerDecision(
                chosen_uri=None,
                confidence=0.5,
                rationale="StubPicker default: no decision wired for this request",
                decision="uncertain",
            )
        return self.decisions[key]


def decide_reconciliation(
    *,
    request: EntityRequest,
    candidates: list[AuthorityCandidate],
    picker: LLMPicker,
) -> ReconciliationOutcome:
    """Apply the four-tier logic from spec § 6 + BUILD_PLAN M9.

    The ordering matters and is committed:
    1. lexical-direct (one candidate ≥ 0.95, no other candidate ≥ 0.95);
    2. llm-pick (multiple high-similarity candidates) when LLM commits;
    3. fallback (LLM uncertain or low-conf) — take highest-lexical;
    4. no-candidate (none clear the lexical floor) — leave unreconciled.
    """
    if not candidates:
        return ReconciliationOutcome(
            request=request,
            stage=STAGE_NO_CANDIDATE,
            chosen_uri=None,
            confidence=0.0,
            rationale="No candidates returned by the authority client.",
            candidates=[],
            needs_review=False,
        )

    sorted_candidates = sorted(candidates, key=lambda c: c.lexical_similarity, reverse=True)
    top = sorted_candidates[0]

    if top.lexical_similarity < LEXICAL_FLOOR:
        return ReconciliationOutcome(
            request=request,
            stage=STAGE_NO_CANDIDATE,
            chosen_uri=None,
            confidence=top.lexical_similarity,
            rationale=(
                f"Top lexical similarity {top.lexical_similarity:.3f} below "
                f"the {LEXICAL_FLOOR:.2f} floor; left unreconciled."
            ),
            candidates=sorted_candidates,
            needs_review=False,
        )

    high_similarity = [
        c for c in sorted_candidates if c.lexical_similarity >= LEXICAL_DIRECT_THRESHOLD
    ]
    if len(high_similarity) == 1:
        winner = high_similarity[0]
        return ReconciliationOutcome(
            request=request,
            stage=STAGE_LEXICAL,
            chosen_uri=winner.uri,
            confidence=winner.lexical_similarity,
            rationale=(
                f"Single candidate cleared the {LEXICAL_DIRECT_THRESHOLD:.2f} "
                f"lexical floor: {winner.pref_label!r} "
                f"({winner.lexical_similarity:.3f})."
            ),
            candidates=sorted_candidates,
            needs_review=False,
        )

    pick = picker.pick(request=request, candidates=sorted_candidates)
    if (
        pick.decision == "chose"
        and pick.chosen_uri is not None
        and pick.confidence >= LLM_CONFIDENCE_THRESHOLD
    ):
        return ReconciliationOutcome(
            request=request,
            stage=STAGE_LLM,
            chosen_uri=pick.chosen_uri,
            confidence=pick.confidence,
            rationale=pick.rationale,
            candidates=sorted_candidates,
            needs_review=False,
        )

    return ReconciliationOutcome(
        request=request,
        stage=STAGE_FALLBACK,
        chosen_uri=top.uri,
        confidence=top.lexical_similarity,
        rationale=(
            f"LLM picker {pick.decision!r} (confidence {pick.confidence:.2f}); "
            f"falling back to highest-lexical candidate {top.pref_label!r} "
            f"({top.lexical_similarity:.3f}). Flagged needs-review."
        ),
        candidates=sorted_candidates,
        needs_review=True,
    )


# --- Authority clients ----------------------------------------------------


class AuthorityClient(Protocol):
    """Protocol all authority lookups satisfy."""

    def query(
        self, *, request: EntityRequest, top_k: int = DEFAULT_TOP_K
    ) -> list[AuthorityCandidate]: ...


_KIND_TO_FINTO_VOCAB: Final[dict[AuthorityKind, str]] = {
    "person": VOCAB_KANTO,
    "corporate_body": VOCAB_KANTO,
    "subject": VOCAB_YSO,
    "genre_form": VOCAB_KAUNO,
    "music_form": VOCAB_MUSO,
}


@dataclass
class FintoSkosmosClient:
    """Real client for Finto's REST API (https://api.finto.fi/rest/v1).

    Caches results per ``(vocab, query, date)`` per spec § 6 / BUILD_PLAN M9
    so re-runs within the day don't hammer the public service. Inject
    ``http_client`` (an ``httpx.Client``) so tests can use
    ``httpx.MockTransport`` to assert on the request shape and feed
    canned JSON.
    """

    http_client: httpx.Client
    base_url: str = FINTO_BASE_URL
    today: str = field(default_factory=lambda: datetime.now(UTC).date().isoformat())
    _cache: dict[tuple[str, str, str], list[AuthorityCandidate]] = field(default_factory=dict)

    def query(
        self,
        *,
        request: EntityRequest,
        top_k: int = DEFAULT_TOP_K,
    ) -> list[AuthorityCandidate]:
        vocab = _KIND_TO_FINTO_VOCAB.get(request.kind)
        if vocab is None:
            return []
        cache_key = (vocab, request.literal, self.today)
        if cache_key in self._cache:
            return self._cache[cache_key][:top_k]
        params = {
            "vocab": vocab,
            "query": request.literal,
            "lang": "fi",
            "maxhits": str(top_k),
        }
        try:
            response = self.http_client.get(f"{self.base_url}/search", params=params, timeout=10.0)
            response.raise_for_status()
        except httpx.HTTPError:
            return []
        try:
            payload = response.json()
        except ValueError:
            return []

        candidates: list[AuthorityCandidate] = []
        for item in payload.get("results", []):
            uri = item.get("uri")
            pref = item.get("prefLabel") or item.get("matchedPrefLabel") or ""
            if not uri:
                continue
            candidates.append(
                AuthorityCandidate(
                    uri=str(uri),
                    pref_label=str(pref),
                    source_vocabulary=vocab,
                    lexical_similarity=lexical_similarity(request.literal, str(pref)),
                )
            )
        self._cache[cache_key] = candidates
        return candidates[:top_k]


@dataclass
class ViafClient:
    """VIAF lookup. Falls back here only when KANTO returned no person/corporate-body match.

    Phase 1 ships the same shape as :class:`FintoSkosmosClient`; the
    actual VIAF AutoSuggest endpoint is wired in phase 2 alongside the
    CLI subcommand. Tests inject a ``StubAuthorityClient`` instead.
    """

    http_client: httpx.Client
    base_url: str = "https://www.viaf.org/viaf/AutoSuggest"

    def query(
        self,
        *,
        request: EntityRequest,
        top_k: int = DEFAULT_TOP_K,
    ) -> list[AuthorityCandidate]:
        if request.kind not in {"person", "corporate_body"}:
            return []
        try:
            response = self.http_client.get(
                self.base_url,
                params={"query": request.literal},
                timeout=10.0,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return []
        try:
            payload = response.json()
        except ValueError:
            return []
        candidates: list[AuthorityCandidate] = []
        for item in payload.get("result", []) or []:
            viaf_id = item.get("viafid") or item.get("id")
            term = item.get("term") or item.get("displayForm") or ""
            if not viaf_id:
                continue
            uri = f"https://viaf.org/viaf/{viaf_id}"
            candidates.append(
                AuthorityCandidate(
                    uri=uri,
                    pref_label=str(term),
                    source_vocabulary=VOCAB_VIAF,
                    lexical_similarity=lexical_similarity(request.literal, str(term)),
                )
            )
        return candidates[:top_k]


@dataclass
class StubAuthorityClient:
    """Test stub: returns a pre-baked candidate list per (kind, literal)."""

    fixtures: dict[tuple[AuthorityKind, str], list[AuthorityCandidate]] = field(
        default_factory=dict
    )

    def query(
        self,
        *,
        request: EntityRequest,
        top_k: int = DEFAULT_TOP_K,
    ) -> list[AuthorityCandidate]:
        return list(self.fixtures.get((request.kind, request.literal), []))[:top_k]


# --- Orchestrator: walk canonical.ttl, reconcile, write back -------------


def _iter_creator_requests(graph: Graph) -> Iterator[EntityRequest]:
    """Yield one creator-reconciliation request per canonical Work agent."""
    for work in graph.subjects(RDF.type, V.BFFI.Work):
        if not isinstance(work, URIRef):
            continue
        for contrib in graph.objects(work, V.BFFI.contribution):
            if V.BFFI.PrimaryContribution not in set(graph.objects(contrib, RDF.type)):
                continue
            for agent in graph.objects(contrib, V.BFFI.agent):
                if not isinstance(agent, URIRef):
                    continue
                for label in graph.objects(agent, V.RDFS.label):
                    if isinstance(label, RdfLiteral):
                        yield EntityRequest(
                            work_uri=str(work),
                            literal=str(label),
                            kind="person",
                        )
                        break


def _admin_block_for(graph: Graph, work: URIRef) -> URIRef | None:
    for block in graph.objects(work, V.adminMetadata):
        if isinstance(block, URIRef):
            return block
    return None


def _bump_admin_metadata(
    graph: Graph,
    work_uri: str,
    *,
    chosen_uri: str,
    needs_review: bool,
    now: datetime,
) -> None:
    """Side-effect: add sourceConsulted + bump descriptionChangeDate.

    On the fallback path also flip ``descriptionAuthentication`` to
    ``<bib:auth/needs-review>``.
    """
    block = _admin_block_for(graph, URIRef(work_uri))
    if block is None:
        return
    graph.add((block, V.sourceConsulted, URIRef(chosen_uri)))
    # Replace any existing descriptionChangeDate with the reconciliation moment.
    for old in list(graph.objects(block, V.descriptionChangeDate)):
        graph.remove((block, V.descriptionChangeDate, old))
    graph.add(
        (
            block,
            V.descriptionChangeDate,
            Literal(now.isoformat(), datatype=V.XSD.dateTime),
        )
    )
    if needs_review:
        for old in list(graph.objects(block, V.descriptionAuthentication)):
            graph.remove((block, V.descriptionAuthentication, old))
        graph.add((block, V.descriptionAuthentication, V.AUTH_NEEDS_REVIEW))


def _link_canonical_creator(graph: Graph, work_uri: str, chosen_uri: str) -> None:
    """Add ``<work> bffi:creator <authority>`` and rewrite the agent URI on the contribution.

    Also adds ``owl:sameAs`` from the existing agent URI to the chosen
    authority URI so downstream consumers of the M3 raw graph still
    have a one-hop bridge to the reconciled identity.
    """
    work = URIRef(work_uri)
    auth = URIRef(chosen_uri)
    graph.add((work, V.BFFI.creator, auth))
    for contrib in graph.objects(work, V.BFFI.contribution):
        if V.BFFI.PrimaryContribution not in set(graph.objects(contrib, RDF.type)):
            continue
        for agent in list(graph.objects(contrib, V.BFFI.agent)):
            if isinstance(agent, URIRef) and str(agent) != chosen_uri:
                graph.add((agent, V.PROV.specializationOf, auth))


def _emit_provenance(
    writer_graph: Graph | None,
    *,
    outcome: ReconciliationOutcome,
    started_at: datetime,
    ended_at: datetime,
) -> None:
    if writer_graph is None:
        return
    P.log_reconciliation(
        writer_graph,
        work_uri=outcome.request.work_uri,
        input_literal=outcome.request.literal,
        source_vocabulary=(
            outcome.candidates[0].source_vocabulary if outcome.candidates else "none"
        ),
        stage=outcome.stage,
        chosen_authority_uri=outcome.chosen_uri,
        candidates=[(c.uri, c.lexical_similarity) for c in outcome.candidates],
        confidence=outcome.confidence,
        rationale=outcome.rationale,
        started_at=started_at,
        ended_at=ended_at,
    )


def reconcile_one(
    *,
    request: EntityRequest,
    client: AuthorityClient,
    fallback_client: AuthorityClient | None,
    picker: LLMPicker,
    top_k: int = DEFAULT_TOP_K,
) -> ReconciliationOutcome:
    """Run the four-tier decision for ``request`` end-to-end."""
    candidates = client.query(request=request, top_k=top_k)
    if not candidates and fallback_client is not None:
        candidates = fallback_client.query(request=request, top_k=top_k)
    return decide_reconciliation(request=request, candidates=candidates, picker=picker)


def apply_reconciliation(
    canonical_path: Path | None = None,
    *,
    output_path: Path | None = None,
    client: AuthorityClient,
    fallback_client: AuthorityClient | None = None,
    picker: LLMPicker,
    provenance_graph: Graph | None = None,
    requests: Iterable[EntityRequest] | None = None,
    graph: Graph | None = None,
    top_k: int = DEFAULT_TOP_K,
    now: datetime | None = None,
) -> tuple[ReconciliationSummary, list[ReconciliationOutcome]]:
    """Walk canonical.ttl, run reconcile_one per creator, and write the graph back.

    ``graph`` is an injection point for tests so the same orchestrator
    can run against an in-memory graph without serialising to disk.
    Production callers leave it ``None``; the orchestrator parses
    ``canonical_path``, mutates the graph, and serialises it back.
    """
    settings = get_settings()
    canonical_path = canonical_path or (settings.data_dir / "canonical.ttl")
    output_path = output_path or canonical_path
    moment = (now or datetime.now(UTC)).replace(microsecond=0)

    own_graph = graph is None
    target_graph: Graph
    if own_graph:
        target_graph = Graph()
        target_graph.parse(str(canonical_path), format="turtle")
    else:
        assert graph is not None  # narrow for mypy
        target_graph = graph

    request_list: list[EntityRequest] = (
        list(requests) if requests is not None else list(_iter_creator_requests(target_graph))
    )

    summary = ReconciliationSummary(total=len(request_list))
    outcomes: list[ReconciliationOutcome] = []

    for request in request_list:
        started = datetime.now(UTC)
        outcome = reconcile_one(
            request=request,
            client=client,
            fallback_client=fallback_client,
            picker=picker,
            top_k=top_k,
        )
        ended = datetime.now(UTC)
        outcomes.append(outcome)

        if outcome.stage == STAGE_LEXICAL:
            summary.lexical += 1
        elif outcome.stage == STAGE_LLM:
            summary.llm_pick += 1
        elif outcome.stage == STAGE_FALLBACK:
            summary.fallback += 1
        else:
            summary.no_candidate += 1

        if outcome.chosen_uri is not None:
            _link_canonical_creator(target_graph, outcome.request.work_uri, outcome.chosen_uri)
            _bump_admin_metadata(
                target_graph,
                outcome.request.work_uri,
                chosen_uri=outcome.chosen_uri,
                needs_review=outcome.needs_review,
                now=moment,
            )
        _emit_provenance(provenance_graph, outcome=outcome, started_at=started, ended_at=ended)

    if own_graph:
        tmp = output_path.with_suffix(output_path.suffix + ".tmp")
        target_graph.serialize(destination=str(tmp), format="turtle")
        tmp.replace(output_path)

    return summary, outcomes


__all__ = [
    "DEFAULT_TOP_K",
    "FINTO_BASE_URL",
    "LEXICAL_DIRECT_THRESHOLD",
    "LEXICAL_FLOOR",
    "LLM_CONFIDENCE_THRESHOLD",
    "STAGE_FALLBACK",
    "STAGE_LEXICAL",
    "STAGE_LLM",
    "STAGE_NO_CANDIDATE",
    "VOCAB_KANTO",
    "VOCAB_KAUNO",
    "VOCAB_MUSO",
    "VOCAB_VIAF",
    "VOCAB_YSO",
    "AuthorityCandidate",
    "AuthorityClient",
    "AuthorityKind",
    "EntityRequest",
    "FintoSkosmosClient",
    "LLMPicker",
    "PickerDecision",
    "ReconciliationOutcome",
    "ReconciliationStage",
    "ReconciliationSummary",
    "StubAuthorityClient",
    "StubPicker",
    "ViafClient",
    "apply_reconciliation",
    "decide_reconciliation",
    "lexical_similarity",
    "reconcile_one",
]
