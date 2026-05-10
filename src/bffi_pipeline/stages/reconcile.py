"""Stage M9: reconciliation against KANTO / VIAF / YSO / KAUNO / MUSO.

Resolves the literal creator / subject strings on canonical Works
into authority URIs. The decision logic (spec § 6 + BUILD_PLAN M9)
keeps the LLM out of the loop when lexical evidence is decisive:

0. ``"reconciliation-local"`` — tier-0 exact-prefLabel match against
   the locally-loaded Finto authority graphs. When a YSO concept's
   ``skos:prefLabel`` exactly matches the cataloguer literal, bind
   that URI without any HTTP round-trip to api.finto.fi. Skipped when
   no ``local_resolver`` is wired.
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

import hashlib
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol
from typing import Literal as LiteralType

if TYPE_CHECKING:
    from bffi_pipeline.stages.local_concept_resolver import (
        LocalConceptHit,
        LocalConceptResolver,
    )

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
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

#: Source-vocabulary keys logged onto the provenance Activity. Also
#: consumed as Finto's ``vocab`` query parameter for the four Finto
#: clients. KANTO is identified as ``finaf`` ("Finnish Authority File")
#: in Finto's API even though the human-facing name is still "KANTO";
#: using ``vocab=kanto`` returns HTTP 500.
VOCAB_KANTO: Final[str] = "finaf"
VOCAB_YSO: Final[str] = "yso"
VOCAB_KAUNO: Final[str] = "kauno"
VOCAB_MUSO: Final[str] = "muso"
VOCAB_VIAF: Final[str] = "viaf"


def _finto_search_query(literal: str) -> str:
    """Build the Finto ``query`` parameter from a cataloguer literal.

    Finto exact-matches on prefLabel by default; we want prefix-match.
    Appends ``*`` unless the caller already supplied one. Trailing MARC
    punctuation (``", "`` after a name) doesn't break the wildcard —
    Finto treats it as part of the prefix and still finds entries whose
    label is the exact prefix.
    """
    return literal if literal.endswith("*") else f"{literal}*"


#: Stage tags, kept aligned with spec § 8 / BUILD_PLAN M9. These are the
#: same Literal type as :data:`ReconciliationStage` below; declared via
#: forward strings so mypy treats the constants as the narrowed Literal,
#: not just ``str``.
STAGE_LOCAL: Final[ReconciliationStage] = "reconciliation-local"
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
    """One reconciliation input drawn from a canonical Work.

    ``predicate_uri`` is set for subject/genre/music requests so the
    dispatcher knows whether to bind the chosen authority back as
    ``bffi:subject`` or ``bffi:genreForm`` — the cataloguer's MARC tag
    chose the predicate at M2 conversion time, and reconciliation must
    preserve it. Creator requests leave it ``None``; the creator linker
    rewrites ``bffi:contribution`` instead.
    """

    work_uri: str
    literal: str
    kind: AuthorityKind
    predicate_uri: str | None = None


@dataclass(frozen=True)
class AuthorityCandidate:
    """One candidate URI returned by an authority lookup."""

    uri: str
    pref_label: str
    source_vocabulary: str
    lexical_similarity: float


#: Stub phrases the picker rationale must NOT contain. Mirrors the M6
#: judge's policy — a hand-wavy rationale that doesn't cite candidate
#: fields can't be cached or trusted.
PICKER_STUB_PHRASES: Final[tuple[str, ...]] = (
    "i don't know",
    "unable to determine",
    "n/a",
    "not sure",
)

#: Maximum confidence allowed when ``decision="uncertain"``. Same value
#: as the M6 judge's UNCERTAIN_MAX_CONFIDENCE so cataloguers see one
#: coherent policy across stages.
PICKER_UNCERTAIN_MAX_CONFIDENCE: Final[float] = 0.7

#: Minimum rationale length, in characters.
PICKER_MIN_RATIONALE_CHARS: Final[int] = 20


class PickerDecision(BaseModel):
    """LLM-picker structured output: chosen URI or ``"uncertain"``.

    Pydantic validators enforce Boundary-4-style coherence:
    ``decision="chose"`` requires a non-null ``chosen_uri``;
    ``decision="uncertain"`` requires confidence ≤ 0.7; the rationale
    must be substantive and free of stub phrases.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: LiteralType["chose", "uncertain"]
    chosen_uri: str | None = None
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="0.0-1.0. Use <0.7 when uncertain; reserve >0.9 for clear matches.",
    )
    rationale: str = Field(
        min_length=PICKER_MIN_RATIONALE_CHARS,
        description=(
            "2-4 sentences citing specific candidate fields (URI, prefLabel, "
            "dates) that drove the decision. Never introduce facts not present "
            "in the inputs."
        ),
    )

    @model_validator(mode="after")
    def _chose_requires_uri(self) -> PickerDecision:
        if self.decision == "chose" and not self.chosen_uri:
            raise ValueError("decision='chose' requires a non-null chosen_uri")
        return self

    @model_validator(mode="after")
    def _coherent_uncertain(self) -> PickerDecision:
        if self.decision == "uncertain" and self.confidence > PICKER_UNCERTAIN_MAX_CONFIDENCE:
            raise ValueError(
                f"decision='uncertain' is incoherent with "
                f"confidence > {PICKER_UNCERTAIN_MAX_CONFIDENCE}"
            )
        return self

    @model_validator(mode="after")
    def _rationale_is_substantive(self) -> PickerDecision:
        text = self.rationale.strip()
        if len(text) < PICKER_MIN_RATIONALE_CHARS:
            raise ValueError(f"rationale shorter than {PICKER_MIN_RATIONALE_CHARS} characters")
        lowered = text.lower()
        for phrase in PICKER_STUB_PHRASES:
            if re.search(rf"\b{re.escape(phrase)}\b", lowered):
                raise ValueError(f"rationale contains stub phrase: {phrase!r}")
        return self


