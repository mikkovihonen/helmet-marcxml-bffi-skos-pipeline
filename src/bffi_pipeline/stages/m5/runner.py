"""Stage M5: embedding-based candidate generation (spec § 6 Stage 2).

Within each Stage-1 block, embed a fixed-format structured string per
BFFI Work, build a FAISS ``IndexHNSWFlat`` over L2-normalised vectors,
and emit candidate pairs above a low threshold for the M6 LLM judge to
look at.

M5 has two operator entry points rather than a single ``run()``:
:func:`build_index` (encode + persist) and :func:`query_candidates`
(reload + top-k + emit pairs). Both are exposed via the package
namespace ``bffi_pipeline.stages.m5`` for the parent runner / the
``python -m bffi_pipeline.stages.m5`` subprocess to drive.

P-38 Phase D: runner.py keeps only the operator-facing entry points
and the public re-export surface. Every dataclass, FAISS knob,
graph-extraction helper, encoder routine, and I/O helper moved to a
cohesive sibling module in this package. The re-import block keeps
the ``m5.runner._private`` test path resolving bit-identically.
"""

from __future__ import annotations

# P-38 Phase D: every M5 helper / dataclass / constant lives in a
# sibling module now. Re-imported here so:
#   1. callsites within this package find the helpers they expect;
#   2. tests + the package-level ``__init__`` keep reaching for
#      private symbols via `m5.runner._foo` bit-identically.
from bffi_pipeline.stages.m5.bands import (  # noqa: F401
    BAND_AUTO_MERGE,
    BAND_REJECT,
    Band,
    _bucket_label,
    classify_band,
)
from bffi_pipeline.stages.m5.build import build_index, query_candidates
from bffi_pipeline.stages.m5.embed import (  # noqa: F401
    _M5_PROGRESS_CADENCE,
    DEFAULT_BATCH_SIZE,
    DEFAULT_DEVICE,
    DEFAULT_MODEL,
    EMBEDDING_DIM,
    _embed,
    _l2_normalize,
)
from bffi_pipeline.stages.m5.faiss_index import (  # noqa: F401
    HNSW_EF_CONSTRUCTION,
    HNSW_EF_SEARCH,
    HNSW_M,
    IDMAP_FILENAME,
    INDEX_FILENAME,
    _build_hnsw,
    _load_persisted_index,
    _reconstruct_vectors,
)
from bffi_pipeline.stages.m5.graph_extract import (  # noqa: F401
    _CONTENT_URI_PREFIX,
    _LANG_URI_PREFIX,
    _YEAR_RE,
    _agent_label,
    _expression_objects,
    _first_pref_label,
    _first_short_segment,
    _normalise_year,
    _origin_year,
    _primary_agent_uris,
    _short_segment,
    embedding_input_string,
    extract_embedding_inputs,
    to_blocking_key,
)
from bffi_pipeline.stages.m5.io import (  # noqa: F401
    CANDIDATES_FILENAME,
    DEFAULT_TOP_K,
    _index_inputs,
    _is_index_fresh,
    _load_corpus_graph,
)
from bffi_pipeline.stages.m5.schemas import (
    CandidatePair,
    EmbedStats,
    IndexBuildResult,
    WorkEmbeddingInput,
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
    "HNSW_EF_CONSTRUCTION",
    "HNSW_EF_SEARCH",
    "HNSW_M",
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
