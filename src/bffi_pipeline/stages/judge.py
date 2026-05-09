"""Stage M6: LLM judge — structured output + two-model cascade.

The judge runs against a local OpenAI-compatible server (Ollama for
development, vllm-mlx for production batches; both speak the same
chat-completions API). Application code talks through
``langchain-openai`` with ``LLM_BASE_URL`` from ``Settings``.

Phase 1 (this module) lands the structural pieces:

- :class:`WorkRecord`, :class:`WorkMatchDecision` schemas with the
  three Boundary-4 ``@model_validator(mode="after")`` checks per
  spec § 7.
- :func:`judge_pair`: single-model judgment wrapping the LangChain
  chain, with validation-failure retry (max 2 retries), connection-
  error retry with exponential backoff (5 / 30 / 120 s, max 3
  retries), and a custom post-validation SQLite cache. Permanent
  failures land as ``decision="uncertain"`` with the error in the
  rationale.
- :func:`cascade_judge`: 32 B primary → 72 B fallback when the
  primary returns ``uncertain`` or ``same_work`` with confidence
  below :data:`FALLBACK_CONFIDENCE_THRESHOLD`. Returns the final
  decision plus a list of :class:`CascadeStep`\\ s so downstream
  provenance writers can log each LLM call with the right
  ``bffi-prov:stage`` value.
- :class:`JudgeCache`: a thin SQLite-backed key/value store keyed on
  ``(model, prompt_hash, record_a_canonical, record_b_canonical)``.
  Writes happen only after a response has passed both structural
  *and* semantic validation. Cache hits return identical
  ``WorkMatchDecision`` objects with no LLM call.

Phase 2 (separate commit) will add the batch driver that consumes
M5's ``embed-candidates.jsonl``, the checkpoint file, the
vllm-mlx concurrent mode, and the ``bffi-pipeline judge`` CLI
subcommand.

Heavy LangChain client construction is deferred to
:func:`_build_chain` and ``judge_pair`` — importing this module is
cheap and never opens a network socket.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from bffi_pipeline.config import get_settings

# --- Constants ------------------------------------------------------------

#: Two-shot prompt source. Hashed at startup; the hash is logged with every
#: provenance record so a future audit can reproduce or regress a decision.
PROMPT_PATH: Final[Path] = Path(__file__).resolve().parents[3] / "prompts" / "judge_v1.txt"

#: Section markers in ``judge_v1.txt`` (the file is plain text — no YAML).
_PROMPT_SECTION_RE: Final[re.Pattern[str]] = re.compile(r"^### (\w+)\s*$", re.MULTILINE)

#: Confidence cutoff below which the primary's ``same_work`` decision is
#: re-run on the 72 B fallback. Documented in spec § 7 / docs/local-inference.md.
FALLBACK_CONFIDENCE_THRESHOLD: Final[float] = 0.85

#: Validation retry: spec § 7 calls for max 2 retries on parse / Boundary-4
#: failures. The total number of LLM attempts is therefore 3.
MAX_VALIDATION_RETRIES: Final[int] = 2

#: Connection retry: spec § 7 calls for max 3 retries with exponential
#: backoff after a connection error or timeout. Total attempts = 4.
MAX_CONNECTION_RETRIES: Final[int] = 3
CONNECTION_BACKOFF_SECONDS: Final[tuple[float, ...]] = (5.0, 30.0, 120.0)

#: ``bffi-prov:stage`` values per spec § 7. Both primary and second-opinion
#: decisions are logged with these tags so post-merge SPARQL queries can
#: distinguish 32 B-only decisions from cascade-resolved ones.
STAGE_PRIMARY: Final[str] = "llm-judge-primary"
STAGE_SECOND_OPINION: Final[str] = "llm-judge-second-opinion"

#: Stub phrases the rationale must NOT contain. Stored already lower-cased.
STUB_PHRASES: Final[tuple[str, ...]] = (
    "i don't know",
    "unable to determine",
    "n/a",
    "not sure",
)

#: Maximum confidence allowed when the model returns ``decision="uncertain"``.
#: Anything higher is incoherent with the decision label and triggers Boundary-4.
UNCERTAIN_MAX_CONFIDENCE: Final[float] = 0.7

#: Minimum rationale length, in characters. Stops one-word answers and
#: punctuation-only payloads from passing as substantive reasoning.
MIN_RATIONALE_CHARS: Final[int] = 20

#: Default cache filename under ``BFFI_DATA_DIR``.
CACHE_FILENAME: Final[str] = "judge-cache.sqlite"

# --- Schemas --------------------------------------------------------------


class WorkRecord(BaseModel):
    """One side of a candidate pair, populated from the BFFI Work + BIBFRAME agent."""

    model_config = ConfigDict(extra="forbid")

    record_id: str
    creator: str | None = None
    creator_uri: str | None = None
    preferred_title: str | None = None
    variant_titles: list[str] = Field(default_factory=list)
    original_language: str | None = None
    expression_language: str | None = None
    content_type: str | None = None
    date_of_origin: str | None = None
    publication_year: str | None = None
    notes: list[str] = Field(default_factory=list)


class WorkMatchDecision(BaseModel):
    """Structured judgment. Per spec § 7 the model must fill exactly this schema."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["same_work", "different_work", "uncertain"]
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="0.0-1.0. Use <0.7 when uncertain; reserve >0.9 for clear cases.",
    )
    rationale: str = Field(
        min_length=20,
        description=(
            "2-4 sentences citing specific field values from BOTH records. "
            "Do not introduce facts not present in the inputs."
        ),
    )
    matching_fields: list[str] = Field(default_factory=list)
    diverging_fields: list[str] = Field(default_factory=list)

    # --- Boundary 4 semantic validators (spec § 10 + § 7) -----------------

    @model_validator(mode="after")
    def _coherent_uncertain(self) -> WorkMatchDecision:
        if self.decision == "uncertain" and self.confidence > UNCERTAIN_MAX_CONFIDENCE:
            raise ValueError(
                f"decision='uncertain' is incoherent with confidence > {UNCERTAIN_MAX_CONFIDENCE}"
            )
        return self

    @model_validator(mode="after")
    def _same_work_needs_evidence(self) -> WorkMatchDecision:
        if self.decision == "same_work" and not self.matching_fields:
            raise ValueError("decision='same_work' requires at least one matching_field")
        return self

    @model_validator(mode="after")
    def _rationale_is_substantive(self) -> WorkMatchDecision:
        text = self.rationale.strip()
        if len(text) < MIN_RATIONALE_CHARS:
            raise ValueError(f"rationale shorter than {MIN_RATIONALE_CHARS} characters")
        lowered = text.lower()
        for phrase in STUB_PHRASES:
            if re.search(rf"\b{re.escape(phrase)}\b", lowered):
                raise ValueError(f"rationale contains stub phrase: {phrase!r}")
        return self


