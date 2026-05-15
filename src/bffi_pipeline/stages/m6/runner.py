"""Stage M6: LLM judge — structured output + two-model cascade.

The judge runs against a local OpenAI-compatible server (Ollama for
development, mlx-lm for production batches; both speak the same
chat-completions API). Application code talks through
``langchain-openai`` with ``LLM_BASE_URL`` from ``Settings``.

The judge is split across five cohesive siblings inside this package:

- :mod:`prompts` — judge_v1 / judge_v1_fast prompt loaders + hashing.
- :mod:`validation` — Pydantic verdict schemas with the three
  Boundary-4 ``@model_validator(mode="after")`` checks per spec § 7.
- :mod:`cache` — SQLite-backed key/value store keyed on the canonical
  ``(model, prompt_hash, record_a, record_b)`` tuple. Post-validation
  writes only.
- :mod:`clients` — LangChain chain construction + retry-error
  classification (timeouts vs. other connection errors).
- :mod:`outcome` — :class:`CascadeStep`, :class:`JudgeOutcome`,
  ``STAGE_*`` provenance tags, the synthetic-decision builders.
- :mod:`cascade` — :func:`judge_pair` (single-call, retry + cache) and
  :func:`cascade_judge` (32 B primary → 72 B fallback).
- :mod:`graph_extract` — combined BFFI + BIBFRAME graph →
  per-Work :class:`WorkRecord` extraction.
- :mod:`sidecars` — candidate / decision JSONL + ``.checkpoint`` mirror.
- :mod:`batch` — :func:`judge_batch` driver, progress + result shapes.

P-38 Phase D: runner.py is a thin re-export shell. The ``# noqa: F401``
imports keep the ``m6.runner._private`` test path resolving
bit-identically; tests that monkeypatch the runner namespace also need
the matching sub-module's symbol patched (see m6 test fixtures).
"""

from __future__ import annotations

# P-38 Phase D: re-export ``get_settings`` so the historical
# ``m6.runner.get_settings`` lookup (used by tests via
# ``judge.get_settings()``) stays reachable.
from bffi_pipeline.config import get_settings  # noqa: F401
from bffi_pipeline.stages.m6.batch import (  # noqa: F401
    CHECKPOINT_INTERVAL,
    DEFAULT_CONCURRENCY,
    CascadeFn,
    JudgeBatchProgress,
    JudgeBatchResult,
    _emit_provenance,
    judge_batch,
)
from bffi_pipeline.stages.m6.cache import (  # noqa: F401
    CACHE_FILENAME,
    JudgeCache,
    _cache_key,
    _canonicalise_record,
    default_cache_path,
)
from bffi_pipeline.stages.m6.cascade import (  # noqa: F401
    CONNECTION_BACKOFF_SECONDS,
    FALLBACK_CONFIDENCE_THRESHOLD,
    MAX_CONNECTION_RETRIES,
    MAX_VALIDATION_RETRIES,
    ChainLike,
    _needs_second_opinion,
    cascade_judge,
    judge_pair,
)
from bffi_pipeline.stages.m6.clients import (  # noqa: F401
    _M6_PROMPT_PREFIX_FAST,
    _M6_PROMPT_PREFIX_FULL,
    _TIMEOUT_EXCEPTION_NAMES,
    _build_chain,
    _build_m6_prompt_prefix,
    _is_connection_error,
    _is_timeout_error,
)
from bffi_pipeline.stages.m6.graph_extract import (  # noqa: F401
    _CONTENT_URI_PREFIX,
    _LANG_URI_PREFIX,
    _expression_summary,
    _first_pref_label,
    _load_work_records_from_corpus,
    _origin_date,
    _primary_creator,
    _strip_loc_prefix,
    extract_work_records,
)
from bffi_pipeline.stages.m6.outcome import (
    STAGE_AUTO_MERGE,
    STAGE_PRIMARY,
    STAGE_SECOND_OPINION,
    STAGE_WATCHDOG,
    CascadeStep,
    JudgeOutcome,
    _uncertain_decision,  # noqa: F401
    synthesize_auto_merge_outcome,
)
from bffi_pipeline.stages.m6.prompts import (  # noqa: F401
    _PROMPT_SECTION_RE,
    PROMPT_PATH,
    PROMPT_PATH_FAST,
    _parse_prompt_sections,
    _parse_prompt_sections_fast,
    _parse_sections,
    prompt_hash,
    prompt_hash_fast,
    prompt_text,
    prompt_text_fast,
)
from bffi_pipeline.stages.m6.sidecars import (  # noqa: F401
    AUTO_MERGE_BAND,
    CHECKPOINT_SUFFIX,
    DECISIONS_FILENAME,
    ESCALATE_BAND,
    JudgeCheckpoint,
    _checkpoint_path_for,
    _load_auto_merge_candidates,
    _load_candidate_jsonl,
    _load_checkpoint,
    _serialise_decision,
    _write_checkpoint,
)
from bffi_pipeline.stages.m6.validation import (
    MIN_RATIONALE_CHARS,
    STUB_PHRASES,
    UNCERTAIN_MAX_CONFIDENCE,  # noqa: F401
    WorkMatchDecision,
    WorkMatchDecisionFast,
    WorkRecord,
    _synthesize_fast_rationale,  # noqa: F401
)

__all__ = [
    "AUTO_MERGE_BAND",
    "CHECKPOINT_INTERVAL",
    "CHECKPOINT_SUFFIX",
    "CONNECTION_BACKOFF_SECONDS",
    "DECISIONS_FILENAME",
    "DEFAULT_CONCURRENCY",
    "ESCALATE_BAND",
    "FALLBACK_CONFIDENCE_THRESHOLD",
    "MAX_CONNECTION_RETRIES",
    "MAX_VALIDATION_RETRIES",
    "MIN_RATIONALE_CHARS",
    "PROMPT_PATH",
    "PROMPT_PATH_FAST",
    "STAGE_AUTO_MERGE",
    "STAGE_PRIMARY",
    "STAGE_SECOND_OPINION",
    "STAGE_WATCHDOG",
    "STUB_PHRASES",
    "CascadeStep",
    "JudgeBatchProgress",
    "JudgeBatchResult",
    "JudgeCache",
    "JudgeCheckpoint",
    "JudgeOutcome",
    "WorkMatchDecision",
    "WorkMatchDecisionFast",
    "WorkRecord",
    "cascade_judge",
    "default_cache_path",
    "extract_work_records",
    "judge_batch",
    "judge_pair",
    "prompt_hash",
    "prompt_hash_fast",
    "prompt_text",
    "prompt_text_fast",
    "synthesize_auto_merge_outcome",
]
