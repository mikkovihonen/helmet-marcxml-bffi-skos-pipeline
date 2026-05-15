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

M9 is split across cohesive siblings inside this package:

- :mod:`schemas` — dataclasses, Literal types, thresholds, stage tags.
- :mod:`probe` — :func:`_m9_probe_dependencies` (Fuseki / mlx-lm / Finto).
- :mod:`lexical` — :func:`lexical_similarity` + normalisation.
- :mod:`picker` — :class:`PickerDecision` Pydantic schema, Protocol +
  :class:`StubPicker`.
- :mod:`picker_prompt` — ``prompts/picker_v1.txt`` loader + hash +
  candidate formatter.
- :mod:`picker_chain` — LangChain chain construction + retry policy
  + :class:`LangChainLLMPicker`.
- :mod:`decisions` — four-tier ladder + outcome factories.
- :mod:`requests` — canonical graph walkers + subject-target
  classifiers.
- :mod:`graph_mutate` — canonical-graph link writes + AdminMetadata
  bumps + provenance emit + canonical-map loader.
- :mod:`pool` — Phase 1 + Phase 2 sequential / thread-pool dispatch
  helpers + per-field wall budget.
- :mod:`apply` — :func:`apply_reconciliation` three-phase orchestrator
  + :func:`reconcile_one`.
- :mod:`authority_clients`, :mod:`picker_cache`,
  :mod:`local_concept_resolver`, :mod:`ysa_disambiguation_report` —
  Phase B siblings already in place.

P-38 Phase D: runner.py is a thin re-export shell. The ``# noqa:
F401`` imports keep the ``m9.runner._private`` test path resolving
bit-identically. Tests that monkeypatch ``reconcile.probe_*`` need
to retarget at :mod:`probe` and / or :mod:`apply` since those are
the import sites the orchestrator's probe call resolves through.
"""

from __future__ import annotations

# P-38 Phase D: re-export ``get_settings`` for tests that reach for it
# via ``reconcile.get_settings()``.
from bffi_pipeline.config import get_settings  # noqa: F401

# P-38 Phase D: re-export the probe symbols so ``conftest.py`` and other
# callers patching ``reconcile.probe_*`` still see the same import
# surface they did pre-refactor (the actual call site is in
# :mod:`probe`, which conftest.py also patches).
from bffi_pipeline.observability.probes import (  # noqa: F401
    emit_health_probes,
    probe_finto,
    probe_fuseki,
    probe_mlx_lm,
)
from bffi_pipeline.stages.m9.apply import (  # noqa: F401
    _CachePending,
    _collect_requests,
    apply_reconciliation,
    reconcile_one,
)
from bffi_pipeline.stages.m9.authority_clients import (
    AuthorityClient,
    FintoSkosmosClient,
    StubAuthorityClient,
    ViafClient,
)
from bffi_pipeline.stages.m9.decisions import (  # noqa: F401
    _DETAILS_CANDIDATE_LIMIT,
    _decide_before_picker,
    _decide_with_pick,
    _fictional_outcome,
    _format_m9_details,
    _local_outcome,
    _watchdog_aborted_outcome,
    decide_reconciliation,
)
from bffi_pipeline.stages.m9.graph_mutate import (  # noqa: F401
    _admin_block_for,
    _apply_canonical_link,
    _bump_admin_metadata,
    _emit_provenance,
    _link_canonical_creator,
    _link_canonical_subject,
    _load_canonical_bib_ids,
)
from bffi_pipeline.stages.m9.lexical import (  # noqa: F401
    _normalise_for_similarity,
    lexical_similarity,
)
from bffi_pipeline.stages.m9.picker import (
    PICKER_MIN_RATIONALE_CHARS,
    PICKER_STUB_PHRASES,
    PICKER_UNCERTAIN_MAX_CONFIDENCE,
    LLMPicker,
    PickerDecision,
    StubPicker,
)
from bffi_pipeline.stages.m9.picker_cache import (
    PICKER_CACHE_FILENAME,
    CacheHit,
    PickerCache,
    compute_finto_shas,
    compute_picker_cache_key,
    hash_finto_dump,
    picker_cache_default_path,
)
from bffi_pipeline.stages.m9.picker_chain import (  # noqa: F401
    PICKER_CONNECTION_BACKOFF_SECONDS,
    PICKER_MAX_CONNECTION_RETRIES,
    PICKER_MAX_VALIDATION_RETRIES,
    LangChainLLMPicker,
    _build_picker_chain,
    _is_picker_connection_error,
    _picker_uncertain,
)
from bffi_pipeline.stages.m9.picker_prompt import (
    _PICKER_SECTION_RE,  # noqa: F401
    PICKER_PROMPT_PATH,
    _format_candidates_for_prompt,  # noqa: F401
    _parse_picker_prompt_sections,  # noqa: F401
    picker_prompt_hash,
    picker_prompt_text,
)
from bffi_pipeline.stages.m9.pool import (  # noqa: F401
    _emit_picker_progress,
    _field_id,
    _order_deferred_picker_queue,
    _phase1_pool,
    _phase1_resolve_one,
    _phase1_seq,
    _Phase1Result,
    _picker_call_with_budget,
    _picker_phase_pool,
    _picker_phase_seq,
    _picker_queue_sort_key,
)
from bffi_pipeline.stages.m9.probe import _m9_probe_dependencies  # noqa: F401
from bffi_pipeline.stages.m9.requests import (  # noqa: F401
    _AGENT_FRAGMENT_TO_KIND,
    _FICTIONAL_CHARACTER_QUALIFIERS,
    _SOURCE_TOKEN_TO_KIND,
    _SUBJECT_AS_NAME_FRAGMENT_RE,
    _classify_subject_source,
    _classify_subject_target,
    _is_fictional_character_literal,
    _iter_creator_requests,
    _iter_subject_requests,
)
from bffi_pipeline.stages.m9.schemas import (
    _CREATOR_KINDS,  # noqa: F401
    _M9_HEALTH_PROBE_CADENCE,  # noqa: F401
    _M9_PROGRESS_CADENCE,  # noqa: F401
    _MIN_CONCURRENCY_FOR_FACTORY,  # noqa: F401
    _SUBJECT_KINDS,  # noqa: F401
    ALL_AUTHORITY_KINDS,
    DEFAULT_TOP_K,
    FINTO_BASE_URL,
    LEXICAL_DIRECT_THRESHOLD,
    LEXICAL_FLOOR,
    LLM_CONFIDENCE_THRESHOLD,
    PICKER_ORDERING_PREFIX_CACHE,
    PICKER_ORDERING_SUBMISSION,
    STAGE_FALLBACK,
    STAGE_FICTIONAL,
    STAGE_LEXICAL,
    STAGE_LLM,
    STAGE_LOCAL,
    STAGE_NO_CANDIDATE,
    STAGE_WATCHDOG_ABORTED,
    VOCAB_KANTO,
    VOCAB_KAUNO,
    VOCAB_MUSO,
    VOCAB_VIAF,
    VOCAB_YSO,
    AuthorityCandidate,
    AuthorityKind,
    EntityRequest,
    PickerOrdering,
    ReconciliationOutcome,
    ReconciliationStage,
    ReconciliationSummary,
    _finto_search_query,  # noqa: F401
)

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
