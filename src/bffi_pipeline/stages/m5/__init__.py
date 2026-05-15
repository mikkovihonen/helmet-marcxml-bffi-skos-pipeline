"""M5 — Embedding + FAISS candidate-pair generation stage.

Public surface is re-exported from :mod:`bffi_pipeline.stages.m5.runner`.
Private helpers (anything prefixed with ``_``) stay reachable via the
submodule path (``from bffi_pipeline.stages.m5.runner import _foo``).
"""

from bffi_pipeline.stages.m5.runner import (
    BAND_AUTO_MERGE,
    BAND_REJECT,
    CANDIDATES_FILENAME,
    DEFAULT_BATCH_SIZE,
    DEFAULT_DEVICE,
    DEFAULT_MODEL,
    DEFAULT_TOP_K,
    EMBEDDING_DIM,
    IDMAP_FILENAME,
    INDEX_FILENAME,
    Band,
    CandidatePair,
    EmbedStats,
    IndexBuildResult,
    WorkEmbeddingInput,
    build_index,
    classify_band,
    embedding_input_string,
    extract_embedding_inputs,
    query_candidates,
    to_blocking_key,
)

__all__ = [
    "BAND_AUTO_MERGE",
    "BAND_REJECT",
    "CANDIDATES_FILENAME",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_DEVICE",
    "DEFAULT_MODEL",
    "DEFAULT_TOP_K",
    "EMBEDDING_DIM",
    "IDMAP_FILENAME",
    "INDEX_FILENAME",
    "Band",
    "CandidatePair",
    "EmbedStats",
    "IndexBuildResult",
    "WorkEmbeddingInput",
    "build_index",
    "classify_band",
    "embedding_input_string",
    "extract_embedding_inputs",
    "query_candidates",
    "to_blocking_key",
]
