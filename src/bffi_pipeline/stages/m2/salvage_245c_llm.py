"""P-41 Phase B.2 — LLM-cascade fallback for the 245$c salvage tier.

Fires from :func:`bffi_pipeline.stages.m2.salvage.try_salvage_minimum_content`
when the deterministic regex parser (B.1, ``salvage_245c``) returned
no agents. Uses the same local-mlx-lm cascade shape as
:mod:`bffi_pipeline.contrib_extract_llm` (and the M6 / M9 cascades):
versioned prompt at ``prompts/salvage-245c-v1.txt``, Pydantic-
validated structured output, two retry layers (validation +
connection backoff), fall-through to an empty result when retries
exhaust.

Key constraint: every returned ``name`` must be a **verbatim
substring** of the input 245$c. The LLM is instructed to do this
in-prompt; the post-processor enforces it. This is the main defence
against the LLM hallucinating an author the cataloguer never typed.
Confidence is capped at **0.7** so synthesised creators land in the
M6 fallback-gating tier P-16 set up — a hallucinated creator can't
auto-merge a Work.

Determinism: temperature 0, fixed seed. Validated outputs are cached
at ``<BFFI_DATA_DIR>/synth-cache.sqlite`` keyed on the
``(prompt_hash, 245$c)`` pair, so a second run on the same record
short-circuits the LLM call — the byte-stability the project's
idempotency contract requires (CLAUDE.md § "Conventions:
Idempotency").

Tests inject a :class:`StubSalvageExtractor` so ``pytest`` never
loads the LangChain stack; no test in this module carries the
``requires_llm`` mark.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from bffi_pipeline.config import get_settings
from bffi_pipeline.llm_json_mode import json_mode_instruction
from bffi_pipeline.stages.m2.salvage_245c import ParsedAgent

SALVAGE_245C_PROMPT_PATH: Final[Path] = (
    Path(__file__).resolve().parents[4] / "prompts" / "salvage-245c-v1.txt"
)
_PROMPT_SECTION_RE: Final[re.Pattern[str]] = re.compile(r"^### (\w+)\s*$", re.MULTILINE)

SALVAGE_MAX_VALIDATION_RETRIES: Final[int] = 2
SALVAGE_MAX_CONNECTION_RETRIES: Final[int] = 3
SALVAGE_CONNECTION_BACKOFF_SECONDS: Final[tuple[float, ...]] = (5.0, 30.0, 120.0)

#: Hard request timeout per ``chain.invoke``. Mirrors
#: :data:`bffi_pipeline.contrib_extract_llm.CONTRIB_REQUEST_TIMEOUT_SECONDS`;
#: warm Qwen3 8B calls land in 8-15s on M2 Max, cold-start ~50s, so
#: 120s leaves headroom while still bounding wedged calls.
SALVAGE_REQUEST_TIMEOUT_SECONDS: Final[float] = 120.0

#: Fallback model identifier when ``Settings.llm_model_primary`` is
#: empty. The 8B model is well-suited to the short, structured
#: 245$c → personal-name task and avoids the 32B cold-start cost
#: on the tail of records this cascade runs against. Production
#: reads the actual mlx-lm model path from
#: ``Settings.llm_model_primary``; this constant is a defensive
#: stub for environments where the env var is unset.
DEFAULT_SALVAGE_MODEL: Final[str] = "qwen3:8b-q4_K_M"

#: Confidence cap for LLM-tier hits. Per P-41 Phase B.2 — synthesised
#: creators from the LLM tier always land in M6's fallback-gating
#: band P-16 set up, so a hallucinated agent can't auto-merge a Work.
LLM_CONFIDENCE_CAP: Final[float] = 0.7

#: Minimum rationale length the model must produce. Same defence as
#: the M6 judge and contrib-extract cascade — too-short rationales
#: are usually stub responses.
_MIN_RATIONALE_CHARS: Final[int] = 20

#: Phrases the rationale validator rejects — stub-detector for
#: degenerate model outputs.
_STUB_PHRASES: Final[tuple[str, ...]] = (
    "i don't know",
    "unable to determine",
    "n/a",
    "not sure",
)

#: SQLite cache filename under ``BFFI_DATA_DIR``. Distinct from
#: ``judge-cache.sqlite`` (M6) so the M2 salvage cache can be wiped
#: independently — they're scoped to different stages.
SYNTH_CACHE_FILENAME: Final[str] = "synth-cache.sqlite"

#: Valid role tags the prompt instructs the LLM to use. Hallucinated
#: roles are reduced to ``"unknown"`` by the post-processor.
_VALID_ROLES: Final[frozenset[str]] = frozenset(
    {"author", "editor", "translator", "illustrator", "compiler", "unknown"}
)

#: Defence-in-depth (2026-06-02): minimum name length the verbatim-
#: substring filter accepts. The B1 regex tier enforces ≥ 2 tokens;
#: the LLM tier is intentionally more permissive (catches single-name
#: artists like Aboriginal performers) but anything shorter than this
#: is almost certainly a fragment or stopword extraction.
_MIN_LLM_NAME_CHARS: Final[int] = 2

#: Defence-in-depth (2026-06-02): names the post-processor rejects
#: even when they're verbatim substrings of the 245$c. The risk: a
#: degenerate LLM response returns a stopword or article (``"the"``,
#: ``"and"``, ``"by"``) that happens to be in the 245$c. The verbatim-
#: substring check alone would accept it. This list catches the
#: highest-risk cases case-insensitively without rejecting legitimate
#: single-name artists. Add to it if production surfaces new
#: hallucination shapes.
_LLM_REJECT_NAMES: Final[frozenset[str]] = frozenset(
    {
        # English articles + prepositions + conjunctions.
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "by",
        "with",
        "for",
        # Finnish particles.
        "ja",
        "tai",
        # Swedish.
        "och",
        # German.
        "und",
        "von",
        # French / Italian / Spanish.
        "et",
        "de",
        "del",
        "el",
        "la",
        "le",
        "du",
        "da",
        "di",
    }
)


# --- Pydantic schema ------------------------------------------------------


class LlmAgent(BaseModel):
    """One agent extracted by the LLM cascade. ``verbatim_substring``
    is a self-report: the post-processor still re-checks the
    substring against the original 245$c text before accepting the
    agent, so a lying LLM doesn't get to fabricate authors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    role: Literal["author", "editor", "translator", "illustrator", "compiler", "unknown"]
    verbatim_substring: bool


