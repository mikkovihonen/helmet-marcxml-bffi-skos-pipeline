"""M5 operator entry points — :func:`build_index` and :func:`query_candidates`.

The two phases of M5: build phase encodes every Work in the corpus
and writes a persistent FAISS HNSW index; query phase reloads that
index, runs top-k retrieval per Work, and emits candidate pairs to
``embed-candidates.jsonl`` for M6 to judge.

Both are idempotent — build skips when the on-disk index is fresh
against the source corpus and matches the chosen model name; query
overwrites the candidate sidecar atomically.

P-38 Phase D: extracted from m5/runner.py. No logic change.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from bffi_pipeline.config import get_settings
from bffi_pipeline.observability.events import emit_if_active
from bffi_pipeline.stages.m5.bands import _bucket_label, classify_band
from bffi_pipeline.stages.m5.embed import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_DEVICE,
    DEFAULT_MODEL,
    EMBEDDING_DIM,
    _embed,
)
from bffi_pipeline.stages.m5.faiss_index import (
    HNSW_EF_CONSTRUCTION,
    HNSW_EF_SEARCH,
    HNSW_M,
    IDMAP_FILENAME,
    INDEX_FILENAME,
    _build_hnsw,
    _load_persisted_index,
    _reconstruct_vectors,
)
from bffi_pipeline.stages.m5.graph_extract import (
    embedding_input_string,
    extract_embedding_inputs,
    to_blocking_key,
)
from bffi_pipeline.stages.m5.io import (
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
)


def build_index(
    corpus_dir: Path | None = None,
    *,
    output_dir: Path | None = None,
    model_name: str = DEFAULT_MODEL,
    device: str = DEFAULT_DEVICE,
    batch_size: int = DEFAULT_BATCH_SIZE,
    force: bool = False,
) -> IndexBuildResult:
    """Build (or skip-when-fresh) the FAISS HNSW index for the BFFI corpus.

    Reads every BFFI Turtle + BIBFRAME RDF/XML under ``corpus_dir``,
    extracts per-Work embedding inputs, encodes them, builds the HNSW
    index, and persists ``embeddings.faiss`` + ``embeddings.idmap.json``
    under ``output_dir``.
    """
    import faiss

    settings = get_settings()
    corpus_dir = corpus_dir or settings.data_dir
    output_dir = output_dir or settings.data_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    bffi_files, bibframe_files = _index_inputs(corpus_dir)
    inputs: list[Path] = [*bffi_files, *bibframe_files]
    index_path = output_dir / INDEX_FILENAME
    idmap_path = output_dir / IDMAP_FILENAME

    if not force and _is_index_fresh(index_path, idmap_path, inputs, model_name):
        idmap = json.loads(idmap_path.read_text(encoding="utf-8"))
        n_cached = idmap.get("n_works", 0)
        # Skip-when-fresh path still emits start + end so the dashboard
        # shows the stage as ``done`` (with zero elapsed) rather than
        # staying ``pending``. The total carries the cached n_works.
        emit_if_active(
            stage="m5",
            event="start",
            counters={"total": n_cached},
            extra={"skipped": True, "model": model_name},
        )
        emit_if_active(
            stage="m5",
            event="end",
            counters={"total": n_cached, "skipped": 1},
            extra={"skipped": True},
        )
        return IndexBuildResult(
            n_works=n_cached,
            ndim=idmap.get("ndim", EMBEDDING_DIM),
            model_name=model_name,
            hnsw_m=idmap.get("hnsw_m", HNSW_M),
            hnsw_ef_construction=idmap.get("hnsw_ef_construction", HNSW_EF_CONSTRUCTION),
            hnsw_ef_search=idmap.get("hnsw_ef_search", HNSW_EF_SEARCH),
            build_seconds=0.0,
            index_path=str(index_path),
            idmap_path=str(idmap_path),
        )

    graph = _load_corpus_graph(corpus_dir)
    works = list(extract_embedding_inputs(graph))
    if not works:
        raise ValueError(
            f"No bffi:Work entities found under {corpus_dir!s}. "
            "Did M2 + M3 run, and is the directory layout bffi/<id>.ttl + bibframe/<id>.rdf?"
        )

    emit_if_active(
        stage="m5",
        event="start",
        counters={"total": len(works)},
        extra={"model": model_name, "device": device, "batch_size": batch_size},
    )
    started = time.monotonic()
    strings = [embedding_input_string(w) for w in works]
    matrix = _embed(strings, model_name=model_name, device=device, batch_size=batch_size)
    index = _build_hnsw(matrix)
    build_seconds = time.monotonic() - started

    # Atomic-rename writes so a half-built index never lands on disk.
    tmp_index = index_path.with_suffix(index_path.suffix + ".tmp")
    tmp_idmap = idmap_path.with_suffix(idmap_path.suffix + ".tmp")
    faiss.write_index(index, str(tmp_index))
    idmap_payload = {
        "model_name": model_name,
        "ndim": int(matrix.shape[1]),
        "n_works": len(works),
        "hnsw_m": HNSW_M,
        "hnsw_ef_construction": HNSW_EF_CONSTRUCTION,
        "hnsw_ef_search": HNSW_EF_SEARCH,
        "build_seconds": build_seconds,
        "ids": [
            {
                "row": i,
                "work_uri": w.work_uri,
                "blocking_key": to_blocking_key(w),
            }
            for i, w in enumerate(works)
        ],
    }
    tmp_idmap.write_text(json.dumps(idmap_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_index.replace(index_path)
    tmp_idmap.replace(idmap_path)

    emit_if_active(
        stage="m5",
        event="end",
        counters={"total": len(works), "ndim": int(matrix.shape[1])},
        extra={"build_seconds": build_seconds},
    )
    return IndexBuildResult(
        n_works=len(works),
        ndim=int(matrix.shape[1]),
        model_name=model_name,
        hnsw_m=HNSW_M,
        hnsw_ef_construction=HNSW_EF_CONSTRUCTION,
        hnsw_ef_search=HNSW_EF_SEARCH,
        build_seconds=build_seconds,
        index_path=str(index_path),
        idmap_path=str(idmap_path),
    )


def query_candidates(
    output_dir: Path | None = None,
    *,
    top_k: int = DEFAULT_TOP_K,
    cross_block: bool = False,
    output_path: Path | None = None,
) -> EmbedStats:
    """Run top-k retrieval over the persisted index and emit candidate pairs.

    Pairs are emitted in JSONL form to ``output_dir / embed-candidates.jsonl``
    (override with ``output_path``). When ``cross_block`` is False
    (default) only pairs sharing a Stage-1 blocking key are kept; the
    blocking-key intersection is the spec § 6 default. Self-matches and
    duplicate ``(a, b) / (b, a)`` orderings are deduplicated.
    """
    output_dir = output_dir or get_settings().data_dir
    index, idmap = _load_persisted_index(output_dir)
    rows = idmap.get("ids", [])
    n_works = len(rows)
    if n_works == 0:
        raise ValueError(f"Idmap under {output_dir!s} contains no Works.")
    work_uris = [row["work_uri"] for row in rows]
    blocking_keys = [row["blocking_key"] for row in rows]
    candidates_path = output_path or (output_dir / CANDIDATES_FILENAME)

    stats = EmbedStats(
        n_works=n_works,
        ndim=int(idmap.get("ndim", EMBEDDING_DIM)),
        model_name=str(idmap.get("model_name", DEFAULT_MODEL)),
        index_path=str(output_dir / INDEX_FILENAME),
    )

    # Reconstruct the row vectors from the persisted index so the
    # query phase can run without re-encoding.
    vectors = _reconstruct_vectors(index, n_works)
    k = min(top_k + 1, n_works)  # +1 because the top hit is always the row itself

    seen: set[tuple[int, int]] = set()
    tmp_path = candidates_path.with_suffix(candidates_path.suffix + ".tmp")
    candidates_path.parent.mkdir(parents=True, exist_ok=True)
    with tmp_path.open("w", encoding="utf-8") as fh:
        scores, neighbours = index.search(vectors, k)
        for i in range(n_works):
            for rank in range(k):
                j = int(neighbours[i, rank])
                if j in (-1, i):
                    continue
                pair = (i, j) if i < j else (j, i)
                if pair in seen:
                    continue
                seen.add(pair)
                similarity = float(scores[i, rank])
                key_a = blocking_keys[pair[0]]
                key_b = blocking_keys[pair[1]]
                same_block = key_a == key_b
                if not cross_block and not same_block:
                    continue
                pair_record = CandidatePair(
                    work_a=work_uris[pair[0]],
                    work_b=work_uris[pair[1]],
                    similarity=similarity,
                    block_a=key_a,
                    block_b=key_b,
                    cross_block=not same_block,
                    band=classify_band(similarity),
                )
                fh.write(json.dumps(asdict(pair_record), ensure_ascii=False) + "\n")
                stats.total_pairs += 1
                stats.band_counts[pair_record.band] += 1
                stats.similarity_buckets[_bucket_label(similarity)] += 1
                if pair_record.cross_block:
                    stats.cross_block_count += 1
    tmp_path.replace(candidates_path)
    return stats
