"""M9 — Authority reconciliation stage (KANTO / VIAF / YSO / KAUNO / MUSO).

Public surface is re-exported from :mod:`bffi_pipeline.stages.m9.runner`.
Private helpers (anything prefixed with ``_``) stay reachable via the
submodule path (``from bffi_pipeline.stages.m9.runner import _foo``).

The package also bundles two satellites that depend on the runner:
:mod:`bffi_pipeline.stages.m9.local_concept_resolver` (tier-0 YSO/KAUNO/
MUSO resolver) and :mod:`bffi_pipeline.stages.m9.ysa_disambiguation_report`
(diagnostic report — moves to ``src/bffi_pipeline/diagnostics/`` in
P-38 Phase C-3).
"""

from bffi_pipeline.stages.m9.runner import (
    ALL_AUTHORITY_KINDS,
    LEXICAL_DIRECT_THRESHOLD,
    LEXICAL_FLOOR,
    LLM_CONFIDENCE_THRESHOLD,
    PICKER_MAX_VALIDATION_RETRIES,
    PICKER_ORDERING_PREFIX_CACHE,
    PICKER_ORDERING_SUBMISSION,
    PICKER_PROMPT_PATH,
    STAGE_FALLBACK,
    STAGE_FICTIONAL,
    STAGE_LEXICAL,
    STAGE_LLM,
    STAGE_LOCAL,
    STAGE_NO_CANDIDATE,
    AuthorityCandidate,
    AuthorityKind,
    EntityRequest,
    FintoSkosmosClient,
    LangChainLLMPicker,
    LLMPicker,
    PickerCache,
    PickerDecision,
    PickerOrdering,
    ReconciliationOutcome,
    ReconciliationSummary,
    StubAuthorityClient,
    StubPicker,
    ViafClient,
    apply_reconciliation,
    compute_finto_shas,
    compute_picker_cache_key,
    decide_reconciliation,
    hash_finto_dump,
    lexical_similarity,
    picker_cache_default_path,
    picker_prompt_hash,
    picker_prompt_text,
    reconcile_one,
)

__all__ = [
    "ALL_AUTHORITY_KINDS",
    "LEXICAL_DIRECT_THRESHOLD",
    "LEXICAL_FLOOR",
    "LLM_CONFIDENCE_THRESHOLD",
    "PICKER_MAX_VALIDATION_RETRIES",
    "PICKER_ORDERING_PREFIX_CACHE",
    "PICKER_ORDERING_SUBMISSION",
    "PICKER_PROMPT_PATH",
    "STAGE_FALLBACK",
    "STAGE_FICTIONAL",
    "STAGE_LEXICAL",
    "STAGE_LLM",
    "STAGE_LOCAL",
    "STAGE_NO_CANDIDATE",
    "AuthorityCandidate",
    "AuthorityKind",
    "EntityRequest",
    "FintoSkosmosClient",
    "LLMPicker",
    "LangChainLLMPicker",
    "PickerCache",
    "PickerDecision",
    "PickerOrdering",
    "ReconciliationOutcome",
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
