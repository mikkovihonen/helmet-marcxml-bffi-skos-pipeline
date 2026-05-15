"""M5 encoder pass — sentence-transformer batched encode + L2-normalise.

``_embed`` runs the chosen sentence-transformer in cadence-sized
chunks so the dashboard's M5 row-2 bargauge + state-tile ETA can
update mid-encode rather than only at the end. Chunk size
(:data:`_M5_PROGRESS_CADENCE`) is independent of batch size — batch
size is the GPU-saturation knob, chunk size is the dashboard-update
knob.

Heavy imports (``sentence_transformers``, ``numpy``, ``faiss``) are
deferred to call time so the rest of the CLI doesn't pay the
multi-second import cost.

P-38 Phase D: extracted from m5/runner.py. No logic change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, cast

from bffi_pipeline.observability.events import emit_if_active

if TYPE_CHECKING:
    import numpy as np

#: BGE-M3 / e5-large / jina-v3 all match this dimension.
EMBEDDING_DIM: Final[int] = 1024

DEFAULT_MODEL: Final[str] = "BAAI/bge-m3"

#: ``mps`` is PyTorch's Metal backend; ``cpu`` is the CI-friendly
#: fallback. Batch 64 saturates the GPU on the target M5 Max without
#: OOM.
DEFAULT_DEVICE: Final[str] = "mps"
DEFAULT_BATCH_SIZE: Final[int] = 64

#: P-12 Phase D cadence for M5 progress events. Chosen so a 5k-record
#: bench emits ~10 events (cheap, dashboard-friendly) and an 800k-record
#: full corpus emits ~1600 (still cheap; the embed loop's per-batch
#: cost dwarfs the emit cost). Each progress event drives the
#: dashboard's M5 row-2 bargauge + the state-tile ETA derivation.
_M5_PROGRESS_CADENCE: Final[int] = 500


def _l2_normalize(matrix: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    import numpy as np

    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return cast("np.ndarray[Any, Any]", matrix / norms)


def _embed(
    strings: list[str],
    *,
    model_name: str,
    device: str,
    batch_size: int,
) -> np.ndarray[Any, Any]:
    """Encode ``strings`` with the configured model. Returns an L2-normalised float32 matrix.

    Encoding happens in cadence-sized chunks rather than one giant
    ``model.encode`` call so the embed loop can emit ``progress`` stage
    events between chunks. The exporter's throughput logic then derives
    a rate + ETA for the dashboard's M5 state tile. The chunk size
    (``_M5_PROGRESS_CADENCE``) is independent of ``batch_size`` — batch
    size is the GPU-saturation knob, chunk size is the dashboard-update
    knob.
    """
    import numpy as np
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(model_name, device=device)
    total = len(strings)
    pieces: list[np.ndarray[Any, Any]] = []
    for chunk_start in range(0, total, _M5_PROGRESS_CADENCE):
        chunk_end = min(chunk_start + _M5_PROGRESS_CADENCE, total)
        chunk_vectors = model.encode(
            strings[chunk_start:chunk_end],
            batch_size=batch_size,
            convert_to_numpy=True,
            normalize_embeddings=False,  # do it ourselves so the policy is explicit
            show_progress_bar=False,  # we drive our own progress via stage events
        )
        pieces.append(np.asarray(chunk_vectors, dtype=np.float32))
        emit_if_active(
            stage="m5",
            event="progress",
            counters={"processed": chunk_end, "total": total},
        )
    if pieces:
        matrix = np.concatenate(pieces, axis=0)
    else:
        matrix = np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
    return _l2_normalize(matrix)
