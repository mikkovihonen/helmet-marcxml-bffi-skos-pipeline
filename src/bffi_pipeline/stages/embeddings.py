"""Stage M5: embedding-based candidate generation (spec § 6 Stage 2).

Within each Stage-1 block, embed a fixed-format structured string per
BFFI Work, build a FAISS ``IndexHNSWFlat`` over L2-normalised vectors,
and emit candidate pairs above a low threshold for the M6 LLM judge to
look at.

The structural pieces (input-string builder, graph extraction, FAISS
build / persist / query, threshold bands, JSONL output, embed-stats)
land in this module and are exercised by unit tests against synthetic
data. The model-benchmark, threshold validation, and ``efSearch``
tuning sub-tasks listed in ``docs/archived/BUILD_PLAN.md`` M5 are gated on the
M12 gold set and remain open.

Heavy ML imports (``sentence_transformers``, ``faiss``, ``numpy``) are
deferred to the functions that need them so the rest of the CLI does
not pay a multi-second import cost.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from rdflib import Graph, URIRef
from rdflib import Literal as RdfLiteral
from rdflib.namespace import RDF, RDFS

from bffi_pipeline.blocking import compute_blocking_key
from bffi_pipeline.config import get_settings
from bffi_pipeline.provenance import vocab as V

if TYPE_CHECKING:
    import numpy as np

# --- Constants ------------------------------------------------------------

DEFAULT_MODEL: Final[str] = "BAAI/bge-m3"
EMBEDDING_DIM: Final[int] = 1024  # BGE-M3 / e5-large / jina-v3 all match.

# FAISS HNSW knobs. Values per spec § 6 / BUILD_PLAN M5:
#  - M=32 controls graph density at insertion time;
#  - efConstruction=200 is the standard high-recall build setting;
#  - efSearch=64 is a placeholder default until M12 gold-set tuning
#    against {32, 64, 128, 256} picks the smallest value with full
#    recall.
HNSW_M: Final[int] = 32
HNSW_EF_CONSTRUCTION: Final[int] = 200
HNSW_EF_SEARCH: Final[int] = 64

# Top-k neighbours per Work per query.
DEFAULT_TOP_K: Final[int] = 20

# Threshold bands. Tightened from the spec's frontier-API defaults
# because the M5 Max LLM judge is throughput-bound; see
# docs/marcxml-to-bffi-skosmos-pipeline.md § 6.
BAND_AUTO_MERGE: Final[float] = 0.90
BAND_REJECT: Final[float] = 0.78

# Encoder defaults. mps is PyTorch's Metal backend; cpu is the
# CI-friendly fallback. Batch 64 saturates the GPU on the target
# M5 Max without OOM.
DEFAULT_DEVICE: Final[str] = "mps"
DEFAULT_BATCH_SIZE: Final[int] = 64

# On-disk filenames. Both files live at <output_dir>/.
INDEX_FILENAME: Final[str] = "embeddings.faiss"
IDMAP_FILENAME: Final[str] = "embeddings.idmap.json"
CANDIDATES_FILENAME: Final[str] = "embed-candidates.jsonl"

_LANG_URI_PREFIX: Final[str] = "http://id.loc.gov/vocabulary/languages/"
_CONTENT_URI_PREFIX: Final[str] = "http://id.loc.gov/vocabulary/contentTypes/"
_YEAR_RE: Final[re.Pattern[str]] = re.compile(r"(?<!\d)(\d{4})(?!\d)")

Band = Literal["auto-merge", "escalate", "reject"]

# --- Public dataclasses ---------------------------------------------------


@dataclass(frozen=True)
class WorkEmbeddingInput:
    """Per-Work tuple consumed by the embedding-input-string builder."""

    work_uri: str
    creator: str | None
    title: str | None
    language: str | None
    year: str | None
    content_type: str | None


@dataclass(frozen=True)
class IndexBuildResult:
    """Summary of a build_index() run, written into the idmap JSON."""

    n_works: int
    ndim: int
    model_name: str
    hnsw_m: int
    hnsw_ef_construction: int
    hnsw_ef_search: int
    build_seconds: float
    index_path: str
    idmap_path: str

    def render(self) -> str:
        """Format the build result as paste-ready text for the embed CLI."""
        return (
            f"FAISS HNSW build complete\n"
            f"  works:               {self.n_works}\n"
            f"  ndim:                {self.ndim}\n"
            f"  model:               {self.model_name}\n"
            f"  HNSW M / efC / efS:  {self.hnsw_m} / "
            f"{self.hnsw_ef_construction} / {self.hnsw_ef_search}\n"
            f"  build seconds:       {self.build_seconds:.1f}\n"
            f"  index file:          {self.index_path}\n"
            f"  idmap file:          {self.idmap_path}"
        )


@dataclass(frozen=True)
class CandidatePair:
    """A single Work-pair candidate produced by the top-k retrieval pass."""

    work_a: str
    work_b: str
    similarity: float
    block_a: str
    block_b: str
    cross_block: bool
    band: Band


@dataclass
class EmbedStats:
    """Aggregate counts for an end-of-run embed-stats report."""

    n_works: int
    ndim: int
    model_name: str
    index_path: str
    similarity_buckets: Counter[str] = field(default_factory=Counter)
    band_counts: Counter[Band] = field(default_factory=Counter)
    cross_block_count: int = 0
    total_pairs: int = 0

    def render(self) -> str:
        """Format the embed-stats report as paste-ready text."""
        lines = [
            "Stage-2 embedding statistics",
            f"  works:        {self.n_works}",
            f"  ndim:         {self.ndim}",
            f"  model:        {self.model_name}",
            f"  index file:   {self.index_path}",
            f"  pairs total:  {self.total_pairs}",
        ]
        if self.total_pairs:
            lines.append("  band counts:")
            bands: tuple[Band, ...] = ("auto-merge", "escalate", "reject")
            for band in bands:
                count = self.band_counts.get(band, 0)
                pct = 100.0 * count / self.total_pairs
                lines.append(f"    {band:<10s}  {count:>8}  ({pct:5.1f}%)")
            cross_pct = 100.0 * self.cross_block_count / self.total_pairs
            lines.append(
                f"  cross-block hits: {self.cross_block_count} ({cross_pct:.1f}% of pairs)"
            )
        if self.similarity_buckets:
            lines.append("  top-k similarity histogram (0.05 buckets):")
            for bucket in sorted(self.similarity_buckets):
                lines.append(f"    {bucket}  {self.similarity_buckets[bucket]:>8}")
        return "\n".join(lines)


# --- Pure functions -------------------------------------------------------


def _short_segment(uri_or_value: str | None, prefix: str) -> str | None:
    """Strip a known LoC prefix to a short code; passthrough otherwise."""
    if not uri_or_value:
        return None
    s = uri_or_value.strip()
    if not s:
        return None
    if s.startswith(prefix):
        return s[len(prefix) :] or None
    return s.rsplit("/", 1)[-1] if "/" in s else s


def _normalise_year(value: str | None) -> str | None:
    """Pull the first 4-digit year from ``value`` if present."""
    if not value:
        return None
    m = _YEAR_RE.search(value)
    return m.group(1) if m else None


def embedding_input_string(work: WorkEmbeddingInput) -> str:
    """Build the fixed-order input string the embedder sees.

    The format matches spec § 6 Stage 2: pipe-separated, fixed field
    order ``creator | title | language | year | type``. Empty fields
    are kept as ``"<field>:"`` so re-embedding the same Work always
    produces an identical vector regardless of which fields were
    populated.
    """

    def part(label: str, value: str | None) -> str:
        """Render one ``label: value`` segment, leaving empty values as ``label:``."""
        return f"{label}: {(value or '').strip()}"

    return " | ".join(
        (
            part("creator", work.creator),
            part("title", work.title),
            part("language", work.language),
            part("year", work.year),
            part("type", work.content_type),
        )
    )


def classify_band(similarity: float) -> Band:
    """Sort a cosine similarity into the three-band space.

    - ``>= BAND_AUTO_MERGE`` → ``"auto-merge"``
    - ``BAND_REJECT < s < BAND_AUTO_MERGE`` → ``"escalate"``
    - ``<= BAND_REJECT`` → ``"reject"``
    """
    if similarity >= BAND_AUTO_MERGE:
        return "auto-merge"
    if similarity <= BAND_REJECT:
        return "reject"
    return "escalate"


# --- Graph extraction -----------------------------------------------------


def _first_pref_label(graph: Graph, subject: URIRef) -> str | None:
    for o in graph.objects(subject, V.SKOS.prefLabel):
        if isinstance(o, RdfLiteral):
            return str(o)
    return None


def _primary_agent_uris(graph: Graph, work: URIRef) -> list[URIRef]:
    agents: list[URIRef] = []
    for contrib in graph.objects(work, V.BFFI.contribution):
        types = set(graph.objects(contrib, RDF.type))
        if V.BFFI.PrimaryContribution not in types:
            continue
        for agent in graph.objects(contrib, V.BFFI.agent):
            if isinstance(agent, URIRef):
                agents.append(agent)
    return agents


def _agent_label(graph: Graph, agent: URIRef) -> str | None:
    for label in graph.objects(agent, RDFS.label):
        if isinstance(label, RdfLiteral):
            return str(label)
    return None


def _expression_objects(graph: Graph, work: URIRef, predicate: URIRef) -> Iterator[str | URIRef]:
    """Yield ``work``'s expressions' values for a given Expression-side predicate."""
    for expr in graph.objects(work, V.BFFI.hasExpression):
        if not isinstance(expr, URIRef):
            continue
        for obj in graph.objects(expr, predicate):
            yield obj if isinstance(obj, URIRef) else str(obj)


def _first_short_segment(graph: Graph, work: URIRef, predicate: URIRef, prefix: str) -> str | None:
    for value in _expression_objects(graph, work, predicate):
        short = _short_segment(str(value), prefix)
        if short:
            return short
    return None


def _origin_year(graph: Graph, work: URIRef) -> str | None:
    for date in graph.objects(work, V.BFFI.originDate):
        year = _normalise_year(str(date))
        if year:
            return year
    return None


def extract_embedding_inputs(graph: Graph) -> Iterator[WorkEmbeddingInput]:
    """Walk a combined BFFI + BIBFRAME graph and yield per-Work embedding inputs."""
    for work in graph.subjects(RDF.type, V.BFFI.Work):
        if not isinstance(work, URIRef):
            continue
        title = _first_pref_label(graph, work)
        creator: str | None = None
        for agent in _primary_agent_uris(graph, work):
            label = _agent_label(graph, agent)
            if label:
                creator = label
                break
        language = _first_short_segment(graph, work, V.BFFI.language, _LANG_URI_PREFIX)
        content = _first_short_segment(graph, work, V.BFFI.content, _CONTENT_URI_PREFIX)
        yield WorkEmbeddingInput(
            work_uri=str(work),
            creator=creator,
            title=title,
            language=language,
            year=_origin_year(graph, work),
            content_type=content,
        )


def to_blocking_key(work: WorkEmbeddingInput) -> str:
    """Compose the same Stage-1 blocking key M4 produces for ``work``."""
    return compute_blocking_key(
        {
            "creator": work.creator,
            "title": work.title,
            "content_type": work.content_type,
        }
    )


# --- Index build ----------------------------------------------------------


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
    """Encode ``strings`` with the configured model. Returns an L2-normalised float32 matrix."""
    import numpy as np
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(model_name, device=device)
    vectors = model.encode(
        strings,
        batch_size=batch_size,
        convert_to_numpy=True,
        normalize_embeddings=False,  # do it ourselves so the policy is explicit
        show_progress_bar=True,
    )
    matrix = np.asarray(vectors, dtype=np.float32)
    return _l2_normalize(matrix)


def _build_hnsw(matrix: np.ndarray[Any, Any]) -> Any:
    """Construct the IndexHNSWFlat over ``matrix`` (which must already be L2-normalised)."""
    import faiss

    ndim = matrix.shape[1]
    index = faiss.IndexHNSWFlat(ndim, HNSW_M, faiss.METRIC_INNER_PRODUCT)
    index.hnsw.efConstruction = HNSW_EF_CONSTRUCTION
    index.hnsw.efSearch = HNSW_EF_SEARCH
    index.add(matrix)
    return index


def _index_inputs(corpus_dir: Path) -> tuple[list[Path], list[Path]]:
    """Return (bffi-turtle, bibframe-rdf) source files under ``corpus_dir``."""
    bffi = sorted((corpus_dir / "bffi").glob("*.ttl")) if (corpus_dir / "bffi").exists() else []
    bibframe = sorted(
        p
        for p in (corpus_dir / "bibframe").glob("*.rdf")
        if (corpus_dir / "bibframe").exists() and not p.name.startswith("_")
    )
    return bffi, bibframe


def _is_index_fresh(
    index_path: Path,
    idmap_path: Path,
    inputs: Iterable[Path],
    model_name: str,
) -> bool:
    """True when both files exist, are newer than every input, and match ``model_name``."""
    if not (index_path.is_file() and idmap_path.is_file()):
        return False
    try:
        idmap = json.loads(idmap_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    if idmap.get("model_name") != model_name:
        return False
    out_mtime = min(index_path.stat().st_mtime, idmap_path.stat().st_mtime)
    return all(p.stat().st_mtime <= out_mtime for p in inputs)


def _load_corpus_graph(corpus_dir: Path) -> Graph:
    """Parse every BFFI Turtle and BIBFRAME RDF/XML under ``corpus_dir`` into one graph.

    Mirrors the M4 ``workkey.load_corpus`` helper rather than importing
    it (per the stage-isolation rule).
    """
    g = Graph()
    bffi, bibframe = _index_inputs(corpus_dir)
    for path in bffi:
        g.parse(str(path), format="turtle")
    for path in bibframe:
        g.parse(str(path), format="xml")
    return g


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
        return IndexBuildResult(
            n_works=idmap.get("n_works", 0),
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


# --- Query ----------------------------------------------------------------


def _load_persisted_index(output_dir: Path) -> tuple[Any, dict[str, Any]]:
    """Reload (faiss_index, idmap_dict) written by :func:`build_index`."""
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


def _bucket_label(similarity: float) -> str:
    bucket = int(max(0.0, min(0.999, similarity)) * 20) / 20
    return f"[{bucket:.2f}, {bucket + 0.05:.2f})"


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


def _reconstruct_vectors(index: Any, n: int) -> np.ndarray[Any, Any]:
    """Pull the L2-normalised matrix back out of a FAISS index.

    ``IndexHNSWFlat`` stores vectors via an inner ``IndexFlat``; both
    expose ``reconstruct_n``.
    """
    import numpy as np

    matrix = index.reconstruct_n(0, n)
    return np.asarray(matrix, dtype=np.float32)


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
