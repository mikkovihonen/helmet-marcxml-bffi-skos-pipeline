"""M5 FAISS HNSW build + persistence + reload.

Wraps the FAISS-side knobs (HNSW M / efConstruction / efSearch) and
the on-disk file layout (``embeddings.faiss`` + ``embeddings.idmap.json``
under ``<output_dir>/``). The query pass reads the same idmap, which
also records the model name + dimensions so a stale index built
against a different model is invalidatable.

Heavy imports (``faiss``, ``numpy``) are deferred to the functions
that need them so the rest of the CLI doesn't pay the import cost.

P-38 Phase D: extracted from m5/runner.py. No logic change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    import numpy as np

#: FAISS HNSW knobs. Values per spec § 6:
#:  - ``M=32`` controls graph density at insertion time;
#:  - ``efConstruction=200`` is the standard high-recall build setting;
#:  - ``efSearch=64`` is a placeholder default until M12 gold-set tuning
#:    against {32, 64, 128, 256} picks the smallest value with full recall.
HNSW_M: Final[int] = 32
HNSW_EF_CONSTRUCTION: Final[int] = 200
HNSW_EF_SEARCH: Final[int] = 64

#: On-disk filenames. Both files live at ``<output_dir>/``.
INDEX_FILENAME: Final[str] = "embeddings.faiss"
IDMAP_FILENAME: Final[str] = "embeddings.idmap.json"


def _build_hnsw(matrix: np.ndarray[Any, Any]) -> Any:
    """Construct the IndexHNSWFlat over ``matrix`` (which must already be L2-normalised)."""
    import faiss

    ndim = matrix.shape[1]
    index = faiss.IndexHNSWFlat(ndim, HNSW_M, faiss.METRIC_INNER_PRODUCT)
    index.hnsw.efConstruction = HNSW_EF_CONSTRUCTION
    index.hnsw.efSearch = HNSW_EF_SEARCH
    index.add(matrix)
    return index


def _load_persisted_index(output_dir: Path) -> tuple[Any, dict[str, Any]]:
    """Reload (faiss_index, idmap_dict) written by :func:`build.build_index`."""
    import faiss

    index_path = output_dir / INDEX_FILENAME
    idmap_path = output_dir / IDMAP_FILENAME
    if not index_path.is_file() or not idmap_path.is_file():
        raise FileNotFoundError(
            f"Missing FAISS index / idmap under {output_dir!s}. Run build_index() first."
        )
    index = faiss.read_index(str(index_path))
    idmap = json.loads(idmap_path.read_text(encoding="utf-8"))
    return index, idmap


def _reconstruct_vectors(index: Any, n: int) -> np.ndarray[Any, Any]:
    """Pull the L2-normalised matrix back out of a FAISS index.

    ``IndexHNSWFlat`` stores vectors via an inner ``IndexFlat``; both
    expose ``reconstruct_n``.
    """
    import numpy as np

    matrix = index.reconstruct_n(0, n)
    return np.asarray(matrix, dtype=np.float32)
