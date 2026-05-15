"""M8 — Canonical Work + Expression mint stage.

Public surface is re-exported from :mod:`bffi_pipeline.stages.m8.runner`.
Private helpers (anything prefixed with ``_``) stay reachable via the
submodule path (``from bffi_pipeline.stages.m8.runner import _foo``).
"""

from bffi_pipeline.stages.m8.runner import (
    CANONICAL_CONFLICTS_FILENAME,
    CANONICAL_FILENAME,
    CANONICAL_MAP_FILENAME,
    CANONICAL_MINT_FAILURES_FILENAME,
    CANONICAL_MINT_FAILURES_TSV_FILENAME,
    HELMET_MAP_FILENAME,
    JUDGE_DECISIONS_FILENAME,
    CanonicalEntry,
    CanonicalWorkInputs,
    ContributionTarget,
    ExpressionContribution,
    GroupConflict,
    HelmetMapEntry,
    JudgeDecisionRow,
    MintFailure,
    SubjectTarget,
    apply_merge,
    extract_work_metadata,
)

__all__ = [
    "CANONICAL_CONFLICTS_FILENAME",
    "CANONICAL_FILENAME",
    "CANONICAL_MAP_FILENAME",
    "CANONICAL_MINT_FAILURES_FILENAME",
    "CANONICAL_MINT_FAILURES_TSV_FILENAME",
    "HELMET_MAP_FILENAME",
    "JUDGE_DECISIONS_FILENAME",
    "CanonicalEntry",
    "CanonicalWorkInputs",
    "ContributionTarget",
    "ExpressionContribution",
    "GroupConflict",
    "HelmetMapEntry",
    "JudgeDecisionRow",
    "MintFailure",
    "SubjectTarget",
    "apply_merge",
    "extract_work_metadata",
]
