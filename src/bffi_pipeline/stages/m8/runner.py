"""Stage M8: merge application — union-find on judge decisions, canonical Works.

Reads M6's ``judge-decisions.jsonl`` and the M3 BFFI Turtle, applies
union-find over ``same_work`` decisions, mints canonical Work URIs
via :func:`bffi_pipeline.uris.mint_work_uri`, unions the
``bf:identifiedBy`` sets so the canonical Work carries one
identifier per absorbed Helmet record, rewrites
``bffi:expressionOf`` to point at the canonical URI, and emits one
``bffi:AdminMetadata`` block per canonical Work per spec § 8.

Outputs land under ``BFFI_DATA_DIR``:

- ``canonical.ttl`` — canonical Works + AdminMetadata + rewritten
  ``bffi:expressionOf`` triples + ``prov:wasDerivedFrom`` links.
- ``canonical-map.jsonl`` — one row per canonical Work
  (``{canonical_work_uri, raw_work_uris, helmet_bib_ids, merged_at}``).
- ``canonical-conflicts.jsonl`` — groups that are flagged because the
  judge produced contradictory decisions
  (``A=B``, ``A≠C``, ``B=C``). Conflicts are *not* silently merged;
  they go to a separate file for human review.

M8 is split across cohesive siblings inside this package:

- :mod:`schemas` — public dataclasses, filename constants, mint-anchor enum.
- :mod:`decisions` — judge JSONL loader + conflict detection.
- :mod:`graph_extract` — BFFI graph → per-Work canonical-mint inputs.
- :mod:`helmet_map` — ``helmet-map.jsonl`` loader.
- :mod:`sidecars` — canonical-map / conflicts / mint-failures emitters.
- :mod:`mint` — propagation + canonical-Work + AdminMetadata writer.
- :mod:`corpus` — BFFI corpus / per-record loader (P-19 fast-path).
- :mod:`apply` — :func:`apply_merge` orchestrator.
- :mod:`union_find` — disjoint-set data structure (Phase B).
- :mod:`contribution_variants` — F2 transliteration-variant bind pass (Phase B).

P-38 Phase D: runner.py is now a thin re-export shell. The ``# noqa:
F401`` imports keep the ``m8.runner._private`` test path resolving
bit-identically. Re-running M8 with the same inputs produces
byte-identical outputs because the anchor for each merge group is
the lex-smallest member URI and the merged-at timestamp is taken
from the latest M2 ``converted_at`` in the group rather than wall clock.
"""

from __future__ import annotations

from bffi_pipeline.stages.m8.apply import _iter_member_pairs, apply_merge  # noqa: F401
from bffi_pipeline.stages.m8.contribution_variants import (  # noqa: F401
    _apply_contrib_variants,
)
from bffi_pipeline.stages.m8.corpus import _load_work_records_from_corpus  # noqa: F401
from bffi_pipeline.stages.m8.decisions import _detect_conflicts, _load_decisions  # noqa: F401
from bffi_pipeline.stages.m8.graph_extract import (  # noqa: F401
    _all_pref_labels,
    _anonymous_work_anchor_uri,
    _collect_contribution_agent_uris,
    _expression_contributions,
    _expression_labels,
    _first_contribution_agent_uri,
    _first_pref_label,
    _helmet_identifiers,
    _is_translator_role,
    _primary_agent_uri,
    _primary_contribution_targets,
    _read_agent,
    _read_role,
    _read_subject_label_and_source,
    _subject_targets,
    extract_work_metadata,
)
from bffi_pipeline.stages.m8.helmet_map import _load_helmet_map  # noqa: F401
from bffi_pipeline.stages.m8.mint import (  # noqa: F401
    _admin_metadata_uri,
    _bind_prefixes,
    _earliest_converted_at,
    _emit_canonical_work,
    _emit_mint_anchor,
    _propagate_expressions,
    _propagate_primary_contributions,
    _propagate_subject_targets,
    _select_description_modifier,
)
from bffi_pipeline.stages.m8.schemas import (
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
    MergeResult,
    MintAnchorKind,
    MintFailure,
    SubjectTarget,
)
from bffi_pipeline.stages.m8.sidecars import (  # noqa: F401
    _emit_canonical_map,
    _emit_conflicts,
    _emit_mint_failures,
    _emit_mint_failures_tsv,
)
from bffi_pipeline.stages.m8.union_find import _UnionFind  # noqa: F401

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
    "MergeResult",
    "MintAnchorKind",
    "MintFailure",
    "SubjectTarget",
    "apply_merge",
    "extract_work_metadata",
]