class SalvageDecision(BaseModel):
    """Per-record output of the salvage LLM cascade.

    The empty case (``agents=[]``) is a valid outcome — the LLM is
    instructed to return an empty list when it can't extract a
    personal name with confidence. The dispatcher then falls through
    to B2 or B3.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    agents: list[LlmAgent] = Field(default_factory=list)
    rationale: str = Field(min_length=_MIN_RATIONALE_CHARS)

    @model_validator(mode="after")
    def _rationale_is_substantive(self) -> SalvageDecision:
        text = self.rationale.strip()
        if len(text) < _MIN_RATIONALE_CHARS:
            raise ValueError(f"rationale shorter than {_MIN_RATIONALE_CHARS} characters")
        lowered = text.lower()
        for phrase in _STUB_PHRASES:
            if re.search(rf"\b{re.escape(phrase)}\b", lowered):
                raise ValueError(f"rationale contains stub phrase: {phrase!r}")
        return self


# --- Protocol + Stub for tests --------------------------------------------


class SalvageExtractor(Protocol):
    """Protocol the salvage-LLM target satisfies."""

    def extract(self, *, c_subfield: str) -> list[ParsedAgent]:
        """Return personal-name agents extracted from ``c_subfield``.

        Returns ``[]`` when the LLM can't extract a clean agent —
        the dispatcher then falls through to the next salvage tier.
        Each :class:`ParsedAgent.confidence` is capped at
        :data:`LLM_CONFIDENCE_CAP`.
        """
        ...


@dataclass
class StubSalvageExtractor:
    """Test extractor keyed on ``c_subfield`` text. The mapping value
    is the list of agents the stub returns; absence returns ``[]``.

    Used by every unit test in this module (and any future test that
    needs B.2 to fire without loading the LangChain stack).
    """

    decisions: dict[str, list[ParsedAgent]]

    def extract(self, *, c_subfield: str) -> list[ParsedAgent]:
        return self.decisions.get(c_subfield, [])


# --- Prompt + chain -------------------------------------------------------


@lru_cache(maxsize=1)
def salvage_245c_prompt_text() -> str:
    if not SALVAGE_245C_PROMPT_PATH.is_file():
        raise FileNotFoundError(f"salvage-245c prompt not found at {SALVAGE_245C_PROMPT_PATH!s}.")
    return SALVAGE_245C_PROMPT_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def salvage_245c_prompt_hash() -> str:
    """Stable ``sha256:<first-16-hex-chars>`` hash of the prompt file.
    Persisted on the Synthesis Activity (via the method tag) so a
    prompt version change is detectable in the provenance graph."""
    return "sha256:" + hashlib.sha256(salvage_245c_prompt_text().encode("utf-8")).hexdigest()[:16]


@lru_cache(maxsize=1)
def _parse_prompt_sections() -> dict[str, str]:
    raw = salvage_245c_prompt_text()
    sections: dict[str, str] = {}
    matches = list(_PROMPT_SECTION_RE.finditer(raw))
    if not matches:
        raise ValueError(f"No '### SECTION' markers found in {SALVAGE_245C_PROMPT_PATH!s}.")
    for i, m in enumerate(matches):
        name = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        sections[name] = raw[start:end].strip()
    for required in ("SYSTEM", "EXAMPLES", "USER"):
        if required not in sections:
            raise ValueError(
                f"{SALVAGE_245C_PROMPT_PATH!s} is missing required '### {required}' section."
            )
    return sections


def _is_connection_error(exc: BaseException) -> bool:
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
    """Compose ``ChatOpenAI(...).with_structured_output(SalvageDecision)``.

    Lazy-imports langchain so test infrastructure that injects a
    :class:`StubSalvageExtractor` never has to install the LLM stack
    — matches the M6 + contrib-extract pattern.
    """
    from langchain_core.prompts import (  # noqa: PLC0415 — deferred LLM-stack import.
        ChatPromptTemplate,
    )
    from langchain_openai import ChatOpenAI  # noqa: PLC0415
    from pydantic import SecretStr  # noqa: PLC0415

    sections = _parse_prompt_sections()
    template = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                sections["SYSTEM"]
                + "\n\n"
                + sections["EXAMPLES"]
                + "\n\n"
                + json_mode_instruction(SalvageDecision),
            ),
            ("user", sections["USER"]),
        ]
    )
    llm = ChatOpenAI(
        base_url=base_url,
        api_key=SecretStr(api_key),
        model=model_name,
        temperature=temperature,
        seed=seed,
        timeout=SALVAGE_REQUEST_TIMEOUT_SECONDS,
        max_retries=0,
    )
    return template | llm.with_structured_output(SalvageDecision, method="json_mode")


# --- SQLite cache ---------------------------------------------------------


def _cache_key(*, prompt_hash_value: str, c_subfield: str) -> str:
    """``sha1(prompt_hash + "\\x00" + c_subfield)`` — matches the
    cache-key shape specified in P-41 Phase B.2's plan body."""
    payload = (prompt_hash_value + "\x00" + c_subfield).encode("utf-8")
    return hashlib.sha1(payload, usedforsecurity=False).hexdigest()