ReconciliationStage = LiteralType[
    "reconciliation-local",
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

    local: int = 0
    lexical: int = 0
    llm_pick: int = 0
    fallback: int = 0
    no_candidate: int = 0
    total: int = 0

    def render(self) -> str:
        """Format the reconciliation summary as paste-ready text for the reconcile CLI."""
        return "\n".join(
            (
                "M9 reconciliation complete",
                f"  total entities:               {self.total:,}",
                f"  reconciliation-local:         {self.local:,}",
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
    ) -> PickerDecision:
        """Pick the authority URI for ``request`` from ``candidates``, or return ``uncertain``."""
        ...


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
        """Look up a wired decision for ``(work_uri, literal)``; default to ``uncertain``."""
        key = (request.work_uri, request.literal)
        if key not in self.decisions:
            return PickerDecision(
                decision="uncertain",
                chosen_uri=None,
                confidence=0.5,
                rationale="StubPicker default: no decision wired for this request.",
            )
        return self.decisions[key]


# --- LangChain-backed picker ---------------------------------------------


#: Picker prompt source. Hashed at startup so reconciliation provenance
#: pins the exact prompt that produced each decision.
PICKER_PROMPT_PATH: Final[Path] = Path(__file__).resolve().parents[3] / "prompts" / "picker_v1.txt"
_PICKER_SECTION_RE: Final[re.Pattern[str]] = re.compile(r"^### (\w+)\s*$", re.MULTILINE)

#: Validation retry: same shape as the M6 judge — max 2 retries on
#: parse / Boundary-4 failures (3 attempts total).
PICKER_MAX_VALIDATION_RETRIES: Final[int] = 2

#: Connection retry: 5 / 30 / 120 seconds backoff (3 retries, 4 attempts).
PICKER_MAX_CONNECTION_RETRIES: Final[int] = 3
PICKER_CONNECTION_BACKOFF_SECONDS: Final[tuple[float, ...]] = (5.0, 30.0, 120.0)


@lru_cache(maxsize=1)
def picker_prompt_text() -> str:
    """Return the raw ``prompts/picker_v1.txt`` contents."""
    if not PICKER_PROMPT_PATH.is_file():
        raise FileNotFoundError(f"Picker prompt not found at {PICKER_PROMPT_PATH!s}.")
    return PICKER_PROMPT_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def picker_prompt_hash() -> str:
    """SHA-256 of :func:`picker_prompt_text`. Logged with each reconciliation."""
    return "sha256:" + hashlib.sha256(picker_prompt_text().encode("utf-8")).hexdigest()[:16]


@lru_cache(maxsize=1)
def _parse_picker_prompt_sections() -> dict[str, str]:
    """Split ``picker_v1.txt`` into ``SYSTEM`` / ``EXAMPLES`` / ``USER`` blocks."""
    raw = picker_prompt_text()
    sections: dict[str, str] = {}
    matches = list(_PICKER_SECTION_RE.finditer(raw))
    if not matches:
        raise ValueError(f"No '### SECTION' markers found in {PICKER_PROMPT_PATH!s}.")
    for i, m in enumerate(matches):
        name = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        sections[name] = raw[start:end].strip()
    for required in ("SYSTEM", "EXAMPLES", "USER"):
        if required not in sections:
            raise ValueError(
                f"{PICKER_PROMPT_PATH!s} is missing required '### {required}' section."
            )
    return sections


def _format_candidates_for_prompt(candidates: list[AuthorityCandidate]) -> str:
    """Render the candidate list in the line-by-line format the prompt expects."""
    if not candidates:
        return "(no candidates were returned by the authority client)"
    return "\n".join(
        f"  {i}. uri={c.uri} prefLabel={c.pref_label!r} "
        f"lexical_similarity={c.lexical_similarity:.3f}"
        for i, c in enumerate(candidates, start=1)
    )


def _is_picker_connection_error(exc: BaseException) -> bool:
    """Mirror :func:`bffi_pipeline.stages.judge._is_connection_error` for the picker."""
    name = type(exc).__name__
    if name in {
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "ReadError",
        "RemoteProtocolError",
        "APIConnectionError",
        "APITimeoutError",
        "Timeout",
    }:
        return True
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        return _is_picker_connection_error(cause)
    return False


def _build_picker_chain(
    *,
    model_name: str,
    base_url: str,
    api_key: str,
    temperature: float = 0.0,
    seed: int = 42,
) -> Any:
    """Compose ``ChatOpenAI(...).with_structured_output(PickerDecision)``."""
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_openai import ChatOpenAI
    from pydantic import SecretStr

    sections = _parse_picker_prompt_sections()
    template = ChatPromptTemplate.from_messages(
        [
            ("system", sections["SYSTEM"] + "\n\n" + sections["EXAMPLES"]),
            ("user", sections["USER"]),
        ]
    )
    llm = ChatOpenAI(
        base_url=base_url,
        api_key=SecretStr(api_key),
        model=model_name,
        temperature=temperature,
        seed=seed,
    )
    return template | llm.with_structured_output(PickerDecision, method="json_schema")


def _picker_uncertain(reason: str) -> PickerDecision:
    """Build a fall-through 'uncertain' decision when the chain can't produce one."""
    cleaned = reason.strip() or "no error message available"
    lowered = cleaned.lower()
    for phrase in PICKER_STUB_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}\b", lowered):
            cleaned = re.sub(
                rf"\b{re.escape(phrase)}\b",
                "[stub phrase elided]",
                cleaned,
                flags=re.IGNORECASE,
            )
    rationale = f"Picker fell through to uncertain after retries exhausted: {cleaned}"
    return PickerDecision(
        decision="uncertain",
        chosen_uri=None,
        confidence=0.0,
        rationale=rationale,
    )


@dataclass
class LangChainLLMPicker:
    """Production picker that calls the local Qwen3 cascade via LangChain.

    Phase-1 callers passed a ``StubPicker``; the production CLI builds
    one of these. Validation-failure retry (max 2) and connection-error
    retry (5 / 30 / 120 s, max 3) mirror the M6 judge's policies so
    cataloguers see one consistent failure-handling story across stages.

    The picker also enforces a *post-parse* check: the LLM's
    ``chosen_uri`` must be one of the URIs the authority client actually
    returned. A pick of an out-of-set URI falls through to ``uncertain``
    with the offending URI in the rationale, never silently bound to a
    bad authority.
    """

    model_name: str | None = None
    chain: Any = None
    sleep: Callable[[float], None] = time.sleep

    def _resolved_chain(self) -> Any:
        if self.chain is not None:
            return self.chain
        settings = get_settings()
        return _build_picker_chain(
            model_name=self.model_name or settings.llm_model_primary,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
        )

    def pick(
        self,
        *,
        request: EntityRequest,
        candidates: list[AuthorityCandidate],
    ) -> PickerDecision:
        """Invoke the configured chain with retries and the candidate-set sanity check."""
        if not candidates:
            return PickerDecision(
                decision="uncertain",
                chosen_uri=None,
                confidence=0.0,
                rationale="No candidates supplied; nothing to pick from.",
            )

        chain = self._resolved_chain()
        invoke_payload = {
            "input_literal": request.literal,
            "source_vocabulary": candidates[0].source_vocabulary,
            "candidates": _format_candidates_for_prompt(candidates),
        }

        connection_attempts = 0
        validation_attempts = 0
        last_error = "unknown failure"

        while True:
            try:
                raw = chain.invoke(invoke_payload)
            except Exception as exc:
                if _is_picker_connection_error(exc):
                    if connection_attempts < PICKER_MAX_CONNECTION_RETRIES:
                        self.sleep(PICKER_CONNECTION_BACKOFF_SECONDS[connection_attempts])
                        connection_attempts += 1
                        last_error = (
                            f"connection error after {connection_attempts} retry(ies): {exc!s}"
                        )
                        continue
                    last_error = (
                        f"connection error after {PICKER_MAX_CONNECTION_RETRIES} retries "
                        f"exhausted: {exc!s}"
                    )
                    break
                last_error = f"unrecoverable LLM error: {exc!s}"
                break

            try:
                if isinstance(raw, PickerDecision):
                    decision = raw
                else:
                    decision = PickerDecision.model_validate(raw)
            except (ValidationError, ValueError) as exc:
                if validation_attempts < PICKER_MAX_VALIDATION_RETRIES:
                    validation_attempts += 1
                    last_error = f"validation failure (attempt {validation_attempts}): {exc!s}"
                    continue
                last_error = (
                    f"validation failed after {PICKER_MAX_VALIDATION_RETRIES} retries: {exc!s}"
                )
                break

            # Post-parse sanity check: the chosen URI must be in the candidate set.
            if decision.decision == "chose":
                allowed = {c.uri for c in candidates}
                if decision.chosen_uri not in allowed:
                    return PickerDecision(
                        decision="uncertain",
                        chosen_uri=None,
                        confidence=0.0,
                        rationale=(
                            f"LLM picked {decision.chosen_uri!r} which is not in the "
                            f"candidate URI set; falling through to uncertain."
                        ),
                    )
            return decision

        return _picker_uncertain(last_error)


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
    ) -> list[AuthorityCandidate]:
        """Return up to ``top_k`` candidates for ``request`` from this authority."""
        ...


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
        """Hit Finto's ``/search`` endpoint for ``request.kind``-mapped vocab; cache by day."""
        vocab = _KIND_TO_FINTO_VOCAB.get(request.kind)
        if vocab is None:
            return []
        cache_key = (vocab, request.literal, self.today)
        if cache_key in self._cache:
            return self._cache[cache_key][:top_k]
        # Finto's `/search` endpoint defaults to exact-match against
        # prefLabel; cataloguer literals like "Puškin, Aleksandr" almost
        # never exact-match a KANTO entry like "Puškin, Aleksandr,
        # 1799-1837". Append `*` for prefix match — the lexical
        # similarity gate downstream still filters spurious matches.
        params = {
            "vocab": vocab,
            "query": _finto_search_query(request.literal),
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
        """Hit VIAF's AutoSuggest endpoint; only persons / corporate bodies route here."""
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
        """Look up a wired candidate list for ``(kind, literal)``; default to empty."""
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


#: Maps the ``bf:source`` value that marc2bibframe2 emits on unresolved
#: 6XX targets to the reconciliation kind that selects the right Finto
#: vocabulary. ``bf:source`` is sometimes a literal (e.g. ``"yso/fin"``
#: from MARC ``$2 yso/fin``) and sometimes a URIRef (e.g.
#: ``<http://id.loc.gov/vocabulary/subjectSchemes/ysa>`` from MARC
#: ``$2 ysa``); we match against either by searching for the prefix
#: token anywhere in the string. Anything not matched defaults to
#: ``"subject"`` (YSO), the broadest of the three subject vocabularies
#: and the safest backstop when ``$2`` is missing or unrecognised.
#: ``ysa`` maps to ``"subject"`` so YSA-tagged terms route through
#: the YSO reconciliation path — YSO inherited the YSA concepts as
#: ``skos:prefLabel@fi`` during the 2014-2018 vocabulary merge.
_SOURCE_TOKEN_TO_KIND: Final[tuple[tuple[str, AuthorityKind], ...]] = (
    ("yso", "subject"),
    ("ysa", "subject"),
    ("kauno", "genre_form"),
    ("muso", "music_form"),
    ("slm", "genre_form"),
)


def _classify_subject_source(source: str | None) -> AuthorityKind:
    """Map a ``bf:source`` value (literal text or URI string) to a
    reconciliation kind by token-substring match against the known
    vocabulary identifiers."""
    if source is None:
        return "subject"
    lowered = source.casefold()
    for token, kind in _SOURCE_TOKEN_TO_KIND:
        if token in lowered:
            return kind
    return "subject"


def _iter_subject_requests(graph: Graph) -> Iterator[EntityRequest]:
    """Yield reconciliation requests for unresolved ``bffi:subject`` /
    ``bffi:genreForm`` targets on canonical Works.

    Reconciles three target shapes (see :class:`SubjectTarget` in
    :mod:`bffi_pipeline.stages.merge`):

    - **Blank-node target** with ``rdfs:label`` + optional ``bf:source``:
      classic unresolved cataloguer-supplied subject.
    - **Local marc2bibframe2-minted URI** (e.g.
      ``http://urn.fi/.../#Place651-37``) carrying ``rdfs:label`` +
      ``bf:source`` — the dominant pattern for MARC ``$2 ysa`` time
      and place fields where the cataloguer didn't supply ``$0``.
    - **Pre-resolved authority URI** (e.g. ``yso/p1018``) carries no
      label / source on the canonical (Skosmos resolves from the
      loaded authority graph). These are skipped — already bound.

    Routing by ``bf:source`` (URI form like
    ``<.../subjectSchemes/ysa>`` or literal ``"yso/fin"``):

    - any ``yso*`` / ``ysa`` token → ``subject`` (YSO, with YSA-via-YSO inheritance)
    - any ``kauno*`` token → ``genre_form`` (KAUNO)
    - any ``muso*`` token → ``music_form`` (MUSO)
    - any ``slm`` token → ``genre_form`` (SLM)
    - missing / unknown → ``subject`` (YSO default)

    Deduplication is *not* applied here — the apply step caches per
    ``(kind, literal)`` lookups, so two canonical Works asking for the
    same subject only hit Finto once.
    """
    from bffi_pipeline.stages.load_finto import graph_uri_for_uri

    for work in graph.subjects(RDF.type, V.BFFI.Work):
        if not isinstance(work, URIRef):
            continue
        for predicate in (V.BFFI.subject, V.BFFI.genreForm):
            for target in graph.objects(work, predicate):
                # Skip URIs that already resolve to an authority graph
                # we have loaded locally (YSO/KANTO/KAUNO/MUSO/SLM via
                # option 3b). They're already bound; their label
                # propagation in M8 was just fallback context for
                # Skosmos rendering, not a request for reconciliation.
                if isinstance(target, URIRef) and graph_uri_for_uri(str(target)) is not None:
                    continue
                label_lit: RdfLiteral | None = None
                for lab in graph.objects(target, V.RDFS.label):
                    if isinstance(lab, RdfLiteral):
                        label_lit = lab
                        break
                if label_lit is None:
                    continue
                source: str | None = None
                for src in graph.objects(target, V.BF.source):
                    if isinstance(src, URIRef):
                        source = str(src)
                        break
                    if isinstance(src, RdfLiteral):
                        source = str(src)
                        break
                yield EntityRequest(
                    work_uri=str(work),
                    literal=str(label_lit),
                    kind=_classify_subject_source(source),
                    predicate_uri=str(predicate),
                )


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

    Also adds ``prov:specializationOf`` from the existing agent URI to
    the chosen authority URI so downstream consumers of the M3 raw graph
    still have a one-hop bridge to the reconciled identity.
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


def _link_canonical_subject(
    graph: Graph,
    *,
    work_uri: str,
    chosen_uri: str,
    predicate_uri: str,
    literal: str,
) -> None:
    """Add ``<work> <predicate> <authority>`` and bridge the original blank node.

    The blank-node target M8 propagated onto the canonical Work stays in
    place (it preserves the cataloguer's literal for audit), and gains a
    ``prov:specializationOf`` triple pointing at the chosen authority.
    The same predicate (``bffi:subject`` or ``bffi:genreForm``) the M8
    propagation used is re-used here — the cataloguer's MARC tag, not
    the Finto vocabulary, decides which slot the authority binds into.
    """
    work = URIRef(work_uri)
    auth = URIRef(chosen_uri)
    predicate = URIRef(predicate_uri)
    graph.add((work, predicate, auth))
    for target in graph.objects(work, predicate):
        if isinstance(target, URIRef):
            continue
        # Bridge only the blank node whose label matches the input literal,
        # so two distinct cataloguer subjects on the same canonical (e.g.
        # "Tampere" and "Helsinki") don't accidentally share a bridge.
        for label in graph.objects(target, V.RDFS.label):
            if isinstance(label, RdfLiteral) and str(label) == literal:
                graph.add((target, V.PROV.specializationOf, auth))
                break


def _apply_canonical_link(graph: Graph, request: EntityRequest, chosen_uri: str) -> None:
    """Dispatch the per-kind binding logic on a successful reconciliation."""
    if request.kind in {"person", "corporate_body"}:
        _link_canonical_creator(graph, request.work_uri, chosen_uri)
        return
    # Subject-side requests must carry their predicate. If a caller
    # hand-built an EntityRequest without going through
    # `_iter_subject_requests`, fall back to bffi:subject so we don't
    # drop the binding silently.
    predicate_uri = request.predicate_uri or str(V.BFFI.subject)
    _link_canonical_subject(
        graph,
        work_uri=request.work_uri,
        chosen_uri=chosen_uri,
        predicate_uri=predicate_uri,
        literal=request.literal,
    )


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


def _local_outcome(request: EntityRequest, hit: LocalConceptHit) -> ReconciliationOutcome:
    """Build a tier-0 outcome for an exact local prefLabel match.

    Synthesises a single :class:`AuthorityCandidate` with similarity
    1.0 so downstream provenance + summary code can treat tier-0 hits
    uniformly with the other tiers.
    """
    candidate = AuthorityCandidate(
        uri=hit.uri,
        pref_label=hit.pref_label,
        source_vocabulary=hit.source_vocabulary,
        lexical_similarity=1.0,
    )
    return ReconciliationOutcome(
        request=request,
        stage=STAGE_LOCAL,
        chosen_uri=hit.uri,
        confidence=1.0,
        rationale=(
            f"Exact prefLabel match in local {hit.source_vocabulary} graph: "
            f"{hit.pref_label!r} (no Finto API call)."
        ),
        candidates=[candidate],
        needs_review=False,
    )


def reconcile_one(
    *,
    request: EntityRequest,
    client: AuthorityClient,
    fallback_client: AuthorityClient | None,
    picker: LLMPicker,
    top_k: int = DEFAULT_TOP_K,
    local_resolver: LocalConceptResolver | None = None,
) -> ReconciliationOutcome:
    """Run the decision logic for ``request`` end-to-end.

    Tier-0 (``local_resolver``) runs first when wired and short-circuits
    on exact prefLabel match against the locally-loaded authority graph
    — no tier-1 HTTP call, no LLM. Otherwise the tier-1+ four-tier
    decision applies.
    """
    if local_resolver is not None:
        hit = local_resolver.resolve(literal=request.literal, kind=request.kind)
        if hit is not None:
            return _local_outcome(request, hit)
    candidates = client.query(request=request, top_k=top_k)
    if not candidates and fallback_client is not None:
        candidates = fallback_client.query(request=request, top_k=top_k)
    return decide_reconciliation(request=request, candidates=candidates, picker=picker)


#: All authority kinds the orchestrator can walk for. Mirrors the
#: ``AuthorityKind`` Literal; kept as a runtime ``frozenset`` so callers
#: can build subsets ergonomically (``{"person", "corporate_body"}``).
ALL_AUTHORITY_KINDS: Final[frozenset[AuthorityKind]] = frozenset(
    {"person", "corporate_body", "subject", "genre_form", "music_form"}
)
_CREATOR_KINDS: Final[frozenset[AuthorityKind]] = frozenset({"person", "corporate_body"})
_SUBJECT_KINDS: Final[frozenset[AuthorityKind]] = frozenset({"subject", "genre_form", "music_form"})


def _collect_requests(
    graph: Graph, selected_kinds: frozenset[AuthorityKind]
) -> list[EntityRequest]:
    """Walk the canonical graph and yield reconciliation requests filtered by kind."""
    out: list[EntityRequest] = []
    if selected_kinds & _CREATOR_KINDS:
        out.extend(r for r in _iter_creator_requests(graph) if r.kind in selected_kinds)
    if selected_kinds & _SUBJECT_KINDS:
        out.extend(r for r in _iter_subject_requests(graph) if r.kind in selected_kinds)
    return out


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
    kinds: set[AuthorityKind] | frozenset[AuthorityKind] | None = None,
    local_resolver: LocalConceptResolver | None = None,
) -> tuple[ReconciliationSummary, list[ReconciliationOutcome]]:
    """Walk canonical.ttl, reconcile creators + subjects, and write the graph back.

    ``graph`` is an injection point for tests so the same orchestrator
    can run against an in-memory graph without serialising to disk.
    Production callers leave it ``None``; the orchestrator parses
    ``canonical_path``, mutates the graph, and serialises it back.

    ``kinds`` filters which reconciliation paths to walk. ``None`` means
    "all kinds" (creators + subjects + genre/forms). Pass
    ``{"person", "corporate_body"}`` to limit to creators, or
    ``{"subject", "genre_form", "music_form"}`` to limit to the subject
    side. Explicit ``requests=`` overrides this filter — the caller is
    assumed to have done the filtering already.
    """
    settings = get_settings()
    canonical_path = canonical_path or (settings.data_dir / "canonical.ttl")
    output_path = output_path or canonical_path
    moment = (now or datetime.now(UTC)).replace(microsecond=0)
    selected_kinds: frozenset[AuthorityKind] = (
        ALL_AUTHORITY_KINDS if kinds is None else frozenset(kinds)
    )

    own_graph = graph is None
    target_graph: Graph
    if own_graph:
        target_graph = Graph()
        target_graph.parse(str(canonical_path), format="turtle")
    else:
        assert graph is not None  # narrow for mypy
        target_graph = graph

    request_list: list[EntityRequest] = (
        list(requests) if requests is not None else _collect_requests(target_graph, selected_kinds)
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
            local_resolver=local_resolver,
        )
        ended = datetime.now(UTC)
        outcomes.append(outcome)

        if outcome.stage == STAGE_LOCAL:
            summary.local += 1
        elif outcome.stage == STAGE_LEXICAL:
            summary.lexical += 1
        elif outcome.stage == STAGE_LLM:
            summary.llm_pick += 1
        elif outcome.stage == STAGE_FALLBACK:
            summary.fallback += 1
        else:
            summary.no_candidate += 1

        if outcome.chosen_uri is not None:
            _apply_canonical_link(target_graph, outcome.request, outcome.chosen_uri)
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
    "ALL_AUTHORITY_KINDS",
    "DEFAULT_TOP_K",
    "FINTO_BASE_URL",
    "LEXICAL_DIRECT_THRESHOLD",
    "LEXICAL_FLOOR",
    "LLM_CONFIDENCE_THRESHOLD",
    "PICKER_CONNECTION_BACKOFF_SECONDS",
    "PICKER_MAX_CONNECTION_RETRIES",
    "PICKER_MAX_VALIDATION_RETRIES",
    "PICKER_MIN_RATIONALE_CHARS",
    "PICKER_PROMPT_PATH",
    "PICKER_STUB_PHRASES",
    "PICKER_UNCERTAIN_MAX_CONFIDENCE",
    "STAGE_FALLBACK",
    "STAGE_LEXICAL",
    "STAGE_LLM",
    "STAGE_LOCAL",
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
    "LangChainLLMPicker",
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
    "picker_prompt_hash",
    "picker_prompt_text",
    "reconcile_one",
]