# --- Cascade record -------------------------------------------------------


@dataclass(frozen=True)
class CascadeStep:
    """One LLM call's outcome inside :func:`cascade_judge`.

    Carries everything a provenance writer needs to mint a per-call
    ``prov:Activity`` later: which model, the stage tag, the cache-hit
    flag, and the resulting decision.
    """

    stage: str  # STAGE_PRIMARY or STAGE_SECOND_OPINION
    model_name: str
    decision: WorkMatchDecision
    cache_hit: bool
    latency_seconds: float


@dataclass
class JudgeOutcome:
    """Cascade result: final decision + per-step record for provenance."""

    final: WorkMatchDecision
    steps: list[CascadeStep] = field(default_factory=list)

    @property
    def used_cascade(self) -> bool:
        return any(s.stage == STAGE_SECOND_OPINION for s in self.steps)


# --- Prompt loading + hashing ---------------------------------------------


@lru_cache(maxsize=1)
def prompt_text() -> str:
    """Return the raw ``prompts/judge_v1.txt`` contents."""
    if not PROMPT_PATH.is_file():
        raise FileNotFoundError(f"Judge prompt not found at {PROMPT_PATH!s}.")
    return PROMPT_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def prompt_hash() -> str:
    """SHA-256 of :func:`prompt_text`. Logged with every provenance record."""
    return "sha256:" + hashlib.sha256(prompt_text().encode("utf-8")).hexdigest()[:16]