class SalvageCache:
    """SQLite-backed cache for validated :class:`SalvageDecision`\\ s.

    Writes happen *only* after the LLM response has cleared both the
    Pydantic structural validation and the verbatim-substring post-
    processor — failed responses are deliberately not cached so a
    re-run can recover once the prompt or model is updated.
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS salvage_cache (
      cache_key   TEXT PRIMARY KEY,
      model_name  TEXT NOT NULL,
      prompt_hash TEXT NOT NULL,
      decision    TEXT NOT NULL,
      created_at  TEXT NOT NULL
    )
    """

    def __init__(self, path: Path | str):
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        with self._lock:
            self._conn.execute(self._SCHEMA)
            self._conn.commit()

    def __enter__(self) -> SalvageCache:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying SQLite connection (idempotent)."""
        with suppress(sqlite3.ProgrammingError), self._lock:
            self._conn.close()

    def get(self, key: str) -> SalvageDecision | None:
        """Return the cached decision for ``key`` or ``None`` if absent."""
        with self._lock:
            row = self._conn.execute(
                "SELECT decision FROM salvage_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return SalvageDecision.model_validate_json(row[0])

    def set(
        self,
        key: str,
        decision: SalvageDecision,
        *,
        model_name: str,
        prompt_hash_value: str,
    ) -> None:
        """Insert-or-replace the cached decision for ``key``."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO salvage_cache "
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


