"""Stage M9: reconciliation against KANTO / VIAF / YSO / KAUNO / MUSO.

Resolves the literal creator / subject strings on canonical Works
into authority URIs. The decision logic (spec § 6)
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

import concurrent.futures
import hashlib
import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol
from typing import Literal as LiteralType

if TYPE_CHECKING:
    from bffi_pipeline.stages.m9.local_concept_resolver import (
        LocalConceptHit,
        LocalConceptResolver,
    )

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from rdflib import Graph, Literal, URIRef
from rdflib import Literal as RdfLiteral
from rdflib.namespace import RDF

from bffi_pipeline.blocking import fold_diacritics, fold_label
from bffi_pipeline.cataloguer_review import append_target_row
from bffi_pipeline.config import get_settings
from bffi_pipeline.llm_json_mode import json_mode_instruction
from bffi_pipeline.observability.events import emit_if_active
from bffi_pipeline.observability.probes import (
    emit_health_probes,
    probe_finto,
    probe_fuseki,
    probe_mlx_lm,
)
from bffi_pipeline.observability.watchdog import emit_watchdog_event
from bffi_pipeline.provenance import logger as P
from bffi_pipeline.provenance import vocab as V

#: P-11 Phase A progress cadence for M9. Phase 2 is LLM-picker-bound
#: at ~2-5s per entity, so a too-sparse cadence makes the dashboard
#: look frozen for 5-15 minutes between updates. 10 keeps the sidecar
#: bounded (~500 events on a 5k-entity Phase 1 walk) and gives the
#: dashboard a tick every ~30-60s of Phase 2 picker work.
_M9_PROGRESS_CADENCE: Final[int] = 10

#: P-11 Phase C re-probe cadence for M9 Phase 1. One health probe per
#: N entities surfaces mid-stage degradation in the 12-hour overnight
#: run (a single entry probe doesn't catch a Fuseki crash at hour 4).
#: Picked larger than the progress cadence so probes don't pile up
#: every status-rendering pass.
_M9_HEALTH_PROBE_CADENCE: Final[int] = 1000


def _m9_probe_dependencies(local_resolver: LocalConceptResolver | None) -> None:
    """Run the M9 dependency probes + emit a single ``health`` event."""
    settings = get_settings()
    probes_to_emit = {
        "mlx-lm": probe_mlx_lm(settings.llm_base_url),
        "finto": probe_finto(),
    }
    if local_resolver is not None:
        probes_to_emit["fuseki"] = probe_fuseki(settings.fuseki_url)
    emit_health_probes("m9", probes_to_emit)


# --- Constants ------------------------------------------------------------

#: Spec § 6 thresholds. Tightening these requires a corresponding
#: plan amendment so policy changes stay visible in review.
LEXICAL_DIRECT_THRESHOLD: Final[float] = 0.95
LEXICAL_FLOOR: Final[float] = 0.70
LLM_CONFIDENCE_THRESHOLD: Final[float] = 0.80

#: Spec-committed authority kinds. KANTO and VIAF cover persons +
#: corporate bodies; YSO/KAUNO/MUSO cover subjects + genre/form. Phase 1
#: wires creators (KANTO+VIAF). Subjects land in phase 2.
#: ``fictional_character`` is a marker kind: cataloguer-tagged
#: ``(fiktiivinen hahmo)`` / ``(fiktiv gestalt)`` qualifiers on MARC
#: 6XX person labels mean the subject is a fictional entity that
#: doesn't exist in any general authority. Reconcile short-circuits
#: with a by-design ``"reconciliation-fictional-character"`` outcome
#: — saves the Finto/VIAF call AND reframes the metric (these aren't
#: pipeline failures, they're cataloguer-marked-unbindable).
AuthorityKind = LiteralType[
    "person",  # → KANTO, VIAF as fallback
    "corporate_body",  # → KANTO, VIAF as fallback
    "subject",  # → YSO
    "genre_form",  # → KAUNO
    "music_form",  # → MUSO
    "fictional_character",  # → skip both tiers; no authority carries fictional persons
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


#: Stage tags, kept aligned with spec § 8. These are the
#: same Literal type as :data:`ReconciliationStage` below; declared via
#: forward strings so mypy treats the constants as the narrowed Literal,
#: not just ``str``.
STAGE_LOCAL: Final[ReconciliationStage] = "reconciliation-local"
STAGE_LEXICAL: Final[ReconciliationStage] = "reconciliation-lexical"
STAGE_LLM: Final[ReconciliationStage] = "reconciliation-llm"
STAGE_FALLBACK: Final[ReconciliationStage] = "reconciliation-fallback"
STAGE_NO_CANDIDATE: Final[ReconciliationStage] = "reconciliation-no-candidate"
STAGE_FICTIONAL: Final[ReconciliationStage] = "reconciliation-fictional-character"

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
    "reconciliation-fictional-character",
]


@dataclass(frozen=True)
class ReconciliationOutcome:
    """Final outcome for one ``EntityRequest``.

    ``was_watchdog_aborted`` is the M9 analogue of M6's
    ``STAGE_WATCHDOG``: the picker call exceeded
    ``LLM_M9_FIELD_TIMEOUT_SECONDS`` so the outcome was built from
    tier-3 fallback (highest-lexical + needs-review). The flag drives
    the ``bffi-prov:stage = "watchdog-aborted"`` literal in the
    provenance graph (overrides ``stage`` for provenance purposes);
    the canonical-graph mutation stays the same as a normal fallback.

    ``cached_activity_uuid`` is populated by the P-10 Phase B
    picker-cache lookup: when set, the freshly-minted provenance
    Activity for this outcome carries ``prov:wasInfluencedBy
    <cached_activity_uuid>`` so the audit trail distinguishes "fresh
    LLM verdict" from "reused cached verdict". ``None`` for cache
    misses and for non-picker outcomes (tier-0 / tier-1 / no-candidate
    / fictional / watchdog-aborted).
    """

    request: EntityRequest
    stage: ReconciliationStage
    chosen_uri: str | None
    confidence: float
    rationale: str
    candidates: list[AuthorityCandidate]
    needs_review: bool
    was_watchdog_aborted: bool = False
    cached_activity_uuid: str | None = None
    # P-10 Phase B.1: the raw PickerDecision that produced this outcome.
    # Populated by tier-2 dispatch paths (STAGE_LLM + STAGE_FALLBACK);
    # ``None`` for tier-0 / tier-1 / no-candidate / fictional / watchdog-
    # aborted. Phase B's write-back logic stores this verbatim so the
    # warm-run lookup can replay the same _decide_with_pick(pick=…)
    # logic and reproduce the cold-run outcome byte-stably — including
    # for low-confidence ("uncertain" / "chose with conf < 0.80")
    # decisions that map to STAGE_FALLBACK. The model's per-call
    # non-determinism near the 0.80 threshold otherwise causes
    # cold/warm tier flips (audit script
    # ``scripts/p10-phase-b-cold-warm-audit.py`` surfaces these).
    picker_decision: PickerDecision | None = None

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
    fictional: int = 0
    watchdog_aborted: int = 0
    total: int = 0

    def render(self) -> str:
        """Format the reconciliation summary as paste-ready text for the reconcile CLI."""
        return "\n".join(
            (
                "M9 reconciliation complete",
                f"  total entities:                          {self.total:,}",
                f"  reconciliation-local:                    {self.local:,}",
                f"  reconciliation-lexical:                  {self.lexical:,}",
                f"  reconciliation-llm:                      {self.llm_pick:,}",
                f"  reconciliation-fallback:                 {self.fallback:,}",
                f"  reconciliation-no-candidate:             {self.no_candidate:,}",
                f"  reconciliation-fictional-character:      {self.fictional:,}",
                f"  watchdog-aborted (subset of fallback):   {self.watchdog_aborted:,}",
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
PICKER_PROMPT_PATH: Final[Path] = Path(__file__).resolve().parents[4] / "prompts" / "picker_v1.txt"
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


#: Max candidates rendered in the cataloguer-facing details column.
#: Caps row length so the TSV stays scannable in a spreadsheet; the
#: cataloguer can drill into the full candidate list via the
#: per-record provenance graph if more context is needed.
_DETAILS_CANDIDATE_LIMIT: Final[int] = 5


def _format_m9_details(outcome: ReconciliationOutcome) -> str:
    """Build the cataloguer-facing free-text context for one M9 outcome.

    Surfaces the literal we tried to reconcile, the top candidates that
    were considered (URI + prefLabel + source vocab + lexical sim), and
    the rationale that led to the no-candidate / fallback / fictional
    verdict. Cataloguers use this to judge whether the pipeline got
    the call right.
    """
    parts = [f"literal={outcome.request.literal!r} ({outcome.request.kind})"]
    if outcome.candidates:
        top = outcome.candidates[:_DETAILS_CANDIDATE_LIMIT]
        rendered = "; ".join(
            f"{c.uri} {c.pref_label!r} ({c.source_vocabulary}, sim={c.lexical_similarity:.2f})"
            for c in top
        )
        extra = len(outcome.candidates) - _DETAILS_CANDIDATE_LIMIT
        suffix = f" (+{extra} more)" if extra > 0 else ""
        parts.append(f"candidates: {rendered}{suffix}")
    else:
        parts.append("candidates: (none returned by the authority client)")
    rationale = (outcome.rationale or "").strip()
    if rationale:
        parts.append(f"rationale: {rationale}")
    return " | ".join(parts)


def _is_picker_connection_error(exc: BaseException) -> bool:
    """Mirror :func:`bffi_pipeline.stages.m6._is_connection_error` for the picker."""
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
    request_timeout_seconds: float | None = None,
) -> Any:
    """Compose ``ChatOpenAI(...).with_structured_output(PickerDecision)``.

    ``request_timeout_seconds`` plumbs the per-call HTTP timeout
    (``LLM_CALL_TIMEOUT_SECONDS``) onto the LangChain client so a
    stuck mlx-lm response is bounded at the HTTP layer rather than
    relying solely on the orchestrator-level per-field budget (plan
    P-10 Phase A.4). ``None`` means "rely on LangChain's own default"
    — used by the existing single-threaded callers and tests.
    """
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_openai import ChatOpenAI
    from pydantic import SecretStr

    sections = _parse_picker_prompt_sections()
    # JSON-mode schema instruction — see judge.py / llm_json_mode.py for the
    # rationale (P-02 A5: mlx-lm 0.31 has no constrained decoding, model
    # otherwise copies few-shot prose).
    template = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                sections["SYSTEM"]
                + "\n\n"
                + sections["EXAMPLES"]
                + "\n\n"
                + json_mode_instruction(PickerDecision),
            ),
            ("user", sections["USER"]),
        ]
    )
    chat_kwargs: dict[str, Any] = {
        "base_url": base_url,
        "api_key": SecretStr(api_key),
        "model": model_name,
        "temperature": temperature,
        "seed": seed,
    }
    if request_timeout_seconds is not None:
        chat_kwargs["request_timeout"] = request_timeout_seconds
    llm = ChatOpenAI(**chat_kwargs)
    return template | llm.with_structured_output(PickerDecision, method="json_mode")


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
            request_timeout_seconds=float(settings.llm_call_timeout_seconds),
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


def _decide_before_picker(
    *,
    request: EntityRequest,
    candidates: list[AuthorityCandidate],
) -> tuple[ReconciliationOutcome | None, list[AuthorityCandidate]]:
    """Tier-0/1 short-circuits without touching the picker.

    Returns ``(outcome, sorted_candidates)``. When ``outcome`` is not
    ``None``, the decision was made by the deterministic tiers
    (no-candidate / lexical-direct) and the picker is unnecessary.
    When ``outcome`` is ``None``, the caller must call
    :func:`_decide_with_pick` with a ``PickerDecision`` (or build a
    watchdog-aborted fallback) to finish the decision.
    """
    if not candidates:
        return (
            ReconciliationOutcome(
                request=request,
                stage=STAGE_NO_CANDIDATE,
                chosen_uri=None,
                confidence=0.0,
                rationale="No candidates returned by the authority client.",
                candidates=[],
                needs_review=False,
            ),
            [],
        )

    sorted_candidates = sorted(candidates, key=lambda c: c.lexical_similarity, reverse=True)
    top = sorted_candidates[0]

    if top.lexical_similarity < LEXICAL_FLOOR:
        return (
            ReconciliationOutcome(
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
            ),
            sorted_candidates,
        )

    high_similarity = [
        c for c in sorted_candidates if c.lexical_similarity >= LEXICAL_DIRECT_THRESHOLD
    ]
    if len(high_similarity) == 1:
        winner = high_similarity[0]
        return (
            ReconciliationOutcome(
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
            ),
            sorted_candidates,
        )

    return None, sorted_candidates


def _decide_with_pick(
    *,
    request: EntityRequest,
    sorted_candidates: list[AuthorityCandidate],
    pick: PickerDecision,
    lexical_fallback_floor: float = LEXICAL_FLOOR,
    lexical_fallback_floor_per_vocab: Mapping[str, float] | None = None,
    disable_fallback: bool = False,
) -> ReconciliationOutcome:
    """Apply tier-2 / tier-3 given an already-computed picker decision.

    P-10 Phase B.1: the original ``pick`` rides on the outcome's
    ``picker_decision`` field so the cache-write site can persist
    *every* picker call's verdict (not only the STAGE_LLM successes).
    Warm-cache replay then byte-stably reproduces the cold-run
    classification, including for low-confidence picks that map to
    STAGE_FALLBACK.

    P-16: ``lexical_fallback_floor``, ``lexical_fallback_floor_per_vocab``
    and ``disable_fallback`` gate the tier-3 fallback path. Defaults
    preserve pre-P-16 behaviour (floor = ``LEXICAL_FLOOR``, no per-vocab
    overrides, fallback enabled).
    """
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
            picker_decision=pick,
        )

    top = sorted_candidates[0]
    # P-16 Knob A + B: the fallback floor is per-vocabulary-overridable.
    # Vocabs not listed in ``lexical_fallback_floor_per_vocab`` fall
    # through to the global floor.
    per_vocab = lexical_fallback_floor_per_vocab or {}
    effective_floor = per_vocab.get(top.source_vocabulary, lexical_fallback_floor)
    # P-16 Knob C: hard-disable the tier-3 fallback path. Knob A/B are
    # subsumed when Knob C is on — we never bind. The order matters for
    # the rationale string.
    if disable_fallback:
        return ReconciliationOutcome(
            request=request,
            stage=STAGE_NO_CANDIDATE,
            chosen_uri=None,
            confidence=top.lexical_similarity,
            rationale=(
                f"LLM picker {pick.decision!r} (confidence "
                f"{pick.confidence:.2f}); tier-3 fallback hard-disabled via "
                f"BFFI_M9_DISABLE_FALLBACK. Top lexical was "
                f"{top.pref_label!r} ({top.lexical_similarity:.3f}). "
                f"Left unreconciled."
            ),
            candidates=sorted_candidates,
            needs_review=False,
            picker_decision=pick,
        )
    if top.lexical_similarity < effective_floor:
        return ReconciliationOutcome(
            request=request,
            stage=STAGE_NO_CANDIDATE,
            chosen_uri=None,
            confidence=top.lexical_similarity,
            rationale=(
                f"LLM picker {pick.decision!r} (confidence "
                f"{pick.confidence:.2f}); top lexical "
                f"{top.lexical_similarity:.3f} below the "
                f"{effective_floor:.2f} fallback floor for "
                f"{top.source_vocabulary!r}. Left unreconciled."
            ),
            candidates=sorted_candidates,
            needs_review=False,
            picker_decision=pick,
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
        picker_decision=pick,
    )


def _watchdog_aborted_outcome(
    *,
    request: EntityRequest,
    sorted_candidates: list[AuthorityCandidate],
    elapsed_seconds: float,
    budget_seconds: int,
) -> ReconciliationOutcome:
    """Build a tier-3-shaped outcome for a picker call that exceeded its budget.

    The canonical-graph mutation is identical to a normal fallback
    (highest-lexical + needs-review), so cataloguers see the same
    Skosmos UX. The ``was_watchdog_aborted`` flag drives the
    ``bffi-prov:stage = "watchdog-aborted"`` literal in the provenance
    graph so the audit trail distinguishes "LLM said uncertain" from
    "LLM never answered in time".
    """
    top = sorted_candidates[0]
    return ReconciliationOutcome(
        request=request,
        stage=STAGE_FALLBACK,
        chosen_uri=top.uri,
        confidence=top.lexical_similarity,
        rationale=(
            f"Picker exceeded the {budget_seconds}s per-field budget "
            f"(elapsed {elapsed_seconds:.1f}s); falling back to highest-lexical "
            f"candidate {top.pref_label!r} ({top.lexical_similarity:.3f}). "
            f"Flagged needs-review and bffi-prov:stage=watchdog-aborted."
        ),
        candidates=sorted_candidates,
        needs_review=True,
        was_watchdog_aborted=True,
    )


def decide_reconciliation(
    *,
    request: EntityRequest,
    candidates: list[AuthorityCandidate],
    picker: LLMPicker,
) -> ReconciliationOutcome:
    """Apply the four-tier logic from spec § 6.

    Kept for backwards compatibility with the ``reconcile_one``
    single-threaded path and existing unit tests. The P-10 Phase A
    concurrent orchestrator uses :func:`_decide_before_picker` and
    :func:`_decide_with_pick` directly so the picker dispatch can be
    parallelised and budget-wrapped.
    """
    outcome, sorted_candidates = _decide_before_picker(request=request, candidates=candidates)
    if outcome is not None:
        return outcome
    pick = picker.pick(request=request, candidates=sorted_candidates)
    return _decide_with_pick(request=request, sorted_candidates=sorted_candidates, pick=pick)


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

    Caches results per ``(vocab, query, date)`` per spec § 6
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
#: ``allars`` and ``bella`` are the Swedish-language parallels to
#: YSO and KAUNO respectively; ``$2 allars`` routes to ``"subject"``
#: and ``$2 bella`` routes to ``"genre_form"`` (same kind as the
#: existing ``$2 kaunokki`` which substring-matches ``"kauno"``).
_SOURCE_TOKEN_TO_KIND: Final[tuple[tuple[str, AuthorityKind], ...]] = (
    ("yso", "subject"),
    ("ysa", "subject"),
    ("allars", "subject"),
    ("kauno", "genre_form"),
    ("bella", "genre_form"),
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


#: Subject-as-name URI patterns minted by marc2bibframe2 for MARC 6XX
#: subject fields that name a person / corporate body / meeting. Frag
#: ID convention: ``#Agent<MARC tag><sequence>-<index>``. MARC 600 →
#: Personal Name; MARC 610 → Corporate Body; MARC 611 → Meeting Name.
#: Detected from the URI fragment because canonical.ttl carries only
#: ``rdfs:label`` on these targets — the upstream ``bf:Person`` /
#: ``bf:Agent`` types and the ``bflc:marcKey`` ``"6XX..."`` pattern
#: don't survive the M3→M8 propagation.
_SUBJECT_AS_NAME_FRAGMENT_RE: Final[re.Pattern[str]] = re.compile(r"#Agent6(00|10|11)-\d+$")
_AGENT_FRAGMENT_TO_KIND: Final[dict[str, AuthorityKind]] = {
    "00": "person",  # MARC 600
    "10": "corporate_body",  # MARC 610
    "11": "corporate_body",  # MARC 611 (meetings) → KANTO conferences
}

#: Parenthetical qualifiers cataloguers attach to MARC 6XX person
#: labels to mark them as fictional characters. ``(fiktiivinen
#: hahmo)`` is the Finnish form, ``(fiktiv gestalt)`` the Swedish
#: parallel — both surfaced on the 200-record corpus smoke. Matched
#: case-insensitively because cataloguing-side capitalisation isn't
#: uniform; literal-trailing because the qualifier always comes after
#: the name (``"Nicholson, Dorothy (fiktiivinen hahmo)"``).
_FICTIONAL_CHARACTER_QUALIFIERS: Final[tuple[str, ...]] = (
    "(fiktiivinen hahmo)",
    "(fiktiv gestalt)",
)


def _is_fictional_character_literal(literal: str) -> bool:
    """Return True iff the cataloguer-supplied label ends with a
    fictional-character qualifier (Finnish or Swedish form)."""
    stripped = literal.rstrip().casefold()
    return any(stripped.endswith(q) for q in _FICTIONAL_CHARACTER_QUALIFIERS)


def _classify_subject_target(
    target: URIRef | None, source: str | None, literal: str | None = None
) -> AuthorityKind:
    """Decide kind for a subject-target node.

    Order:

    1. Fictional-character qualifier in the literal (``"X (fiktiivinen
       hahmo)"``) → ``fictional_character``. Highest priority — no
       authority carries fictional persons; routing to KANTO would
       just spend a Finto call to learn nothing.
    2. ``Agent6XX`` URI-fragment pattern from marc2bibframe2 → ``person``
       / ``corporate_body`` so tier-1 hits KANTO instead of YSO.
    3. Fall back to :func:`_classify_subject_source` (``bf:source``
       token routing).
    """
    if literal is not None and _is_fictional_character_literal(literal):
        return "fictional_character"
    if target is not None:
        match = _SUBJECT_AS_NAME_FRAGMENT_RE.search(str(target))
        if match is not None:
            return _AGENT_FRAGMENT_TO_KIND[match.group(1)]
    return _classify_subject_source(source)


def _iter_subject_requests(graph: Graph) -> Iterator[EntityRequest]:
    """Yield reconciliation requests for unresolved ``bffi:subject`` /
    ``bffi:genreForm`` targets on canonical Works.

    Reconciles three target shapes (see :class:`SubjectTarget` in
    :mod:`bffi_pipeline.stages.m8`):

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
    from bffi_pipeline.stages.m10.load_finto import graph_uri_for_uri

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
                target_uri = target if isinstance(target, URIRef) else None
                literal_str = str(label_lit)
                yield EntityRequest(
                    work_uri=str(work),
                    literal=literal_str,
                    kind=_classify_subject_target(target_uri, source, literal_str),
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
    """Dispatch the per-kind binding logic on a successful reconciliation.

    The dispatch hinges on whether the request came from the *creator*
    walker (no ``predicate_uri`` set; reconciles MARC 100/700 agents
    on the canonical's primary contribution) or the *subject* walker
    (``predicate_uri`` set to ``bffi:subject`` / ``bffi:genreForm``;
    reconciles MARC 6XX subject-as-name + topical/place/genre fields).
    Same kind (``person`` / ``corporate_body``) routes through KANTO at
    tier-1 in both cases — the predicate decides whether the bound URI
    lands as ``bffi:creator`` or ``bffi:subject`` on the canonical.
    """
    if request.predicate_uri is None:
        # Creator-walker request — kind must be person or corporate_body.
        _link_canonical_creator(graph, request.work_uri, chosen_uri)
        return
    _link_canonical_subject(
        graph,
        work_uri=request.work_uri,
        chosen_uri=chosen_uri,
        predicate_uri=request.predicate_uri,
        literal=request.literal,
    )


#: Provenance ``bffi-prov:stage`` literal for outcomes where the picker
#: exceeded its per-field budget. Mirrors M6's ``STAGE_WATCHDOG`` literal
#: in ``stages/judge.py`` so cataloguers see one consistent marker
#: across stages.
STAGE_WATCHDOG_ABORTED: Final[str] = "watchdog-aborted"


def _load_canonical_bib_ids(path: Path) -> dict[str, list[str]]:
    """Read ``canonical-map.jsonl`` and build ``canonical_work_uri →
    [helmet_bib_id, …]`` for the P-31 Phase C target-review wiring.

    Returns an empty dict when the file isn't present (M9 run against
    a hand-crafted canonical.ttl without M8 having produced the
    sidecar). The target-review rows just carry empty
    ``member_bib_ids`` in that case — the cataloguer still has the
    canonical Work URI to drill into.
    """
    if not path.is_file():
        return {}
    out: dict[str, list[str]] = {}
    with path.open(encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            row = json.loads(line)
            uri = row.get("canonical_work_uri")
            ids = row.get("helmet_bib_ids") or []
            if isinstance(uri, str) and isinstance(ids, list):
                out[uri] = [b for b in ids if isinstance(b, str)]
    return out


def _emit_provenance(
    writer_graph: Graph | None,
    *,
    outcome: ReconciliationOutcome,
    started_at: datetime,
    ended_at: datetime,
) -> URIRef | None:
    if writer_graph is None:
        return None
    # Watchdog-aborted outcomes are recorded as ``"watchdog-aborted"`` in
    # provenance (matching M6's contract). The ``outcome.stage`` field
    # stays ``STAGE_FALLBACK`` for canonical-graph purposes — the
    # binding *is* a fallback — but the provenance Activity distinguishes
    # "LLM said uncertain" (``reconciliation-fallback``) from "LLM never
    # answered in time" (``watchdog-aborted``).
    stage_literal: str = STAGE_WATCHDOG_ABORTED if outcome.was_watchdog_aborted else outcome.stage
    # P-10 Phase B: cache-hit outcomes carry the cached Activity URI so
    # the new Activity links back via ``prov:wasInfluencedBy``.
    was_influenced_by = (
        URIRef(outcome.cached_activity_uuid) if outcome.cached_activity_uuid else None
    )
    return P.log_reconciliation(
        writer_graph,
        work_uri=outcome.request.work_uri,
        input_literal=outcome.request.literal,
        source_vocabulary=(
            outcome.candidates[0].source_vocabulary if outcome.candidates else "none"
        ),
        stage=stage_literal,
        chosen_authority_uri=outcome.chosen_uri,
        candidates=[(c.uri, c.lexical_similarity) for c in outcome.candidates],
        confidence=outcome.confidence,
        rationale=outcome.rationale,
        started_at=started_at,
        ended_at=ended_at,
        was_influenced_by=was_influenced_by,
    )


def _local_outcome(request: EntityRequest, hit: LocalConceptHit) -> ReconciliationOutcome:
    """Build a tier-0 outcome for a local-graph match.

    Synthesises a single :class:`AuthorityCandidate` with similarity
    1.0 so downstream provenance + summary code can treat tier-0 hits
    uniformly with the other tiers.

    P-10 Phase C: when the bind required the diacritic-fold + strip
    (``hit.is_fuzzy_match == True``), the outcome sets
    ``needs_review`` so cataloguers see the imperfect match in
    Skosmos. Exact-string matches keep ``needs_review=False`` and the
    binding is treated as auto-merged.
    """
    candidate = AuthorityCandidate(
        uri=hit.uri,
        pref_label=hit.pref_label,
        source_vocabulary=hit.source_vocabulary,
        lexical_similarity=1.0,
    )
    if hit.is_fuzzy_match:
        rationale = (
            f"Folded-label match in local {hit.source_vocabulary} graph: "
            f"cataloguer literal {request.literal!r} aligned with "
            f"{hit.pref_label!r} after diacritic-fold + decoration strip "
            f"(no Finto API call). Flagged needs-review for cataloguer audit."
        )
    else:
        rationale = (
            f"Exact prefLabel match in local {hit.source_vocabulary} graph: "
            f"{hit.pref_label!r} (no Finto API call)."
        )
    return ReconciliationOutcome(
        request=request,
        stage=STAGE_LOCAL,
        chosen_uri=hit.uri,
        confidence=1.0,
        rationale=rationale,
        candidates=[candidate],
        needs_review=hit.is_fuzzy_match,
    )


def _fictional_outcome(request: EntityRequest) -> ReconciliationOutcome:
    """Build a by-design no-bind outcome for a fictional-character label.

    No candidates, no chosen URI, ``needs_review=False`` — cataloguer
    already classified this entity as fictional with the parenthetical
    qualifier; downstream review queues should NOT show these. The
    distinct ``STAGE_FICTIONAL`` stage tag separates them from genuine
    ``reconciliation-no-candidate`` failures in summary + provenance.
    """
    return ReconciliationOutcome(
        request=request,
        stage=STAGE_FICTIONAL,
        chosen_uri=None,
        confidence=0.0,
        rationale=(
            f"Fictional-character label (cataloguer-tagged): {request.literal!r}. "
            "No general authority carries fictional persons; skipped by design."
        ),
        candidates=[],
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

    Order of short-circuits:

    1. ``fictional_character`` kind → ``reconciliation-fictional-character``
       outcome with no candidates. Cataloguer marked the entity as
       fictional; no authority carries it.
    2. Tier-0 ``local_resolver`` exact prefLabel match → no HTTP call,
       no LLM.
    3. Tier-1 ``client.query`` (with optional ``fallback_client``) and
       the four-tier decision logic.
    """
    if request.kind == "fictional_character":
        return _fictional_outcome(request)
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
    {
        "person",
        "corporate_body",
        "subject",
        "genre_form",
        "music_form",
        "fictional_character",
    }
)
_CREATOR_KINDS: Final[frozenset[AuthorityKind]] = frozenset({"person", "corporate_body"})
#: ``fictional_character`` walks alongside the subject kinds because
#: the marker comes from a MARC 6XX subject target whose label
#: carries the ``(fiktiivinen hahmo)`` qualifier; without it included
#: here, ``--kinds subjects`` would drop the marker before
#: ``reconcile_one`` could emit the by-design outcome.
_SUBJECT_KINDS: Final[frozenset[AuthorityKind]] = frozenset(
    {"subject", "genre_form", "music_form", "fictional_character"}
)


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


@dataclass(frozen=True)
class _Phase1Result:
    """One Phase 1 (tier-0 + candidate query) outcome.

    Either ``outcome`` is set (the entity resolved at fictional /
    tier-0 / lexical / no-candidate, picker dispatch unnecessary) or
    ``sorted_candidates`` is set (the entity needs tier-2 picker
    dispatch in Phase 2). Never both, never neither.
    """

    idx: int
    request: EntityRequest
    outcome: ReconciliationOutcome | None
    sorted_candidates: list[AuthorityCandidate] | None
    started_at: datetime


def _phase1_resolve_one(
    *,
    idx: int,
    request: EntityRequest,
    client: AuthorityClient,
    fallback_client: AuthorityClient | None,
    top_k: int,
    local_resolver: LocalConceptResolver | None,
) -> _Phase1Result:
    """Run tier-0 + candidate query for one entity.

    Stateless worker — all dependencies passed in. Thread-safe given
    that ``client``, ``fallback_client``, and ``local_resolver`` are
    HTTP-client-backed and stateless.
    """
    started = datetime.now(UTC)
    # Fictional-character marker short-circuit (tier-0 sibling).
    if request.kind == "fictional_character":
        return _Phase1Result(
            idx=idx,
            request=request,
            outcome=_fictional_outcome(request),
            sorted_candidates=None,
            started_at=started,
        )
    # Tier-0: local exact-prefLabel match.
    if local_resolver is not None:
        hit = local_resolver.resolve(literal=request.literal, kind=request.kind)
        if hit is not None:
            return _Phase1Result(
                idx=idx,
                request=request,
                outcome=_local_outcome(request, hit),
                sorted_candidates=None,
                started_at=started,
            )
    # Authority client candidate query.
    candidates = client.query(request=request, top_k=top_k)
    if not candidates and fallback_client is not None:
        candidates = fallback_client.query(request=request, top_k=top_k)
    # Tier-1 short-circuit OR queue for picker dispatch.
    outcome_or_none, sorted_candidates = _decide_before_picker(
        request=request, candidates=candidates
    )
    if outcome_or_none is not None:
        return _Phase1Result(
            idx=idx,
            request=request,
            outcome=outcome_or_none,
            sorted_candidates=None,
            started_at=started,
        )
    return _Phase1Result(
        idx=idx,
        request=request,
        outcome=None,
        sorted_candidates=sorted_candidates,
        started_at=started,
    )


def _phase1_seq(
    request_list: list[EntityRequest],
    *,
    client: AuthorityClient,
    fallback_client: AuthorityClient | None,
    top_k: int,
    local_resolver: LocalConceptResolver | None,
) -> list[_Phase1Result]:
    """Sequential (``phase1_concurrency <= 1``) path through Phase 1."""
    return [
        _phase1_resolve_one(
            idx=idx,
            request=request,
            client=client,
            fallback_client=fallback_client,
            top_k=top_k,
            local_resolver=local_resolver,
        )
        for idx, request in enumerate(request_list)
    ]


def _phase1_pool(
    request_list: list[EntityRequest],
    *,
    client: AuthorityClient,
    fallback_client: AuthorityClient | None,
    top_k: int,
    local_resolver: LocalConceptResolver | None,
    phase1_concurrency: int,
) -> list[_Phase1Result]:
    """Concurrent (``phase1_concurrency >= 2``) path through Phase 1.

    Workers share the orchestrator's ``client`` / ``fallback_client`` /
    ``local_resolver`` — all built on ``httpx.Client`` (thread-safe)
    plus stateless SPARQL queries. Results are sorted by submission
    index so downstream graph mutations + provenance emit
    deterministically regardless of completion order.
    """
    results: list[_Phase1Result] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=phase1_concurrency) as pool:
        futures = [
            pool.submit(
                _phase1_resolve_one,
                idx=idx,
                request=request,
                client=client,
                fallback_client=fallback_client,
                top_k=top_k,
                local_resolver=local_resolver,
            )
            for idx, request in enumerate(request_list)
        ]
        for fut in concurrent.futures.as_completed(futures):
            results.append(fut.result())
    results.sort(key=lambda r: r.idx)
    return results


def _field_id(request: EntityRequest) -> str:
    """Stable key for one M9 reconciliation field.

    Used as the ``pair_id`` argument when emitting watchdog events
    (the watchdog API uses ``pair_id`` for both M6 pairs and M9
    fields). The format mirrors what cataloguers see in the
    canonical graph: ``<work_uri>|<predicate>|<literal>``.
    """
    predicate = request.predicate_uri or request.kind
    return f"{request.work_uri}|{predicate}|{request.literal}"


#: Valid values for ``BFFI_M9_PICKER_ORDERING`` / ``apply_reconciliation``'s
#: ``picker_ordering`` parameter. ``submission`` (default) preserves the
#: walk order ``_collect_requests`` yielded; ``prefix-cache`` sorts by
#: prompt-prefix-similarity. Phase E's 2026-05-13 A/B bench (see
#: ``docs/performance/2026-05-13-5k-m2-max-phase-e.md``) showed
#: ``prefix-cache`` was a +5 % regression on the 5 k sample, so default
#: stays on ``submission`` until a re-bench on a more-homogeneous corpus
#: shows otherwise.
PickerOrdering = LiteralType["prefix-cache", "submission"]

PICKER_ORDERING_PREFIX_CACHE: Final[PickerOrdering] = "prefix-cache"
PICKER_ORDERING_SUBMISSION: Final[PickerOrdering] = "submission"


def _picker_queue_sort_key(
    entry: tuple[int, EntityRequest, list[AuthorityCandidate]],
) -> tuple[str, str, str, str]:
    """Sort key for the deferred picker queue (P-10 Phase E).

    Orders entries so that consecutive ``POST /v1/chat/completions``
    calls share the longest possible prompt prefix — mlx-lm's
    prompt-prefix cache then collapses per-call wall to roughly
    decode-time on runs of same-kind / same-vocabulary calls.

    Key, in order:

    1. ``request.kind`` — clusters fictional-character picks together,
       then person, then corporate_body, etc. The picker prompt has
       kind-conditional sections in ``prompts/picker_v1.txt``, so picks
       of the same kind share the longest static prompt prefix.
    2. ``candidates[0].source_vocabulary`` — within a kind, cluster by
       the dominant candidate vocabulary (``yso``, ``finaf``, ``kauno``,
       ``viaf``, …). Same-vocabulary candidates share authority-style
       formatting in the rendered candidate list.
    3. A stable fingerprint of ``sorted(c.uri for c in candidates)`` —
       within a kind+vocab cluster, group calls with overlapping
       candidate sets. Identical / near-identical candidate sets share
       long prompt-body prefixes.
    4. ``request.literal`` — final tie-breaker for byte-stability
       across runs (the literal varies last in the prompt).

    Output of ``_apply_reconciliation`` is byte-stable regardless of the
    ordering chosen, because the orchestrator sorts ``picker_results`` by
    submission ``idx`` before applying graph mutations.
    """
    _idx, request, candidates = entry
    vocab = candidates[0].source_vocabulary if candidates else ""
    fingerprint = "|".join(sorted(c.uri for c in candidates))
    return (request.kind, vocab, fingerprint, request.literal)


def _order_deferred_picker_queue(
    deferred: list[tuple[int, EntityRequest, list[AuthorityCandidate]]],
    *,
    ordering: PickerOrdering,
) -> list[tuple[int, EntityRequest, list[AuthorityCandidate]]]:
    """Return ``deferred`` in the order requested by ``ordering``.

    The orchestrator dispatches the returned list to the picker pool;
    the result-merge that follows re-sorts by submission ``idx`` so the
    canonical Turtle is byte-stable across both ordering modes. See
    :func:`_picker_queue_sort_key` for the prefix-cache key.
    """
    if ordering == PICKER_ORDERING_SUBMISSION:
        return deferred
    # ``prefix-cache`` — Python's ``sorted`` is stable, so equal keys
    # preserve their submission order (deterministic tie-break).
    return sorted(deferred, key=_picker_queue_sort_key)


def _emit_picker_progress(
    completed: int,
    *,
    total: int,
    cache_hits: int,
    watchdog_aborted: int,
    llm_pick: int,
    fallback: int,
) -> None:
    """Emit one M9 Phase 2 ``progress`` event.

    Centralised here so the seq + pool paths share one payload shape;
    the dashboard's m9 progress panel can render both cold and warm
    runs without per-path branching. P-12 Phase D.

    ``llm_pick`` and ``fallback`` are mid-run cumulative tier counts
    the exporter mirrors into ``bffi_stage_outcomes_total`` so the
    dashboard's M9 outcome bargauge populates live during Phase 2
    instead of jumping from empty to fully populated at the ``end``
    event.
    """
    emit_if_active(
        stage="m9",
        event="progress",
        phase="phase2",
        counters={"processed": completed, "total": total},
        extra={
            "cache_hits": cache_hits,
            "watchdog_aborted": watchdog_aborted,
            "llm_pick": llm_pick,
            "fallback": fallback,
        },
    )


def _picker_phase_seq(
    deferred: list[tuple[int, EntityRequest, list[AuthorityCandidate]]],
    *,
    picker: LLMPicker,
    field_timeout_seconds: int,
    model_name: str,
    watchdog_sidecar_path: Path | None,
    progress_cadence: int = _M9_PROGRESS_CADENCE,
    cache_hits: int = 0,
    lexical_fallback_floor: float = LEXICAL_FLOOR,
    lexical_fallback_floor_per_vocab: Mapping[str, float] | None = None,
    disable_fallback: bool = False,
) -> list[tuple[int, ReconciliationOutcome]]:
    """Sequential (c=1) path: call the shared picker inline per field.

    P-12 Phase D: emit a ``progress`` event every ``progress_cadence``
    completed calls. ``cache_hits`` is fixed at Phase-1.5 exit time so
    the caller passes it in once; ``watchdog_aborted`` is tallied
    locally from the results stream.

    P-16: ``lexical_fallback_floor`` / ``lexical_fallback_floor_per_vocab``
    / ``disable_fallback`` forward to :func:`_decide_with_pick` to gate
    the tier-3 fallback. Defaults preserve pre-P-16 behaviour.
    """
    results: list[tuple[int, ReconciliationOutcome]] = []
    watchdog_aborted = 0
    llm_pick = 0
    fallback = 0
    for idx, request, sorted_candidates in deferred:
        outcome, _events = _picker_call_with_budget(
            picker=picker,
            request=request,
            sorted_candidates=sorted_candidates,
            field_timeout_seconds=field_timeout_seconds,
            model_name=model_name,
            watchdog_sidecar_path=watchdog_sidecar_path,
            lexical_fallback_floor=lexical_fallback_floor,
            lexical_fallback_floor_per_vocab=lexical_fallback_floor_per_vocab,
            disable_fallback=disable_fallback,
        )
        results.append((idx, outcome))
        if outcome.was_watchdog_aborted:
            watchdog_aborted += 1
        if outcome.stage == STAGE_LLM:
            llm_pick += 1
        elif outcome.stage == STAGE_FALLBACK:
            fallback += 1
        if progress_cadence > 0 and len(results) % progress_cadence == 0:
            _emit_picker_progress(
                len(results),
                total=len(deferred),
                cache_hits=cache_hits,
                watchdog_aborted=watchdog_aborted,
                llm_pick=llm_pick,
                fallback=fallback,
            )
    # End-of-phase flush: emit one final progress event when the run
    # didn't land on a cadence boundary, so the dashboard's processed
    # gauge ends at 100 % of the phase total instead of plateauing at
    # the last cadence multiple.
    if progress_cadence > 0 and len(results) > 0 and len(results) % progress_cadence != 0:
        _emit_picker_progress(
            len(results),
            total=len(deferred),
            cache_hits=cache_hits,
            watchdog_aborted=watchdog_aborted,
            llm_pick=llm_pick,
            fallback=fallback,
        )
    return results


def _picker_phase_pool(
    deferred: list[tuple[int, EntityRequest, list[AuthorityCandidate]]],
    *,
    picker_factory: Callable[[], LLMPicker],
    concurrency: int,
    field_timeout_seconds: int,
    model_name: str,
    watchdog_sidecar_path: Path | None,
    progress_cadence: int = _M9_PROGRESS_CADENCE,
    cache_hits: int = 0,
    lexical_fallback_floor: float = LEXICAL_FLOOR,
    lexical_fallback_floor_per_vocab: Mapping[str, float] | None = None,
    disable_fallback: bool = False,
) -> list[tuple[int, ReconciliationOutcome]]:
    """Concurrent (c>=2) path: thread-local pickers, parallel dispatch.

    Each worker thread constructs its own ``LLMPicker`` on first use
    via ``threading.local()``. LangChain's underlying OpenAI-compat
    client has no documented thread-safety guarantee, and building
    one picker per worker is cheap.

    Worker results are collected and returned in submission-index
    order so the caller can apply graph mutations deterministically
    regardless of completion order.

    P-12 Phase D: the orchestrator-side ``as_completed`` loop is
    single-threaded, so the cadence counter + emit run inline there
    (no worker-thread emission, no shared lock). Each completed
    future increments ``completed``; on a cadence boundary the
    progress event fires with the running counts of cache hits +
    watchdog-aborted picks so the dashboard surfaces picker stress
    live.
    """
    thread_local = threading.local()
    _RunArgs = tuple[int, EntityRequest, list[AuthorityCandidate]]
    _RunResult = tuple[int, ReconciliationOutcome]

    def _run(args: _RunArgs) -> _RunResult:
        idx, request, sorted_candidates = args
        picker = getattr(thread_local, "picker", None)
        if picker is None:
            picker = picker_factory()
            thread_local.picker = picker
        outcome, _events = _picker_call_with_budget(
            picker=picker,
            request=request,
            sorted_candidates=sorted_candidates,
            field_timeout_seconds=field_timeout_seconds,
            model_name=model_name,
            watchdog_sidecar_path=watchdog_sidecar_path,
            lexical_fallback_floor=lexical_fallback_floor,
            lexical_fallback_floor_per_vocab=lexical_fallback_floor_per_vocab,
            disable_fallback=disable_fallback,
        )
        return idx, outcome

    results: list[tuple[int, ReconciliationOutcome]] = []
    watchdog_aborted = 0
    llm_pick = 0
    fallback = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_run, item) for item in deferred]
        for fut in concurrent.futures.as_completed(futures):
            idx, outcome = fut.result()
            results.append((idx, outcome))
            if outcome.was_watchdog_aborted:
                watchdog_aborted += 1
            if outcome.stage == STAGE_LLM:
                llm_pick += 1
            elif outcome.stage == STAGE_FALLBACK:
                fallback += 1
            if progress_cadence > 0 and len(results) % progress_cadence == 0:
                _emit_picker_progress(
                    len(results),
                    total=len(deferred),
                    cache_hits=cache_hits,
                    watchdog_aborted=watchdog_aborted,
                    llm_pick=llm_pick,
                    fallback=fallback,
                )
    # End-of-phase flush — see _picker_phase_seq for rationale.
    if progress_cadence > 0 and len(results) > 0 and len(results) % progress_cadence != 0:
        _emit_picker_progress(
            len(results),
            total=len(deferred),
            cache_hits=cache_hits,
            watchdog_aborted=watchdog_aborted,
            llm_pick=llm_pick,
            fallback=fallback,
        )
    results.sort(key=lambda t: t[0])
    return results


def _picker_call_with_budget(
    *,
    picker: LLMPicker,
    request: EntityRequest,
    sorted_candidates: list[AuthorityCandidate],
    field_timeout_seconds: int,
    model_name: str,
    watchdog_sidecar_path: Path | None,
    lexical_fallback_floor: float = LEXICAL_FLOOR,
    lexical_fallback_floor_per_vocab: Mapping[str, float] | None = None,
    disable_fallback: bool = False,
) -> tuple[ReconciliationOutcome, int]:
    """Run ``picker.pick`` with a per-field wall budget.

    Returns ``(outcome, watchdog_event_count)``. When the budget is
    exceeded, the outcome is a tier-3 fallback marked
    ``was_watchdog_aborted=True`` and one
    ``field_budget_exceeded`` event is emitted to stderr +
    ``watchdog_sidecar_path``. ``field_timeout_seconds <= 0`` disables
    budget enforcement (test / rollback use case).

    Budget enforcement uses a single-thread ``ThreadPoolExecutor``
    inside the worker so a stuck picker call doesn't block the outer
    thread's progress. The inner thread is then ``shutdown(wait=False)``
    — the stuck call eventually completes (bounded by
    ``LLM_CALL_TIMEOUT_SECONDS`` times the picker retry count via the
    underlying httpx client) and reclaims its slot.
    """
    pair_id = _field_id(request)
    started = time.monotonic()

    if field_timeout_seconds <= 0:
        pick = picker.pick(request=request, candidates=sorted_candidates)
        outcome = _decide_with_pick(
            request=request,
            sorted_candidates=sorted_candidates,
            pick=pick,
            lexical_fallback_floor=lexical_fallback_floor,
            lexical_fallback_floor_per_vocab=lexical_fallback_floor_per_vocab,
            disable_fallback=disable_fallback,
        )
        return outcome, 0

    inner = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix=f"picker-budget-{pair_id[:32]}"
    )
    fut = inner.submit(picker.pick, request=request, candidates=sorted_candidates)
    try:
        pick = fut.result(timeout=field_timeout_seconds)
    except concurrent.futures.TimeoutError:
        elapsed = time.monotonic() - started
        inner.shutdown(wait=False)
        emit_watchdog_event(
            pair_id=pair_id,
            event="field_budget_exceeded",
            model_name=model_name,
            elapsed_seconds=elapsed,
            retry_n=0,
            sidecar_path=watchdog_sidecar_path,
        )
        outcome = _watchdog_aborted_outcome(
            request=request,
            sorted_candidates=sorted_candidates,
            elapsed_seconds=elapsed,
            budget_seconds=field_timeout_seconds,
        )
        return outcome, 1

    inner.shutdown(wait=False)
    outcome = _decide_with_pick(
        request=request,
        sorted_candidates=sorted_candidates,
        pick=pick,
        lexical_fallback_floor=lexical_fallback_floor,
        lexical_fallback_floor_per_vocab=lexical_fallback_floor_per_vocab,
        disable_fallback=disable_fallback,
    )
    return outcome, 0


#: Concurrency value at or above which ``picker_factory`` becomes
#: mandatory (each worker thread builds its own LLMPicker).
_MIN_CONCURRENCY_FOR_FACTORY: Final[int] = 2


# --- P-10 Phase B: persistent picker decision cache -----------------------
#
# Mirrors M6's :class:`~bffi_pipeline.stages.m6.JudgeCache`:
# a SQLite-backed key/value store that survives ``apply_reconciliation``
# runs so the same ``(literal, candidate_set, prompt, model,
# finto_vocab+sha)`` tuple does not re-pay an LLM picker call. The key
# is Finto-version-aware: refreshing a vocabulary dump (different
# SHA-256) invalidates the per-vocab slice cleanly on next lookup.

#: Default filename. Mirrors M6's ``judge-cache.sqlite`` naming.
PICKER_CACHE_FILENAME: Final[str] = "reconcile-cache.sqlite"


def picker_cache_default_path() -> Path:
    """Return ``<BFFI_DATA_DIR>/reconcile-cache.sqlite`` from live Settings."""
    return get_settings().data_dir / PICKER_CACHE_FILENAME


def hash_finto_dump(path: Path) -> str:
    """SHA-256 of one Finto vocabulary dump file.

    Read in 1 MiB chunks so the hash is bounded by I/O, not by file
    size — KANTO is ~183 MB and LCSH ~465 MB decompressed.
    """
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_finto_shas(dumps_dir: Path) -> dict[str, str]:
    """Hash every ``<vocab>-skos.ttl`` in ``dumps_dir``.

    Returns a dict keyed by vocab slug (the part before ``-skos.ttl``).
    Missing / non-existent dumps directories return an empty dict so
    callers distinguish "vocab not cached locally" (skip caching) from
    "vocab cached but refreshed" (key mismatch → cache miss). Computed
    once per :func:`apply_reconciliation` run, not per-call.
    """
    if not dumps_dir.is_dir():
        return {}
    shas: dict[str, str] = {}
    for path in sorted(dumps_dir.glob("*-skos.ttl")):
        vocab = path.stem.removesuffix("-skos")
        shas[vocab] = hash_finto_dump(path)
    return shas


def compute_picker_cache_key(
    *,
    request: EntityRequest,
    candidates: Iterable[AuthorityCandidate],
    prompt_hash_value: str,
    model_name: str,
    finto_shas: dict[str, str],
) -> tuple[str, str, str] | None:
    """Return ``(key, vocab, finto_sha)`` or ``None`` if the call must skip cache.

    Skip conditions (return ``None``):

    - Empty candidate list — picker would short-circuit to ``uncertain``
      anyway, no value in caching.
    - VIAF-source candidates — no local dump to anchor the version,
      so caching would risk binding to upstream-drifted data.
    - Vocab has no on-disk Finto dump (no SHA to invalidate against).

    Otherwise key = SHA-256 of:

    .. code-block:: text

        fold(literal) | sorted(candidate.uri) | prompt_hash |
        model_name | vocab:finto_sha

    ``fold(literal)`` uses :func:`bffi_pipeline.blocking.fold_label`
    so diacritic-equivalent literals hit the same cached decision.
    """
    candidate_list = list(candidates)
    if not candidate_list:
        return None
    vocab = candidate_list[0].source_vocabulary
    if vocab == VOCAB_VIAF:
        return None
    finto_sha = finto_shas.get(vocab)
    if not finto_sha:
        return None
    payload = "|".join(
        (
            fold_label(request.literal),
            ",".join(sorted(c.uri for c in candidate_list)),
            prompt_hash_value,
            model_name,
            f"{vocab}:{finto_sha}",
        )
    )
    key = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return key, vocab, finto_sha


@dataclass(frozen=True)
class CacheHit:
    """One row returned by :meth:`PickerCache.get`.

    Carries the cached :class:`PickerDecision` plus the original
    Activity URI so the orchestrator wires the new provenance Activity
    through ``prov:wasInfluencedBy``.
    """

    decision: PickerDecision
    activity_uuid: str


class PickerCache:
    """SQLite-backed cache for validated :class:`PickerDecision` rows.

    Writes happen only after the picker has returned a structurally
    and semantically valid response (the same gate
    :class:`~bffi_pipeline.stages.m6.JudgeCache` applies): a
    ``ValidationError`` short-circuits before the write so a re-run
    can recover once the model is updated or the prompt is fixed.

    Concurrency contract: one instance is shared across picker-pool
    worker threads. A :class:`threading.Lock` serialises every SQLite
    call (mirroring M6's cross-thread fix in commit ``1452a4f``: the
    ``sqlite3`` module does *not* serialise concurrent statement
    execution on a shared connection). The lock is held only for the
    SQLite call itself, so contention is negligible next to the
    seconds-long LLM call. ``BEGIN IMMEDIATE`` on writes prevents two
    threads from racing on the same key.
    """

    _SCHEMA: Final[str] = """
    CREATE TABLE IF NOT EXISTS picker_cache (
      cache_key       TEXT PRIMARY KEY,
      decision_json   TEXT NOT NULL,
      finto_vocab     TEXT NOT NULL,
      finto_sha       TEXT NOT NULL,
      prompt_hash     TEXT NOT NULL,
      model_name      TEXT NOT NULL,
      activity_uuid   TEXT NOT NULL,
      decided_at      TEXT NOT NULL
    )
    """

    _INDEX: Final[str] = (
        "CREATE INDEX IF NOT EXISTS picker_cache_vocab_sha ON picker_cache(finto_vocab, finto_sha)"
    )

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        with self._lock:
            self._conn.execute(self._SCHEMA)
            self._conn.execute(self._INDEX)
            self._conn.commit()

    def __enter__(self) -> PickerCache:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying SQLite connection (idempotent)."""
        with suppress(sqlite3.ProgrammingError), self._lock:
            self._conn.close()

    def get(self, key: str) -> CacheHit | None:
        """Return the cached :class:`CacheHit` for ``key`` or ``None``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT decision_json, activity_uuid FROM picker_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return CacheHit(
            decision=PickerDecision.model_validate_json(row[0]),
            activity_uuid=row[1],
        )

    def set(
        self,
        key: str,
        *,
        decision: PickerDecision,
        finto_vocab: str,
        finto_sha: str,
        prompt_hash_value: str,
        model_name: str,
        activity_uuid: str,
    ) -> None:
        """Insert-or-replace the cached decision for ``key``.

        Uses ``BEGIN IMMEDIATE`` so two worker threads that compute
        the same key concurrently never produce a half-written row —
        the second writer blocks at the BEGIN and then overwrites
        cleanly (idempotent in content; decision JSON / Activity URI
        are deterministic per input).
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO picker_cache "
                    "(cache_key, decision_json, finto_vocab, finto_sha, "
                    "prompt_hash, model_name, activity_uuid, decided_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        decision.model_dump_json(),
                        finto_vocab,
                        finto_sha,
                        prompt_hash_value,
                        model_name,
                        activity_uuid,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise


@dataclass(frozen=True)
class _CachePending:
    """Phase-2 → Phase-3 hand-off for cache misses that need write-back.

    Stored per-``idx`` while the picker pool runs. Phase 3 reads this
    after :func:`_emit_provenance` mints the Activity URI; the URI is
    then committed to the cache as ``activity_uuid``.
    """

    cache_key: str
    finto_vocab: str
    finto_sha: str
    decision: PickerDecision


def apply_reconciliation(  # noqa: PLR0912, PLR0915 — three-phase orchestrator (tier-0/1, picker dispatch, mutation + provenance); splitting fragments shared state across phases.
    canonical_path: Path | None = None,
    *,
    output_path: Path | None = None,
    client: AuthorityClient,
    fallback_client: AuthorityClient | None = None,
    picker: LLMPicker | None = None,
    picker_factory: Callable[[], LLMPicker] | None = None,
    provenance_graph: Graph | None = None,
    requests: Iterable[EntityRequest] | None = None,
    graph: Graph | None = None,
    top_k: int = DEFAULT_TOP_K,
    now: datetime | None = None,
    kinds: set[AuthorityKind] | frozenset[AuthorityKind] | None = None,
    local_resolver: LocalConceptResolver | None = None,
    concurrency: int = 1,
    field_timeout_seconds: int = 0,
    watchdog_sidecar_path: Path | None = None,
    phase1_concurrency: int = 1,
    picker_ordering: PickerOrdering = PICKER_ORDERING_PREFIX_CACHE,
    picker_cache: PickerCache | None = None,
    finto_dumps_dir: Path | None = None,
    lexical_fallback_floor: float = LEXICAL_FLOOR,
    lexical_fallback_floor_per_vocab: Mapping[str, float] | None = None,
    disable_fallback: bool = False,
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

    P-10 Phase A: tier-2 picker calls are dispatched through a
    ``ThreadPoolExecutor(max_workers=concurrency)``; tier-0, tier-1,
    tier-3 (no-candidate) stay single-threaded. ``concurrency == 1``
    keeps the pre-Phase-A sequential behaviour for rollback /
    deterministic tests. Each picker call is wrapped in a
    ``field_timeout_seconds``-second wall budget; on exceed, the field
    falls through to tier-3 fallback (highest-lexical + needs-review)
    with the provenance Activity stamped
    ``bffi-prov:stage = "watchdog-aborted"``.

    P-10 Phase A2: Phase 1 (tier-0 SPARQL + Finto/VIAF candidate
    query) is also dispatched through its own pool sized by
    ``phase1_concurrency``. Defaults to ``1`` (sequential) so existing
    callers / tests are byte-stable; production CLI passes the
    ``M9_PHASE1_CONCURRENCY`` setting (default 8) — Phase 1's
    binding constraint is HTTP / SPARQL throughput rather than the
    GPU-bound mlx-lm picker, so it tolerates higher concurrency.

    P-10 Phase E: ``picker_ordering`` controls the dispatch order of
    deferred picker entries. ``"submission"`` (default) preserves the
    walk order ``_collect_requests`` yielded; ``"prefix-cache"`` sorts
    so consecutive ``POST /v1/chat/completions`` calls share the longest
    possible prompt prefix, intended to lift mlx-lm prefix-cache reuse.
    The 2026-05-13 A/B bench showed ``"prefix-cache"`` regressed the
    picker-phase wall by 5 % on the heterogeneous 5 k sample, so the
    default stays on ``"submission"``. Output is byte-stable under both
    modes — the orchestrator re-sorts results by submission ``idx``
    before graph mutation.

    P-10 Phase B: ``picker_cache`` is a :class:`PickerCache` shared
    across worker threads; when set, picker-bound entries first
    consult the cache before dispatching to the LLM. Cache hits skip
    the picker entirely and write a provenance Activity with
    ``prov:wasInfluencedBy <cached-activity>``. Cache misses run the
    picker as before, then write the verdict back so a re-run hits.
    ``finto_dumps_dir`` (defaults to ``settings.finto_dump_dir`` —
    ``<repo>/finto-dumps`` out of the box, overridable via
    ``BFFI_FINTO_DUMP_DIR``) locates the per-vocab dumps whose SHA-256 anchors cache validity —
    a refresh of one ``<vocab>-skos.ttl`` invalidates that vocab's
    cached entries on the next lookup.

    Pass ``picker_factory`` for concurrent runs (one ``LLMPicker`` is
    built per worker thread). Pass ``picker`` for single-threaded
    runs (existing callers / tests). At least one of the two must be
    supplied.
    """
    settings = get_settings()
    canonical_path = canonical_path or (settings.data_dir / "canonical.ttl")
    output_path = output_path or canonical_path
    moment = (now or datetime.now(UTC)).replace(microsecond=0)
    # P-31 Phase C: load the canonical-map sidecar so the M9 target-
    # review row carries member_bib_ids — cataloguers use those to
    # locate the source MARC and estimate the bug's severity (a wrong
    # cluster spanning two famous authors is more impactful than one
    # between two obscure ones).
    canonical_bib_ids = _load_canonical_bib_ids(settings.data_dir / "canonical-map.jsonl")
    # P-12 Phase D: cadence is operator-tunable via BFFI_M9_PROGRESS_CADENCE
    # so short benches can crank it down (e.g. 50) for a livelier dashboard.
    # Default 200 matches the pre-P-12 module-level constant.
    progress_cadence = settings.m9_progress_cadence
    selected_kinds: frozenset[AuthorityKind] = (
        ALL_AUTHORITY_KINDS if kinds is None else frozenset(kinds)
    )

    if picker is None and picker_factory is None:
        raise ValueError("apply_reconciliation requires picker or picker_factory")
    if concurrency >= _MIN_CONCURRENCY_FOR_FACTORY and picker_factory is None:
        raise ValueError(
            "apply_reconciliation requires picker_factory when concurrency >= 2 "
            "(each worker thread builds its own LLMPicker)"
        )
    # Single picker for the c=1 path. ``picker`` takes precedence if both are
    # supplied so tests that inject a ``StubPicker`` continue to work.
    seq_picker: LLMPicker | None = picker
    if seq_picker is None and picker_factory is not None:
        seq_picker = picker_factory()

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

    # P-11 Phase A: stage-level start event so the status CLI / dashboard
    # see M9 begin. Per-phase progress events fire from the helpers below.
    emit_if_active(
        stage="m9",
        event="start",
        counters={"total": len(request_list)},
        extra={
            "concurrency": concurrency,
            "phase1_concurrency": phase1_concurrency,
            "field_timeout_seconds": field_timeout_seconds,
            "picker_ordering": picker_ordering,
        },
    )
    # P-11 Phase C: probe Fuseki / mlx-lm / Finto at entry; surfaces a
    # red panel on the dashboard immediately if any are unreachable.
    _m9_probe_dependencies(local_resolver)

    # --- Phase 1: walk requests through tier-0 + candidate-query ----------
    # Each request resolves either to a final outcome (fictional / tier-0 /
    # lexical / no-candidate) or to a deferred ``(request, sorted_candidates)``
    # picker entry for Phase 2. P-10 Phase A2 dispatches the walk through a
    # ``ThreadPoolExecutor`` sized by ``phase1_concurrency`` — Phase 1's
    # cost is dominated by HTTP / SPARQL throughput, not GPU, so it scales
    # independently of the picker concurrency.
    pre_outcomes: dict[int, ReconciliationOutcome] = {}
    deferred: list[tuple[int, EntityRequest, list[AuthorityCandidate]]] = []
    started_at: dict[int, datetime] = {}

    emit_if_active(
        stage="m9",
        event="phase_boundary",
        phase="phase1",
        counters={"total": len(request_list)},
    )

    phase1_results: list[_Phase1Result]
    if phase1_concurrency <= 1:
        phase1_results = _phase1_seq(
            request_list,
            client=client,
            fallback_client=fallback_client,
            top_k=top_k,
            local_resolver=local_resolver,
        )
    else:
        phase1_results = _phase1_pool(
            request_list,
            client=client,
            fallback_client=fallback_client,
            top_k=top_k,
            local_resolver=local_resolver,
            phase1_concurrency=phase1_concurrency,
        )

    # Phase 1 result collation + per-cadence progress emission. We tally
    # per-tier outcomes as we go so the dashboard's M9 outcome bargauge
    # populates live during Phase 1 — split out by tier (local /
    # lexical / no_candidate / fictional) so the exporter can mirror
    # the keys into ``bffi_stage_outcomes_total`` via the
    # ``_PROGRESS_OUTCOME_KEYS`` bridge.
    phase1_local = 0
    phase1_deferred = 0
    tier_local = 0
    tier_lexical = 0
    tier_no_candidate = 0
    tier_fictional = 0
    for i, result in enumerate(phase1_results):
        started_at[result.idx] = result.started_at
        if result.outcome is not None:
            pre_outcomes[result.idx] = result.outcome
            phase1_local += 1
            if result.outcome.stage == STAGE_LOCAL:
                tier_local += 1
            elif result.outcome.stage == STAGE_LEXICAL:
                tier_lexical += 1
            elif result.outcome.stage == STAGE_NO_CANDIDATE:
                tier_no_candidate += 1
            elif result.outcome.stage == STAGE_FICTIONAL:
                tier_fictional += 1
        else:
            assert result.sorted_candidates is not None  # invariant from _Phase1Result
            deferred.append((result.idx, result.request, result.sorted_candidates))
            phase1_deferred += 1
        if progress_cadence > 0 and (i + 1) % progress_cadence == 0:
            emit_if_active(
                stage="m9",
                event="progress",
                phase="phase1",
                counters={"processed": i + 1, "total": len(request_list)},
                extra={
                    "resolved": phase1_local,
                    "deferred_to_picker": phase1_deferred,
                    "local": tier_local,
                    "lexical": tier_lexical,
                    "no_candidate": tier_no_candidate,
                    "fictional": tier_fictional,
                },
            )
        # P-11 Phase C: re-probe mid-stage so the dashboard catches a
        # late-run dependency outage (e.g. Fuseki OOM at hour 4 of an
        # overnight run). Cheap — one probe per 1000 entities.
        if (i + 1) % _M9_HEALTH_PROBE_CADENCE == 0:
            _m9_probe_dependencies(local_resolver)

    # End-of-phase flush: emit one final progress event when the walk
    # didn't land on a cadence boundary so the dashboard reads 100 %
    # of phase 1 instead of plateauing at the last cadence multiple.
    if (
        progress_cadence > 0
        and len(phase1_results) > 0
        and len(phase1_results) % progress_cadence != 0
    ):
        emit_if_active(
            stage="m9",
            event="progress",
            phase="phase1",
            counters={
                "processed": len(phase1_results),
                "total": len(request_list),
            },
            extra={
                "resolved": phase1_local,
                "deferred_to_picker": phase1_deferred,
                "local": tier_local,
                "lexical": tier_lexical,
                "no_candidate": tier_no_candidate,
                "fictional": tier_fictional,
            },
        )

    # --- Phase 1.5: consult the picker cache for deferred entries ---------
    # P-10 Phase B: single-threaded loop *before* the pool dispatch so that
    # N worker threads cannot race on the same uncached key. Cache hits
    # short-circuit Phase 2 entirely with the cached PickerDecision +
    # the original Activity URI (later wired through wasInfluencedBy).
    # Cache misses stay in ``deferred_misses`` and feed Phase 2; their
    # write-back metadata is stashed in ``cache_pending`` for Phase 3.
    model_name_for_cache = (
        getattr(seq_picker, "model_name", None) if seq_picker is not None else None
    ) or "unknown"
    prompt_hash_value = picker_prompt_hash() if picker_cache is not None else ""
    finto_shas: dict[str, str] = {}
    if picker_cache is not None:
        dumps_dir = finto_dumps_dir if finto_dumps_dir is not None else settings.finto_dump_dir
        finto_shas = compute_finto_shas(dumps_dir)
    cache_lookup_keys: dict[int, tuple[str, str, str]] = {}
    deferred_misses: list[tuple[int, EntityRequest, list[AuthorityCandidate]]] = []
    cache_hits = 0
    for idx, request, sorted_candidates in deferred:
        key_info: tuple[str, str, str] | None = None
        if picker_cache is not None:
            key_info = compute_picker_cache_key(
                request=request,
                candidates=sorted_candidates,
                prompt_hash_value=prompt_hash_value,
                model_name=model_name_for_cache,
                finto_shas=finto_shas,
            )
        hit: CacheHit | None = None
        if key_info is not None and picker_cache is not None:
            hit = picker_cache.get(key_info[0])
        if hit is not None:
            outcome = _decide_with_pick(
                request=request,
                sorted_candidates=sorted_candidates,
                pick=hit.decision,
                lexical_fallback_floor=lexical_fallback_floor,
                lexical_fallback_floor_per_vocab=lexical_fallback_floor_per_vocab,
                disable_fallback=disable_fallback,
            )
            pre_outcomes[idx] = ReconciliationOutcome(
                request=outcome.request,
                stage=outcome.stage,
                chosen_uri=outcome.chosen_uri,
                confidence=outcome.confidence,
                rationale=outcome.rationale,
                candidates=outcome.candidates,
                needs_review=outcome.needs_review,
                was_watchdog_aborted=outcome.was_watchdog_aborted,
                cached_activity_uuid=hit.activity_uuid,
            )
            cache_hits += 1
        else:
            deferred_misses.append((idx, request, sorted_candidates))
            if key_info is not None:
                cache_lookup_keys[idx] = key_info

    emit_if_active(
        stage="m9",
        event="progress",
        phase="cache-lookup",
        counters={
            "deferred_to_picker": len(deferred),
            "cache_hits": cache_hits,
            "cache_misses": len(deferred_misses),
        },
    )

    # --- Phase 2: dispatch deferred picker calls --------------------------
    # P-10 Phase E: reorder the queue so consecutive picker calls share
    # the longest possible prompt prefix. Output Turtle stays byte-stable
    # because the result-merge below sorts by submission ``idx``.
    deferred_misses = _order_deferred_picker_queue(deferred_misses, ordering=picker_ordering)
    emit_if_active(
        stage="m9",
        event="phase_boundary",
        phase="phase2",
        counters={
            # ``total`` echoes ``deferred_to_picker`` so the exporter sets
            # ``bffi_stage_entities_total{phase="phase2"}`` at phase entry.
            # Without this the dashboard's M9 phase-2 bar stays empty until
            # the first progress event lands at ``processed=cadence``
            # (~2-3 min into Phase 2). Phase 1 / Phase 3 already follow
            # this pattern.
            "total": len(deferred_misses),
            "deferred_to_picker": len(deferred_misses),
            "cache_hits": cache_hits,
        },
        extra={"picker_ordering": picker_ordering},
    )
    picker_results: list[tuple[int, ReconciliationOutcome]] = []
    if deferred_misses:
        # Derive a model_name string for watchdog events. Falls back to "
        # unknown" if the picker is a stub or doesn't expose model_name.
        probe_picker = (
            seq_picker
            if seq_picker is not None
            else (picker_factory() if picker_factory is not None else None)
        )
        model_name = getattr(probe_picker, "model_name", None) or "unknown"

        if concurrency <= 1:
            assert seq_picker is not None  # narrow for mypy; validated above
            picker_results = _picker_phase_seq(
                deferred_misses,
                picker=seq_picker,
                field_timeout_seconds=field_timeout_seconds,
                model_name=model_name,
                watchdog_sidecar_path=watchdog_sidecar_path,
                progress_cadence=progress_cadence,
                cache_hits=cache_hits,
                lexical_fallback_floor=lexical_fallback_floor,
                lexical_fallback_floor_per_vocab=lexical_fallback_floor_per_vocab,
                disable_fallback=disable_fallback,
            )
        else:
            assert picker_factory is not None  # narrow; validated above
            picker_results = _picker_phase_pool(
                deferred_misses,
                picker_factory=picker_factory,
                concurrency=concurrency,
                field_timeout_seconds=field_timeout_seconds,
                model_name=model_name,
                watchdog_sidecar_path=watchdog_sidecar_path,
                progress_cadence=progress_cadence,
                cache_hits=cache_hits,
                lexical_fallback_floor=lexical_fallback_floor,
                lexical_fallback_floor_per_vocab=lexical_fallback_floor_per_vocab,
                disable_fallback=disable_fallback,
            )

    # P-10 Phase B: stash write-back data per idx — Phase 3 reads it
    # after _emit_provenance returns the freshly-minted Activity URI.
    # P-10 Phase B.1: cache *every* picker decision, not only STAGE_LLM
    # successes. Storing the raw ``PickerDecision`` lets the warm-run
    # lookup replay ``_decide_with_pick`` byte-stably — including for
    # low-confidence picks that map to STAGE_FALLBACK. Without this,
    # the model's per-call non-determinism near the 0.80 LLM-confidence
    # threshold flips cold→warm tier classifications (see
    # ``scripts/p10-phase-b-cold-warm-audit.py``). Watchdog-aborted
    # outcomes still aren't cached: those reflect a budget timeout, not
    # a real picker verdict, and a re-run should re-attempt.
    cache_pending: dict[int, _CachePending] = {}
    for idx, outcome in picker_results:
        pre_outcomes[idx] = outcome
        if (
            picker_cache is not None
            and idx in cache_lookup_keys
            and not outcome.was_watchdog_aborted
            and outcome.picker_decision is not None
        ):
            cache_key, vocab, finto_sha = cache_lookup_keys[idx]
            cache_pending[idx] = _CachePending(
                cache_key=cache_key,
                finto_vocab=vocab,
                finto_sha=finto_sha,
                decision=outcome.picker_decision,
            )

    # --- Phase 3: apply graph mutations + provenance in request order -----
    emit_if_active(
        stage="m9",
        event="phase_boundary",
        phase="phase3",
        counters={"total": len(request_list)},
    )
    # Deterministic by construction: sorted by the original request index.
    outcomes: list[ReconciliationOutcome] = []
    for idx in range(len(request_list)):
        outcome = pre_outcomes[idx]
        outcomes.append(outcome)

        if outcome.was_watchdog_aborted:
            summary.watchdog_aborted += 1
        if outcome.stage == STAGE_LOCAL:
            summary.local += 1
        elif outcome.stage == STAGE_LEXICAL:
            summary.lexical += 1
        elif outcome.stage == STAGE_LLM:
            summary.llm_pick += 1
        elif outcome.stage == STAGE_FALLBACK:
            summary.fallback += 1
        elif outcome.stage == STAGE_FICTIONAL:
            summary.fictional += 1
        else:
            summary.no_candidate += 1

        # P-31 Phase C: pipeline-transformation review surfaces. Three
        # M9 outcome shapes get a target-review row so the cataloguer
        # can verify whether the pipeline got the reconciliation right
        # (and feed pipeline-incorrect rows back into prompt iteration,
        # gold-set growth, FP veto plans). member_bib_ids resolves
        # canonical Work URI → source Helmet bib_ids via the M8
        # canonical-map sidecar; cataloguers use those to inspect the
        # source MARC and estimate the bug's severity.
        target_bib_ids = canonical_bib_ids.get(outcome.request.work_uri, [])
        if outcome.stage == STAGE_FALLBACK:
            append_target_row(
                member_bib_ids=target_bib_ids,
                reason="m9-fallback",
                confidence=outcome.confidence,
                details=_format_m9_details(outcome),
                dedup_key=outcome.request.work_uri,
            )
        elif outcome.stage == STAGE_FICTIONAL:
            append_target_row(
                member_bib_ids=target_bib_ids,
                reason="fictional-character",
                confidence=None,
                details=_format_m9_details(outcome),
                dedup_key=outcome.request.work_uri,
            )
        elif outcome.chosen_uri is None and outcome.stage != STAGE_FICTIONAL:
            # no-candidate path (everything not handled above with a
            # bound chosen_uri AND not the fictional short-circuit)
            append_target_row(
                member_bib_ids=target_bib_ids,
                reason="m9-no-candidate",
                confidence=None,
                details=_format_m9_details(outcome),
                dedup_key=outcome.request.work_uri,
            )

        if outcome.chosen_uri is not None:
            _apply_canonical_link(target_graph, outcome.request, outcome.chosen_uri)
            _bump_admin_metadata(
                target_graph,
                outcome.request.work_uri,
                chosen_uri=outcome.chosen_uri,
                needs_review=outcome.needs_review,
                now=moment,
            )
        activity_uri = _emit_provenance(
            provenance_graph,
            outcome=outcome,
            started_at=started_at[idx],
            ended_at=datetime.now(UTC),
        )
        # P-10 Phase B: commit fresh picker verdicts to the cache *after*
        # the Activity URI is minted, so the cache row's ``activity_uuid``
        # matches the URI a future cache hit will hand to wasInfluencedBy.
        # Cache hits don't reappear here (cache_pending only has misses
        # that went through the picker).
        if picker_cache is not None and activity_uri is not None and idx in cache_pending:
            pending = cache_pending[idx]
            picker_cache.set(
                pending.cache_key,
                decision=pending.decision,
                finto_vocab=pending.finto_vocab,
                finto_sha=pending.finto_sha,
                prompt_hash_value=prompt_hash_value,
                model_name=model_name_for_cache,
                activity_uuid=str(activity_uri),
            )

    if own_graph:
        tmp = output_path.with_suffix(output_path.suffix + ".tmp")
        target_graph.serialize(destination=str(tmp), format="turtle")
        tmp.replace(output_path)

    emit_if_active(
        stage="m9",
        event="end",
        counters={
            "total": summary.total,
            "local": summary.local,
            "lexical": summary.lexical,
            "llm_pick": summary.llm_pick,
            "fallback": summary.fallback,
            "no_candidate": summary.no_candidate,
            "fictional": summary.fictional,
            "watchdog_aborted": summary.watchdog_aborted,
        },
    )
    return summary, outcomes


__all__ = [
    "ALL_AUTHORITY_KINDS",
    "DEFAULT_TOP_K",
    "FINTO_BASE_URL",
    "LEXICAL_DIRECT_THRESHOLD",
    "LEXICAL_FLOOR",
    "LLM_CONFIDENCE_THRESHOLD",
    "PICKER_CACHE_FILENAME",
    "PICKER_CONNECTION_BACKOFF_SECONDS",
    "PICKER_MAX_CONNECTION_RETRIES",
    "PICKER_MAX_VALIDATION_RETRIES",
    "PICKER_MIN_RATIONALE_CHARS",
    "PICKER_ORDERING_PREFIX_CACHE",
    "PICKER_ORDERING_SUBMISSION",
    "PICKER_PROMPT_PATH",
    "PICKER_STUB_PHRASES",
    "PICKER_UNCERTAIN_MAX_CONFIDENCE",
    "STAGE_FALLBACK",
    "STAGE_FICTIONAL",
    "STAGE_LEXICAL",
    "STAGE_LLM",
    "STAGE_LOCAL",
    "STAGE_NO_CANDIDATE",
    "STAGE_WATCHDOG_ABORTED",
    "VOCAB_KANTO",
    "VOCAB_KAUNO",
    "VOCAB_MUSO",
    "VOCAB_VIAF",
    "VOCAB_YSO",
    "AuthorityCandidate",
    "AuthorityClient",
    "AuthorityKind",
    "CacheHit",
    "EntityRequest",
    "FintoSkosmosClient",
    "LLMPicker",
    "LangChainLLMPicker",
    "PickerCache",
    "PickerDecision",
    "PickerOrdering",
    "ReconciliationOutcome",
    "ReconciliationStage",
    "ReconciliationSummary",
    "StubAuthorityClient",
    "StubPicker",
    "ViafClient",
    "apply_reconciliation",
    "compute_finto_shas",
    "compute_picker_cache_key",
    "decide_reconciliation",
    "hash_finto_dump",
    "lexical_similarity",
    "picker_cache_default_path",
    "picker_prompt_hash",
    "picker_prompt_text",
    "reconcile_one",
]