@lru_cache(maxsize=1)
def _parse_prompt_sections() -> dict[str, str]:
    """Split ``judge_v1.txt`` into ``SYSTEM`` / ``EXAMPLES`` / ``USER`` blocks."""
    raw = prompt_text()
    sections: dict[str, str] = {}
    matches = list(_PROMPT_SECTION_RE.finditer(raw))
    if not matches:
        raise ValueError(f"No '### SECTION' markers found in {PROMPT_PATH!s}.")
    for i, m in enumerate(matches):
        name = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        sections[name] = raw[start:end].strip()
    for required in ("SYSTEM", "EXAMPLES", "USER"):
        if required not in sections:
            raise ValueError(f"{PROMPT_PATH!s} is missing required '### {required}' section.")
    return sections


# --- Custom SQLite cache (post-validation only) ---------------------------


def _canonicalise_record(record: WorkRecord) -> str:
    """Stable JSON dump of a record — sorted keys, no nones, ASCII-safe."""
    return record.model_dump_json(exclude_none=True, by_alias=False)


def _cache_key(
    *,
    model_name: str,
    prompt_hash_value: str,
    record_a: WorkRecord,
    record_b: WorkRecord,
) -> str:
    payload = "|".join(
        (
            model_name,
            prompt_hash_value,
            _canonicalise_record(record_a),
            _canonicalise_record(record_b),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class JudgeCache:
    """Tiny SQLite-backed cache for validated :class:`WorkMatchDecision`\\ s.

    Writes happen *only* after a response has passed structural and
    semantic validation — see :func:`judge_pair`. Validation-failed
    responses are deliberately not cached so a re-run can recover
    once the model is updated or the prompt is fixed.
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS judge_cache (
      cache_key   TEXT PRIMARY KEY,
      model_name  TEXT NOT NULL,
      prompt_hash TEXT NOT NULL,
      decision    TEXT NOT NULL,
      created_at  TEXT NOT NULL
    )
    """

    def __init__(self, path: Path | str):
        self._path = path
        self._conn = sqlite3.connect(str(path))
        self._conn.execute(self._SCHEMA)
        self._conn.commit()

    def __enter__(self) -> JudgeCache:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        with suppress(sqlite3.ProgrammingError):
            self._conn.close()

    def get(self, key: str) -> WorkMatchDecision | None:
        row = self._conn.execute(
            "SELECT decision FROM judge_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return WorkMatchDecision.model_validate_json(row[0])

    def set(
        self,
        key: str,
        decision: WorkMatchDecision,
        *,
        model_name: str,
        prompt_hash_value: str,
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO judge_cache "
            "(cache_key, model_name, prompt_hash, decision, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                key,
                model_name,
                prompt_hash_value,
                decision.model_dump_json(),
                datetime.now(UTC).isoformat(),
            ),
        )
        self._conn.commit()


def default_cache_path() -> Path:
    return get_settings().data_dir / CACHE_FILENAME


# --- LangChain chain construction (deferred) ------------------------------


def _is_connection_error(exc: BaseException) -> bool:
    """Treat any low-level network or timeout error as a retry-worthy event.

    LangChain wraps OpenAI client errors; the underlying httpx ``ConnectError``,
    ``ReadTimeout`` and ``RemoteProtocolError`` should all backoff. We
    detect by class-name suffix so this stays robust if LangChain changes
    the wrapping path.
    """
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
    # Walk the cause chain — LangChain wraps original errors in OutputParserException etc.
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        return _is_connection_error(cause)
    return False


def _build_chain(
    *,
    model_name: str,
    base_url: str,
    api_key: str,
    temperature: float = 0.0,
    seed: int = 42,
) -> Any:
    """Compose ``ChatOpenAI(...).with_structured_output(WorkMatchDecision)``."""
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_openai import ChatOpenAI
    from pydantic import SecretStr

    sections = _parse_prompt_sections()
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
    return template | llm.with_structured_output(WorkMatchDecision, method="json_schema")


# Type alias for the injectable chain — anything with .invoke({record_a, record_b, sim}).
ChainLike = Any


# --- judge_pair -----------------------------------------------------------


def _uncertain_decision(reason: str) -> WorkMatchDecision:
    """Build the canonical 'fall-through' decision for unrecoverable failures.

    Confidence is pinned to 0.0 to satisfy the ``_coherent_uncertain``
    validator (which requires confidence ≤ 0.7 when decision is
    ``uncertain``); the rationale carries the original error text so a
    later operator can grep for it. Stub phrases are stripped from
    ``reason`` because the rationale validator forbids them, and ``reason``
    is also padded to ≥ 20 characters with a stable prefix.
    """
    cleaned = reason.strip() or "no error message available"
    lowered = cleaned.lower()
    for phrase in STUB_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}\b", lowered):
            cleaned = re.sub(
                rf"\b{re.escape(phrase)}\b",
                "[stub phrase elided]",
                cleaned,
                flags=re.IGNORECASE,
            )
    rationale = f"Judge fell through to uncertain after retries exhausted: {cleaned}"
    return WorkMatchDecision(
        decision="uncertain",
        confidence=0.0,
        rationale=rationale,
        matching_fields=[],
        diverging_fields=[],
    )


def judge_pair(  # noqa: PLR0912 — two retry layers (connection + validation) keep this single-purpose, splitting would scatter state.
    record_a: WorkRecord,
    record_b: WorkRecord,
    sim: float,
    *,
    model_name: str | None = None,
    chain: ChainLike | None = None,
    cache: JudgeCache | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[WorkMatchDecision, bool, float]:
    """Judge a single Work-pair with retry, post-validation cache.

    Returns ``(decision, cache_hit, latency_seconds)``. ``cache_hit`` lets
    the caller (e.g. cascade_judge / the future batch driver) record
    whether this answer cost an LLM call.

    ``chain`` and ``cache`` are injection points for tests; production
    callers leave them ``None`` so the defaults — a fresh
    ``ChatOpenAI`` chain pointed at the configured base URL, and the
    SQLite cache under ``data_dir`` — are constructed lazily.
    """
    settings = get_settings()
    effective_model = model_name or settings.llm_model_primary
    chain = chain or _build_chain(
        model_name=effective_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
    )

    own_cache = cache is None
    if own_cache:
        cache = JudgeCache(default_cache_path())

    started = time.monotonic()
    try:
        ph = prompt_hash()
        key = _cache_key(
            model_name=effective_model,
            prompt_hash_value=ph,
            record_a=record_a,
            record_b=record_b,
        )

        cached = cache.get(key) if cache else None
        if cached is not None:
            return cached, True, time.monotonic() - started

        invoke_payload = {
            "record_a": record_a.model_dump_json(indent=2, exclude_none=True),
            "record_b": record_b.model_dump_json(indent=2, exclude_none=True),
            "sim": sim,
        }

        connection_attempts = 0
        validation_attempts = 0
        last_error: str = "unknown failure"

        while True:
            try:
                raw = chain.invoke(invoke_payload)
            except Exception as exc:
                if _is_connection_error(exc):
                    if connection_attempts < MAX_CONNECTION_RETRIES:
                        sleep(CONNECTION_BACKOFF_SECONDS[connection_attempts])
                        connection_attempts += 1
                        last_error = (
                            f"connection error after {connection_attempts} retry(ies): {exc!s}"
                        )
                        continue
                    last_error = (
                        f"connection error after {MAX_CONNECTION_RETRIES} retries exhausted: "
                        f"{exc!s}"
                    )
                    break
                last_error = f"unrecoverable LLM error: {exc!s}"
                break

            try:
                if isinstance(raw, WorkMatchDecision):
                    decision = raw
                else:
                    decision = WorkMatchDecision.model_validate(raw)
            except (ValidationError, ValueError) as exc:
                if validation_attempts < MAX_VALIDATION_RETRIES:
                    validation_attempts += 1
                    last_error = f"validation failure (attempt {validation_attempts}): {exc!s}"
                    continue
                last_error = f"validation failed after {MAX_VALIDATION_RETRIES} retries: {exc!s}"
                break

            if cache is not None:
                cache.set(key, decision, model_name=effective_model, prompt_hash_value=ph)
            return decision, False, time.monotonic() - started

        return _uncertain_decision(last_error), False, time.monotonic() - started
    finally:
        if own_cache and cache is not None:
            cache.close()


# --- cascade_judge --------------------------------------------------------


def _needs_second_opinion(decision: WorkMatchDecision) -> bool:
    if decision.decision == "uncertain":
        return True
    return decision.decision == "same_work" and decision.confidence < FALLBACK_CONFIDENCE_THRESHOLD


def cascade_judge(
    record_a: WorkRecord,
    record_b: WorkRecord,
    sim: float,
    *,
    primary_model: str | None = None,
    fallback_model: str | None = None,
    primary_chain: ChainLike | None = None,
    fallback_chain: ChainLike | None = None,
    cache: JudgeCache | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> JudgeOutcome:
    """Two-stage cascade per spec § 7 / docs/local-inference.md.

    Runs the primary model first; re-runs the fallback model when the
    primary returns ``uncertain`` or ``same_work`` with confidence below
    :data:`FALLBACK_CONFIDENCE_THRESHOLD`. Both decisions are returned
    in :attr:`JudgeOutcome.steps` so the future provenance writer can
    log them with the ``llm-judge-primary`` and
    ``llm-judge-second-opinion`` ``bffi-prov:stage`` tags.
    """
    settings = get_settings()
    primary_name = primary_model or settings.llm_model_primary
    fallback_name = fallback_model or settings.llm_model_fallback

    own_cache = cache is None
    if own_cache:
        cache = JudgeCache(default_cache_path())

    try:
        primary_decision, primary_cache_hit, primary_latency = judge_pair(
            record_a,
            record_b,
            sim,
            model_name=primary_name,
            chain=primary_chain,
            cache=cache,
            sleep=sleep,
        )
        steps = [
            CascadeStep(
                stage=STAGE_PRIMARY,
                model_name=primary_name,
                decision=primary_decision,
                cache_hit=primary_cache_hit,
                latency_seconds=primary_latency,
            )
        ]
        if not _needs_second_opinion(primary_decision):
            return JudgeOutcome(final=primary_decision, steps=steps)

        fallback_decision, fallback_cache_hit, fallback_latency = judge_pair(
            record_a,
            record_b,
            sim,
            model_name=fallback_name,
            chain=fallback_chain,
            cache=cache,
            sleep=sleep,
        )
        steps.append(
            CascadeStep(
                stage=STAGE_SECOND_OPINION,
                model_name=fallback_name,
                decision=fallback_decision,
                cache_hit=fallback_cache_hit,
                latency_seconds=fallback_latency,
            )
        )
        return JudgeOutcome(final=fallback_decision, steps=steps)
    finally:
        if own_cache and cache is not None:
            cache.close()


__all__ = [
    "CONNECTION_BACKOFF_SECONDS",
    "FALLBACK_CONFIDENCE_THRESHOLD",
    "MAX_CONNECTION_RETRIES",
    "MAX_VALIDATION_RETRIES",
    "PROMPT_PATH",
    "STAGE_PRIMARY",
    "STAGE_SECOND_OPINION",
    "STUB_PHRASES",
    "CascadeStep",
    "JudgeCache",
    "JudgeOutcome",
    "WorkMatchDecision",
    "WorkRecord",
    "cascade_judge",
    "default_cache_path",
    "judge_pair",
    "prompt_hash",
    "prompt_text",
]