def default_synth_cache_path() -> Path:
    """Return ``<BFFI_DATA_DIR>/synth-cache.sqlite`` from live Settings."""
    return get_settings().data_dir / SYNTH_CACHE_FILENAME


# --- Post-processor -------------------------------------------------------


def _is_plausible_name(name: str) -> bool:
    """Defence-in-depth shape check for B1-LLM agents.

    The verbatim-substring filter ensures the name is in the 245$c,
    but a degenerate LLM response could return a stopword or article
    that happens to be there too (``"the"``, ``"and"``, ``"by"``).
    This function returns False for those cases without rejecting
    legitimate single-name artists (e.g. Aboriginal performers like
    ``"Blanasi"``, ``"David"`` — both ≥ 5 chars, neither in the
    reject list).

    Three checks:

    1. Length ≥ :data:`_MIN_LLM_NAME_CHARS` (≥ 2 chars after strip).
    2. Lowercased name not in :data:`_LLM_REJECT_NAMES`.
    3. Contains at least one letter (rejects pure punctuation /
       digits the LLM might pick up from the 245$c).
    """
    stripped = name.strip()
    if len(stripped) < _MIN_LLM_NAME_CHARS:
        return False
    if stripped.lower() in _LLM_REJECT_NAMES:
        return False
    return any(c.isalpha() for c in stripped)


def _enforce_verbatim_substring(
    decision: SalvageDecision,
    *,
    c_subfield: str,
) -> list[ParsedAgent]:
    """The load-bearing defence against LLM hallucination.

    Every returned name MUST appear as a substring of the original
    245$c. Names that don't are dropped — silently in the result,
    loudly in the rationale that gets logged to provenance via the
    method tag.

    A secondary :func:`_is_plausible_name` shape check rejects
    stopwords / articles / single-char fragments that pass the
    substring test but aren't plausible agent names. Legitimate
    mononyms (``"Blanasi"``, ``"David"``) clear both filters.

    Roles outside :data:`_VALID_ROLES` are reduced to ``"unknown"``.
    Confidence is capped at :data:`LLM_CONFIDENCE_CAP`.
    """
    accepted: list[ParsedAgent] = []
    for agent in decision.agents:
        if agent.name not in c_subfield:
            # Hallucinated name — drop without caching, without
            # synthesising, without raising. The dispatcher falls
            # through to the next tier.
            continue
        if not _is_plausible_name(agent.name):
            # Substring match but obvious stopword / fragment.
            # Same silent-drop semantics as the hallucination case.
            continue
        # ``agent.role`` is already typed as the same Literal alias via the
        # LlmAgent schema, but a defence-in-depth check against
        # _VALID_ROLES catches any future widening of the schema that
        # didn't propagate to this enforcement layer.
        role: Literal["author", "editor", "translator", "illustrator", "compiler", "unknown"] = (
            agent.role if agent.role in _VALID_ROLES else "unknown"
        )
        accepted.append(
            ParsedAgent(
                name=agent.name,
                role=role,
                confidence=LLM_CONFIDENCE_CAP,
            )
        )
    return accepted


# --- LangChain-backed extractor ------------------------------------------


@dataclass
class LangChainSalvageExtractor:
    """Production extractor that calls Qwen3 via LangChain.

    Validation-failure retry (max 2) and connection-error retry
    (5 / 30 / 120 s, max 3) mirror :class:`LangChainContribExtractor`
    and the M6 / M9 cascades. On unrecoverable failure, returns ``[]``
    so the dispatcher cleanly falls through to B2 or B3 instead of
    raising and dropping the record.

    Cache is opt-in via ``cache`` constructor arg; when ``None``,
    every call hits the LLM. The production CLI wires
    :func:`default_synth_cache_path` so re-runs are deterministic.
    """

    model_name: str | None = None
    chain: Any = None
    cache: SalvageCache | None = None
    sleep: Callable[[float], None] = time.sleep

    def _resolved_chain(self) -> Any:
        if self.chain is not None:
            return self.chain
        settings = get_settings()
        # Resolution order: constructor arg → Settings.llm_model_primary
        # (the mlx-lm model path from .env) → DEFAULT_SALVAGE_MODEL
        # (defensive fallback for unset env).
        model_name = self.model_name or settings.llm_model_primary or DEFAULT_SALVAGE_MODEL
        # Prefer the primary mlx-lm server URL when set; the cascade
        # cares about one model so the fallback URL stays unused.
        base_url = settings.llm_base_url_primary or settings.llm_base_url
        return _build_chain(
            model_name=model_name,
            base_url=base_url,
            api_key=settings.llm_api_key,
        )

    def extract(self, *, c_subfield: str) -> list[ParsedAgent]:
        # Side-channel telemetry for the audit log writer in
        # stages/m2/salvage_audit.py. The Protocol's return shape
        # stays list[ParsedAgent]; the audit writer reads
        # ``getattr(extractor, "_last_call", None)`` to stamp the
        # row with the rationale + cache_hit flag. Cleared at the
        # top of each call so a stub extractor's stale state can't
        # leak into the next record's audit row.
        from bffi_pipeline.stages.m2.salvage_audit import (  # noqa: PLC0415
            SalvageCallTelemetry,
        )

        self._last_call: SalvageCallTelemetry | None = None
        c_subfield = (c_subfield or "").strip()
        if not c_subfield:
            return []

        prompt_hash_value = salvage_245c_prompt_hash()
        cache_key = _cache_key(prompt_hash_value=prompt_hash_value, c_subfield=c_subfield)

        if self.cache is not None:
            cached = self.cache.get(cache_key)
            if cached is not None:
                self._last_call = SalvageCallTelemetry(
                    rationale=cached.rationale,
                    cache_hit=True,
                )
                return _enforce_verbatim_substring(cached, c_subfield=c_subfield)

        chain = self._resolved_chain()
        invoke_payload = {"c_subfield": c_subfield}

        connection_attempts = 0
        validation_attempts = 0

        while True:
            try:
                raw = chain.invoke(invoke_payload)
            except Exception as exc:
                if _is_connection_error(exc):
                    if connection_attempts < SALVAGE_MAX_CONNECTION_RETRIES:
                        self.sleep(SALVAGE_CONNECTION_BACKOFF_SECONDS[connection_attempts])
                        connection_attempts += 1
                        continue
                    return []
                return []

            try:
                if isinstance(raw, SalvageDecision):
                    decision = raw
                else:
                    decision = SalvageDecision.model_validate(raw)
            except ValidationError, ValueError:
                if validation_attempts < SALVAGE_MAX_VALIDATION_RETRIES:
                    validation_attempts += 1
                    continue
                return []

            # Cache only validated decisions — failed validations
            # might be the model's fault (recoverable on re-run) or
            # the prompt's fault (recoverable on prompt fix).
            if self.cache is not None:
                self.cache.set(
                    cache_key,
                    decision,
                    model_name=self.model_name or DEFAULT_SALVAGE_MODEL,
                    prompt_hash_value=prompt_hash_value,
                )
            self._last_call = SalvageCallTelemetry(
                rationale=decision.rationale,
                cache_hit=False,
            )
            return _enforce_verbatim_substring(decision, c_subfield=c_subfield)


__all__ = [
    "DEFAULT_SALVAGE_MODEL",
    "LLM_CONFIDENCE_CAP",
    "SALVAGE_245C_PROMPT_PATH",
    "SYNTH_CACHE_FILENAME",
    "LangChainSalvageExtractor",
    "LlmAgent",
    "SalvageCache",
    "SalvageDecision",
    "SalvageExtractor",
    "StubSalvageExtractor",
    "default_synth_cache_path",
    "salvage_245c_prompt_hash",
    "salvage_245c_prompt_text",
]
